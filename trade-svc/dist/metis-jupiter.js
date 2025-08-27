import axios from 'axios';
import https from 'node:https';
const httpsAgent = new https.Agent({ keepAlive: true, family: 4, maxSockets: 50 });
const UA = 'trade-svc/1.0';
const METIS_BASE = (process.env.METIS_BASE || '').replace(/\/$/, '');
const JUP_PRO = (process.env.JUP_PRO || 'https://api.jup.ag/swap/v1').replace(/\/$/, '');
const JUP_LITE = (process.env.JUP_LITE || 'https://lite-api.jup.ag/swap/v1').replace(/\/$/, '');
const PUBLIC_JUP = (process.env.PUBLIC_JUP || 'https://www.jupiterapi.com').replace(/\/$/, '');
const BASES = [METIS_BASE, JUP_PRO, JUP_LITE, PUBLIC_JUP].filter(Boolean);
function headersFor(base) {
    const h = { 'User-Agent': UA };
    const key = process.env.JUP_API_KEY;
    if (key && /api\.jup\.ag/.test(base))
        h['X-API-KEY'] = key;
    return h;
}
function url(base, path) { return `${base}${path.startsWith('/') ? path : '/' + path}`; }
function mapAxiosErr(e) {
    const ae = e;
    if (ae?.response)
        return `HTTP ${ae.response.status} ${String(JSON.stringify(ae.response.data)).slice(0, 300)}`;
    if (ae?.request)
        return `network_error to ${(ae.config?.url) || 'unknown'}: ${ae.code || ae.message}`;
    return e?.message || String(e);
}
export async function getQuote(p) {
    const params = new URLSearchParams({
        inputMint: p.inputMint,
        outputMint: p.outputMint,
        amount: String(p.amountRaw),
    });
    if (p.slippageBps != null)
        params.set('slippageBps', String(p.slippageBps));
    if (p.swapMode)
        params.set('swapMode', p.swapMode);
    if (p.asLegacyTransaction)
        params.set('asLegacyTransaction', 'true');
    if (p.dynamicSlippage != null)
        params.set('dynamicSlippage', String(!!p.dynamicSlippage));
    if (p.extra)
        for (const [k, v] of Object.entries(p.extra))
            params.set(k, String(v));
    let lastErr = '';
    for (const base of BASES) {
        try {
            const r = await axios.get(url(base, '/quote'), { httpsAgent, timeout: 12000, validateStatus: () => true, headers: headersFor(base), params });
            if (r.status === 200 && r.data && (r.data.routePlan || r.data.outAmount || r.data.otherAmountThreshold)) {
                return r.data; // full quote object
            }
            lastErr = `quote ${r.status} ${String(JSON.stringify(r.data)).slice(0, 300)}`;
        }
        catch (e) {
            lastErr = mapAxiosErr(e);
        }
    }
    throw new Error(lastErr || 'quote_failed');
}
export async function buildSwapTx(opts) {
    const baseBody = {
        userPublicKey: opts.userPublicKey,
        quoteResponse: opts.quote,
        wrapAndUnwrapSol: opts.wrapAndUnwrapSol !== false,
        dynamicComputeUnitLimit: opts.dynamicComputeUnitLimit !== false,
    };
    // Priority fee handling - prefer new API format
    if (opts.priorityFeeLamports != null) {
        // Use new Jupiter API format
        baseBody.prioritizationFeeLamports = {
            priorityLevelWithMaxLamports: {
                maxLamports: opts.priorityFeeLamports,
                priorityLevel: "veryHigh"
            }
        };
    }
    else if (opts.computeUnitPriceMicroLamports != null) {
        // Legacy fallback
        baseBody.computeUnitPriceMicroLamports = opts.computeUnitPriceMicroLamports;
    }
    if (opts.asLegacyTransaction)
        baseBody.asLegacyTransaction = true;
    if (opts.destinationTokenAccount)
        baseBody.destinationTokenAccount = opts.destinationTokenAccount;
    if (opts.feeAccount)
        baseBody.feeAccount = opts.feeAccount;
    if (opts.dynamicSlippage != null)
        baseBody.dynamicSlippage = !!opts.dynamicSlippage;
    if (opts.extra)
        Object.assign(baseBody, opts.extra);
    for (const base of BASES) {
        try {
            const r = await axios.post(url(base, '/swap'), baseBody, { httpsAgent, timeout: 15000, validateStatus: () => true, headers: { 'Content-Type': 'application/json', ...headersFor(base) } });
            if (r.status === 200 && r.data?.swapTransaction)
                return r.data.swapTransaction;
            if ((r.status === 400 || r.status === 422) && !baseBody.asLegacyTransaction) {
                const r2 = await axios.post(url(base, '/swap'), { ...baseBody, asLegacyTransaction: true }, { httpsAgent, timeout: 15000, validateStatus: () => true, headers: { 'Content-Type': 'application/json', ...headersFor(base) } });
                if (r2.status === 200 && r2.data?.swapTransaction)
                    return r2.data.swapTransaction;
            }
        }
        catch (_) {
            // try next base
        }
    }
    throw new Error('swap_failed');
}
