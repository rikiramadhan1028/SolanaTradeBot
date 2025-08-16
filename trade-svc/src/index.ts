// file: trade-svc/src/index.ts
import express, { Request, Response } from 'express';
import dns from 'node:dns/promises';
import https from 'node:https';
import axios from 'axios';
import {
  getSolBalance,
  getTokenBalances,
  getSpecificTokenBalance,
  getMintDecimals,
} from './wallet.js';
import { attachMetaRoutes } from "./meta.js";

import { cfg } from './config.js';
import { connectDb, Trade } from './db.js';
import {
  keypairFromInput,
  deriveAddressFromPk,
  sendSignedVersionedTx,
  signFromBase64Unsigned,
  signFromBase58Unsigned,
  conn,
} from './solana.js';
import { tradeLocalSingle, tradeLocalBundle, PumpAction } from './pumpfun.js';
import { sendBundleToJito } from './jito.js';
import { dexSwap } from './dex.js';
import bs58 from 'bs58';

// ---- Jupiter Swap API v1 (Pro/Lite) ----
const JUP_QUOTE_PRO  = 'https://api.jup.ag/swap/v1/quote';
const JUP_QUOTE_LITE = 'https://lite-api.jup.ag/swap/v1/quote';
const HAS_JUP_KEY    = !!process.env.JUP_API_KEY;
const jupHeaders = HAS_JUP_KEY
  ? { 'X-API-KEY': String(process.env.JUP_API_KEY), 'User-Agent': 'trade-svc/1.0' }
  : { 'User-Agent': 'trade-svc/1.0' };

const httpsAgent = new https.Agent({ keepAlive: true, maxSockets: 50, family: 4 });

const app = express();
app.set('trust proxy', true);
app.use(express.json({ limit: '1mb' }));
attachMetaRoutes(app);
// ---------- Health & Diagnostics ----------
app.get('/health', (_req: Request, res: Response) => res.json({ ok: true }));

app.get('/diag/ping', async (_req: Request, res: Response) => {
  try {
    const c = conn(cfg.rpcUrl);
    const hb = await c.getLatestBlockhash();
    return res.json({ ok: true, rpc: cfg.rpcUrl, blockhash: hb.blockhash });
  } catch (e: any) {
    return res.status(500).json({ error: String(e?.message || e) });
  }
});

app.get('/diag/jup', async (_req: Request, res: Response) => {
  try {
    // cek keduanya
    const lite = await dns.lookup('lite-api.jup.ag', { all: true, verbatim: true });
    let pro: unknown = null;
    try { pro = await dns.lookup('api.jup.ag', { all: true, verbatim: true }); } catch {}
    return res.json({ lite, pro });
  } catch (e: any) {
    return res.status(500).json({ error: String(e?.message || e) });
  }
});

/**
 * GET /diag/quote?input=<mint>&output=<mint>&amount=<lamports>&direct=1
 * - direct=1/true -> onlyDirectRoutes=true (Raydium-direct)
 * - amount in lamports (integer > 0)
 * - sumber: Jupiter Swap API v1 (Pro/Lite fallback)
 */
app.get('/diag/quote', async (req: Request, res: Response) => {
  try {
    const input = String(req.query.input || '').trim();
    const output = String(req.query.output || '').trim();
    const amountStr = String(req.query.amount || '').trim();
    const directRaw = String(req.query.direct || '0').trim().toLowerCase();
    const onlyDirectRoutes = directRaw === '1' || directRaw === 'true';

    if (!input || !output || !amountStr) {
      return res.status(400).json({ error: 'missing query params: input, output, amount' });
    }
    const amount = Number(amountStr);
    if (!Number.isFinite(amount) || amount <= 0) {
      return res.status(400).json({ error: 'invalid amount (lamports)' });
    }

    const qs = new URLSearchParams({
      inputMint: input,
      outputMint: output,
      amount: String(amount),
      slippageBps: '50',
      onlyDirectRoutes: String(onlyDirectRoutes),
      restrictIntermediateTokens: 'true',
      swapMode: 'ExactIn',
    }).toString();

    // Prefer Pro jika ada key, lalu fallback ke Lite (atau sebaliknya jika tanpa key)
    const bases = HAS_JUP_KEY ? [JUP_QUOTE_PRO, JUP_QUOTE_LITE] : [JUP_QUOTE_LITE, JUP_QUOTE_PRO];
    let lastStatus = 500;
    let lastBody: any = { error: 'quote_failed' };

    for (const base of bases) {
      const url = `${base}?${qs}`;
      try {
        const r = await axios.get(url, {
          httpsAgent,
          timeout: 12000,
          validateStatus: () => true,
          headers: jupHeaders,
        });
        if (r.status === 200) return res.json(r.data);
        lastStatus = r.status;
        lastBody = r.data;
      } catch (e: any) {
        lastStatus = 599;
        lastBody = { error: e?.message || String(e) };
      }
    }
    return res.status(lastStatus).json(lastBody);
  } catch (e: any) {
    const msg = e?.response
      ? `HTTP ${e.response.status} ${JSON.stringify(e.response.data).slice(0, 300)}`
      : String(e?.message || e);
    return res.status(500).json({ error: msg });
  }
});

// ---------- Wallet helper ----------
app.post('/derive-address', (req: Request, res: Response) => {
  try {
    const { privateKey } = req.body ?? {};
    if (!privateKey) return res.status(400).json({ error: 'missing privateKey' });
    const address = deriveAddressFromPk(privateKey);
    res.json({ address });
  } catch (e: any) {
    res.status(400).json({ error: String(e?.message || e) });
  }
});

// ---------- DEX swap (Jupiter / Raydium-direct via Jupiter) ----------
app.post('/dex/swap', async (req: Request, res: Response) => {
  try {
    const {
      privateKey,
      inputMint,
      outputMint,
      amountLamports,
      dex = 'jupiter',
      slippageBps = 50,
      priorityFee = 0,
    } = req.body || {};

    if (!privateKey || !inputMint || !outputMint || amountLamports === undefined) {
      return res.status(400).json({ error: 'missing fields' });
    }
    if (!Number.isFinite(Number(amountLamports)) || Number(amountLamports) <= 0) {
      return res.status(400).json({ error: 'invalid amountLamports' });
    }

    const sig = await dexSwap({
      privateKey,
      inputMint: String(inputMint),
      outputMint: String(outputMint),
      amountLamports: Number(amountLamports),
      dex: (String(dex) as 'jupiter' | 'raydium'),
      slippageBps: Number(slippageBps),
      priorityFee: Number(priorityFee),
    });

    return res.json({ signature: sig });
  } catch (e: any) {
    const msg = String(e?.message || e);
    console.error('[dex/swap]', msg);
    return res.status(500).json({ error: msg });
  }
});

// ---------- Pump.fun local trade (+ optional Jito bundle) ----------
app.post('/pumpfun/swap', async (req: Request, res: Response) => {
  const {
    privateKey,
    action,
    mint,
    amount,
    useJito = false,
    bundleCount = 1,
    slippage = 10,
    priorityFee = 0.00005,
  } = req.body || {};

  if (!privateKey || !action || !mint || amount === undefined) {
    return res.status(400).json({ error: 'missing fields' });
  }

  const kp = keypairFromInput(privateKey);
  const publicKey = kp.publicKey.toBase58();

  const trade = await Trade.create({
    type: 'pumpfun',
    action: String(action).toLowerCase(),
    mint: String(mint),
    amount,
    jito: !!useJito,
    request: { slippage, priorityFee, bundleCount, publicKey },
  });

  try {
    if (useJito) {
      const arr = await tradeLocalBundle(
        Array.from({ length: Math.max(1, Number(bundleCount)) }).map((_, i) => ({
          publicKey,
          action: String(action).toLowerCase() as PumpAction,
          mint,
          amount,
          slippage: Number(slippage),
          priorityFee: i === 0 ? Number(priorityFee) : 0,
          pool: 'auto',
        })),
      );

      const signedList = arr.map((b58) => {
        const vtx = signFromBase58Unsigned(b58, kp);
        return bs58.encode(vtx.serialize());
      });

      try {
        await sendBundleToJito(signedList);
      } catch (e: any) {
        const msg = String(e?.message || e).toLowerCase();
        if (msg.includes('rate-limited')) {
          const txB64 = await tradeLocalSingle({
            publicKey,
            action: String(action).toLowerCase() as PumpAction,
            mint,
            amount,
            slippage: Number(slippage),
            priorityFee: Number(priorityFee),
            pool: 'auto',
          });
          const tx = signFromBase64Unsigned(txB64, kp);

          try {
            const connection = conn(cfg.rpcUrl);
            const sim = await connection.simulateTransaction(tx, {
              sigVerify: false,
              replaceRecentBlockhash: true,
            });
            if (sim.value.err) {
              const tail = (sim.value.logs ?? []).slice(-5).join(' | ');
              await Trade.findByIdAndUpdate(trade._id, {
                status: 'failed',
                error: { simErr: sim.value.err, logs: tail },
              });
              return res
                .status(400)
                .json({ error: 'simulation_failed', details: sim.value.err, logs: tail });
            }
          } catch {}

          const sig = await sendSignedVersionedTx(cfg.rpcUrl, tx);
          await Trade.findByIdAndUpdate(trade._id, { status: 'confirmed', signature: sig });
          return res.json({ signature: sig, fallback: true });
        }
        throw e;
      }

      await Trade.findByIdAndUpdate(trade._id, { status: 'submitted', signature: 'bundle_sent' });
      return res.json({ bundle: true, submitted: true });
    }

    // Non-bundle (single)
    const txB64 = await tradeLocalSingle({
      publicKey,
      action: String(action).toLowerCase() as PumpAction,
      mint,
      amount,
      slippage: Number(slippage),
      priorityFee: Number(priorityFee),
      pool: 'auto',
    });
    const unsigned = signFromBase64Unsigned(txB64, kp);

    try {
      const connection = conn(cfg.rpcUrl);
      const sim = await connection.simulateTransaction(unsigned, {
        sigVerify: false,
        replaceRecentBlockhash: true,
      });
      if (sim.value.err) {
        const tail = (sim.value.logs ?? []).slice(-5).join(' | ');
        await Trade.findByIdAndUpdate(trade._id, {
          status: 'failed',
          error: { simErr: sim.value.err, logs: tail },
        });
        return res
          .status(400)
          .json({ error: 'simulation_failed', details: sim.value.err, logs: tail });
      }
    } catch {}

    const sig = await sendSignedVersionedTx(cfg.rpcUrl, unsigned);
    await Trade.findByIdAndUpdate(trade._id, { status: 'confirmed', signature: sig });
    return res.json({ signature: sig });
  } catch (e: any) {
    const msg = String(e?.message || e);
    await Trade.findByIdAndUpdate(trade._id, { status: 'failed', error: msg });
    return res.status(500).json({ error: msg });
  }
});

// ---------- Boot ----------
(async () => {
  await connectDb();
  app.listen(cfg.port, '0.0.0.0', () => console.log(`trade-svc running on :${cfg.port}`));
})();

// ---------- Wallet (web3.js) ----------
app.get('/wallet/:addr/balance', async (req: Request, res: Response) => {
  try {
    const v = await getSolBalance(cfg.rpcUrl, String(req.params.addr));
    res.json({ sol: v });
  } catch (e: any) {
    res.status(500).json({ error: String(e?.message || e) });
  }
});

app.get('/wallet/:addr/tokens', async (req: Request, res: Response) => {
  try {
    const rows = await getTokenBalances(cfg.rpcUrl, String(req.params.addr));
    // opsional filter kosong
    const min = Number(req.query.min || '0');
    const filtered = rows.filter(r => r.amount > min);
    res.json({ tokens: filtered });
  } catch (e: any) {
    res.status(500).json({ error: String(e?.message || e) });
  }
});

app.get('/wallet/:addr/token/:mint/balance', async (req: Request, res: Response) => {
  try {
    const v = await getSpecificTokenBalance(cfg.rpcUrl, String(req.params.addr), String(req.params.mint));
    res.json({ amount: v });
  } catch (e: any) {
    res.status(500).json({ error: String(e?.message || e) });
  }
});

app.get('/wallet/mint/:mint/decimals', async (req: Request, res: Response) => {
  try {
    const d = await getMintDecimals(cfg.rpcUrl, String(req.params.mint));
    res.json({ decimals: d });
  } catch (e: any) {
    res.status(500).json({ error: String(e?.message || e) });
  }
});