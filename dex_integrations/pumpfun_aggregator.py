import httpx
import json
import base64
import config

PUMPPAL_TRADE_API_URL = "https://pumpportal.fun/api/trade-local"

async def get_pumpfun_swap_transaction(public_key: str, action: str, mint: str, amount: float):
    """
    Mengambil transaksi yang sudah disiapkan dari API Pumpfun untuk ditandatangani secara lokal.
    action: 'buy' atau 'sell'
    mint: alamat mint token yang ingin diperdagangkan
    amount: jumlah SOL atau token untuk diperdagangkan
    """
    headers = {
        "Authorization": f"Bearer {config.PUMPPAL_API_KEY}"
    }

    payload = {
        "publicKey": public_key,
        "action": action,
        "mint": mint,
        "amount": str(amount),
        "denominatedInSol": "true" if action == "buy" else "false",
        "slippage": "10",
        "priorityFee": "1",
        "pool": "pump"
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(PUMPPAL_TRADE_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            response_data = response.json()
            return response_data.get("transaction")
    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}.")
        return None