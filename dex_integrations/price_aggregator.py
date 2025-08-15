# file: dex_integrations/price_aggregator.py
# Price via Jupiter Price API v3 + helpers you already had.
from typing import Dict
import httpx

JUP_PRICE_URL = "https://price.jup.ag/v3/price"  # official v3

async def get_token_price(mint: str, vs_token: str = "USDC") -> Dict:
    """
    Get price for a single token mint using Jupiter Price API v3.
    Returns: {"price": float, "mc": "N/A", "source": "jup"}
    If unavailable, returns price=0.0
    """
    try:
        params = {"ids": mint, "vsToken": vs_token}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(JUP_PRICE_URL, params=params)
            r.raise_for_status()
            data = r.json() or {}
            # v3 response format example:
            # { "data": { "<mint>": { "id": "...", "price": 0.123..., "vsToken": "USDC", ... } } }
            entry = (data.get("data") or {}).get(mint)
            if not entry:
                return {"price": 0.0, "mc": "N/A", "source": "jup"}
            price = entry.get("price")
            if price is None:
                return {"price": 0.0, "mc": "N/A", "source": "jup"}
            return {"price": float(price), "mc": "N/A", "source": "jup"}
    except Exception:
        return {"price": 0.0, "mc": "N/A", "source": "jup"}


# Keep these if other parts of the bot import them
# (your main already calls these as fallbacks)
async def get_token_price_from_raydium(mint: str) -> Dict:
    # stub/fallback kept for compatibility – implement as in your repo if needed
    return {"price": 0.0, "mc": "N/A", "source": "raydium"}

async def get_token_price_from_pumpfun(mint: str) -> Dict:
    # stub/fallback kept for compatibility – implement as in your repo if needed
    return {"price": 0.0, "mc": "N/A", "source": "pumpfun"}
