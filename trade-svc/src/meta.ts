// trade-svc/src/meta.ts
import type { Express, Request, Response } from "express";
import https from "node:https";
import axios from "axios";

const httpsAgent = new https.Agent({ keepAlive: true, family: 4, maxSockets: 50 });

export type TokenMeta = {
  mint: string;
  symbol: string;
  name: string;
  decimals?: number;
  source: "jup" | "dexscreener" | "fallback";
};

const TTL_MS = (Number(process.env.META_TTL_SEC) || 3600) * 1000; // default 1 jam
const _cache = new Map<string, { data: TokenMeta; exp: number }>();

function _norm(mint: string) {
  return (mint || "").trim();
}

function _set(mint: string, data: TokenMeta) {
  _cache.set(_norm(mint), { data, exp: Date.now() + TTL_MS });
}

function _get(mint: string): TokenMeta | null {
  const it = _cache.get(_norm(mint));
  if (!it) return null;
  if (Date.now() > it.exp) {
    _cache.delete(_norm(mint));
    return null;
  }
  return it.data;
}

async function fetchFromJupiter(mint: string): Promise<TokenMeta | null> {
  const url = `https://tokens.jup.ag/token/${mint}`;
  try {
    const r = await axios.get(url, {
      httpsAgent,
      timeout: 8000,
      validateStatus: () => true,
      headers: { "User-Agent": "trade-svc/1.0" },
    });
    if (r.status !== 200 || !r.data) return null;
    const d = r.data as any;
    // shape example: { address, chainId, decimals, name, symbol, ... }
    const symbol = (d.symbol || d.name || "").toString().trim();
    const name = (d.name || d.symbol || symbol || "").toString().trim();
    if (!symbol && !name) return null;
    return {
      mint,
      symbol: symbol || name || mint.slice(0, 6).toUpperCase(),
      name: name || symbol || "",
      decimals: Number.isFinite(d.decimals) ? Number(d.decimals) : undefined,
      source: "jup",
    };
  } catch {
    return null;
  }
}

async function fetchFromDexScreener(mint: string): Promise<TokenMeta | null> {
  const url = `https://api.dexscreener.com/latest/dex/tokens/${mint}`;
  try {
    const r = await axios.get(url, {
      httpsAgent,
      timeout: 8000,
      validateStatus: () => true,
      headers: { "User-Agent": "trade-svc/1.0" },
    });
    if (r.status !== 200 || !r.data?.pairs?.length) return null;
    // cari pasangan yg base/quote address == mint
    for (const p of r.data.pairs as any[]) {
      const base = p?.baseToken || {};
      const quote = p?.quoteToken || {};
      if (base.address === mint) {
        const symbol = (base.symbol || base.name || "").toString().trim();
        const name = (base.name || base.symbol || "").toString().trim();
        return {
          mint,
          symbol: symbol || name || mint.slice(0, 6).toUpperCase(),
          name: name || symbol || "",
          source: "dexscreener",
        };
      }
      if (quote.address === mint) {
        const symbol = (quote.symbol || quote.name || "").toString().trim();
        const name = (quote.name || quote.symbol || "").toString().trim();
        return {
          mint,
          symbol: symbol || name || mint.slice(0, 6).toUpperCase(),
          name: name || symbol || "",
          source: "dexscreener",
        };
      }
    }
    return null;
  } catch {
    return null;
  }
}

export async function resolveMeta(mint: string): Promise<TokenMeta> {
  const m = _norm(mint);
  const cached = _get(m);
  if (cached) return cached;

  let meta = await fetchFromJupiter(m);
  if (!meta) meta = await fetchFromDexScreener(m);

  if (!meta) {
    meta = {
      mint: m,
      symbol: m.slice(0, 6).toUpperCase(),
      name: "",
      source: "fallback",
    };
  }
  _set(m, meta);
  return meta;
}

export function attachMetaRoutes(app: Express) {
  // Single token
  app.get("/meta/token/:mint", async (req: Request, res: Response) => {
    const mint = _norm(req.params.mint || "");
    if (!mint) return res.status(400).json({ error: "missing mint" });
    try {
      const meta = await resolveMeta(mint);
      return res.json(meta);
    } catch (e: any) {
      return res.status(500).json({ error: String(e?.message || e) });
    }
  });

  // Batch: /meta/tokens?mints=a,b,c
  app.get("/meta/tokens", async (req: Request, res: Response) => {
    const raw = String(req.query.mints || "").trim();
    if (!raw) return res.status(400).json({ error: "missing mints" });
    const mints = raw.split(",").map(_norm).filter(Boolean);
    try {
      const metas = await Promise.all(mints.map(resolveMeta));
      return res.json({ items: metas });
    } catch (e: any) {
      return res.status(500).json({ error: String(e?.message || e) });
    }
  });
}
