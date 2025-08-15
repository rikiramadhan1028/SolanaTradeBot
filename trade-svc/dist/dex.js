import { jupQuote, jupBuildSwapTx } from './jupiter.js';
import { Connection, Keypair } from '@solana/web3.js';
import { cfg } from './config.js';
import { Trade } from './db.js';
import bs58 from 'bs58';
export async function dexSwap(params) {
    let sec;
    if (params.privateKey.trim().startsWith('['))
        sec = Uint8Array.from(JSON.parse(params.privateKey));
    else
        sec = bs58.decode(params.privateKey.trim());
    if (sec.length !== 64)
        throw new Error('Invalid private key length');
    const kp = Keypair.fromSecretKey(sec);
    const userPk = kp.publicKey.toBase58();
    const onlyDirect = params.dex === 'raydium';
    const priorityMicroLamports = undefined; // set if you want CU price; optional
    const log = await Trade.create({
        type: 'dex', action: 'swap', dex: params.dex ?? 'jupiter',
        mintIn: params.inputMint, mintOut: params.outputMint, amount: params.amountLamports,
        request: { slippageBps: params.slippageBps ?? 50, onlyDirect, priorityFee: params.priorityFee }
    });
    const quote = await jupQuote({
        inputMint: params.inputMint,
        outputMint: params.outputMint,
        amount: params.amountLamports,
        slippageBps: params.slippageBps ?? 50,
        onlyDirectRoutes: onlyDirect,
    });
    const unsigned = await jupBuildSwapTx({
        quoteResponse: quote,
        userPublicKey: userPk,
        computeUnitPriceMicroLamports: priorityMicroLamports
    });
    const conn = new Connection(cfg.rpcUrl, 'confirmed');
    try {
        const sim = await conn.simulateTransaction(unsigned, { sigVerify: false, replaceRecentBlockhash: true });
        if (sim.value.err) {
            const tail = (sim.value.logs ?? []).slice(-8).join(' | ');
            await Trade.findByIdAndUpdate(log._id, { status: 'failed', error: { simErr: sim.value.err, logs: tail } });
            throw new Error(`Simulation failed: ${JSON.stringify(sim.value.err)} | logs: ${tail}`);
        }
    }
    catch { /* continue */ }
    unsigned.sign([kp]);
    const sig = await conn.sendRawTransaction(unsigned.serialize(), { skipPreflight: false, preflightCommitment: 'confirmed' });
    await conn.confirmTransaction(sig, 'confirmed');
    await Trade.findByIdAndUpdate(log._id, { status: 'confirmed', signature: sig });
    return sig;
}
