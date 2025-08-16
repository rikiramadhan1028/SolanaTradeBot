// trade-svc/src/wallet.ts
import { PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, getMint } from '@solana/spl-token';
import { conn } from './solana.js';

type TokenRow = {
  mint: string;
  amount: number;   // ui amount
  decimals: number;
  program: 'spl' | 'token2022';
};

export async function getSolBalance(rpcUrl: string, owner: string) {
  const c = conn(rpcUrl);
  const pk = new PublicKey(owner);
  const lamports = await c.getBalance(pk, { commitment: 'confirmed' });
  return lamports / LAMPORTS_PER_SOL;
}

export async function getTokenBalances(rpcUrl: string, owner: string): Promise<TokenRow[]> {
  const c = conn(rpcUrl);
  const pk = new PublicKey(owner);

  async function fetchByProgram(programId: PublicKey, label: 'spl'|'token2022'): Promise<TokenRow[]> {
    const accs = await c.getParsedTokenAccountsByOwner(pk, { programId }, 'confirmed');
    const out: TokenRow[] = [];
    for (const { account } of accs.value) {
      const parsed: any = account.data?.parsed;
      const info: any = parsed?.info;
      const mint: string | undefined = info?.mint;
      const ta: any = info?.tokenAmount;
      if (!mint || !ta) continue;
      const decimals = Number(ta.decimals ?? 0);
      const ui = (ta.uiAmount != null) ? Number(ta.uiAmount)
               : (ta.uiAmountString != null) ? Number(ta.uiAmountString)
               : (Number(ta.amount || 0) / (10 ** decimals || 1));
      out.push({ mint, amount: ui, decimals, program: label });
    }
    return out;
  }

  const [spl, t22] = await Promise.all([
    fetchByProgram(TOKEN_PROGRAM_ID, 'spl'),
    fetchByProgram(TOKEN_2022_PROGRAM_ID, 'token2022').catch(() => [] as TokenRow[]),
  ]);

  // gabung & akumulasikan jika ada multi-ATA utk mint sama
  const map = new Map<string, TokenRow>();
  for (const row of [...spl, ...t22]) {
    const prev = map.get(row.mint);
    if (!prev) map.set(row.mint, row);
    else prev.amount += row.amount;
  }
  return Array.from(map.values()).sort((a,b) => b.amount - a.amount);
}

export async function getSpecificTokenBalance(rpcUrl: string, owner: string, mint: string) {
  const c = conn(rpcUrl);
  const ownerPk = new PublicKey(owner);
  const mintPk = new PublicKey(mint);
  const accs = await c.getParsedTokenAccountsByOwner(ownerPk, { mint: mintPk }, 'confirmed');
  let total = 0;
  for (const { account } of accs.value) {
    const ta: any = account.data?.parsed?.info?.tokenAmount;
    if (!ta) continue;
    if (ta.uiAmount != null) total += Number(ta.uiAmount);
    else if (ta.uiAmountString != null) total += Number(ta.uiAmountString);
    else {
      const dec = Number(ta.decimals ?? 0);
      total += Number(ta.amount || 0) / (10 ** dec || 1);
    }
  }
  return total;
}

export async function getMintDecimals(rpcUrl: string, mint: string): Promise<number> {
  const c = conn(rpcUrl);
  const mintPk = new PublicKey(mint);
  try {
    const info = await getMint(c, mintPk, 'confirmed');
    return Number(info.decimals);
  } catch {
    return 6;
  }
}
