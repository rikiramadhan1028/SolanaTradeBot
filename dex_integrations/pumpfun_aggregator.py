# file: dex_integrations/pumpfun_aggregator.py
import httpx
import json
import base64
import config

PUMPPORTAL_TRADE_API_URL = "https://pumpportal.fun/api/trade-local"


async def get_pumpfun_swap_transaction(
    public_key: str, action: str, mint: str, amount: float
):
    """
    Ambil transaksi siap-tanda-tangan dari Pumpfun (local sign).
    action: 'buy' | 'sell'
    mint: alamat mint token
    amount: jumlah (SOL untuk buy, token untuk sell)
    """
    headers = {
        # BUG diperbaiki: gunakan PUMPPORTAL_API_KEY
        "Authorization": f"Bearer {getattr(config, 'PUMPPORTAL_API_KEY', '')}",
    }
    payload = {
        "publicKey": public_key,
        "action": action,
        "mint": mint,
        # kirim angka agar backend bebas caste
        "amount": float(amount),
        "denominatedInSol": True if action.lower() == "buy" else False,
        "slippage": "10",
        "priorityFee": "1",
        "pool": "pump",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                PUMPPORTAL_TRADE_API_URL, json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("transaction")
    except httpx.HTTPStatusError as e:
        print(f"[Pumpfun HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Pumpfun ReqError] {e.request.url!r}")
        return None
