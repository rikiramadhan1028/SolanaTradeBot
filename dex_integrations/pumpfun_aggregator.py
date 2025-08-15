# --- dex_integrations/pumpfun_aggregator.py (perbaikan local + tambah bundle) ---
import base64
from typing import Union, Optional, List
import httpx

PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"


def _bool_str(v: bool) -> str:
    return "true" if v else "false"


async def get_pumpfun_swap_transaction(
    public_key: str,
    action: str,
    mint: str,
    amount: Union[float, str],
    *,
    slippage: int = 10,
    priority_fee: float = 0.00001,
    pool: Optional[str] = "auto",
) -> Optional[str]:
    """
    Single local trade: balikan BASE64 dari response BYTES trade-local.
    Doc: response = bytes, deserialize+sign lalu kirim sendiri. :contentReference[oaicite:2]{index=2}
    """
    is_buy = action.lower() == "buy"
    payload = {
        "publicKey": public_key,
        "action": action.lower(),
        "mint": mint,
        "amount": amount if isinstance(amount, str) else float(amount),
        # SELL "100%" → harus token-denominated
        "denominatedInSol": _bool_str(is_buy and not (isinstance(amount, str) and amount.endswith("%"))),
        "slippage": int(slippage),
        "priorityFee": float(priority_fee),
        "pool": pool,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(PUMPPORTAL_TRADE_LOCAL, json=payload)
            if r.status_code != 200:
                r = await client.post(PUMPPORTAL_TRADE_LOCAL, data=payload)
            r.raise_for_status()
            return base64.b64encode(r.content).decode()
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun Local HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun Local ReqError] {getattr(e, 'request', None) and e.request.url!r}")
        return None


async def get_pumpfun_bundle_unsigned_base58(
    public_keys: list[str],
    actions: list[str],
    mints: list[str],
    amounts: list[Union[float, str]],
    *,
    slippage: int = 10,
    priority_fee: float = 0.00005,
    pool: Optional[str] = "auto",
) -> Optional[list[str]]:
    """
    Build Jito bundle tx via trade-local (array payload). Balik list base58 unsigned tx.
    Doc: body = ARRAY of trade objects; response JSON = array base58. :contentReference[oaicite:3]{index=3}
    """
    assert len({len(public_keys), len(actions), len(mints), len(amounts)}) == 1
    body = []
    for i in range(len(public_keys)):
        is_buy = actions[i].lower() == "buy"
        amt = amounts[i]
        body.append({
            "publicKey": public_keys[i],
            "action": actions[i].lower(),
            "mint": mints[i],
            "amount": amt if isinstance(amt, str) else float(amt),
            "denominatedInSol": _bool_str(is_buy and not (isinstance(amt, str) and str(amt).endswith("%"))),
            "slippage": int(slippage),
            # tip diambil dari tx PERTAMA di bundle
            "priorityFee": float(priority_fee if i == 0 else 0.0),
            "pool": pool,
        })
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(PUMPPORTAL_TRADE_LOCAL, json=body)
            r.raise_for_status()
            return r.json()  # list of base58-encoded unsigned tx
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun Bundle HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun Bundle ReqError] {getattr(e, 'request', None) and e.request.url!r}")
        return None
