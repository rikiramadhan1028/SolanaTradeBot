# dex_integrations/raydium_aggregator.py
import httpx
import json
import base64

RAYDIUM_QUOTE_API_URL = "https://api-v3.raydium.io/swap/quote" 
RAYDIUM_SWAP_API_URL = "https://api-v3.raydium.io/swap/transaction"

async def get_swap_quote(input_mint: str, output_mint: str, amount: int):
    """
    Mendapatkan kuotasi swap dari API Raydium.
    """
    payload = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": 50,
        "mode": "ExactIn"
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(RAYDIUM_QUOTE_API_URL, json=payload)
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
    payload = {
        "quote": quote,
        "wallet": user_public_key,
        "computeUnitPriceMicroLamports": "auto",
        "txVersion": "V0",
        "wrapSol": True,
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