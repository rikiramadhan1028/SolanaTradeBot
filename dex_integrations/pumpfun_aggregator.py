# file: dex_integrations/pumpfun_aggregator.py
import base64
from typing import Union, Optional, List
import httpx

PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"


def _bool_str(v: bool) -> str:
    # Kenapa string? Contoh Python resmi PumpPortal pakai "true"/"false".
    return "true" if v else "false"


def _is_percent(x: Union[str, float]) -> bool:
    return isinstance(x, str) and x.strip().endswith("%")


def _normalize_amount(x: Union[str, float]) -> Union[str, float]:
    # Biarkan "100%" tetap string; selain itu paksa float
    return x if _is_percent(x) else float(x)


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
    Bangun transaksi single via /trade-local untuk local signing.
    Return: base64 dari BYTES transaksi (agar client bisa from_bytes()).
    """
    act = action.lower().strip()
    if act not in {"buy", "sell"}:
        raise ValueError("action must be 'buy' or 'sell'")

    amt = _normalize_amount(amount)
    # WHY: BUY → denominatedInSol True; SELL persen → False (token-denominated)
    denom_sol = (act == "buy") and not _is_percent(amt)

    payload = {
        "publicKey": public_key,
        "action": act,
        "mint": mint,
        "amount": amt,
        "denominatedInSol": _bool_str(denom_sol),
        "slippage": int(slippage),
        "priorityFee": float(priority_fee),
        "pool": pool,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(PUMPPORTAL_TRADE_LOCAL, json=payload)
            if r.status_code != 200:
                # Beberapa edge-case server lebih suka form-encoded
                r = await client.post(PUMPPORTAL_TRADE_LOCAL, data=payload)
            r.raise_for_status()

            if not r.content:
                print("[Pumpfun Local] Empty response content")
                return None

            # Response BYTES → base64 string
            return base64.b64encode(r.content).decode()
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun Local HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun Local ReqError] {getattr(e, 'request', None) and e.request.url!r}")
        return None
    except Exception as e:
        print(f"[Pumpfun Local Error] {e}")
        return None


async def get_pumpfun_bundle_unsigned_base58(
    public_keys: List[str],
    actions: List[str],
    mints: List[str],
    amounts: List[Union[float, str]],
    *,
    slippage: int = 10,
    priority_fee: float = 0.00005,
    pool: Optional[str] = "auto",
) -> Optional[List[str]]:
    """
    Bangun bundle via /trade-local (body ARRAY). Return: list base58 unsigned tx.
    WHY: Jito bundle butuh base58 unsigned → ditandatangani lokal di client.
    """
    n = len(public_keys)
    if not (len(actions) == len(mints) == len(amounts) == n):
        raise ValueError("arrays must have the same length")

    body = []
    for i in range(n):
        act = actions[i].lower().strip()
        if act not in {"buy", "sell"}:
            raise ValueError("action must be 'buy' or 'sell'")
        amt = _normalize_amount(amounts[i])
        denom_sol = (act == "buy") and not _is_percent(amt)

        body.append(
            {
                "publicKey": public_keys[i],
                "action": act,
                "mint": mints[i],
                "amount": amt,
                "denominatedInSol": _bool_str(denom_sol),
                "slippage": int(slippage),
                # WHY: tip/jito diambil dari tx pertama agar bundle tidak dobel tip
                "priorityFee": float(priority_fee if i == 0 else 0.0),
                "pool": pool,
            }
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(PUMPPORTAL_TRADE_LOCAL, json=body)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                print(f"[Pumpfun Bundle] Unexpected response: {data!r}")
                return None
            return data
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun Bundle HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun Bundle ReqError] {getattr(e, 'request', None) and e.request.url!r}")
        return None
    except Exception as e:
        print(f"[Pumpfun Bundle Error] {e}")
        return None
