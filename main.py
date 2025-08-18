# -*- coding: utf-8 -*-
# main.py — RokuTrade Bot (secure + realtime SOL price)
import os
import json
import re
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional
from copy_trading import copytrading_loop

# -------- env must be loaded BEFORE os.getenv is called --------
from dotenv import load_dotenv
load_dotenv()

# -------- ENV --------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRADE_SVC_URL      = os.getenv("TRADE_SVC_URL", "http://localhost:8080").rstrip("/")

# Optional platform fee (default OFF)
# FEE_BPS: basis points, 100 = 1%
FEE_BPS     = int(os.getenv("FEE_BPS", "0"))
FEE_WALLET  = (os.getenv("FEE_WALLET") or "").strip()
FEE_ENABLED = FEE_BPS > 0 and len(FEE_WALLET) >= 32

import config
import database
import wallet_manager
from blockchain_clients.solana_client import SolanaClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

# === Fast refresh infra (HTTP client + caches) ===
import time
from collections import defaultdict

_HTTPX = httpx.AsyncClient(http2=True,
                           timeout=httpx.Timeout(10.0, connect=3.0, read=8.0),
                           limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
                           headers={"User-Agent": "rokutrade/fast-refresh"})

class MetaCache:
    """Cache symbol/name per mint (TTL 1 hari)."""
    TTL = 24 * 3600
    _store: dict[str, tuple[float, dict]] = {}

    @classmethod
    async def get(cls, mint: str) -> dict:
        now = time.time()
        hit = cls._store.get(mint)
        if hit and (now - hit[0] < cls.TTL):
            return hit[1]
        try:
            r = await _HTTPX.get(f"{TRADE_SVC_URL}/meta/token/{mint}")
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        cls._store[mint] = (now, data or {})
        return data or {}

class DexCache:
    """
    Cache harga/LP/MC per mint (TTL 3s) + background warmer.
    Gunakan get_bulk() saat render; warmer akan menyegarkan berkala supaya
    Refresh terasa instant.
    """
    TTL = 3.0
    _store: dict[str, tuple[float, dict]] = {}
    _watch: set[str] = set()

    @classmethod
    async def _fetch_bulk(cls, mints: list[str]) -> dict[str, dict]:
        if not mints:
            return {}
        # de-dupe & batasi panjang URL (pecah per 50 mint)
        uniq = list(dict.fromkeys([m for m in mints if m]))
        out: dict[str, dict] = {}
        for i in range(0, len(uniq), 50):
            chunk = uniq[i:i+50]
            url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk)
            try:
                r = await _HTTPX.get(url)
                data = r.json() if r.status_code == 200 else {}
                pairs = data.get("pairs") or []
                # pilih pair dengan LP terbesar per baseToken.address
                best: dict[str, dict] = {}
                for p in pairs:
                    base = (p.get("baseToken") or {}).get("address")
                    if not base:
                        continue
                    lp = float((p.get("liquidity") or {}).get("usd") or 0)
                    cur_lp = float((best.get(base, {}).get("liquidity") or {}).get("usd") or 0)
                    if lp >= cur_lp:
                        best[base] = p
                for mint, p in best.items():
                    out[mint] = {
                        "price": float(p.get("priceUsd") or 0.0) or 0.0,
                        "lp": float((p.get("liquidity") or {}).get("usd") or 0.0),
                        "mc": float(p.get("fdv") or p.get("marketCap") or 0.0),
                    }
            except Exception:
                continue
        now = time.time()
        for m, pack in out.items():
            DexCache._store[m] = (now, pack)
        return out

    @classmethod
    async def get_bulk(cls, mints: list[str], *, prefer_cache: bool = True) -> dict[str, dict]:
        """Kembalikan dict mint->pack (price/lp/mc). Ambil cache < TTL, sisanya fetch sekali (batch)."""
        now = time.time()
        out: dict[str, dict] = {}
        missing: list[str] = []
        for m in mints:
            cls._watch.add(m)  # daftarkan untuk warmer
            hit = cls._store.get(m)
            if prefer_cache and hit and (now - hit[0] < cls.TTL):
                out[m] = hit[1]
            else:
                missing.append(m)
        if missing:
            fresh = await cls._fetch_bulk(missing)
            out.update({m: fresh.get(m, {"price": 0.0, "lp": 0.0, "mc": 0.0}) for m in missing})
        return out

    @classmethod
    async def loop(cls, stop_event: asyncio.Event):
        """Warm cache setiap 2s untuk semua mint yang pernah dirender."""
        while not stop_event.is_set():
            try:
                if cls._watch:
                    await cls._fetch_bulk(list(cls._watch))
            except Exception:
                pass
            await asyncio.sleep(2.0)

# ============ DEX & Pump.fun via Node microservice ============
from services.trade_service import (
    dex_swap,
    pumpfun_swap,
    svc_get_mint_decimals,
    svc_get_sol_balance,
    svc_get_token_balance,
    svc_get_token_balances,
)

# ============ Price aggregator ============
from dex_integrations.price_aggregator import (
    get_token_price,
    get_token_price_from_raydium,
    get_token_price_from_pumpfun,
)

# ================== Init ==================
SOLANA_NATIVE_TOKEN_MINT = "So11111111111111111111111111111111111111112"
solana_client = SolanaClient(config.SOLANA_RPC_URL)

# Conversation states
(
    AWAITING_TOKEN_ADDRESS,
    AWAITING_TRADE_ACTION,
    AWAITING_AMOUNT,
    PUMPFUN_AWAITING_TOKEN,
    SET_SLIPPAGE,
    PUMPFUN_AWAITING_ACTION,
    PUMPFUN_AWAITING_BUY_AMOUNT,
    PUMPFUN_AWAITING_SELL_PERCENTAGE,
) = range(8)

(COPY_AWAIT_LEADER, COPY_AWAIT_RATIO, COPY_AWAIT_MAX) = range(8, 11)

# ================== UI Helpers ==================
def back_markup(prev_cb: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    if prev_cb:
        rows.append(InlineKeyboardButton("⬅️ Back", callback_data=prev_cb))
    rows.append(InlineKeyboardButton("🏠 Menu", callback_data="back_to_main_menu"))
    return InlineKeyboardMarkup([rows])

def solscan_tx(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"

def short_err_text(err: str) -> str:
    s = (err or "").strip()
    low = s.lower()
    if "balance_low" in low or ("insufficient" in low and "sol" in low):
        return "Insufficient SOL for amount + fees."
    if "token_balance_low" in low or "insufficient balance for token" in low:
        return "Insufficient token balance to sell."
    if "simulation_failed" in low:
        return "Simulation failed (route/slippage). Try smaller amount."
    if "rate" in low and "limit" in low:
        return "Rate limited. Please retry shortly."
    if "network_error" in low:
        return "Network error to aggregator."
    if "no route" in low or ("quote" in low and ("404" in low or "400" in low)):
        return "No route found."
    if s.startswith(("http", "HTTP")):
        return "Aggregator error."
    return (s[:200] + "…") if len(s) > 200 else s

async def reply_ok_html(message, text: str, prev_cb: str | None = None, signature: str | None = None):
    extra = ""
    if signature:
        extra = f'\n🔗 <a href="{solscan_tx(signature)}">Solscan</a>\n<code>{signature}</code>'
    await message.reply_html(text + extra, reply_markup=back_markup(prev_cb))

async def reply_err_html(message, text: str, prev_cb: str | None = None):
    await message.reply_html(text, reply_markup=back_markup(prev_cb))

def _is_valid_pubkey(addr: str) -> bool:
    if not addr or not isinstance(addr, str) or not (32 <= len(addr) <= 44):
        return False
    try:
        import base58
        base58.b58decode(addr)
        return True
    except Exception:
        return False
    
class PrivateKeyFilter(filters.MessageFilter):

    def filter(self, message: Message) -> bool:
        try:
            wallet_manager.validate_and_clean_private_key(message.text or "")
            return True
        except ValueError:
            return False

class PubkeyFilter(filters.MessageFilter):
    def filter(self, message: Message) -> bool:
        return _is_valid_pubkey((message.text or "").strip())
    
def format_usd(v: float | str) -> str:
    try:
        f = float(v)
        if f == 0.0:   return "$0"
        if f < 0.01:   return f"${f:.6f}"
        if f < 1:      return f"${f:.4f}"
        if f < 1000:   return f"${f:.2f}"
        for s, m in [("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)]:
            if f >= m:
                return f"${f/m:.2f}{s}"
    except Exception:
        pass
    return "N/A"

def percent_label(bps: int | None, default_pct: float = 5.0) -> str:
    if not isinstance(bps, int) or bps <= 0:
        return f"{default_pct:.0f}%"
    return f"{bps/100:.0f}%"

def dexscreener_url(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"

# ===== Assets view config =====
ASSETS_PAGE_SIZE = 3         # small page biar pesan gak kepanjangan
DEFAULT_DUST_USD = 0.0

def format_pct(x: float | None) -> str:
    try:
        if x is None: return "—"
        return f"{x*100:.2f}%"
    except Exception:
        return "—"

def _sol_from_usd(usd: float, sol_price: float) -> float:
    try:
        if sol_price <= 0: return 0.0
        return float(usd) / float(sol_price)
    except Exception:
        return 0.0

# ================== Data Helpers (Dexscreener) ==================
async def get_dexscreener_stats(mint: str) -> dict:
    """Return {priceUsd, fdvUsd, liquidityUsd, name, symbol} or {}."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            pairs = (data or {}).get("pairs") or []
            if not pairs:
                return {}
            pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            p0 = pairs[0]
            base = p0.get("baseToken") or {}
            fdv = p0.get("fdv")
            if fdv is None:
                fdv = p0.get("marketCap")
            return {
                "priceUsd": p0.get("priceUsd"),
                "fdvUsd": fdv,
                "liquidityUsd": (p0.get("liquidity") or {}).get("usd"),
                "name": base.get("name"),
                "symbol": base.get("symbol"),
            }
    except Exception:
        return {}

async def _update_position_after_trade(
    *,
    user_id: int,
    mint: str,
    side: str,                   # "buy" | "sell"
    delta_tokens: float,         # +tokens saat buy, +tokens_sold saat sell
    delta_sol: float,            # SOL spent saat buy (positif), SOL received saat sell (positif)
    price_usd: float | None = None,
    mc_usd: float | None = None
):
    """Accumulate counters + weighted averages."""
    side = side.lower()
    if delta_tokens <= 0 and delta_sol <= 0:
        return

    pos = database.position_get(user_id, mint) or {
        "user_id": user_id,
        "mint": mint,
        "buy_count": 0,
        "sell_count": 0,
        "buy_sol": 0.0,
        "sell_sol": 0.0,
        "buy_tokens": 0.0,
        "sell_tokens": 0.0,
        "avg_entry_price_usd": None,
        "avg_entry_mc_usd": None,
    }

    if side == "buy":
        pos["buy_count"] = int(pos.get("buy_count", 0)) + 1
        pos["buy_sol"]   = float(pos.get("buy_sol", 0.0)) + float(delta_sol)
        old_tok          = float(pos.get("buy_tokens", 0.0))
        new_tok          = max(0.0, float(delta_tokens))
        pos["buy_tokens"] = old_tok + new_tok

        # Weighted avg by tokens
        if price_usd and new_tok > 0:
            old_avg = pos.get("avg_entry_price_usd")
            if isinstance(old_avg, (int, float)) and old_tok > 0:
                pos["avg_entry_price_usd"] = (old_avg * old_tok + float(price_usd) * new_tok) / (old_tok + new_tok)
            else:
                pos["avg_entry_price_usd"] = float(price_usd)

        if mc_usd and new_tok > 0:
            old_mc = pos.get("avg_entry_mc_usd")
            if isinstance(old_mc, (int, float)) and old_tok > 0:
                pos["avg_entry_mc_usd"] = (old_mc * old_tok + float(mc_usd) * new_tok) / (old_tok + new_tok)
            else:
                pos["avg_entry_mc_usd"] = float(mc_usd)

    else:  # sell
        pos["sell_count"]  = int(pos.get("sell_count", 0)) + 1
        pos["sell_sol"]    = float(pos.get("sell_sol", 0.0)) + float(delta_sol)
        pos["sell_tokens"] = float(pos.get("sell_tokens", 0.0)) + max(0.0, float(delta_tokens))

    database.position_upsert(pos)

# ---- Realtime SOL/USD price ----
_SOL_CACHE = {"ts": 0.0, "px": 0.0}

async def get_sol_price_usd() -> float:
    now = time.time()
    if now - _SOL_CACHE["ts"] < 2.0 and _SOL_CACHE["px"] > 0:
        return _SOL_CACHE["px"]
    price = 0.0
    try:
        p = await get_token_price(SOLANA_NATIVE_TOKEN_MINT)
        price = float((p or {}).get("price", 0) if isinstance(p, dict) else p or 0)
    except Exception:
        pass
    if price <= 0:
        try:
            ds = await DexCache.get_bulk([SOLANA_NATIVE_TOKEN_MINT])
            price = float(ds.get(SOLANA_NATIVE_TOKEN_MINT, {}).get("price") or 0.0)
        except Exception:
            price = 0.0
    _SOL_CACHE.update({"ts": now, "px": price})
    return price


# ================== Start menu, wallet, etc ==================
def clear_user_context(context: ContextTypes.DEFAULT_TYPE):
    if hasattr(context, "user_data"):
        context.user_data.clear()

def get_start_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("⚡ Import Wallet", callback_data="import_wallet"),
            InlineKeyboardButton("🏆 Invite Friends", callback_data="invite_friends"),
        ],
        [
            InlineKeyboardButton("💰 Buy/Sell", callback_data="buy_sell"),
            InlineKeyboardButton("🧾 Asset", callback_data="view_assets"),
        ],
        [
            InlineKeyboardButton("📋 Copy Trading", callback_data="copy_menu"),
            InlineKeyboardButton("📉 Limit Order", callback_data="limit_order"),
            InlineKeyboardButton("Auto Sell", callback_data="dummy_auto_sell"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
            InlineKeyboardButton("👛 Wallet", callback_data="menu_wallet"),
        ],
        [
            InlineKeyboardButton("🌐 Language", callback_data="change_language"),
            InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_dynamic_start_message_text(user_id: int, user_mention: str) -> str:
    """Display real-time SOL balance + USD estimate on the start/menu screen."""
    wallet_info = database.get_user_wallet(user_id)
    solana_address = wallet_info.get("address", "--")
    sol_balance = None
    sol_balance_str = "--"
    usd_str = "$~"

    if solana_address and solana_address != "--":
        try:
            sol_balance = await svc_get_sol_balance(solana_address)  # float SOL
            sol_balance_str = f"{sol_balance:.4f} SOL"
            sol_price = await get_sol_price_usd()
            if sol_price > 0 and isinstance(sol_balance, (int, float)):
                usd_str = format_usd(sol_balance * sol_price)
        except Exception:
            sol_balance_str = "Error"
            usd_str = "N/A"

    return (
        f"👋 Hello {user_mention}! Welcome to <b>RokuTrade</b>\n\n"
        f"Wallet address: <code>{solana_address}</code>\n"
        f"Wallet balance: <code>{sol_balance_str}</code> ({usd_str})\n\n"
        f"🔗 Referral link: https://t.me/RokuTrade?start=ref_{user_id}\n\n"
        f"✅ Send a contract address to start trading."
    )

def validate_and_clean_private_key(key_data: str) -> str:
    key_data = key_data.strip()
    if key_data.startswith("["):
        parsed = json.loads(key_data)
        if not isinstance(parsed, list):
            raise ValueError("JSON key must be a list of integers.")
        if len(parsed) != 64:
            raise ValueError("Private key must be 64 bytes.")
        return key_data
    else:
        try:
            import base58
            decoded = base58.b58decode(key_data)
            if len(decoded) != 64:
                raise ValueError("Private key must be 64 bytes.")
            return key_data
        except Exception as decode_error:
            try:
                if key_data.startswith("0x"):
                    key_data = key_data[2:]
                key_bytes = bytes.fromhex(key_data)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")
                import base58
                return base58.b58encode(key_bytes).decode()
            except Exception:
                raise ValueError(f"Invalid private key format. Not valid Base58 or Hex: {decode_error}")

async def handle_direct_private_key_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles direct paste of private key without 'import' command."""
    user_id = update.effective_user.id
    key_data = update.message.text.strip()

    try:
        cleaned_key = wallet_manager.validate_and_clean_private_key(key_data)

        old_wallet = database.get_user_wallet(user_id)
        already_exists = old_wallet.get("address") is not None

        pubkey = wallet_manager.get_solana_pubkey_from_private_key_json(cleaned_key)
        database.set_user_wallet(user_id, cleaned_key, str(pubkey))

        msg = f"✅ Solana wallet {'replaced' if already_exists else 'imported'}!\nAddress: `{pubkey}`"
        if already_exists:
            msg += "\n⚠️ Previous Solana wallet was overwritten."
        await update.message.reply_text(
            msg, parse_mode="Markdown", reply_markup=back_markup("back_to_main_menu")
        )

    except ValueError as e:
        await update.message.reply_text(
            f"❌ Error importing Solana wallet: {e}",
            reply_markup=back_markup("back_to_main_menu"),
        )
    except Exception as e:
        print(f"Direct import error: {e}")
        await update.message.reply_text(
            "❌ Unexpected error during import. Please check your private key format.",
            reply_markup=back_markup("back_to_main_menu"),
        )

    finally:
        try:
            await update.message.delete()
        except Exception:
            pass # Ignore if message can't be deleted
 
# ================== Token Panel (no DEX selection) ==================
def token_panel_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    buy_bps = int(context.user_data.get("slippage_bps_buy", 500))   # default 5%
    sell_bps = int(context.user_data.get("slippage_bps_sell", 500)) # default 5%
    kb: list[list[InlineKeyboardButton]] = []
    kb.append([
        InlineKeyboardButton("Smart Money", callback_data="noop_smart"),
        InlineKeyboardButton("↻ Refresh", callback_data="token_panel_refresh"),
    ])
    kb.append([
        InlineKeyboardButton("✅ Swap", callback_data="noop_swap"),
        InlineKeyboardButton("Limit Orders", callback_data="dummy_limit_orders"),
    ])
    kb.append([
        InlineKeyboardButton("Buy 0.2 SOL", callback_data="buy_fixed_0.2"),
        InlineKeyboardButton("Buy 0.5 SOL", callback_data="buy_fixed_0.5"),
        InlineKeyboardButton("Buy 1 SOL",   callback_data="buy_fixed_1"),
    ])
    kb.append([
        InlineKeyboardButton("Buy 2 SOL", callback_data="buy_fixed_2"),
        InlineKeyboardButton("Buy 5 SOL", callback_data="buy_fixed_5"),
        InlineKeyboardButton("Buy X SOL…", callback_data="buy_custom"),
    ])
    kb.append([
        InlineKeyboardButton("Sell 10%",  callback_data="sell_pct_10"),
        InlineKeyboardButton("Sell 25%",  callback_data="sell_pct_25"),
        InlineKeyboardButton("Sell 50%",  callback_data="sell_pct_50"),
        InlineKeyboardButton("Sell All",  callback_data="sell_pct_100"),
    ])
    kb.append([
        InlineKeyboardButton(f"✓ {percent_label(buy_bps)} Buy Slippage",  callback_data="set_buy_slippage"),
        InlineKeyboardButton(f"× {percent_label(sell_bps)} Sell Slippage", callback_data="set_sell_slippage"),
    ])
    kb.append([
        InlineKeyboardButton("⬅️ Change Token", callback_data="back_to_buy_sell_menu"),
        InlineKeyboardButton("🏠 Menu",   callback_data="back_to_main_menu"),
    ])
    return InlineKeyboardMarkup(kb)

async def build_token_panel(user_id: int, mint: str) -> str:
    """Compact summary with price & LP from Dexscreener; unknown -> N/A."""
    wallet_info = database.get_user_wallet(user_id)
    addr = wallet_info.get("address", "--") if wallet_info else "--"

    # SOL Balance
    balance_text = "N/A"
    if addr and addr != "--":
        try:
            bal = await svc_get_sol_balance(addr)
            balance_text = f"{bal:.4f} SOL"
        except Exception:
            balance_text = "Error"

    # Price + meta
    price_text = "N/A"
    mc_text = "N/A"
    lp_text = "N/A"
    display_name = None

    ds = await get_dexscreener_stats(mint)
    if ds:
        symbol = (ds.get("symbol") or "") or ""
        name = (ds.get("name") or "") or ""
        if symbol:
            display_name = symbol if symbol.startswith("$") else f"${symbol}"
        elif name:
            display_name = name

        price_text = format_usd(ds.get("priceUsd") or 0)
        mc_text    = format_usd(ds.get("fdvUsd") or 0)
        lp_text    = format_usd(ds.get("liquidityUsd") or 0)
    else:
        price_data = await get_token_price(mint)
        if price_data["price"] <= 0:
            price_data = await get_token_price_from_raydium(mint)
        if price_data["price"] <= 0:
            price_data = await get_token_price_from_pumpfun(mint)
        price_text = format_usd(price_data.get("price") or 0)
        mc_val = price_data.get("mc")
        mc_text = format_usd(mc_val if isinstance(mc_val, (int, float)) else 0)

    if not display_name:
        display_name = f"{mint[:4]}…{mint[-4:]}"

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]

    lines = []
    lines.append(f"Swap <b>{display_name}</b> 📈")
    lines.append("")
    lines.append(f"<a href=\"{dexscreener_url(mint)}\">{mint[:4]}…{mint[-4:]}</a>")
    lines.append(f"• SOL Balance: {balance_text}")
    lines.append(f"• Price: {price_text}   LP: {lp_text}   MC: {mc_text}")
    lines.append("• Raydium CPMM")
    lines.append(f'• <a href="{dexscreener_url(mint)}">DEX Screener</a>')
    lines.append("")
    lines.append(f"🕒 Last updated: {ts}")
    return "\n".join(lines)

# ================== Bot Handlers ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    user_id = update.effective_user.id
    user_mention = update.effective_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await update.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))

async def handle_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
    # init state default agar tidak menyaring dust & tampilkan detail
        context.user_data.setdefault("assets_state", {
        "page": 1, "sort": "value", "hide_dust": False,
        "dust_usd": DEFAULT_DUST_USD, "detail": True, "hidden_mints": set()
    })
        await _render_assets_detailed_view(q, context)


async def _render_assets_detailed_view(q_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Render SPL tokens sebagai kartu detail ala screenshot."""
    user_id = q_or_msg.from_user.id if hasattr(q_or_msg, "from_user") else context._user_id
    w = database.get_user_wallet(user_id)
    addr = (w or {}).get("address")
    if not addr:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]])
        await q_or_msg.edit_message_text("📊 <b>Your Asset Balances</b>\n\nNo wallet yet.", parse_mode="HTML", reply_markup=kb)
        return

    st = context.user_data.get("assets_state", {})
    page      = int(st.get("page", 1))
    sort_key  = st.get("sort", "value")
    hide_dust = bool(st.get("hide_dust", True))
    dust_usd  = float(st.get("dust_usd", DEFAULT_DUST_USD))
    hidden    = set(st.get("hidden_mints", set()))

    # === SOL header ===
    try:
        sol_amount = await svc_get_sol_balance(addr)
    except Exception:
        sol_amount = 0.0
    sol_price = await get_sol_price_usd()
    sol_usd   = sol_amount * sol_price if sol_price > 0 else 0.0

    # === tokens from svc ===
    try:
        tokens = await svc_get_token_balances(addr, min_amount=0.0)
    except Exception:
        tokens = []

    items = []
    mints = []
    for t in tokens or []:
        mint = t.get("mint") or t.get("mintAddress")
        amt  = float(t.get("amount") or t.get("uiAmount") or 0)
        if not mint or amt <= 0: 
            continue
        if mint in hidden:
            continue
        mints.append(mint)
        items.append({"mint": mint, "amount": amt})

    # === meta & harga (Dexscreener → aggregator fallback) ===
    async def meta_of(mint: str) -> dict:
        return await MetaCache.get(mint)

    # harga/LP/MC via batch cache
    packs_by_mint = await DexCache.get_bulk(mints, prefer_cache=True)
    metas  = await asyncio.gather(*(meta_of(m) for m in mints), return_exceptions=True)

    # optional positions (PNL/cost basis)
    def _pos(mint: str) -> dict:
        try:
            doc = database.position_get(user_id, mint) or {}
            # skema yang didukung (jika ada):
            # { buy_sol, buy_tokens, buy_count, sell_sol, sell_tokens, sell_count,
            #   realized_pnl_sol, avg_entry_price_usd, avg_entry_mc_usd }
            return doc
        except Exception:
            return {}

    # gabungkan
    enriched = []
    for it, meta in zip(items, metas):
        pack = packs_by_mint.get(it["mint"], {"price": 0.0, "lp": 0.0, "mc": 0.0})
        meta = meta if isinstance(meta, dict) else {}
        pack = pack if isinstance(pack, dict) else {"price": 0.0, "lp": 0.0, "mc": 0.0}
        sym  = (meta.get("symbol") or "").strip() or (meta.get("name") or "").strip() or it["mint"][:6].upper()
        px   = float(pack.get("price") or 0.0)
        usd  = it["amount"] * px if px > 0 else 0.0
        enriched.append({
            **it,
            "symbol": sym,
            "price_usd": px,
            "value_usd": usd,
            "value_sol": _sol_from_usd(usd, sol_price),
            "lp_usd": float(pack.get("lp") or 0.0),
            "mc_usd": float(pack.get("mc") or 0.0),
            "pos": _pos(it["mint"]),
        })

    # portfolio
    tokens_total_usd = sum((x["value_usd"] for x in enriched), 0.0)
    total_usd        = sol_usd + tokens_total_usd
    for x in enriched:
        x["pct"] = (x["value_usd"]/total_usd) if total_usd>0 and x["value_usd"]>0 else 0.0

    # dust filter
    filtered = [x for x in enriched if (x["value_usd"] >= dust_usd) or not hide_dust]

    # sort
    if sort_key == "alpha":
        filtered.sort(key=lambda x: x["symbol"].upper())
    elif sort_key == "pct":
        filtered.sort(key=lambda x: x["pct"], reverse=True)
    else:
        filtered.sort(key=lambda x: x["value_usd"], reverse=True)

    # paging
    total_items = len(filtered)
    total_pages = max(1, (total_items + ASSETS_PAGE_SIZE - 1) // ASSETS_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    st["page"] = page
    start = (page-1)*ASSETS_PAGE_SIZE
    page_items = filtered[start:start+ASSETS_PAGE_SIZE]

    # header
    lines = []
    lines.append("📊 <b>Your Asset Balances</b>\n")
    lines.append(f"👛 <code>{addr}</code>")
    lines.append(f"• SOL: <code>{sol_amount:.6f} SOL</code> ({format_usd(sol_usd)})  @ {format_usd(sol_price)}")
    lines.append(f"• Tokens value: <b>{format_usd(tokens_total_usd)}</b>")
    lines.append(f"• <b>Total Portfolio:</b> <b>{format_usd(total_usd)}</b>\n")

    # token cards
    if not page_items:
        lines.append("(No tokens on this page)")
    for x in page_items:
        mint = x["mint"]; sym = x["symbol"]
        val_sol = x["value_sol"]; val_usd = x["value_usd"]; price = x["price_usd"]
        lp = x["lp_usd"]; mc = x["mc_usd"]; amt = x["amount"]
        pos = x["pos"] or {}

        # pnl (optional)
        pnl_pct = None; pnl_sol = None
        if pos:
            # unrealized PnL jika ada avg_entry_price_usd
            avg_px = pos.get("avg_entry_price_usd")
            if isinstance(avg_px, (int, float)) and avg_px>0 and price>0:
                cost_usd = amt * avg_px
                pnl_usd = val_usd - cost_usd
                pnl_sol = _sol_from_usd(pnl_usd, sol_price)
                pnl_pct = (pnl_usd/cost_usd) if cost_usd>0 else None
        # indikator
        indicator = "📈" if (pnl_pct is not None and pnl_pct >= 0) else ("📉" if pnl_pct is not None else "📊")
        danger = "🟩" if (pnl_pct is not None and pnl_pct >= 0) else ("🟥" if pnl_pct is not None else "")

        lines.append(f"<b>${sym}</b> {indicator} : <code>{val_sol:.3f} SOL</code> ({format_usd(val_usd)}) "
                     f"[<a href='tg://callback?component=hide'>Hide</a>]")  # label saja, tombol real di bawah
        lines.append(f"<code>{mint}</code>(Tap to copy)")
        if pnl_pct is not None:
            lines.append(f"• PNL: {format_pct(pnl_pct)} "
                         f"({(pnl_sol or 0):.3f} SOL/{format_usd((pnl_sol or 0)*sol_price)}) {danger}")
        else:
            lines.append("• PNL: —")
        lines.append("[Share]")
        avg_mc = pos.get("avg_entry_mc_usd")
        avg_px = pos.get("avg_entry_price_usd")
        lines.append(f"• Avg Entry: {format_usd(avg_px or 0)}  Avg Entry MC: {format_usd(avg_mc or 0)}")
        lines.append(f"• Price: {format_usd(price)}  LP: {format_usd(lp)}  MC: {format_usd(mc)}")
        buy_sol  = float(pos.get("buy_sol", 0) or 0);  buy_cnt  = int(pos.get("buy_count", 0) or 0)
        sell_sol = float(pos.get("sell_sol", 0) or 0); sell_cnt = int(pos.get("sell_count", 0) or 0)
        lines.append(f"• Balance: {amt:,.0f}")
        lines.append(f"• Buy: {buy_sol:.3f} SOL ({buy_cnt} Buy)")
        lines.append(f"• Sell: {sell_sol:.3f} SOL ({sell_cnt} Sell)")
        lines.append(f"• <a href='{dexscreener_url(mint)}'>DEX Screener</a>\n")

    text = "\n".join(lines)

    # keyboard
    sort_next = {"value": "alpha", "alpha": "pct", "pct": "value"}[sort_key]
    sort_label = {"value": "Sort: Value", "alpha": "Sort: A→Z", "pct": "Sort: %Port"}[sort_key]
    prev_btn = InlineKeyboardButton("⬅️ Prev", callback_data=f"assets_pg_{page-1}") if page>1 else None
    next_btn = InlineKeyboardButton("Next ➡️", callback_data=f"assets_pg_{page+1}") if page<total_pages else None

    row0 = [
        InlineKeyboardButton("View: Detailed", callback_data="assets_view_detailed"),
        InlineKeyboardButton("Compact", callback_data="assets_view_compact"),
    ]
    row1 = [
        InlineKeyboardButton(f"{sort_label}", callback_data=f"assets_sort_{sort_next}"),
        InlineKeyboardButton(("Hide Dust" if not hide_dust else "Show All"),callback_data="assets_toggle_dust"),
    ]
    row2 = [b for b in (prev_btn, InlineKeyboardButton("↻ Refresh", callback_data="assets_refresh"), next_btn) if b]
    # baris tombol contextual per kartu tidak bisa inline per baris di teks HTML,
    # jadi kita sediakan “Hide / Share” via callback per-mint di bawah sebagai baris tambahan:
    card_buttons = []
    for x in page_items:
        mint = x["mint"]
        card_buttons.append([
            InlineKeyboardButton(f"🔕 Hide {x['symbol']}", callback_data=f"assets_hide_{mint}"),
            InlineKeyboardButton("🔗 Share", callback_data=f"assets_share_{mint}"),
        ])

    back = [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]

    kb_rows = [row0, row1]
    if row2: kb_rows.append(row2)
    kb_rows += card_buttons
    kb_rows.append(back)
    keyboard = InlineKeyboardMarkup(kb_rows)

    if hasattr(q_or_msg, "edit_message_text"):
        await q_or_msg.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    else:
        await q_or_msg.message.reply_html(text, reply_markup=keyboard, disable_web_page_preview=True)

async def handle_assets_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    st = context.user_data.setdefault("assets_state", {
        "page": 1, "sort": "value", "hide_dust": False, "dust_usd": DEFAULT_DUST_USD, "detail": True, "hidden_mints": set()
    })
    data = q.data

    if data.startswith("assets_pg_"):
        try:
            st["page"] = max(1, int(data.rsplit("_",1)[1]))
        except Exception:
            pass

    elif data == "assets_refresh":
        pass

    elif data == "assets_toggle_dust":
        st["hide_dust"] = not bool(st.get("hide_dust", True))
        st["page"] = 1

    elif data.startswith("assets_sort_"):
        st["sort"] = data.split("_",2)[2]
        st["page"] = 1

    elif data == "assets_view_compact":
        st["detail"] = False
        # jika mau, bisa panggil renderer compact lama (mis. _render_assets_view)
        # sementara tetap gunakan detailed agar konsisten
    elif data == "assets_view_detailed":
        st["detail"] = True

    elif data.startswith("assets_hide_"):
        mint = data.split("_", 2)[2]
        hidden = set(st.get("hidden_mints", set()))
        hidden.add(mint)
        st["hidden_mints"] = hidden

    elif data.startswith("assets_share_"):
        mint = data.split("_", 2)[2]
        # kirim share card terpisah
        try:
            # cari data terakhir di state render dengan fetch ulang singkat hanya token itu
            await q.message.reply_text(f"Mint: {mint}\nDexScreener: {dexscreener_url(mint)}\n(RokuTrade)", disable_web_page_preview=True)
        except Exception:
            pass

    await _render_assets_detailed_view(q, context)

# ===== Copy Trading UI =====
async def handle_copy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    follows = database.copy_follow_list_for_user(user_id) or []
    rows = []
    kb_rows = []

    if follows:
        for f in follows:
            leader = f["leader_address"]
            st = "🟢 ON" if f.get("active") else "⚪ OFF"
            ratio = f.get("ratio", 1.0)
            mx = f.get("max_sol_per_trade", 0.5)
            rows.append(f"• <code>{leader}</code>  r={ratio:g}  max={mx:g}  [{st}]")

            # ON/OFF + Remove buttons for each leader
            kb_rows.append([
                InlineKeyboardButton("Toggle ON/OFF", callback_data=f"copy_toggle:{leader}"),
                InlineKeyboardButton("🗑️ Remove", callback_data=f"copy_remove:{leader}"),
            ])
    else:
        rows.append("No leaders yet.")

    body = "\n".join(rows)
    text = (
        "📋 <b>Copy Trading</b>\n\n"
        f"{body}\n\n"
        "Tip: You can also use quick commands:\n"
        "<code>copyadd LEADER RATIO MAX_SOL</code>\n"
        "<code>copyon LEADER</code> / <code>copyoff LEADER</code> / <code>copyrm LEADER</code>\n"
    )

    # global buttons
    keyboard = [
        [InlineKeyboardButton("➕ Add Leader", callback_data="copy_add_wizard"),
         InlineKeyboardButton("↻ Refresh", callback_data="copy_menu")],
        *kb_rows,
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")],
    ]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    
def _is_pubkey(x: str) -> bool:
    try:
        import base58
        return 32 <= len(x) <= 44 and base58.b58decode(x) is not None
    except Exception:
        return False

async def handle_copy_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch commands: copyadd, copyon, copyoff, copyrm."""
    user_id = update.effective_user.id
    txt = (update.message.text or "").strip()
    parts = txt.split()
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd == "copyadd" and len(parts) >= 4:
        leader = parts[1].strip()
        if not _is_pubkey(leader):
            await update.message.reply_html("❌ Invalid leader pubkey.", reply_markup=back_markup("back_to_main_menu"))
            return
        try:
            ratio = float(parts[2])
            max_sol = float(parts[3])
        except Exception:
            await update.message.reply_html("❌ Usage: <code>copyadd LEADER_PUBKEY RATIO MAX_SOL</code>", reply_markup=back_markup("back_to_main_menu"))
            return
        database.copy_follow_upsert(user_id, leader, ratio=ratio, max_sol_per_trade=max_sol, active=True)
        await update.message.reply_html("✅ Copy-follow added/updated.", reply_markup=back_markup("back_to_main_menu"))
        return

    if cmd == "copyon" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_upsert(user_id, leader, active=True)
        await update.message.reply_html("✅ Copy-follow turned ON.", reply_markup=back_markup("back_to_main_menu"))
        return

    if cmd == "copyoff" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_upsert(user_id, leader, active=False)
        await update.message.reply_html("✅ Copy-follow turned OFF.", reply_markup=back_markup("back_to_main_menu"))
        return

    if cmd == "copyrm" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_remove(user_id, leader)
        await update.message.reply_html("🗑️ Copy-follow removed.", reply_markup=back_markup("back_to_main_menu"))
        return

async def handle_copy_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    leader = q.data.split(":", 1)[1]
    # read state then toggle
    exists = False
    for f in database.copy_follow_list_for_user(user_id) or []:
        if f["leader_address"] == leader:
            exists = True
            database.copy_follow_upsert(user_id, leader, active=not f.get("active", True))
            break
    if not exists:
        await q.edit_message_text("❌ Leader not found.", reply_markup=back_markup("copy_menu"))
        return
    await handle_copy_menu(update, context)

async def handle_copy_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    leader = q.data.split(":", 1)[1]
    database.copy_follow_remove(user_id, leader)
    await handle_copy_menu(update, context)
    
async def copy_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the add-leader wizard and shows the copy menu."""
    clear_user_context(context)
    # This function is called via a callback query, so update.callback_query is guaranteed to exist.
    await handle_copy_menu(update, context) # This will redraw the menu
    return ConversationHandler.END

async def copy_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data.pop("copy_leader", None)
    context.user_data.pop("copy_ratio", None)
    await q.edit_message_text(
        "🧭 <b>Add Leader</b>\nSend the <b>public key</b> of the wallet you want to copy.",
        parse_mode="HTML",
        reply_markup=back_markup("copy_menu"),
    )
    return COPY_AWAIT_LEADER

async def copy_add_leader(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    leader = (update.message.text or "").strip()
    if not _is_pubkey(leader):
        await update.message.reply_html("❌ Invalid pubkey. Please try again.", reply_markup=back_markup("copy_menu"))
        return COPY_AWAIT_LEADER
    context.user_data["copy_leader"] = leader
    await update.message.reply_html(
        "✅ Leader accepted.\n\nNow send the <b>ratio</b> (e.g. <code>1</code> for 1:1, <code>0.5</code> for half).",
        reply_markup=back_markup("copy_menu"),
    )
    return COPY_AWAIT_RATIO

async def copy_add_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ratio = float((update.message.text or "").strip())
        if ratio <= 0 or ratio > 100:
            raise ValueError()
    except Exception:
        await update.message.reply_html("❌ Invalid ratio. Example: <code>1</code> or <code>0.5</code>.",
                                        reply_markup=back_markup("copy_menu"))
        return COPY_AWAIT_RATIO
    context.user_data["copy_ratio"] = ratio
    await update.message.reply_html(
        "👌 Now send the <b>max SOL per trade</b> (e.g. <code>0.25</code>).",
        reply_markup=back_markup("copy_menu"),
    )
    return COPY_AWAIT_MAX

async def copy_add_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        max_sol = float((update.message.text or "").strip())
        if max_sol <= 0 or max_sol > 1000:
            raise ValueError()
    except Exception:
        await update.message.reply_html("❌ Invalid max SOL. Example: <code>0.25</code>.",
                                        reply_markup=back_markup("copy_menu"))
        return COPY_AWAIT_MAX

    user_id = update.effective_user.id
    leader = context.user_data.get("copy_leader")
    ratio  = context.user_data.get("copy_ratio", 1.0)

    database.copy_follow_upsert(user_id, leader, ratio=ratio, max_sol_per_trade=max_sol, active=True)

    # clear context & return to menu
    context.user_data.pop("copy_leader", None)
    context.user_data.pop("copy_ratio", None)

    await update.message.reply_html("✅ Leader added & activated.", reply_markup=back_markup("copy_menu"))
    # refresh menu
    #fake_cb = Update(update.update_id, callback_query=update.to_dict().get("callback_query"))
    #await handle_copy_menu(update, context)  # or just let the user click Back
    

async def handle_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    keyboard_buttons = []
    keyboard_buttons.append(
        [
            InlineKeyboardButton("Create Solana Wallet", callback_data="create_wallet:solana"),
            InlineKeyboardButton("🗑️ Delete", callback_data="delete_wallet:solana"),
        ]
    )
    keyboard_buttons.append([InlineKeyboardButton("Import Wallet", callback_data="import_wallet")])
    keyboard_buttons.append([InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")])
    await query.edit_message_text("Wallet Options:", reply_markup=InlineKeyboardMarkup(keyboard_buttons))

async def handle_create_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    private_key_output, public_address = wallet_manager.create_solana_wallet()
    database.set_user_wallet(user_id, private_key_output, public_address)
    # ⚠️ Display PK so the user can back it up, but give a strong warning
    await query.edit_message_text(
        "🔐 <b>New Solana wallet created & saved.</b>\n"
        f"Address:\n<code>{public_address}</code>\n\n"
        "⚠️ <b>Private Key (BACKUP & DO NOT SHARE):</b>\n"
        f"<code>{private_key_output}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]),
    )

async def handle_import_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🔐 Please send your private key in the format:\n"
        "`import [private_key]`\n\n"
        "Supported formats: **JSON array**, **Base58 string**, **Hex**\n"
        "Example: `import 3WbX...`",
        parse_mode="Markdown",
        reply_markup=back_markup("back_to_main_menu"),
    )

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().replace("\n", " ")
    command, *args = text.split(maxsplit=1)
    command = command.lower()

    if command == "import":
        if len(args) == 0:
            await update.message.reply_text(
                "❌ Invalid format. Use: `import [private_key]`",
                parse_mode="Markdown",
                reply_markup=back_markup("back_to_main_menu"),
            )
            return
        try:
            key_data = args[0].strip()
            cleaned_key = validate_and_clean_private_key(key_data)

            old_wallet = database.get_user_wallet(user_id)
            already_exists = old_wallet.get("address") is not None

            try:
                pubkey = wallet_manager.get_solana_pubkey_from_private_key_json(cleaned_key)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Invalid private key: {e}",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            database.set_user_wallet(user_id, cleaned_key, str(pubkey))

            msg = f"✅ Solana wallet {'replaced' if already_exists else 'imported'}!\nAddress: `{pubkey}`"
            if already_exists:
                msg += "\n⚠️ Previous Solana wallet was overwritten."
            await update.message.reply_text(
                msg, parse_mode="Markdown", reply_markup=back_markup("back_to_main_menu")
            )

        except ValueError as e:
            await update.message.reply_text(
                f"❌ Error importing Solana wallet: {e}",
                reply_markup=back_markup("back_to_main_menu"),
            )
        except Exception as e:
            print(f"Import error: {e}")
            await update.message.reply_text(
                "❌ Unexpected error during import. Please check your private key format.",
                reply_markup=back_markup("back_to_main_menu"),
            )
        finally:
            try:
                await update.message.delete()
            except Exception:
                pass # Ignore if message can't be deleted
        return

    if command == "send":
        try:
            if len(args) == 0:
                await update.message.reply_text(
                    "❌ Invalid format. Use `send [address] [amount]`",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            match = re.match(r"^(\w+)\s+([\d.]+)$", args[0].strip())
            if not match:
                await update.message.reply_text(
                    "❌ Invalid format. Use `send [address] [amount]`",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            to_addr, amount_str = match.groups()
            amount = float(amount_str)
            if amount <= 0:
                await update.message.reply_text(
                    "❌ Amount must be greater than 0",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text(
                    "❌ No Solana wallet found.",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            tx = solana_client.send_sol(wallet["private_key"], to_addr, amount)
            if tx and not tx.lower().startswith("error"):
                solscan_link = f"https://solscan.io/tx/{tx}"
                await update.message.reply_text(
                    f"✅ Sent {amount} SOL!\nTx: [`{tx}`]({solscan_link})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=back_markup("back_to_main_menu"),
                )
            else:
                await update.message.reply_text(
                    f"❌ Failed to send SOL.\n{tx}",
                    parse_mode="Markdown",
                    reply_markup=back_markup("back_to_main_menu"),
                )
        except (ValueError, AttributeError):
            await update.message.reply_text(
                "❌ Invalid format. Use `send [address] [amount]`",
                reply_markup=back_markup("back_to_main_menu"),
            )
        except Exception as e:
            print(f"Send error: {e}")
            await update.message.reply_text(
                f"❌ Error: {e}",
                reply_markup=back_markup("back_to_main_menu"),
            )
        return

    if command == "sendtoken":
        try:
            if len(args) == 0:
                await update.message.reply_text(
                    "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            parts = args[0].strip().split()
            if len(parts) != 3:
                await update.message.reply_text(
                    "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            token_addr, to_addr, amount_str = parts
            amount = float(amount_str)
            if amount <= 0:
                await update.message.reply_text(
                    "❌ Amount must be greater than 0",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text(
                    "❌ No Solana wallet found.",
                    reply_markup=back_markup("back_to_main_menu"),
                )
                return

            tx = solana_client.send_spl_token(wallet["private_key"], token_addr, to_addr, amount)
            if tx and not tx.lower().startswith("error"):
                solscan_link = f"https://solscan.io/tx/{tx}"
                await update.message.reply_text(
                    f"✅ Sent {amount} SPL Token!\nTx: [`{tx}`]({solscan_link})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=back_markup("back_to_main_menu"),
                )
            else:
                await update.message.reply_text(
                    f"❌ Failed to send SPL token.\n{tx}",
                    parse_mode="Markdown",
                    reply_markup=back_markup("back_to_main_menu"),
                )
        except (ValueError, IndexError):
            await update.message.reply_text(
                "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`",
                reply_markup=back_markup("back_to_main_menu"),
            )
        except Exception as e:
            print(f"SendToken error: {e}")
            await update.message.reply_text(
                f"❌ Error: {e}",
                reply_markup=back_markup("back_to_main_menu"),
            )
        return

    # slippage text flow
    if context.user_data.get("awaiting_slippage_input"):
        await handle_set_slippage_value(update, context)
        return

    await update.message.reply_text(
        "❌ Unrecognized command. Please use `import`, `send`, or `sendtoken`.",
        reply_markup=back_markup("back_to_main_menu"),
    )

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_mention = query.from_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await query.edit_message_text(welcome_text, reply_markup=get_start_menu_keyboard(user_id), parse_mode="HTML")

async def back_to_main_menu_and_end_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the conversation and shows the main menu."""
    await back_to_main_menu(update, context)
    return ConversationHandler.END

async def handle_back_to_buy_sell_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # return to mint input mode
    clear_user_context(context)
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📄 Please send the <b>token contract address</b> you want to trade.",
        parse_mode="HTML",
        reply_markup=back_markup("back_to_main_menu"),
    )
    return AWAITING_TOKEN_ADDRESS

async def handle_back_to_token_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    mint = context.user_data.get("token_address")
    if not mint:
        return await handle_back_to_buy_sell_menu(update, context)
    panel = await build_token_panel(q.from_user.id, mint)
    await q.edit_message_text(panel, reply_markup=token_panel_keyboard(context), parse_mode="HTML")
    return AWAITING_TRADE_ACTION

async def dummy_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"🛠️ Feature `{query.data}` is under development.",
        reply_markup=back_markup("back_to_main_menu"),
    )

async def handle_delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    database.delete_user_wallet(user_id)
    await query.edit_message_text(
        "🗑️ Your Solana wallet has been deleted.",
        reply_markup=back_markup("back_to_main_menu"),
    )

async def handle_send_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "✉️ To send assets, use format:\n"
        "`send WALLET_ADDRESS AMOUNT` for native SOL\n"
        "`sendtoken TOKEN_ADDRESS TO_WALLET_ADDRESS AMOUNT` for SPL Tokens\n\n"
        "Example:\n"
        "`send Fk...9N 0.5`\n"
        "`sendtoken EPj...V1 G8...A7 0.01`",
        parse_mode="Markdown",
        reply_markup=back_markup("back_to_main_menu"),
    )



# ================== Trading flows ==================
async def buy_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Buy", callback_data="dummy_buy"), InlineKeyboardButton("Sell", callback_data="dummy_sell")],
        [InlineKeyboardButton("✈️ Copy Trade", callback_data="copy_menu")],
        [InlineKeyboardButton("🤖 Auto Trade - Pump.fun", callback_data="pumpfun_trade")],
        [InlineKeyboardButton("📉 Limit Orders", callback_data="dummy_limit_orders"), InlineKeyboardButton("Auto Sell", callback_data="dummy_auto_sell")],
        [InlineKeyboardButton("📈 Positions", callback_data="dummy_positions"), InlineKeyboardButton("👛 Wallet", callback_data="dummy_wallet"), InlineKeyboardButton("❓ Help", callback_data="dummy_help")],
        [InlineKeyboardButton("💵 Smart Wallet", callback_data="dummy_smart_wallet"), InlineKeyboardButton("🖥️ Extension", callback_data="dummy_extension")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="dummy_settings"), InlineKeyboardButton("💰 Referrals", callback_data="dummy_referrals")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")],
    ]

    message_text = "Choose a trading option or enter a token address to start trading."
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_TOKEN_ADDRESS

async def handle_dummy_trade_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer(f"Feature '{query.data}' is under development.", show_alert=True)
    return AWAITING_TOKEN_ADDRESS

async def handle_token_address_for_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message if update.message else update.callback_query.message
    token_address = message.text.strip()

    if not _is_valid_pubkey(token_address):
        await message.reply_text(
            "❌ Invalid token address format. Please enter a valid Solana token address.",
            reply_markup=back_markup("back_to_main_menu"),
        )
        return AWAITING_TOKEN_ADDRESS

    context.user_data["token_address"] = token_address
    context.user_data["selected_dex"] = "jupiter"  # fixed route
    context.user_data.setdefault("slippage_bps_buy", 500)   # 5%
    context.user_data.setdefault("slippage_bps_sell", 500)  # 5%

    panel = await build_token_panel(update.effective_user.id, token_address)
    await message.reply_html(panel, reply_markup=token_panel_keyboard(context))
    return AWAITING_TRADE_ACTION

async def handle_refresh_token_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    mint = context.user_data.get("token_address")
    if not mint:
        return await handle_back_to_buy_sell_menu(update, context)
    panel = await build_token_panel(q.from_user.id, mint)
    await q.edit_message_text(panel, reply_markup=token_panel_keyboard(context), parse_mode="HTML")
    return AWAITING_TRADE_ACTION

async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("Coming soon", show_alert=False)
    return AWAITING_TRADE_ACTION

async def handle_buy_sell_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action.startswith("buy_fixed_"):
        amount_str = action.split("_")[-1]
        amount = float(amount_str)
        context.user_data["trade_type"] = "buy"
        context.user_data["amount_type"] = "sol"
        await perform_trade(update, context, amount)
        return ConversationHandler.END

    elif action == "buy_custom":
        context.user_data["trade_type"] = "buy"
        context.user_data["amount_type"] = "sol"
        await query.edit_message_text(
            "Please enter the amount of SOL you want to buy with:",
            reply_markup=back_markup("back_to_token_panel"),
        )
        return AWAITING_AMOUNT

    elif action.startswith("sell_pct_"):
        percentage_str = action.split("_")[-1]
        percentage = int(percentage_str)
        context.user_data["trade_type"] = "sell"
        context.user_data["amount_type"] = "percentage"
        await perform_trade(update, context, percentage)
        return ConversationHandler.END

    await query.message.reply_text(
        "This action is not yet implemented.",
        reply_markup=back_markup("back_to_token_panel"),
    )
    return AWAITING_TRADE_ACTION

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text(
                "❌ Amount must be greater than 0.",
                reply_markup=back_markup("back_to_token_panel"),
            )
            return AWAITING_AMOUNT
        context.user_data["trade_type"] = context.user_data.get("trade_type", "buy")
        context.user_data["amount_type"] = "sol"
        await perform_trade(update, context, amount)
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid amount. Please enter a valid number.",
            reply_markup=back_markup("back_to_token_panel"),
        )
        return AWAITING_AMOUNT
    return ConversationHandler.END

# ------------------------- FEE helper -------------------------
def _fee_ui(val_ui: float) -> float:
    return max(0.0, float(val_ui) * (FEE_BPS / 10_000.0))

async def _send_fee_sol_if_any(private_key: str, ui_amount: float, reason: str):
    if not FEE_ENABLED:
        return None
    fee_ui = _fee_ui(ui_amount)
    if fee_ui <= 0.00001:
        return None
    print(f"Attempting to send {fee_ui:.6f} SOL fee ({reason}) to {FEE_WALLET}")
    tx = solana_client.send_sol(private_key, FEE_WALLET, fee_ui)
    if isinstance(tx, str) and not tx.lower().startswith("error"):
        print(f"✅ Platform fee successful. Signature: {tx}")
        return tx
    else:
        print(f"⚠️ Platform fee transfer failed: {tx}")
        return None


async def _prepare_buy_trade(wallet: dict, amount: float, token_mint: str, slippage_bps: int) -> dict:
    """Prepares parameters for a buy trade, checking balance and handling pre-swap fees."""
    total_sol_to_spend = float(amount)
    fee_amount_ui = _fee_ui(total_sol_to_spend) if FEE_ENABLED else 0.0
    actual_swap_amount_ui = total_sol_to_spend - fee_amount_ui

    try:
        sol_balance = await svc_get_sol_balance(wallet["address"])
    except Exception:
        sol_balance = 0.0

    buffer_ui = 0.002  # Gas fees buffer
    if sol_balance < total_sol_to_spend + buffer_ui:
        return {
            "status": "error",
            "message": f"❌ Not enough SOL. Need ~{(total_sol_to_spend + buffer_ui):.4f} SOL (amount + fees), you have {sol_balance:.4f} SOL.",
        }

    # Send fee now, before the swap
    if FEE_ENABLED and fee_amount_ui > 0:
        await _send_fee_sol_if_any(wallet["private_key"], total_sol_to_spend, "BUY")

    return {
        "status": "ok",
        "params": {
            "input_mint": SOLANA_NATIVE_TOKEN_MINT,
            "output_mint": token_mint,
            "amount_lamports": int(actual_swap_amount_ui * 1_000_000_000),
            "slippage_bps": slippage_bps,
        },
    }

async def _prepare_sell_trade(wallet: dict, amount: float, amount_type: str, token_mint: str, slippage_bps: int) -> dict:
    """Prepares parameters for a sell trade, checking balance and getting pre-swap SOL balance for fees."""
    try:
        decimals = int(await svc_get_mint_decimals(token_mint))
        token_balance_ui = float(await svc_get_token_balance(wallet["address"], token_mint))
    except Exception:
        return {"status": "error", "message": "❌ Could not fetch token balance or decimals."}

    if amount_type == "percentage":
        if token_balance_ui <= 0:
            return {"status": "error", "message": f"❌ Insufficient balance for token `{token_mint}`."}
        sell_ui = token_balance_ui * (float(amount) / 100.0)
    else:  # Custom token amount
        sell_ui = float(amount)
        if sell_ui > token_balance_ui + 1e-12:
            return {"status": "error", "message": "❌ Amount exceeds wallet balance."}

    pre_sol_ui = 0.0
    if FEE_ENABLED:
        try:
            pre_sol_ui = await svc_get_sol_balance(wallet["address"])
        except Exception:
            pass # Not critical if this fails

    return {
        "status": "ok",
        "params": {
            "input_mint": token_mint,
            "output_mint": SOLANA_NATIVE_TOKEN_MINT,
            "amount_lamports": int(sell_ui * (10 ** decimals)),
            "slippage_bps": slippage_bps,
        },
        "pre_sol_ui": pre_sol_ui,
    }

async def _handle_sell_fee(wallet: dict, pre_sol_ui: float):
    """After a successful sell, calculates the SOL gain and sends the fee."""
    if not FEE_ENABLED or pre_sol_ui is None:
        return
    try:
        await asyncio.sleep(1.5) # Wait for balance to update
        post_sol_ui = await svc_get_sol_balance(wallet["address"])
        delta_ui = max(0.0, post_sol_ui - pre_sol_ui)
        if delta_ui > 0:
            await _send_fee_sol_if_any(wallet["private_key"], delta_ui, "SELL")
    except Exception as e:
        # Log this error but don't bother the user with a fee-related failure message
        print(f"⚠️ Fee check/send failed after sell: {e}")

# ganti/buat versi ini
async def _handle_trade_response(
    message,
    res: dict,
    *,
    trade_type: str,
    wallet: dict,
    user_id: int,
    token_mint: str,
    pre_sol_ui: float,
    pre_token_ui: float,
    prev_cb: str = "back_to_token_panel",   # <-- default aman
):
    if isinstance(res, dict) and (res.get("signature") or res.get("bundle")):
        # ==== update posisi (buy/sell) ====
        try:
            await asyncio.sleep(2.0)
            post_sol_ui   = await svc_get_sol_balance(wallet["address"])
            post_token_ui = await svc_get_token_balance(wallet["address"], token_mint)

            price_usd = None
            mc_usd    = None
            try:
                ds = await get_dexscreener_stats(token_mint)
                if ds:
                    price_usd = float(ds.get("priceUsd") or 0) or None
                    mc_usd    = float(ds.get("fdvUsd") or 0) or None
            except Exception:
                pass

            if trade_type == "buy":
                delta_tokens = max(0.0, post_token_ui - pre_token_ui)
                delta_sol    = max(0.0, pre_sol_ui - post_sol_ui)
                await _update_position_after_trade(
                    user_id=user_id,
                    mint=token_mint,
                    side="buy",
                    delta_tokens=delta_tokens,
                    delta_sol=delta_sol,
                    price_usd=price_usd,
                    mc_usd=mc_usd,
                )
            else:
                delta_tokens = max(0.0, pre_token_ui - post_token_ui)
                delta_sol    = max(0.0, post_sol_ui - pre_sol_ui)
                await _update_position_after_trade(
                    user_id=user_id,
                    mint=token_mint,
                    side="sell",
                    delta_tokens=delta_tokens,
                    delta_sol=delta_sol,
                    price_usd=price_usd,
                    mc_usd=mc_usd,
                )
        except Exception as e:
            try:
                await reply_err_html(message, f"⚠️ Position update failed: {e}", prev_cb=prev_cb)
            except Exception:
                pass

        sig = res.get("signature") or res.get("bundle")
        await reply_ok_html(message, "✅ Swap successful!", prev_cb=prev_cb, signature=sig)
    else:
        err = res.get("error") if isinstance(res, dict) else res
        await reply_err_html(message, f"❌ Swap failed: {short_err_text(str(err))}", prev_cb=prev_cb)


# ------------------------- Trade core -------------------------
async def perform_trade(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    amount,
    prev_cb_on_end: str | None = None,   # <-- dibuat optional
):
    message = update.message if update.message else update.callback_query.message
    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    selected_dex = (context.user_data.get("selected_dex") or "jupiter").lower()

    # fallback tombol back: sesuaikan otomatis bila tidak dikirim dari pemanggil
    prev_cb = prev_cb_on_end or ("pumpfun_back_to_panel" if selected_dex == "pumpfun" else "back_to_token_panel")

    if not wallet or not wallet.get("private_key") or not wallet.get("address"):
        await reply_err_html(
            message,
            "❌ No Solana wallet found. Please create or import one first.",
            prev_cb=prev_cb,
        )
        return

    # context
    trade_type   = (context.user_data.get("trade_type") or "").lower()      # buy|sell
    amount_type  = (context.user_data.get("amount_type") or "").lower()     # sol|percentage
    token_mint   = context.user_data.get("token_address")
    buy_slip_bps = int(context.user_data.get("slippage_bps_buy",  500))
    sel_slip_bps = int(context.user_data.get("slippage_bps_sell", 500))

    if not token_mint:
        await reply_err_html(message, "❌ No token mint in context.", prev_cb="back_to_buy_sell_menu")
        return

    # snapshot pra-trade
    try:
        pre_sol_ui   = await svc_get_sol_balance(wallet["address"])
        pre_token_ui = await svc_get_token_balance(wallet["address"], token_mint)
    except Exception:
        pre_sol_ui, pre_token_ui = 0.0, 0.0

    # siapkan parameter
    if trade_type == "buy":
        prep = await _prepare_buy_trade(wallet, amount, token_mint, buy_slip_bps)
    else:
        prep = await _prepare_sell_trade(wallet, amount, amount_type, token_mint, sel_slip_bps)
        if isinstance(prep, dict) and prep.get("pre_sol_ui") is not None:
            pre_sol_ui = float(prep["pre_sol_ui"])

    if prep.get("status") == "error":
        await reply_err_html(message, prep["message"], prev_cb=prev_cb)
        return

    await reply_ok_html(
        message,
        f"⏳ Performing {trade_type} on `{token_mint}` via {selected_dex.capitalize()}…",
        prev_cb=prev_cb,
    )

    # eksekusi
    try:
        if selected_dex == "pumpfun":
            slip_pct = max(0.0, min(100.0, (prep["params"]["slippage_bps"] / 100.0)))
            if trade_type == "sell" and amount_type == "percentage":
                amt_param = f"{int(float(amount))}%"
                denom_sol = False
            else:
                amt_param = float(amount)
                denom_sol = True  # buy by SOL

            res = await pumpfun_swap(
                private_key=wallet["private_key"],
                action=trade_type,
                mint=token_mint,
                amount=amt_param,
                denominated_in_sol=denom_sol,
                slippage_pct=slip_pct,
                priority_fee_sol=0.0,
                pool="auto",
            )
        else:
            res = await dex_swap(
                private_key=wallet["private_key"],
                **prep["params"],
                priority_fee_sol=0.0,
            )

        # handle sukses/gagal + update posisi
        await _handle_trade_response(
            message,
            res,
            trade_type=trade_type,
            wallet=wallet,
            user_id=user_id,
            token_mint=token_mint,
            pre_sol_ui=pre_sol_ui,
            pre_token_ui=pre_token_ui,
            prev_cb=prev_cb,
        )

        # fee SELL (pasca-swap)
        if trade_type == "sell" and FEE_ENABLED:
            try:
                await asyncio.sleep(1.5)
                post_sol_ui = await svc_get_sol_balance(wallet["address"])
                delta_ui = max(0.0, post_sol_ui - pre_sol_ui)
                if delta_ui > 0:
                    await _send_fee_sol_if_any(wallet["private_key"], delta_ui, "SELL")
            except Exception as e:
                print(f"⚠️ Fee check/send failed after sell: {e}")

    except Exception as e:
        await reply_err_html(
            message,
            f"❌ An unexpected error occurred: {short_err_text(str(e))}",
            prev_cb=prev_cb,
        )
    finally:
        clear_user_context(context)

# ----- Slippage set flow -----
async def handle_set_slippage_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    target = "buy" if q.data == "set_buy_slippage" else "sell"
    context.user_data["awaiting_slippage_input"] = True
    context.user_data["slippage_target"] = target
    await q.edit_message_text(
        f"✏️ Enter {target.upper()} slippage in % (e.g., 5 or 18).",
        reply_markup=back_markup("back_to_token_panel"),
    )
    return SET_SLIPPAGE

async def handle_set_slippage_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip().replace("%", "")
    try:
        pct = float(txt)
        if pct <= 0 or pct > 100:
            raise ValueError("out of range")
        bps = int(round(pct * 100))
        tgt = context.user_data.get("slippage_target", "buy")
        if tgt == "sell":
            context.user_data["slippage_bps_sell"] = bps
        else:
            context.user_data["slippage_bps_buy"] = bps
        context.user_data.pop("awaiting_slippage_input", None)
        context.user_data.pop("slippage_target", None)
        panel = await build_token_panel(update.effective_user.id, context.user_data.get("token_address", ""))
        await update.message.reply_html(
            f"✅ Slippage {tgt.upper()} set to {pct:.0f}%.",
            reply_markup=token_panel_keyboard(context),
        )
        await update.message.reply_html(panel, reply_markup=token_panel_keyboard(context))
        return AWAITING_TRADE_ACTION
    except Exception:
        await update.message.reply_text(
            "❌ Invalid number. Enter % like `5` or `18`.",
            reply_markup=back_markup("back_to_token_panel"),
        )
        return SET_SLIPPAGE

# ================== Pump.fun Trading Flow (NEW & COMPLETE) ==================

async def pumpfun_trade_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the Pump.fun flow."""
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 <b>Pump.fun Auto Trade</b>\n\n"
        "Please send the <b>token mint address</b> you want to trade.",
        parse_mode="HTML",
        reply_markup=back_markup("back_to_main_menu"),
    )
    return PUMPFUN_AWAITING_TOKEN

async def pumpfun_handle_token_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the token address and displays the trade panel."""
    message = update.message
    token_address = message.text.strip()

    if not _is_valid_pubkey(token_address):
        await message.reply_text(
            "❌ Invalid token address format.",
            reply_markup=back_markup("pumpfun_trade"),
        )
        return PUMPFUN_AWAITING_TOKEN

    context.user_data["token_address"] = token_address
    context.user_data["selected_dex"] = "pumpfun" # IMPORTANT: Tag this as a Pump.fun transaction
    context.user_data.setdefault("slippage_bps_buy", 500)  # 5% default

    panel_text = f"🤖 <b>Pump.fun Trade</b>\n\nToken: <code>{token_address}</code>"
    keyboard = [
        [
            InlineKeyboardButton("Buy (SOL)", callback_data="pumpfun_buy"),
            InlineKeyboardButton("Sell (%)", callback_data="pumpfun_sell"),
        ],
        [
             InlineKeyboardButton(f"Slippage: {percent_label(context.user_data['slippage_bps_buy'])}",
                callback_data="pumpfun_set_slippage")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ]
    await message.reply_html(panel_text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PUMPFUN_AWAITING_ACTION

async def pumpfun_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the Buy or Sell choice."""
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "pumpfun_buy":
        context.user_data["trade_type"] = "buy"
        keyboard = [
            [
                InlineKeyboardButton("0.1 SOL", callback_data="pumpfun_buy_fixed_0.1"),
                InlineKeyboardButton("0.5 SOL", callback_data="pumpfun_buy_fixed_0.5"),
                InlineKeyboardButton("1 SOL", callback_data="pumpfun_buy_fixed_1"),
            ],
            [
                InlineKeyboardButton("2 SOL", callback_data="pumpfun_buy_fixed_2"),
                InlineKeyboardButton("5 SOL", callback_data="pumpfun_buy_fixed_5"),
                InlineKeyboardButton("X SOL...", callback_data="pumpfun_buy_custom"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="pumpfun_back_to_panel")]
        ]
        await query.edit_message_text(
            "Select amount to buy:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PUMPFUN_AWAITING_BUY_AMOUNT

    elif action == "pumpfun_sell":
        context.user_data["trade_type"] = "sell"
        context.user_data["amount_type"] = "percentage"
        keyboard = [
            [
                InlineKeyboardButton("10%", callback_data="pumpfun_sell_pct_10"),
                InlineKeyboardButton("25%", callback_data="pumpfun_sell_pct_25"),
                InlineKeyboardButton("50%", callback_data="pumpfun_sell_pct_50"),
                InlineKeyboardButton("100%", callback_data="pumpfun_sell_pct_100"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="pumpfun_back_to_panel")]
        ]
        await query.edit_message_text(
            "Select percentage to sell:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PUMPFUN_AWAITING_SELL_PERCENTAGE
        
    elif action == "pumpfun_set_slippage":
        await query.answer("Slippage setting coming soon!", show_alert=True)
        return PUMPFUN_AWAITING_ACTION

async def pumpfun_handle_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles buy amount input from buttons."""
    query = update.callback_query
    await query.answer()

    if query.data == "pumpfun_buy_custom":
        await query.edit_message_text(
            "Enter amount of SOL to buy:",
            reply_markup=back_markup("pumpfun_back_to_panel")
        )
        return PUMPFUN_AWAITING_BUY_AMOUNT
    
    amount_str = query.data.split("_")[-1]
    amount = float(amount_str)
    context.user_data["trade_type"] = "buy"
    await perform_trade(update, context, amount)
    return ConversationHandler.END

async def pumpfun_handle_text_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text input for custom buy amount."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError()
        context.user_data["trade_type"] = "buy"
        await perform_trade(update, context, amount)
        return ConversationHandler.END
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid amount. Please enter a valid number.",
            reply_markup=back_markup("pumpfun_back_to_panel"),
        )
        return PUMPFUN_AWAITING_BUY_AMOUNT

async def pumpfun_handle_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles sell percentage input."""
    query = update.callback_query
    await query.answer()
    
    percentage_str = query.data.split("_")[-1]
    percentage = int(percentage_str)
    context.user_data["trade_type"] = "sell"
    context.user_data["amount_type"] = "percentage"
    await perform_trade(update, context, percentage)
    return ConversationHandler.END

async def pumpfun_back_to_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Returns to the Pump.fun trade panel."""
    query = update.callback_query
    await query.answer()
    token_address = context.user_data.get("token_address")

    panel_text = f"🤖 <b>Pump.fun Trade</b>\n\nToken: <code>{token_address}</code>"
    keyboard = [
        [
            InlineKeyboardButton("Buy (SOL)", callback_data="pumpfun_buy"),
            InlineKeyboardButton("Sell (%)", callback_data="pumpfun_sell"),
        ],
        [
             InlineKeyboardButton(f"Slippage: {percent_label(context.user_data.get('slippage_bps_buy', 500))}",
                callback_data="pumpfun_set_slippage")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ]
    await query.edit_message_text(panel_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return PUMPFUN_AWAITING_ACTION

# ================== App bootstrap ==================
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    trade_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_sell, pattern="^buy_sell$"),
            MessageHandler(
                (filters.TEXT & ~filters.COMMAND & PubkeyFilter()),
                handle_token_address_for_trade,
            ),
        ],
        states={
            AWAITING_TOKEN_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_address_for_trade),
                CallbackQueryHandler(handle_dummy_trade_buttons, pattern=r"^(dummy_.*)$"),

            ],
            AWAITING_TRADE_ACTION: [
                CallbackQueryHandler(handle_buy_sell_action, pattern="^(buy_.*|sell_.*)$"),
                CallbackQueryHandler(handle_back_to_buy_sell_menu, pattern="^back_to_buy_sell_menu$"),
                CallbackQueryHandler(handle_back_to_token_panel, pattern="^back_to_token_panel$"),
                CallbackQueryHandler(handle_refresh_token_panel, pattern="^token_panel_refresh$"),
                CallbackQueryHandler(handle_set_slippage_entry, pattern="^set_(buy|sell)_slippage$"),
                MessageHandler(
                    (filters.TEXT & ~filters.COMMAND & PubkeyFilter()),
                    handle_token_address_for_trade,
                ),
                
            ],
            AWAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount),
                CallbackQueryHandler(handle_back_to_token_panel, pattern="^back_to_token_panel$"),
            ],
            SET_SLIPPAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_slippage_value),
                CallbackQueryHandler(handle_back_to_token_panel, pattern="^back_to_token_panel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(back_to_main_menu_and_end_conv, pattern="^back_to_main_menu$"),
            CommandHandler("start", start),
        ],
        per_message=False
    )

    pumpfun_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(pumpfun_trade_entry, pattern="^pumpfun_trade$")],
        states={
            PUMPFUN_AWAITING_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pumpfun_handle_token_address),
            ],
            PUMPFUN_AWAITING_ACTION: [
                CallbackQueryHandler(pumpfun_handle_action, pattern="^pumpfun_(buy|sell|set_slippage)$"),
                                CallbackQueryHandler(back_to_main_menu_and_end_conv, pattern="^back_to_main_menu$")
            ],
            PUMPFUN_AWAITING_BUY_AMOUNT: [
                CallbackQueryHandler(pumpfun_handle_buy_amount, pattern="^pumpfun_buy_fixed_.*$"),
                CallbackQueryHandler(pumpfun_handle_buy_amount, pattern="^pumpfun_buy_custom$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pumpfun_handle_text_buy_amount),
                CallbackQueryHandler(pumpfun_back_to_panel, pattern="^pumpfun_back_to_panel$"),
            ],
            PUMPFUN_AWAITING_SELL_PERCENTAGE: [
                CallbackQueryHandler(pumpfun_handle_sell_percentage, pattern="^pumpfun_sell_pct_.*$"),
                CallbackQueryHandler(pumpfun_back_to_panel, pattern="^pumpfun_back_to_panel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(back_to_main_menu_and_end_conv, pattern="^back_to_main_menu$"),
        ],
        per_message=False
    )

    copy_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(copy_add_start, pattern="^copy_add_wizard$")],
        states={
            COPY_AWAIT_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_leader)],
            COPY_AWAIT_RATIO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_ratio)],
            COPY_AWAIT_MAX:    [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_max)],
        },
        fallbacks=[
            CallbackQueryHandler(copy_add_cancel, pattern="^copy_menu$"),
            CallbackQueryHandler(back_to_main_menu_and_end_conv, pattern="^back_to_main_menu$"),
        ],
        per_message=False,
    )

    # --- Copy wizard conversation ---
    application.add_handler(copy_conv_handler)

    # --- Copy menu & item actions (once only) ---
    application.add_handler(CallbackQueryHandler(handle_copy_menu, pattern="^copy_menu$"))
    application.add_handler(CallbackQueryHandler(handle_copy_toggle, pattern=r"^copy_toggle:.+$"))
    application.add_handler(CallbackQueryHandler(handle_copy_remove, pattern=r"^copy_remove:.+$"))

    # --- Command & other conversations ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(trade_conv_handler)
    application.add_handler(pumpfun_conv_handler)

    # --- Other callback menus ---
    application.add_handler(CallbackQueryHandler(handle_assets, pattern="^view_assets$"))
    application.add_handler(CallbackQueryHandler(handle_assets_callbacks, pattern=r"^assets_.*$"))
    application.add_handler(CallbackQueryHandler(handle_wallet_menu, pattern="^menu_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_create_wallet_callback, pattern=r"^create_wallet:.*$"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$"))
    application.add_handler(CallbackQueryHandler(handle_import_wallet, pattern="^import_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_delete_wallet, pattern=r"^delete_wallet:solana$"))
    application.add_handler(CallbackQueryHandler(handle_send_asset, pattern="^send_asset$"))
    application.add_handler(
        CallbackQueryHandler(
            dummy_response,
            pattern=r"^(invite_friends|copy_trading|limit_order|change_language|menu_help|menu_settings)$",
        )
    )

    # --- TEXT handlers (ORDER MATTERS!) ---
    # 1) First, catch copy* commands (case-insensitive)
    application.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND & PrivateKeyFilter()),
            handle_direct_private_key_import,
        ),
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(r"(?i)^(copyadd|copyon|copyoff|copyrm)\b"),
            handle_copy_text_commands,
        )
    )
    # 2) Then, catch-all for other text, and also exclude copy* commands
    application.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) & ~filters.Regex(r"(?i)^(copyadd|copyon|copyoff|copyrm)\b"),
            handle_text_commands,
        )
    )

    # --- background worker: copy trading loop ---
    stop_event = asyncio.Event()

    async def _on_start(app: Application):
        asyncio.create_task(copytrading_loop(stop_event))
        asyncio.create_task(DexCache.loop(stop_event))

    async def _on_shutdown(app: Application):
        stop_event.set()

    application.post_init = _on_start
    application.post_shutdown = _on_shutdown

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()