import express from 'express';
import { cfg } from './config.js';
import { connectDb, Trade } from './db.js';
import { keypairFromInput, deriveAddressFromPk, sendSignedVersionedTx, signFromBase64Unsigned, signFromBase58Unsigned, conn, } from './solana.js';
import { tradeLocalSingle, tradeLocalBundle } from './pumpfun.js';
import { sendBundleToJito } from './jito.js';
import { dexSwap } from './dex.js';
import bs58 from 'bs58';
const app = express();
app.use(express.json({ limit: '1mb' }));
app.get('/health', (_req, res) => res.json({ ok: true }));
app.post('/derive-address', (req, res) => {
    try {
        const { privateKey } = req.body ?? {};
        const address = deriveAddressFromPk(privateKey);
        res.json({ address });
    }
    catch (e) {
        res.status(400).json({ error: String(e.message || e) });
    }
});
app.post('/dex/swap', async (req, res) => {
    try {
        const { privateKey, inputMint, outputMint, amountLamports, dex = 'jupiter', slippageBps = 50, priorityFee = 0 } = req.body || {};
        if (!privateKey || !inputMint || !outputMint || !amountLamports) {
            return res.status(400).json({ error: 'missing fields' });
        }
        const sig = await dexSwap({ privateKey, inputMint, outputMint, amountLamports, dex, slippageBps, priorityFee });
        return res.json({ signature: sig });
    }
    catch (e) {
        return res.status(500).json({ error: String(e.message || e) });
    }
});
app.post('/pumpfun/swap', async (req, res) => {
    const { privateKey, action, mint, amount, useJito = false, bundleCount = 1, slippage = 10, priorityFee = 0.00005 } = req.body || {};
    if (!privateKey || !action || !mint || amount === undefined)
        return res.status(400).json({ error: 'missing fields' });
    const kp = keypairFromInput(privateKey);
    const publicKey = kp.publicKey.toBase58();
    const trade = await Trade.create({
        type: 'pumpfun',
        action,
        mint,
        amount,
        jito: !!useJito,
        request: { slippage, priorityFee, bundleCount, publicKey },
    });
    try {
        if (useJito) {
            const arr = await tradeLocalBundle(Array.from({ length: Math.max(1, Number(bundleCount)) }).map((_, i) => ({
                publicKey,
                action,
                mint,
                amount,
                slippage,
                priorityFee: i === 0 ? priorityFee : 0,
                pool: 'auto',
            })));
            const signedList = arr.map((b58) => {
                const vtx = signFromBase58Unsigned(b58, kp);
                return bs58.encode(vtx.serialize());
            });
            try {
                await sendBundleToJito(signedList);
            }
            catch (e) {
                if (String(e.message || e).toLowerCase().includes('rate-limited')) {
                    const txB64 = await tradeLocalSingle({ publicKey, action, mint, amount, slippage, priorityFee, pool: 'auto' });
                    const tx = signFromBase64Unsigned(txB64, kp);
                    const sig = await sendSignedVersionedTx(cfg.rpcUrl, tx);
                    await Trade.findByIdAndUpdate(trade._id, { status: 'confirmed', signature: sig });
                    return res.json({ signature: sig, fallback: true });
                }
                throw e;
            }
            await Trade.findByIdAndUpdate(trade._id, { status: 'submitted', signature: 'bundle_sent' });
            return res.json({ bundle: true, submitted: true });
        }
        else {
            const txB64 = await tradeLocalSingle({ publicKey, action, mint, amount, slippage, priorityFee, pool: 'auto' });
            const unsigned = signFromBase64Unsigned(txB64, kp);
            try {
                const connection = conn(cfg.rpcUrl);
                const sim = await connection.simulateTransaction(unsigned, { sigVerify: false, replaceRecentBlockhash: true });
                if (sim.value.err) {
                    const tail = (sim.value.logs ?? []).slice(-5).join(' | ');
                    await Trade.findByIdAndUpdate(trade._id, { status: 'failed', error: { simErr: sim.value.err, logs: tail } });
                    return res.status(400).json({ error: 'simulation_failed', details: sim.value.err, logs: tail });
                }
            }
            catch { }
            const sig = await sendSignedVersionedTx(cfg.rpcUrl, unsigned);
            await Trade.findByIdAndUpdate(trade._id, { status: 'confirmed', signature: sig });
            return res.json({ signature: sig });
        }
    }
    catch (e) {
        await Trade.findByIdAndUpdate(trade._id, { status: 'failed', error: String(e?.message || e) });
        return res.status(500).json({ error: String(e?.message || e) });
    }
});
(async () => {
    await connectDb(); // no-op if no MONGO_URL
    app.listen(cfg.port, () => console.log(`trade-svc running on :${cfg.port}`));
})();
