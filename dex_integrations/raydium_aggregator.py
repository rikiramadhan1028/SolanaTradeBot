# file: dex_integrations/raydium_aggregator.py
import httpx
import json
import base64

# Catatan: endpoint /v2/amm/pools bukan quote murni; biarkan fail-fast bila tak sesuai
RAYDIUM_QUOTE_API_URL = "https://api.raydium.io/v2/amm/pools"
RAYDIUM_SWAP_API_URL = "https://api.raydium.io/v2/transaction/swap"


async def get_swap_quote(input_mint: str, output_mint: str, amount: int):
    """
    DUMMY quote handler agar caller bisa fail-fast jika respons bukan quote yang diharapkan.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippage": 0.5,  # 0.5%
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(RAYDIUM_QUOTE_API_URL, params=params)
            r.raise_for_status()
            data = r.json()
            # Tidak ada format quote resmi di endpoint ini; kembalikan None agar caller fallback.
            return None
    except httpx.HTTPStatusError as e:
        print(f"[Raydium HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Raydium ReqError] {e.request.url!r}")
        return None


async def get_swap_transaction(quote: dict, user_public_key: str):
    """
    DUMMY swap tx; mempertahankan API supaya tidak memecahkan import caller.
    """
    payload = {
        "owner": user_public_key,
        "inputMint": quote.get("inputMint") if quote else None,
        "outputMint": quote.get("outputMint") if quote else None,
        "amount": quote.get("amount") if quote else None,
        "slippage": 0.5,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(RAYDIUM_SWAP_API_URL, json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("transaction")
    except httpx.HTTPStatusError as e:
        print(f"[Raydium HTTP] {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"[Raydium ReqError] {e.request.url!r}")
        return None
