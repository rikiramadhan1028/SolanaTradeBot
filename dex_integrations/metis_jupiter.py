# =============================
# file: dex_integrations/metis_jupiter.py
# =============================
from __future__ import annotations
import os, typing as t
import httpx

Json = t.Dict[str, t.Any]

"""
Metis/Jupiter client with ordered fallback and proper bodies for /quote and /swap.
Env:
  METIS_BASE   = https://jupiter-swap-api.quiknode.pro/<YOUR_KEY>
  JUP_PRO      = https://api.jup.ag/swap/v1
  JUP_LITE     = https://lite-api.jup.ag/swap/v1
  PUBLIC_JUP   = https://www.jupiterapi.com
  JUP_API_KEY  = <optional, for Pro>
  JUP_TIMEOUT  = seconds (default 15)
"""

DEFAULT_BASES: t.List[str] = [
    os.getenv("METIS_BASE", "").rstrip("/"),
    os.getenv("JUP_PRO", "https://api.jup.ag/swap/v1").rstrip("/"),
    os.getenv("JUP_LITE", "https://lite-api.jup.ag/swap/v1").rstrip("/"),
    os.getenv("PUBLIC_JUP", "https://www.jupiterapi.com").rstrip("/"),
]
BASES = [b for b in DEFAULT_BASES if b]
TIMEOUT = float(os.getenv("JUP_TIMEOUT", "15"))


def _headers_for(base: str) -> Json:
    h = {"User-Agent": "metis-integration/1.0"}
    api_key = os.getenv("JUP_API_KEY")
    if api_key and ("api.jup.ag" in base):
        h["X-API-KEY"] = api_key
    return h


def _url(base: str, path: str) -> str:
    return f"{base}{path if path.startswith('/') else '/' + path}"


async def get_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    *,
    slippage_bps: int | None = None,
    swap_mode: str | None = None,  # "ExactIn" | "ExactOut"
    as_legacy: bool = False,
    dynamic_slippage: bool | None = None,
    extra: Json | None = None,
) -> Json:
    params: Json = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
    }
    if slippage_bps is not None:
        params["slippageBps"] = slippage_bps
    if swap_mode:
        params["swapMode"] = swap_mode
    if as_legacy:
        params["asLegacyTransaction"] = True
    if dynamic_slippage is not None:
        params["dynamicSlippage"] = bool(dynamic_slippage)
    if extra:
        params.update(extra)

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for base in BASES:
            try:
                r = await client.get(_url(base, "/quote"), params=params, headers=_headers_for(base))
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and (
                        data.get("routePlan") or data.get("outAmount") or data.get("otherAmountThreshold")
                    ):
                        return data
                last_err = RuntimeError(f"{base} /quote {r.status_code} {str(r.text)[:300]}")
            except Exception as e:  # network/timeout
                last_err = e
    raise last_err or RuntimeError("quote_failed")


async def build_swap_tx(
    quote_response: Json,
    user_public_key: str,
    *,
    wrap_and_unwrap_sol: bool = True,
    compute_unit_price_micro_lamports: int | None = None,
    as_legacy: bool = False,
    fee_account: str | None = None,
    destination_token_account: str | None = None,
    dynamic_cu_limit: bool = True,
    dynamic_slippage: bool | None = None,
    extra: Json | None = None,
) -> str:
    body: Json = {
        "userPublicKey": user_public_key,
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": wrap_and_unwrap_sol,
        "dynamicComputeUnitLimit": dynamic_cu_limit,
    }
    if compute_unit_price_micro_lamports is not None:
        body["computeUnitPriceMicroLamports"] = int(compute_unit_price_micro_lamports)
    if as_legacy:
        body["asLegacyTransaction"] = True
    if fee_account:
        body["feeAccount"] = fee_account
    if destination_token_account:
        body["destinationTokenAccount"] = destination_token_account
    if dynamic_slippage is not None:
        body["dynamicSlippage"] = bool(dynamic_slippage)
    if extra:
        body.update(extra)

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for base in BASES:
            try:
                r = await client.post(_url(base, "/swap"), json=body, headers=_headers_for(base))
                js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code == 200 and isinstance(js, dict) and js.get("swapTransaction"):
                    return js["swapTransaction"]
                if (r.status_code in (400, 422)) and not as_legacy:
                    body2 = dict(body)
                    body2["asLegacyTransaction"] = True
                    r2 = await client.post(_url(base, "/swap"), json=body2, headers=_headers_for(base))
                    js2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
                    if r2.status_code == 200 and js2.get("swapTransaction"):
                        return js2["swapTransaction"]
                last_err = RuntimeError(f"{base} /swap {r.status_code} {str(r.text)[:300]}")
            except Exception as e:
                last_err = e
    raise last_err or RuntimeError("swap_failed")


# --- Backward-compatible shims (keep old import sites working) ---
async def get_swap_route(input_mint: str, output_mint: str, amount: int):
    return await get_quote(input_mint, output_mint, amount)


async def get_swap_transaction(route_or_quote: Json, user_public_key: str):
    qr = route_or_quote
    if not isinstance(qr, dict) or not qr.get("inputMint"):
        raise ValueError("get_swap_transaction now requires the full quote object; call get_swap_route() first")
    return await build_swap_tx(qr, user_public_key, wrap_and_unwrap_sol=True)
