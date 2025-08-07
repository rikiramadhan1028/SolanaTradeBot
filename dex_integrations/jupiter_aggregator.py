# dex_integrations/jupiter_aggregator.py
import httpx
import json

QUOTE_API_URL = "https://lite-api.jup.ag/swap/v1quote"
SWAP_API_URL = "https://lite-api.jup.ag/swap/v1/swap"

async def get_swap_route(input_mint: str, output_mint: str, amount: int):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            QUOTE_API_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "onlyDirectRoutes": True,
                "slippageBps": 50
            }
        )
        response.raise_for_status()
        routes = response.json().get("data")
        if not routes:
            return None
        return routes[0]

async def get_swap_transaction(route, user_public_key: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            SWAP_API_URL,
            json={
                "quoteResponse": route,
                "userPublicKey": user_public_key,
                "wrapUnwrapSOL": True,
                "prioritizationFeeLamports": "auto"
            }
        )
        response.raise_for_status()
        return response.json().get("swapTransaction")