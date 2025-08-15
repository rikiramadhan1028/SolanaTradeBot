import os
import httpx

TRADE_SVC_URL = os.getenv("TRADE_SVC_URL", "http://localhost:8080")

async def derive_address(private_key: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{TRADE_SVC_URL}/derive-address", json={"privateKey": private_key})
        r.raise_for_status()
        return r.json()["address"]

async def pumpfun_swap(private_key: str, action: str, mint: str, amount, use_jito: bool=False, slippage: int=10, priority_fee: float=0.00005):
    payload = {
        "privateKey": private_key,
        "action": action,
        "mint": mint,
        "amount": amount,
        "useJito": use_jito,
        "slippage": slippage,
        "priorityFee": priority_fee
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{TRADE_SVC_URL}/pumpfun/swap", json=payload)
        if r.status_code == 200:
            return r.json()
        try:
            return {"error": r.json()}
        except Exception:
            return {"error": r.text}

async def dex_swap(private_key: str, input_mint: str, output_mint: str, amount_lamports: int, dex: str="jupiter", slippage_bps: int=50, priority_fee_sol: float=0.0):
    payload = {
        "privateKey": private_key,
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amountLamports": int(amount_lamports),
        "dex": dex,
        "slippageBps": slippage_bps,
        "priorityFee": priority_fee_sol
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{TRADE_SVC_URL}/dex/swap", json=payload)
        if r.status_code == 200:
            return r.json()
        try:
            return {"error": r.json()}
        except Exception:
            return {"error": r.text}
