import 'dotenv/config';
export const cfg = {
    rpcUrl: process.env.SOLANA_RPC_URL || '',
    pumpBase: process.env.PUMPPORTAL_BASE || 'https://pumpportal.fun',
    jitoEndpoint: process.env.JITO_BUNDLE_ENDPOINT || 'https://mainnet.block-engine.jito.wtf/api/v1/bundles',
    mongoUrl: process.env.MONGO_URL || '', // now optional
    port: Number(process.env.PORT || 8080),
};
if (!cfg.rpcUrl) {
    throw new Error('Missing env: SOLANA_RPC_URL');
}
