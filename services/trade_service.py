# file: services/trade_service.py
import os
import httpx

raw = os.getenv("TRADE_SVC_URL", "http://localhost:8080").strip().rstrip("/")
if raw and not raw.startswith(("http://", "https://")):
    # default to https in cloud environments
    raw = "https://" + raw
TRADE_SVC_URL = raw

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, connect=5.0, read=15.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    headers={"User-Agent": "solana-tradebot/1.0"},
)

async def _post(path: str, payload: dict):
    url = f"{TRADE_SVC_URL}{path}"
    try:
        r = await _client.post(url, json=payload)
        if r.status_code == 200:
            return r.json()
        try:
            return {"error": r.json()}
        except Exception:
            return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
    except httpx.ConnectError as e:
        return {"error": f"connect_error to {url}: {e}"}
    except httpx.ReadTimeout:
        return {"error": f"timeout calling {url}"}
    except Exception as e:
        return {"error": f"unexpected error calling {url}: {e}"}

async def derive_address(private_key: str) -> str:
    r = await _post("/derive-address", {"privateKey": private_key})
    if isinstance(r, dict) and "address" in r:
        return r["address"]
    raise RuntimeError(f"derive_address failed: {r}")

async def pumpfun_swap(private_key: str, action: str, mint: str, amount, use_jito: bool=False, slippage: int=10, priority_fee: float=0.00005):
    payload = {"privateKey": private_key, "action": action, "mint": mint, "amount": amount,
               "useJito": use_jito, "slippage": slippage, "priorityFee": priority_fee}
    return await _post("/pumpfun/swap", payload)

async def dex_swap(private_key: str, input_mint: str, output_mint: str, amount_lamports: int, dex: str="jupiter", slippage_bps: int=50, priority_fee_sol: float=0.0):
    payload = {"privateKey": private_key, "inputMint": input_mint, "outputMint": output_mint,
               "amountLamports": int(amount_lamports), "dex": dex, "slippageBps": slippage_bps,
               "priorityFee": priority_fee_sol}
    return await _post("/dex/swap", payload)
