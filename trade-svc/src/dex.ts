// file: trade-svc/src/dex.ts
import axios from 'axios';
import bs58 from 'bs58';
import { Keypair, VersionedTransaction } from '@solana/web3.js';
import { cfg } from './config.js';
import { conn, signFromBase64Unsigned, sendSignedVersionedTx } from './solana.js';

const JUP_BASE = 'https://quote-api.jup.ag/v6';

type SwapParams = {
  privateKey: string;            // base58 | json array | hex
  inputMint: string;
  outputMint: string;
  amountLamports: number;
  dex?: 'jupiter' | 'raydium';
  slippageBps?: number;
  priorityFee?: number;
};

function bool(v: any) { return !!v; }

export async function dexSwap(params: SwapParams): Promise<string> {
  const {
    privateKey, inputMint, outputMint, amountLamports,
    dex = 'jupiter', slippageBps = 50, priorityFee = 0
  } = params;

  // get route/quote
  const onlyDirectRoutes = dex === 'raydium';
  const q = new URLSearchParams({
    inputMint, outputMint,
    amount: String(amountLamports),
    slippageBps: String(slippageBps),
    onlyDirectRoutes: String(onlyDirectRoutes),
    restrictIntermediateTokens: 'true'
  });

  try {
    const quoteRes = await axios.get(`${JUP_BASE}/quote?${q.toString()}`, {
      timeout: 12000,
      headers: { 'User-Agent': 'trade-svc/1.0' },
      validateStatus: () => true
    });

    if (quoteRes.status !== 200) {
      throw new Error(`quote HTTP ${quoteRes.status} ${JSON.stringify(quoteRes.data).slice(0,300)}`);
    }
    const quote = quoteRes.data;
    if (!quote || !quote.routePlan) {
      throw new Error(`no route returned by Jupiter`);
    }

    // build unsigned tx
    const userKp = Keypair.fromSecretKey(
      deriveKeypairBytes(privateKey)
    );
    const swapRes = await axios.post(`${JUP_BASE}/swap`, {
      quoteResponse: quote,
      userPublicKey: userKp.publicKey.toBase58(),
      wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
      prioritizationFeeLamports: priorityFee ? Math.floor(priorityFee * 1_000_000_000) : undefined
    }, {
      timeout: 12000,
      headers: { 'Content-Type': 'application/json', 'User-Agent': 'trade-svc/1.0' },
      validateStatus: () => true
    });

    if (swapRes.status !== 200 || !swapRes.data?.swapTransaction) {
      throw new Error(`swap build HTTP ${swapRes.status} ${JSON.stringify(swapRes.data).slice(0,300)}`);
    }

    // sign + send
    const unsignedB64 = swapRes.data.swapTransaction as string;
    const vtx = signFromBase64Unsigned(unsignedB64, userKp);

    // simulate first for better errors (optional but helpful on Railway)
    try {
      const connection = conn(cfg.rpcUrl);
      const sim = await connection.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true });
      if (sim.value.err) {
        const tail = (sim.value.logs ?? []).slice(-5).join(' | ');
        throw new Error(`simulation failed: ${JSON.stringify(sim.value.err)} logs=${tail}`);
      }
    } catch (e) {
      // continue to send; comment the next line to force-stop on sim error
      // throw e;
    }

    const sig = await sendSignedVersionedTx(cfg.rpcUrl, vtx);
    return sig;

  } catch (e: any) {
    // normalize error message for Python client
    const msg = e?.response?.data
      ? `HTTP ${e.response.status} ${JSON.stringify(e.response.data).slice(0,300)}`
      : (e?.message || String(e));
    throw new Error(msg);
  }
}

// utils
function deriveKeypairBytes(input: string): Uint8Array {
  const trimmed = (input || '').trim();
  try {
    if (trimmed.startsWith('[')) {
      const arr = JSON.parse(trimmed);
      return Uint8Array.from(arr);
    }
    if (/^[0-9A-Fa-fx]+$/.test(trimmed)) {
      const hex = trimmed.startsWith('0x') ? trimmed.slice(2) : trimmed;
      const buf = Buffer.from(hex, 'hex');
      return new Uint8Array(buf);
    }
    // base58
    return bs58.decode(trimmed);
  } catch (e) {
    throw new Error('invalid private key format');
  }
}
