# file: dex_integrations/jupiter_aggregator.py
import httpx
import json
from .metis_jupiter import (
    get_quote as get_swap_route,
    build_swap_tx as _build_swap_tx,
)

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

async def get_swap_transaction(route_or_quote, user_public_key: str):
    # why: enforce full quote body for /swap
    if not isinstance(route_or_quote, dict) or not route_or_quote.get("inputMint"):
        raise ValueError("get_swap_transaction requires the full quote object returned by get_swap_route()")
    return await _build_swap_tx(route_or_quote, user_public_key, wrap_and_unwrap_sol=True)

