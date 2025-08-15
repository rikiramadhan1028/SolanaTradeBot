import axios from 'axios';
import { VersionedTransaction } from '@solana/web3.js';
const QUOTE = 'https://quote-api.jup.ag/v6/quote';
const SWAP = 'https://quote-api.jup.ag/v6/swap';
export async function jupQuote(params) {
    const url = new URL(QUOTE);
    url.searchParams.set('inputMint', params.inputMint);
    url.searchParams.set('outputMint', params.outputMint);
    url.searchParams.set('amount', String(params.amount));
    url.searchParams.set('swapMode', 'ExactIn');
    url.searchParams.set('slippageBps', String(params.slippageBps ?? 50));
    url.searchParams.set('onlyDirectRoutes', String(!!params.onlyDirectRoutes));
    const r = await axios.get(url.toString(), { validateStatus: () => true });
    if (r.status !== 200)
        throw new Error(`jup quote failed: ${r.status} ${typeof r.data === 'string' ? r.data : JSON.stringify(r.data)}`);
    if (!r.data?.routePlan)
        throw new Error('jup quote: empty routePlan');
    return r.data;
}
export async function jupBuildSwapTx(args) {
    const body = {
        quoteResponse: args.quoteResponse,
        userPublicKey: args.userPublicKey,
        wrapAndUnwrapSol: true,
        dynamicComputeUnitLimit: true,
        useSharedAccounts: true,
        asLegacyTransaction: false,
        computeUnitPriceMicroLamports: args.computeUnitPriceMicroLamports,
    };
    const r = await axios.post(SWAP, body, { validateStatus: () => true });
    if (r.status !== 200 || !r.data?.swapTransaction) {
        throw new Error(`jup swap build failed: ${r.status} ${typeof r.data === 'string' ? r.data : JSON.stringify(r.data)}`);
    }
    const buf = Buffer.from(r.data.swapTransaction, 'base64');
    return VersionedTransaction.deserialize(buf);
}
