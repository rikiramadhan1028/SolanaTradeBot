# file: dex_integrations/pumpfun_aggregator.py
import base64
from typing import Union, Optional

import httpx

PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"


def _bool_as_str(v: bool) -> str:
    # Local API menerima "true"/"false" (string) pada contoh Python resminya.
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
    Ambil serialized transaction (base64) untuk local signing dari PumpPortal.
    - Response asli adalah BYTES; fungsi ini mengembalikan BASE64 agar seragam dengan Jupiter path.
    - action: "buy" | "sell"
    - amount:
        * BUY: float jumlah SOL (denominatedInSol=True)
        * SELL: float jumlah token, atau string persen seperti "100%"
    """
    is_buy = action.lower() == "buy"

    # amount bisa float atau "100%". Biarkan server melakukan parse angka/string.
    payload = {
        "publicKey": public_key,
        "action": action.lower(),
        "mint": mint,
        "amount": amount if isinstance(amount, str) else float(amount),
        "denominatedInSol": _bool_as_str(is_buy if not (isinstance(amount, str) and amount.endswith("%")) else False),
        "slippage": int(slippage),
        "priorityFee": float(priority_fee),
        "pool": pool,
    }

    # Coba JSON terlebih dahulu; jika server kaku, fallback ke form-encoded.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(PUMPPORTAL_TRADE_LOCAL, json=payload)
            if r.status_code != 200:
                # Fallback: form data seperti contoh Python di docs
                r = await client.post(PUMPPORTAL_TRADE_LOCAL, data=payload)
            r.raise_for_status()
            # Penting: response adalah BYTES serialized VersionedTransaction.
            # Kita kembalikan BASE64 agar caller konsisten dengan Jupiter.
            return base64.b64encode(r.content).decode()
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun Local HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun Local ReqError] {getattr(e, 'request', None) and e.request.url!r}")
        return None
