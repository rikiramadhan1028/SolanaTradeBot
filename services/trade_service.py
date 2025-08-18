# file: services/trade_service.py
import os
import httpx
from typing import Any, Dict, Optional

# ---- Base URL normalizer ----
_raw = os.getenv("TRADE_SVC_URL", "http://localhost:8080").strip().rstrip("/")
if _raw and not _raw.startswith(("http://", "https://")):
    _raw = "https://" + _raw
TRADE_SVC_URL = _raw

# Shared auth (jangan kosongin di prod)
TRADE_SVC_TOKEN = os.getenv("TRADE_SVC_TOKEN", "").strip()

# Default pool utk PumpPortal. Bisa override via env:
#   PUMPFUN_POOL=auto|pump|raydium|raydium-cpmm|pump-amm|launchlab|bonk
PUMPFUN_DEFAULT_POOL = os.getenv("PUMPFUN_POOL", "auto").strip() or "auto"

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": "solana-tradebot/1.0",
    "Accept": "application/json",
}
if TRADE_SVC_TOKEN:
    DEFAULT_HEADERS["X-Auth-Token"] = TRADE_SVC_TOKEN

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, connect=5.0, read=15.0),
    limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
    headers=DEFAULT_HEADERS,
)

# ---- Core request helper (GET/POST) dengan retry ringan ----
async def _request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    retries: int = 2,
) -> Dict[str, Any]:
    global _client  # <-- PENTING: deklarasi global sebelum _client dipakai
    url = f"{TRADE_SVC_URL}{path}"
    attempt = 0
    while True:
        try:
            r = await _client.request(method.upper(), url, params=params, json=json)
            # retry untuk 429/5xx
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                try:
                    await _client.aclose()
                except Exception:
                    pass
                _client = httpx.AsyncClient(  # rebuild pool
                    timeout=httpx.Timeout(20.0, connect=5.0, read=15.0),
                    limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
                    headers=DEFAULT_HEADERS,
                )
                continue

            if r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
            else:
                data = {"message": r.text}

            if r.status_code == 200:
                return data if isinstance(data, dict) else {"data": data}
            return {
                "error": data if isinstance(data, dict) else {"message": str(data)},
                "status": r.status_code,
            }
        except httpx.ConnectError as e:
            return {"error": f"connect_error to server: {e}"}
        except httpx.ReadTimeout:
            return {"error": f"timeout calling server"}
        except Exception as e:
            return {"error": f"unexpected error calling server: {e}"}

# ---- Public helpers ----

async def derive_address(private_key: str) -> str:
    # NB: jangan log private_key
    r = await _request("POST", "/derive-address", json={"privateKey": private_key})
    if isinstance(r, dict) and "address" in r:
        return str(r["address"])
    raise RuntimeError(f"derive_address failed: {r}")

def _bps_to_pct(bps: Optional[int]) -> int:
    try:
        return max(0, min(100, int(round((bps or 0) / 100))))
    except Exception:
        return 10

async def pumpfun_swap(
    private_key: str,
    action: str,
    mint: str,
    amount,
    *,
    denominated_in_sol: Optional[bool] = None,   # True kalau amount SOL; default: buy=True, sell=False
    slippage_bps: Optional[int] = None,          # dari UI (bps) → dikonversi ke %
    pool: Optional[str] = None,                  # default: env PUMPFUN_POOL
    # kompat lama:
    use_jito: bool = False,
    slippage: Optional[int] = None,              # persen (legacy)
    priority_fee: float = 0.00005,               # SOL
) -> Dict[str, Any]:
    """
    Payload disesuaikan dengan dokumen resmi PumpPortal /api/trade-local:
      publicKey, action, mint, amount, denominatedInSol, slippage(%), priorityFee, pool
    Node trade-svc kamu yang akan memanggil PumpPortal, menandatangani, dan mengirim tx.
    """
    act = (action or "").lower().strip()
    if denominated_in_sol is None:
        denominated_in_sol = (act == "buy")

    # slippage: prioritaskan bps -> %, fallback ke slippage (persen)
    if slippage_bps is not None:
        slip_pct = _bps_to_pct(slippage_bps)
    else:
        slip_pct = max(0, min(100, int(slippage or 10)))

    # SELL dengan angka → kirim "X%" spt dokumen
    send_amount = amount
    if not denominated_in_sol and isinstance(amount, (int, float)):
        send_amount = f"{amount}%"

    try:
        public_key = await derive_address(private_key)
    except Exception:
        public_key = ""

    payload = {
        # untuk Node signer
        "privateKey": private_key,
        "useJito": bool(use_jito),

        # untuk PumpPortal trade-local
        "publicKey": public_key,
        "action": act,
        "mint": mint,
        "amount": send_amount,
        "denominatedInSol": "true" if denominated_in_sol else "false",
        "slippage": slip_pct,                 # persen
        "priorityFee": float(priority_fee),   # SOL
        "pool": (pool or PUMPFUN_DEFAULT_POOL),
    }

    return await _request("POST", "/pumpfun/swap", json=payload)

async def dex_swap(
    private_key: str,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    dex: str = "jupiter",
    slippage_bps: int = 50,
    priority_fee_sol: float = 0.0,
) -> Dict[str, Any]:
    payload = {
        "privateKey": private_key,
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amountLamports": int(amount_lamports),
        "dex": dex,
        "slippageBps": int(slippage_bps),
        "priorityFee": float(priority_fee_sol),
    }
    return await _request("POST", "/dex/swap", json=payload)

# ---- Wallet/Meta helpers (GET) ----

async def svc_get_sol_balance(address: str) -> float:
    r = await _request("GET", f"/wallet/{address}/balance")
    try:
        return float(r.get("sol", 0.0))
    except Exception:
        return 0.0

async def svc_get_token_balances(address: str, min_amount: float = 0.0):
    params = {"min": str(min_amount)} if min_amount > 0 else None
    r = await _request("GET", f"/wallet/{address}/tokens", params=params)
    return r.get("tokens", []) if isinstance(r, dict) else []

async def svc_get_token_balance(address: str, mint: str) -> float:
    r = await _request("GET", f"/wallet/{address}/token/{mint}/balance")
    try:
        return float(r.get("amount", 0.0))
    except Exception:
        return 0.0

async def svc_get_mint_decimals(mint: str) -> int:
    r = await _request("GET", f"/wallet/mint/{mint}/decimals")
    try:
        return int(r.get("decimals", 6))
    except Exception:
        return 6
