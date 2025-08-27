import axios, { AxiosError } from 'axios';
import https from 'node:https';

export type QuoteParams = {
  inputMint: string;
  outputMint: string;
  amountRaw: number; // base units
  slippageBps?: number;
  swapMode?: 'ExactIn' | 'ExactOut';
  asLegacyTransaction?: boolean;
  dynamicSlippage?: boolean;
  extra?: Record<string, any>;
};

export type SwapOpts = {
  userPublicKey: string;
  quote: any; // full quote object
  computeUnitPriceMicroLamports?: number;
  priorityFeeLamports?: number; // NEW: Direct lamports fee for Jupiter API
  asLegacyTransaction?: boolean;
  wrapAndUnwrapSol?: boolean;
  destinationTokenAccount?: string;
  feeAccount?: string;
  dynamicComputeUnitLimit?: boolean;
  dynamicSlippage?: boolean;
  extra?: Record<string, any>;
};

const httpsAgent = new https.Agent({ keepAlive: true, family: 4, maxSockets: 50 });
const UA = 'trade-svc/1.0';

const METIS_BASE = (process.env.METIS_BASE || '').replace(/\/$/, '');
const JUP_PRO    = (process.env.JUP_PRO    || 'https://api.jup.ag/swap/v1').replace(/\/$/, '');
const JUP_LITE   = (process.env.JUP_LITE   || 'https://lite-api.jup.ag/swap/v1').replace(/\/$/, '');
const PUBLIC_JUP = (process.env.PUBLIC_JUP || 'https://quote-api.jup.ag/v6').replace(/\/$/, '');

// Prioritize public API first, then others
const BASES = [PUBLIC_JUP, METIS_BASE, JUP_LITE, JUP_PRO].filter(Boolean);

function headersFor(base: string) {
  const h: Record<string, string> = { 'User-Agent': UA };
  const key = process.env.JUP_API_KEY;
  if (key && /api\.jup\.ag/.test(base)) h['X-API-KEY'] = key;
  return h;
}

function url(base: string, path: string) { return `${base}${path.startsWith('/') ? path : '/' + path}`; }

function mapAxiosErr(e: unknown): string {
  const ae = e as AxiosError;
  if (ae?.response) return `HTTP ${ae.response.status} ${String(JSON.stringify(ae.response.data)).slice(0, 300)}`;
  if (ae?.request)  return `network_error to ${(ae.config?.url)||'unknown'}: ${ae.code||ae.message}`;
  return (e as any)?.message || String(e);
}

export async function getQuote(p: QuoteParams): Promise<any> {
  const params = new URLSearchParams({
    inputMint: p.inputMint,
    outputMint: p.outputMint,
    amount: String(p.amountRaw),
  });
  if (p.slippageBps != null) params.set('slippageBps', String(p.slippageBps));
  if (p.swapMode) params.set('swapMode', p.swapMode);
  if (p.asLegacyTransaction) params.set('asLegacyTransaction', 'true');
  if (p.dynamicSlippage != null) params.set('dynamicSlippage', String(!!p.dynamicSlippage));
  if (p.extra) for (const [k, v] of Object.entries(p.extra)) params.set(k, String(v));

  let lastErr = '';
  for (const base of BASES) {
    try {
      const r = await axios.get(url(base, '/quote'), { httpsAgent, timeout: 12000, validateStatus: () => true, headers: headersFor(base), params });
      if (r.status === 200 && r.data && (r.data.routePlan || r.data.outAmount || r.data.otherAmountThreshold)) {
        return r.data; // full quote object
      }
      lastErr = `quote ${r.status} ${String(JSON.stringify(r.data)).slice(0, 300)}`;
    } catch (e) {
      lastErr = mapAxiosErr(e);
    }
  }
  throw new Error(lastErr || 'quote_failed');
}

export async function buildSwapTx(opts: SwapOpts): Promise<string> {
  const baseBody: Record<string, any> = {
    userPublicKey: opts.userPublicKey,
    quoteResponse: opts.quote,
    wrapAndUnwrapSol: opts.wrapAndUnwrapSol !== false,
    dynamicComputeUnitLimit: opts.dynamicComputeUnitLimit !== false,
  };
  
  // Priority fee handling - try multiple formats
  function addPriorityFeeToBody(body: any, base: string) {
    if (opts.priorityFeeLamports != null) {
      // For v6 API (quote-api.jup.ag) - use direct lamports format
      if (base.includes('quote-api.jup.ag')) {
        // Simple direct priority fee in lamports - this is the correct format for v6
        body.prioritizationFeeLamports = opts.priorityFeeLamports;
        console.log(`DEBUG Jupiter v6 API: Direct prioritizationFeeLamports = ${opts.priorityFeeLamports}`);
      }
      // For v1 APIs (api.jup.ag, lite-api.jup.ag) - use legacy format
      else {
        // Convert lamports to CU price (more aggressive conversion)
        const cuPrice = Math.round((opts.priorityFeeLamports * 10) / 1000);
        body.computeUnitPriceMicroLamports = cuPrice;
        console.log(`DEBUG Jupiter v1 API: computeUnitPriceMicroLamports = ${cuPrice} (from ${opts.priorityFeeLamports} lamports)`);
      }
    } else if (opts.computeUnitPriceMicroLamports != null) {
      body.computeUnitPriceMicroLamports = opts.computeUnitPriceMicroLamports;
      console.log(`DEBUG Jupiter API: computeUnitPriceMicroLamports = ${opts.computeUnitPriceMicroLamports}`);
    } else {
      console.log(`DEBUG Jupiter API: No priority fee parameters set`);
    }
  }
  
  if (opts.asLegacyTransaction) baseBody.asLegacyTransaction = true;
  if (opts.destinationTokenAccount) baseBody.destinationTokenAccount = opts.destinationTokenAccount;
  if (opts.feeAccount) baseBody.feeAccount = opts.feeAccount;
  if (opts.dynamicSlippage != null) baseBody.dynamicSlippage = !!opts.dynamicSlippage;
  if (opts.extra) Object.assign(baseBody, opts.extra);

  for (const base of BASES) {
    try {
      // Create body copy and add appropriate priority fee format
      const requestBody = { ...baseBody };
      addPriorityFeeToBody(requestBody, base);
      
      const r = await axios.post(url(base, '/swap'), requestBody, { 
        httpsAgent, 
        timeout: 15000, 
        validateStatus: () => true, 
        headers: { 'Content-Type': 'application/json', ...headersFor(base) } 
      });
      
      // Debug: Log Jupiter API response
      if (r.status === 200) {
        console.log(`✅ Jupiter API Success: ${base} responded with status 200`);
        if (r.data?.swapTransaction) {
          console.log(`✅ Got swapTransaction from Jupiter (length: ${r.data.swapTransaction.length})`);
          return r.data.swapTransaction as string;
        }
      } else {
        console.log(`❌ Jupiter API Error: ${base} responded with status ${r.status}`);
        console.log(`   Response: ${JSON.stringify(r.data).slice(0, 500)}`);
      }
      
      // Retry with legacy transaction if needed
      if ((r.status === 400 || r.status === 422) && !requestBody.asLegacyTransaction) {
        const retryBody = { ...requestBody, asLegacyTransaction: true };
        const r2 = await axios.post(url(base, '/swap'), retryBody, { 
          httpsAgent, 
          timeout: 15000, 
          validateStatus: () => true, 
          headers: { 'Content-Type': 'application/json', ...headersFor(base) } 
        });
        if (r2.status === 200 && r2.data?.swapTransaction) return r2.data.swapTransaction as string;
      }
    } catch (_) {
      // try next base
    }
  }
  throw new Error('swap_failed');
}
