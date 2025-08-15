# file: dex_integrations/jupiter_aggregator.py
import httpx
import json

QUOTE_API_URL = "https://quote-api.jup.ag/v6/quote"
SWAP_API_URL = "https://lite-api.jup.ag/swap/v1/swap"


async def get_swap_route(input_mint: str, output_mint: str, amount: int):
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            QUOTE_API_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "onlyDirectRoutes": False,  # rute terbaik
                "slippageBps": 50,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        routes = (data.get("data") or data.get("routePlan") or [])
        if not routes:
            return None
        return routes[0]


async def get_swap_transaction(route, user_public_key: str):
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            SWAP_API_URL,
            json={
                "quoteResponse": route,
                "userPublicKey": user_public_key,
                # API Jupiter umum: wrap & unwrap SOL
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": "auto",
            },
        )
        resp.raise_for_status()
        return resp.json().get("swapTransaction")
