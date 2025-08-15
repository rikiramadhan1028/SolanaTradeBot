import axios from 'axios';
import { cfg } from './config.js';
export async function sendBundleToJito(signedBase58List) {
    const payload = { jsonrpc: '2.0', id: 1, method: 'sendBundle', params: [signedBase58List] };
    const r = await axios.post(cfg.jitoEndpoint, payload, { validateStatus: () => true });
    if (r.status === 429)
        throw new Error('jito rate-limited (429)');
    if (r.status >= 400)
        throw new Error(`jito error ${r.status}: ${typeof r.data === 'string' ? r.data : JSON.stringify(r.data)}`);
}
