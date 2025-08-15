// file: trade-svc/src/dex.ts
import axios, { AxiosError } from 'axios';
import https from 'node:https';
import bs58 from 'bs58';
import {
  Keypair,
  VersionedTransaction,
  PublicKey,
  ParsedAccountData,
} from '@solana/web3.js';
import { getAssociatedTokenAddressSync } from '@solana/spl-token';
import { cfg } from './config.js';
import { conn, signFromBase64Unsigned, sendSignedVersionedTx } from './solana.js';

export type SwapParams = {
  privateKey: string;
  inputMint: string;
  outputMint: string;
  amountLamports: number;   // ExactIn: inAmount (raw units) ; ExactOut: outAmount (raw units)
  dex?: 'jupiter' | 'raydium';
  slippageBps?: number;
  priorityFee?: number;     // SOL → heuristik computeUnitPriceMicroLamports
  exactOut?: boolean;       // if true → swapMode=ExactOut
  forceLegacy?: boolean;    // if true → asLegacyTransaction
  computeUnitPriceMicroLamports?: number; // override langsung
};

const httpsAgent = new https.Agent({ keepAlive: true, family: 4, maxSockets: 50 });

// Jupiter Swap API v1 (Pro/Lite)
const JUP_PRO  = 'https://api.jup.ag/swap/v1';
const JUP_LITE = 'https://lite-api.jup.ag/swap/v1';
const HAS_KEY  = !!process.env.JUP_API_KEY;

function apiHeaders() {
  return HAS_KEY ? { 'X-API-KEY': String(process.env.JUP_API_KEY), 'User-Agent': 'trade-svc/1.0' }
                 : { 'User-Agent': 'trade-svc/1.0' };
}

function keyBytes(input: string): Uint8Array {
  const s = (input || '').trim();
  if (!s) throw new Error('empty privateKey');
  try {
    if (s.startsWith('[')) return Uint8Array.from(JSON.parse(s));
    if (/^(0x)?[0-9a-fA-F]+$/.test(s)) {
      const hex = s.startsWith('0x') ? s.slice(2) : s;
      return new Uint8Array(Buffer.from(hex, 'hex'));
    }
    return bs58.decode(s);
  } catch {
    throw new Error('invalid private key format');
  }
}

function mapAxiosErr(e: unknown): string {
  const ae = e as AxiosError;
  if (ae?.response) return `HTTP ${ae.response.status} ${JSON.stringify(ae.response.data).slice(0,300)}`;
  if (ae?.request)  return `network_error to ${(ae.config?.url)||'unknown'}: ${ae.code||ae.message}`;
  return (e as any)?.message || String(e);
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

// ---------- Helpers: Jupiter v1 ----------
async function getQuote(params: URLSearchParams) {
  const bases = HAS_KEY ? [JUP_PRO, JUP_LITE] : [JUP_LITE, JUP_PRO];
  let lastErr = '';
  for (const base of bases) {
    try {
      const url = `${base}/quote?${params.toString()}`;
      const r = await axios.get(url, {
        httpsAgent, timeout: 12000, validateStatus: () => true, headers: apiHeaders(),
      });
      if (r.status === 200 && r.data?.routePlan) return r.data;
      lastErr = `quote ${r.status} ${JSON.stringify(r.data).slice(0,300)}`;
    } catch (e) {
      lastErr = mapAxiosErr(e);
    }
  }
  throw new Error(lastErr || 'quote_failed');
}

async function buildSwapTx(body: any) {
  const bases = HAS_KEY ? [JUP_PRO, JUP_LITE] : [JUP_LITE, JUP_PRO];
  let lastErr = '';
  for (const base of bases) {
    try {
      const r = await axios.post(`${base}/swap`, body, {
        httpsAgent, timeout: 15000, validateStatus: () => true,
        headers: { 'Content-Type': 'application/json', ...apiHeaders() },
      });
      if (r.status === 200 && r.data?.swapTransaction) return r.data.swapTransaction as string;
      lastErr = `swap build ${r.status} ${JSON.stringify(r.data).slice(0,300)}`;
    } catch (e) {
      lastErr = mapAxiosErr(e);
    }
  }
  throw new Error(lastErr || 'swap_build_failed');
}

// ---------- Pre-checks untuk error "Attempt to debit..." ----------
const NATIVE_SOL = 'So11111111111111111111111111111111111111112';
const ATA_SIZE = 165;

async function ensureSufficientBalances(opts: {
  user: PublicKey;
  inputMint: string;
  outputMint: string;
  amountRaw: number; // in raw units (lamports for SOL; token base units for SPL)
}) {
  const { user, inputMint, outputMint, amountRaw } = opts;
  const c = conn(cfg.rpcUrl);

  // SOL balance
  const balLamports = await c.getBalance(user);

  // Rent for ATA
  const rentAta = await c.getMinimumBalanceForRentExemption(ATA_SIZE);

  // Base required fees (rough safety margin)
  let required = 10_000; // ~0.00001 SOL

  // If input is SOL: need to fund WSOL temp account + amount
  if (inputMint === NATIVE_SOL) {
    required += amountRaw + rentAta;
  }

  // If output is SPL: ensure ATA exists; if not, add rent
  if (outputMint !== NATIVE_SOL) {
    try {
      const outMint = new PublicKey(outputMint);
      const ataOut = getAssociatedTokenAddressSync(outMint, user);
      const info = await c.getAccountInfo(ataOut);
      if (!info) required += rentAta;
    } catch { /* ignore parsing errors */ }
  }

  if (balLamports < required) {
    const need = (required - balLamports) / 1e9;
    throw new Error(
      `balance_low: need ~${(required/1e9).toFixed(6)} SOL, have ${(balLamports/1e9).toFixed(6)} SOL. ` +
      `Top up at least ~${need.toFixed(6)} SOL (wrap WSOL/ATA/fees).`
    );
  }

  // If selling SPL: ensure token balance >= amountRaw
  if (inputMint !== NATIVE_SOL) {
    try {
      const mintPk = new PublicKey(inputMint);
      const resp = await c.getParsedTokenAccountsByOwner(user, { mint: mintPk });
      let totalRaw = 0n;
      for (const acc of resp.value) {
        const data = acc.account.data as ParsedAccountData;
        const info = (data?.parsed as any)?.info;
        const amtStr = info?.tokenAmount?.amount ?? '0';
        totalRaw += BigInt(amtStr);
      }
      if (totalRaw < BigInt(amountRaw)) {
        throw new Error(
          `token_balance_low: have ${totalRaw.toString()} raw, need ${amountRaw} raw for input mint ${inputMint}`
        );
      }
    } catch (e) {
      // If fails to fetch, skip strict check; Jupiter sim will still catch it.
    }
  }
}

// ---------- Main swap ----------
export async function dexSwap(p: SwapParams): Promise<string> {
  const {
    privateKey,
    inputMint,
    outputMint,
    amountLamports,
    dex = 'jupiter',
    slippageBps = 50,
    priorityFee = 0,
    exactOut = false,
    forceLegacy = false,
    computeUnitPriceMicroLamports: computeOverride,
  } = p;

  if (!privateKey || !inputMint || !outputMint || !amountLamports) {
    throw new Error('missing fields');
  }

  const onlyDirectRoutes = dex === 'raydium'; // force single-market via Jupiter router
  const swapMode = exactOut ? 'ExactOut' : 'ExactIn';

  const user = Keypair.fromSecretKey(keyBytes(privateKey));

  // Pre-check to avoid "Attempt to debit..." with clear message
  await ensureSufficientBalances({
    user: user.publicKey,
    inputMint,
    outputMint,
    amountRaw: amountLamports,
  });

  // Heuristic compute price (micro lamports/CU) dari priorityFee (SOL) jika override tidak diberikan
  const computeUnitPriceMicroLamports =
    computeOverride ??
    (priorityFee > 0 ? Math.max(1, Math.floor((priorityFee * 1_000_000_000) / 1_000_000)) : undefined);

  // Retry 3x utk network/5xx/429
  let lastErr = '';
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      // 1) Quote
      const qs = new URLSearchParams({
        inputMint,
        outputMint,
        amount: String(amountLamports),
        slippageBps: String(slippageBps),
        onlyDirectRoutes: String(onlyDirectRoutes),
        restrictIntermediateTokens: 'true',
        swapMode, // 'ExactIn' | 'ExactOut'
      });
      const quote = await getQuote(qs);

      // 2) Build unsigned tx (base64)
      const txB64 = await buildSwapTx({
        quoteResponse: quote,
        userPublicKey: user.publicKey.toBase58(),
        wrapAndUnwrapSol: true,
        dynamicComputeUnitLimit: true,
        computeUnitPriceMicroLamports,
        asLegacyTransaction: !!forceLegacy,
        // dynamicSlippage: true, // enable if you want Jupiter to tweak slippage
      });

      const vtx: VersionedTransaction = signFromBase64Unsigned(txB64, user);

      // 3) Optional simulate → surface errors early
      try {
        const c = conn(cfg.rpcUrl);
        const sim = await c.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true });
        if (sim.value.err) {
          const logs = (sim.value.logs ?? []).slice(-10).join(' | ');
          throw new Error(`simulation_failed ${JSON.stringify(sim.value.err)} logs=${logs}`);
        }
      } catch {
        // uncomment next line to hard-fail on simulation error:
        // throw simErr;
      }

      // 4) Send
      const sig = await sendSignedVersionedTx(cfg.rpcUrl, vtx);
      return sig;

    } catch (e) {
      lastErr = mapAxiosErr(e);
      // retry only for transient issues
      if (!/network_error|HTTP 5\d\d|429/.test(lastErr) || attempt === 2) {
        throw new Error(lastErr);
      }
      await sleep(500 * Math.pow(2, attempt));
    }
  }
  throw new Error(lastErr || 'unknown_error');
}
