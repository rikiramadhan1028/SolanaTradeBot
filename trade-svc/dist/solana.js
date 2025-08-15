import { Connection, Keypair, VersionedTransaction } from '@solana/web3.js';
import bs58 from 'bs58';
export const conn = (rpcUrl) => new Connection(rpcUrl, 'confirmed');
export function keypairFromInput(input) {
    let secret;
    if (input.trim().startsWith('[')) {
        secret = Uint8Array.from(JSON.parse(input));
    }
    else {
        secret = bs58.decode(input.trim());
    }
    if (secret.length !== 64)
        throw new Error('Private key must be 64 bytes');
    return Keypair.fromSecretKey(secret);
}
export function deriveAddressFromPk(input) {
    return keypairFromInput(input).publicKey.toBase58();
}
export async function sendSignedVersionedTx(rpcUrl, tx) {
    const connection = conn(rpcUrl);
    const sig = await connection.sendRawTransaction(tx.serialize(), {
        skipPreflight: false,
        preflightCommitment: 'confirmed'
    });
    await connection.confirmTransaction(sig, 'confirmed');
    return sig;
}
export function signFromBase64Unsigned(unsignedB64, kp) {
    const buf = Buffer.from(unsignedB64, 'base64');
    const unsigned = VersionedTransaction.deserialize(buf);
    unsigned.sign([kp]);
    return unsigned;
}
export function signFromBase58Unsigned(unsignedB58, kp) {
    const buf = Buffer.from(bs58.decode(unsignedB58));
    const unsigned = VersionedTransaction.deserialize(buf);
    unsigned.sign([kp]);
    return unsigned;
}
