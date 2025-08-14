# dex_integrations/raydium_aggregator.py
import httpx
import json
import base64

RAYDIUM_SWAP_API_URL = "https://api-v3.raydium.io/swap/transaction/swap-base-in"

async def get_swap_route(input_mint: str, output_mint: str, amount: int):
    # Logika untuk mendapatkan rute swap dari API Raydium
    return {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
    }

async def get_swap_transaction(route: dict, user_public_key: str):
    """
    Mendapatkan transaksi swap dari API Raydium.
    """
    payload = {
        "computeUnitPriceMicroLamports": "auto",
        "txVersion": "V0",
        "wrapSol": True,
        "wallet": user_public_key,
        "inputMint": route.get("inputMint"),
        "outputMint": route.get("outputMint"),
        "amount": route.get("amount"),
        "slippageBps": 50,
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