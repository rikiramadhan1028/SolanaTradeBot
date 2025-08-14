# dex_integrations/raydium_aggregator.py
import httpx
import json
import base64

RAYDIUM_QUOTE_API_URL = "https://api.raydium.io/v2/amm/pools" 
RAYDIUM_SWAP_API_URL = "https://api.raydium.io/v2/transaction/swap"

async def get_swap_quote(input_mint: str, output_mint: str, amount: int):
    """
    Mendapatkan kuotasi swap dari API Raydium.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippage": 0.5, # 0.5% slippage
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(RAYDIUM_QUOTE_API_URL, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred on Raydium: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"An error occurred while requesting Raydium API: {e.request.url!r}.")
        return None

async def get_swap_transaction(quote: dict, user_public_key: str):
    """
    Mendapatkan transaksi swap dari API Raydium.
    """
    # API Raydium V2 tidak membutuhkan quote dalam payload swap transaction
    # Sebaliknya, ia membutuhkan input yang sama dengan quote endpoint
    # ditambah public key pengguna
    payload = {
        "owner": user_public_key,
        "inputMint": quote.get('inputMint'),
        "outputMint": quote.get('outputMint'),
        "amount": quote.get('amount'),
        "slippage": 0.5
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(RAYDIUM_SWAP_API_URL, json=payload)
            response.raise_for_status()
            response_data = response.json()
            return response_data.get("transaction")
    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred on Raydium: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"An error occurred while requesting Raydium API: {e.request.url!r}.")
        return None