// file: trade-svc/src/dex.ts
import axios, { AxiosError } from 'axios';
import https from 'node:https';
import bs58 from 'bs58';
import { Keypair, VersionedTransaction } from '@solana/web3.js';
import { cfg } from './config.js';
import { conn, signFromBase64Unsigned, sendSignedVersionedTx } from './solana.js';

type SwapParams = {
  privateKey: string;
  inputMint: string;
  outputMint: string;
  amountLamports: number;   // ExactIn: inAmount; ExactOut: outAmount
  dex?: 'jupiter' | 'raydium';
  slippageBps?: number;
  priorityFee?: number;     // SOL (heuristic → computeUnitPriceMicroLamports)
  exactOut?: boolean;       // if true → swapMode=ExactOut
};

const httpsAgent = new https.Agent({ keepAlive: true, family: 4, maxSockets: 50 });

// Prefer Pro when API key available, else Lite; both have same path
const JUP_PRO  = 'https://api.jup.ag/swap/v1';
const JUP_LITE = 'https://lite-api.jup.ag/swap/v1';
const HAS_KEY  = !!process.env.JUP_API_KEY;

function apiHeaders() {
  return HAS_KEY ? { 'X-API-KEY': String(process.env.JUP_API_KEY), 'User-Agent': 'trade-svc/1.0' } :
                   { 'User-Agent': 'trade-svc/1.0' };
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

export async function dexSwap(p: SwapParams): Promise<string> {
  const {
    privateKey, inputMint, outputMint, amountLamports,
    dex = 'jupiter', slippageBps = 50, priorityFee = 0, exactOut = false
  } = p;

  if (!privateKey || !inputMint || !outputMint || !amountLamports) {
    throw new Error('missing fields');
  }

  const onlyDirectRoutes = dex === 'raydium';   // force 1-market route via Jupiter
  const swapMode = exactOut ? 'ExactOut' : 'ExactIn';

  // Heuristic compute price (micro lamports / CU) dari priorityFee (SOL)
  const computeUnitPriceMicroLamports =
    priorityFee > 0 ? Math.max(1, Math.floor((priorityFee * 1_000_000_000) / 1_000_000)) : undefined;

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
        swapMode, // 'ExactIn'|'ExactOut'
      });
      const quote = await getQuote(qs);

      // 2) Build unsigned tx (base64)
      const user = Keypair.fromSecretKey(keyBytes(privateKey));
      const txB64 = await buildSwapTx({
        quoteResponse: quote,
        userPublicKey: user.publicKey.toBase58(),
        wrapAndUnwrapSol: true,
        dynamicComputeUnitLimit: true,
        computeUnitPriceMicroLamports,
        // dynamicSlippage: true, // aktifkan jika ingin
      });

      const vtx: VersionedTransaction = signFromBase64Unsigned(txB64, user);

      // 3) Optional simulate → error lebih jelas
      try {
        const c = conn(cfg.rpcUrl);
        const sim = await c.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true });
        if (sim.value.err) {
          const logs = (sim.value.logs ?? []).slice(-8).join(' | ');
          throw new Error(`simulation_failed ${JSON.stringify(sim.value.err)} logs=${logs}`);
        }
      } catch (simErr) {
        // bisa di-uncomment untuk hard fail: throw simErr;
      }

      // 4) Send
      const sig = await sendSignedVersionedTx(cfg.rpcUrl, vtx);
      return sig;

    } catch (e) {
      lastErr = mapAxiosErr(e);
      if (!/network_error|HTTP 5\d\d|429/.test(lastErr) || attempt === 2) {
        throw new Error(lastErr);
      }
      await sleep(500 * Math.pow(2, attempt));
    }
  }
  throw new Error(lastErr || 'unknown_error');
}
