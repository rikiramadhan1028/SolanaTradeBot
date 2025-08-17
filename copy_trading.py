# file: copy_trading.py
import os
import asyncio
import time
from typing import Dict, Any, List, Optional, Tuple

import httpx

import database
from services.trade_service import dex_swap, svc_get_sol_balance, svc_get_mint_decimals, svc_get_token_balance

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
HELIUS_RPC = os.getenv("HELIUS_RPC", "").strip()  # optional: custom helius rpc
HELIUS_REST = f"https://api.helius.xyz/v0/addresses"  # enhanced tx endpoint
# Poll interval per leader (seconds)
COPY_POLL_INTERVAL = float(os.getenv("COPY_POLL_INTERVAL", "4.0"))

SOL_MINT = "So11111111111111111111111111111111111111112"

# ----------------- Utils -----------------
def _now() -> int:
    return int(time.time())

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

# ----------------- Helius fetch -----------------
async def _fetch_leader_txs(leader: str, before_sig: Optional[str]=None, limit: int=10) -> List[Dict[str, Any]]:
    """
    Ambil enhanced tx untuk address leader.
    API: POST https://api.helius.xyz/v0/addresses/<leader>/transactions?api-key=...
    Body: { "before": <signature>, "limit": 10 }
    Returns: list of tx dict (newest first).
    """
    if not HELIUS_API_KEY:
        return []
    url = f"{HELIUS_REST}/{leader}/transactions?api-key={HELIUS_API_KEY}"
    payload = {"limit": limit}
    if before_sig:
        payload["before"] = before_sig
    try:
        async with httpx.AsyncClient(timeout=15.0) as s:
            r = await s.post(url, json=payload)
            if r.status_code == 200:
                arr = r.json() or []
                # newest first:
                return arr
    except Exception:
        pass
    return []

def _parse_swap_from_enhanced_tx(tx: Dict[str, Any], leader: str) -> Optional[Dict[str, Any]]:
    """
    Coba ekstrak event SWAP sederhana:
    - Jika ada 'type' = "SWAP" pada 'events' atau
    - Derive dari tokenTransfers + nativeTransfers (SOL)
    Output:
      {
        "side": "buy"|"sell",
        "mint": "<token mint>",
        "ui_sol_spent": float,   # utk BUY (SOL -> token)
        "ui_token_sold": float,  # utk SELL (token -> SOL)
      }
    NOTE: ini simplified; Helius 'events' biasanya sudah tandai swap & mints.
    """
    # Prefer events
    evt = (tx.get("events") or {})
    swaps = evt.get("swap") or evt.get("swaps")  # Helius format bervariasi
    if swaps:
        s = swaps[0] if isinstance(swaps, list) else swaps
        # Helius swap event biasanya ada fields: sourceMint, destinationMint, nativeInput, nativeOutput, tokenAmountIn/Out
        src_mint = s.get("sourceMint")
        dst_mint = s.get("destinationMint")
        # Detect side by source mint
        if src_mint == SOL_MINT and dst_mint and dst_mint != SOL_MINT:
            # BUY
            ui_sol = float(s.get("nativeInput", 0)) / 1e9
            return {"side": "buy", "mint": dst_mint, "ui_sol_spent": max(0.0, ui_sol)}
        if dst_mint == SOL_MINT and src_mint and src_mint != SOL_MINT:
            # SELL
            # Try token amount in (UI) if available
            amt_token = 0.0
            try:
                amt_token = float(s.get("tokenAmountIn", 0))
            except Exception:
                amt_token = 0.0
            return {"side": "sell", "mint": src_mint, "ui_token_sold": max(0.0, amt_token)}

    # Fallback via tokenTransfers + nativeTransfers
    tts = tx.get("tokenTransfers") or []
    nats = tx.get("nativeTransfers") or []
    # Check SOL spent by leader
    sol_spent_ui = 0.0
    for n in nats:
        if n.get("fromUserAccount") == leader:
            lam = int(n.get("amount", 0))
            sol_spent_ui += lam / 1e9

    # If leader spent SOL and received token
    if sol_spent_ui > 0 and tts:
        # pick biggest token in to leader
        recv = [t for t in tts if t.get("toUserAccount") == leader]
        if recv:
            # choose highest ui
            best = max(recv, key=lambda x: float(x.get("tokenAmount", 0)))
            mint = best.get("mint")
            return {"side": "buy", "mint": mint, "ui_sol_spent": sol_spent_ui}

    # If leader received SOL and sent token => SELL
    sol_recv_ui = 0.0
    for n in nats:
        if n.get("toUserAccount") == leader:
            lam = int(n.get("amount", 0))
            sol_recv_ui += lam / 1e9
    if sol_recv_ui > 0 and tts:
        sent = [t for t in tts if t.get("fromUserAccount") == leader]
        if sent:
            best = max(sent, key=lambda x: float(x.get("tokenAmount", 0)))
            mint = best.get("mint")
            amt_token = float(best.get("tokenAmount", 0))
            return {"side": "sell", "mint": mint, "ui_token_sold": amt_token}

    return None

# ----------------- Core executor -----------------
async def _exec_for_followers(leader_addr: str, event: Dict[str, Any]) -> None:
    followers = database.copy_follow_list_for_leader(leader_addr)
    if not followers:
        return

    for f in followers:
        if not f.get("active"):
            continue
        user_id = f["user_id"]
        cfg_ratio = float(f.get("ratio", 1.0))  # 1.0 = 100%
        max_sol   = float(f.get("max_sol_per_trade", 0.5))
        slip_bps  = int(f.get("slippage_bps") or (500 if event["side"] == "buy" else 500))
        follow_buys  = bool(f.get("follow_buys", True))
        follow_sells = bool(f.get("follow_sells", True))

        w = database.get_user_wallet(user_id)
        if not w or not w.get("address") or not w.get("has_secret"):
            continue

        try:
            # decrypt secret (v1 app-key). Jika pk terenkripsi passphrase (v2), get_private_key_decrypted(None) akan None.
            priv = database.get_private_key_decrypted(user_id, None)
            if not priv:
                # skip (user perlu mengaktifkan v1 atau menyediakan passphrase di sistem otomatis — sengaja tidak disimpan)
                continue
        except Exception:
            continue

        try:
            if event["side"] == "buy":
                if not follow_buys:
                    continue
                # spend: leader SOL * ratio, capped by max_sol
                want_ui = _clamp(float(event["ui_sol_spent"]) * cfg_ratio, 0.0, max_sol)
                if want_ui <= 0.0:
                    continue
                # cek saldo sol agar tidak gagal
                try:
                    bal_ui = await svc_get_sol_balance(w["address"])
                    if bal_ui < (want_ui + 0.002):
                        continue
                except Exception:
                    continue

                amount_lamports = int(want_ui * 1e9)
                await dex_swap(
                    private_key=priv,
                    input_mint=SOL_MINT,
                    output_mint=event["mint"],
                    amount_lamports=amount_lamports,
                    dex="jupiter",
                    slippage_bps=slip_bps,
                    priority_fee_sol=0.0,
                )

            else:  # SELL
                if not follow_sells:
                    continue
                # jual proporsional: jika tx leader punya 'ui_token_sold', pakai ratio
                token_mint = event["mint"]
                try:
                    decimals = int(await svc_get_mint_decimals(token_mint))
                except Exception:
                    decimals = 6

                # balance follower
                try:
                    bal_ui = float(await svc_get_token_balance(w["address"], token_mint))
                except Exception:
                    bal_ui = 0.0
                if bal_ui <= 0:
                    continue

                base_sell_ui = float(event.get("ui_token_sold", 0.0))
                if base_sell_ui > 0:
                    want_ui = _clamp(base_sell_ui * cfg_ratio, 0.0, bal_ui)
                else:
                    # jika tak tahu jumlah token sold leader, fallback: jual 25% * ratio (mis. 25% * 1.0)
                    want_ui = _clamp(0.25 * bal_ui * cfg_ratio, 0.0, bal_ui)

                if want_ui <= 0:
                    continue

                amount_lamports = int(want_ui * (10 ** decimals))
                await dex_swap(
                    private_key=priv,
                    input_mint=token_mint,
                    output_mint=SOL_MINT,
                    amount_lamports=amount_lamports,
                    dex="jupiter",
                    slippage_bps=slip_bps,
                    priority_fee_sol=0.0,
                )

        except Exception as e:
            # jangan crash loop gara-gara satu follower
            print(f"[copy] follower exec error (user {user_id}): {e}")

# ----------------- Public: background loop -----------------
async def copytrading_loop(stop_event: asyncio.Event):
    """
    Loop global:
      - Ambil daftar leader aktif dari DB
      - Untuk masing2 leader, poll Helius untuk tx terbaru sejak last_sig
      - Parse swap -> eksekusi untuk followers
      - Simpan last_sig untuk leader
    """
    if not HELIUS_API_KEY:
        print("[copy] HELIUS_API_KEY missing: copy trading disabled.")
        return

    # cache last seen sig per leader
    last_sig: Dict[str, str] = {}

    while not stop_event.is_set():
        try:
            leaders = database.copy_leaders_active()
            for leader in leaders:
                addr = leader["leader_address"]
                before = None
                # panggil enhanced tx; newest first
                txs = await _fetch_leader_txs(addr, before_sig=None, limit=10)
                if not txs:
                    continue

                # proses dari lama -> baru untuk menjaga urutan
                txs_reversed = list(reversed(txs))
                for tx in txs_reversed:
                    sig = tx.get("signature") or tx.get("transaction", {}).get("signatures", [None])[0]
                    if not sig:
                        continue
                    if last_sig.get(addr) == sig:
                        # sudah sampai ke tx yang pernah diproses
                        continue
                    evt = _parse_swap_from_enhanced_tx(tx, addr)
                    if evt:
                        await _exec_for_followers(addr, evt)
                    # update last sig
                    last_sig[addr] = sig

        except Exception as e:
            print(f"[copy] loop error: {e}")

        await asyncio.sleep(COPY_POLL_INTERVAL)
