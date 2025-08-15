// file: trade-svc/src/dex.ts
import axios, { AxiosError } from 'axios';
import https from 'node:https';
import bs58 from 'bs58';
import { Keypair, VersionedTransaction } from '@solana/web3.js';
import { cfg } from './config.js';
import { conn, signFromBase64Unsigned, sendSignedVersionedTx } from './solana.js';

const JUP_BASE = 'https://quote-api.jup.ag/v6';

// Reuse sockets, force IPv4 (Railway sering gagal di IPv6)
const httpsAgent = new https.Agent({ keepAlive: true, maxSockets: 50, family: 4 });

type SwapParams = {
  privateKey: string;
  inputMint: string;
  outputMint: string;
  amountLamports: number;
  dex?: 'jupiter' | 'raydium';
  slippageBps?: number;
  priorityFee?: number;
};

function deriveKeypairBytes(input: string): Uint8Array {
  const trimmed = (input || '').trim();
  try {
    if (trimmed.startsWith('[')) {
      const arr = JSON.parse(trimmed);
      return Uint8Array.from(arr);
    }
    if (/^[0-9A-Fa-fx]+$/.test(trimmed)) {
      const hex = trimmed.startsWith('0x') ? trimmed.slice(2) : trimmed;
      return new Uint8Array(Buffer.from(hex, 'hex'));
    }
    return bs58.decode(trimmed);
  } catch {
    throw new Error('invalid private key format');
  }
}

async function axiosGet(url: string) {
  const r = await axios.get(url, {
    timeout: 12000,
    httpsAgent,
    headers: { 'User-Agent': 'trade-svc/1.0' },
    validateStatus: () => true,
  });
  return r;
}

async function axiosPost(url: string, data: any) {
  const r = await axios.post(url, data, {
    timeout: 15000,
    httpsAgent,
    headers: { 'User-Agent': 'trade-svc/1.0', 'Content-Type': 'application/json' },
    validateStatus: () => true,
  });
  return r;
}

export async function dexSwap(params: SwapParams): Promise<string> {
  const {
    privateKey, inputMint, outputMint, amountLamports,
    dex = 'jupiter', slippageBps = 50, priorityFee = 0
  } = params;

  const onlyDirectRoutes = dex === 'raydium';
  const q = new URLSearchParams({
    inputMint, outputMint,
    amount: String(amountLamports),
    slippageBps: String(slippageBps),
    onlyDirectRoutes: String(onlyDirectRoutes),
    restrictIntermediateTokens: 'true',
  });

  try {
    // 1) Quote
    const qurl = `${JUP_BASE}/quote?${q.toString()}`;
    const quoteRes = await axiosGet(qurl);
    if (quoteRes.status !== 200) {
      throw new Error(`quote HTTP ${quoteRes.status} ${JSON.stringify(quoteRes.data).slice(0,300)}`);
    }
    if (!quoteRes.data || !quoteRes.data.routePlan) {
      throw new Error('no route returned by Jupiter');
    }

    // 2) Build swap tx
    const userKp = Keypair.fromSecretKey(deriveKeypairBytes(privateKey));
    const swapRes = await axiosPost(`${JUP_BASE}/swap`, {
      quoteResponse: quoteRes.data,
      userPublicKey: userKp.publicKey.toBase58(),
      wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
      prioritizationFeeLamports: priorityFee ? Math.floor(priorityFee * 1_000_000_000) : undefined,
    });

    if (swapRes.status !== 200 || !swapRes.data?.swapTransaction) {
      throw new Error(`swap build HTTP ${swapRes.status} ${JSON.stringify(swapRes.data).slice(0,300)}`);
    }

    const unsignedB64 = swapRes.data.swapTransaction as string;
    const vtx: VersionedTransaction = signFromBase64Unsigned(unsignedB64, userKp);

    // Optional simulate: memberi error detail lebih jelas
    try {
      const connection = conn(cfg.rpcUrl);
      const sim = await connection.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true });
      if (sim.value.err) {
        const tail = (sim.value.logs ?? []).slice(-8).join(' | ');
        throw new Error(`simulation failed: ${JSON.stringify(sim.value.err)} logs=${tail}`);
      }
    } catch (e) {
      // Boleh lanjut kirim; uncomment utk stop on sim error
      // throw e;
    }

    // 3) Send
    const sig = await sendSignedVersionedTx(cfg.rpcUrl, vtx);
    return sig;

  } catch (e: any) {
    // Format error supaya Python dapat pesan jelas
    let msg = e?.message || String(e);
    if (e && (e as AxiosError).isAxiosError) {
      const ae = e as AxiosError;
      if (ae.response) {
        msg = `HTTP ${ae.response.status} ${JSON.stringify(ae.response.data).slice(0,300)}`;
      } else if (ae.request) {
        msg = `network_error to ${(ae.config?.url)||'unknown'}: ${ae.code||ae.message}`;
      }
    }
    console.error('[dexSwap]', msg);
    throw new Error(msg);
  }
}
