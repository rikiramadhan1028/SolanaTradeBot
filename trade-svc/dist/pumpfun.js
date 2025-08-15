import axios from 'axios';
import { cfg } from './config.js';
const url = `${cfg.pumpBase}/api/trade-local`;
const boolStr = (b) => (b ? 'true' : 'false');
export async function tradeLocalSingle(req) {
    const payload = {
        publicKey: req.publicKey,
        action: req.action,
        mint: req.mint,
        amount: typeof req.amount === 'string' ? req.amount : Number(req.amount),
        denominatedInSol: boolStr(req.action === 'buy' && !(typeof req.amount === 'string' && req.amount.endsWith('%'))),
        slippage: req.slippage ?? 10,
        priorityFee: req.priorityFee ?? 0.00005,
        pool: req.pool ?? 'auto',
    };
    const r = await axios.post(url, payload, { responseType: 'arraybuffer', validateStatus: () => true });
    if (r.status !== 200) {
        const r2 = await axios.post(url, new URLSearchParams(Object.entries(payload)), { responseType: 'arraybuffer', validateStatus: () => true });
        if (r2.status !== 200)
            throw new Error(`pumpfun trade-local failed: ${r.status} ${typeof r.data === 'string' ? r.data : ''}`);
        return Buffer.from(r2.data).toString('base64');
    }
    return Buffer.from(r.data).toString('base64');
}
export async function tradeLocalBundle(body) {
    const normalized = body.map((x, i) => ({
        publicKey: x.publicKey,
        action: x.action,
        mint: x.mint,
        amount: typeof x.amount === 'string' ? x.amount : Number(x.amount),
        denominatedInSol: (x.action === 'buy') && !(typeof x.amount === 'string' && String(x.amount).endsWith('%')) ? 'true' : 'false',
        slippage: x.slippage ?? 10,
        priorityFee: i === 0 ? (x.priorityFee ?? 0.0001) : 0,
        pool: x.pool ?? 'auto',
    }));
    const r = await axios.post(url, normalized, { validateStatus: () => true });
    if (r.status !== 200)
        throw new Error(`pumpfun bundle trade-local failed: ${r.status} ${typeof r.data === 'string' ? r.data : JSON.stringify(r.data)}`);
    if (!Array.isArray(r.data))
        throw new Error(`unexpected bundle response: ${JSON.stringify(r.data)}`);
    return r.data; // base58 unsigned
}
