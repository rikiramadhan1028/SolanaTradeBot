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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

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
    if not addr or not (32 <= len(addr) <= 44):
        return False
    try:
        import base58
        base58.b58decode(addr)
        return True
    except Exception:
        return False

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

# ---- Realtime SOL/USD price ----
async def get_sol_price_usd() -> float:
    """Fetch real-time SOL/USD price via Jupiter (fallback Dexscreener)."""
    try:
        p = await get_token_price(SOLANA_NATIVE_TOKEN_MINT)
        price = float((p or {}).get("price", 0) if isinstance(p, dict) else p or 0)
        if price > 0:
            return price
    except Exception:
        pass
    try:
        ds = await get_dexscreener_stats(SOLANA_NATIVE_TOKEN_MINT)
        return float(ds.get("priceUsd") or 0) if ds else 0.0
    except Exception:
        return 0.0

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

# ================== Token Panel (no DEX selection) ==================
def token_panel_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    buy_bps = int(context.user_data.get("slippage_bps_buy", 500))   # default 5%
    sell_bps = int(context.user_data.get("slippage_bps_sell", 500)) # default 5%
    kb: list[list[InlineKeyboardButton]] = []
    kb.append([
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_buy_sell_menu"),
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
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_buy_sell_menu"),
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
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    wallet_info = database.get_user_wallet(user_id)
    solana_address = wallet_info.get("address")
    sol_balance = "N/A"

    # also display real-time SOL + USD in assets
    if solana_address:
        try:
            sol_amount = await svc_get_sol_balance(solana_address)
            sol_price  = await get_sol_price_usd()
            if sol_price > 0:
                sol_balance = f"{sol_amount:.6f} SOL  ({format_usd(sol_amount * sol_price)})"
            else:
                sol_balance = f"{sol_amount:.6f} SOL"
        except Exception as e:
            sol_balance = "Error"
            print(f"[Solana Balance Error] {e}")

    msg = "📊 <b>Your Asset Balances</b>\n\n"
    msg += f"Solana: <code>{solana_address or '--'}</code>\n<b>↳ {sol_balance}</b>\n"

    keyboard = [
        [InlineKeyboardButton("🔁 Withdraw/Send", callback_data="send_asset")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")],
    ]

    # Use Node/web3.js so Token-2022 is also read
    try:
        tokens = await svc_get_token_balances(solana_address, min_amount=0.0000001) if solana_address else []
    except Exception as e:
        print(f"[svc tokens] {e}")
        tokens = []

    async def fetch_token_meta(mint: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=8.0) as s:
                r = await s.get(f"{TRADE_SVC_URL}/meta/token/{mint}")
            if r.status_code == 200:
                return r.json() or {}
        except Exception as e:
            print(f"[meta] fetch error for {mint}: {e}")
        return {}

    def format_token_label(meta: dict, mint: str) -> str:
        sym = (meta.get("symbol") or "").strip()
        name = (meta.get("name") or "").strip()
        if sym:
            return sym
        if name:
            return name
        return mint[:6].upper()

    if tokens:
        msg += "\n\n🔹 <b>SPL Tokens</b>\n"
        mints = [t.get("mint") or t.get("mintAddress") for t in tokens if (t.get("amount") or 0) > 0]
        metas = await asyncio.gather(*(fetch_token_meta(m) for m in mints), return_exceptions=True)
        for t, meta in zip(tokens, metas):
            if (t.get("amount") or 0) <= 0:
                continue
            label = format_token_label(meta if isinstance(meta, dict) else {}, t.get("mint") or t.get("mintAddress"))
            msg += f"{float(t.get('amount',0)):.6f} <b>{label}</b>\n"

    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
    fake_cb = Update(update.update_id, callback_query=update.to_dict().get("callback_query"))
    await handle_copy_menu(update, context)  # or just let the user click Back
    return ConversationHandler.END

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

async def handle_cancel_in_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Trade has been cancelled.", reply_markup=back_markup("back_to_main_menu"))
    elif update.message:
        await update.message.reply_text("Trade has been cancelled.", reply_markup=back_markup("back_to_main_menu"))
    return ConversationHandler.END

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

    if len(token_address) < 32 or len(token_address) > 44:
        await message.reply_text(
            "❌ Invalid token address format. Please enter a valid Solana token address.",
            reply_markup=back_markup("back_to_buy_sell_menu"),
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

async def _send_fee_sol_if_any(private_key: str, ui_amount: float, message, reason: str):
    if not FEE_ENABLED:
        return None
    fee_ui = _fee_ui(ui_amount)
    if fee_ui <= 0.00001:  # minimum to avoid dust/gas wasting
        return None
        
    print(f"Attempting to send {fee_ui:.6f} SOL fee ({reason}) to {FEE_WALLET}")
    tx = solana_client.send_sol(private_key, FEE_WALLET, fee_ui)
    
    if isinstance(tx, str) and not tx.lower().startswith("error"):
        # Message to user has been removed, we only print to log
        print(f"✅ Platform fee successful. Signature: {tx}")
        return tx
    else:
        # Error message to user has also been removed
        print(f"⚠️ Platform fee transfer failed: {tx}")
        return None

# ------------------------- Trade core -------------------------
async def perform_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, amount):
    """
    - BUY  : SOL -> token (fee is taken from the input SOL before the swap)
    - SELL : token -> SOL (fee is taken from the resulting SOL after the swap)
    - Balances/decimals via trade-svc (web3.js) for accuracy.
    """
    message = update.message if update.message else update.callback_query.message

    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    if not wallet or not wallet.get("private_key") or not wallet.get("address"):
        await reply_err_html(
            message,
            "❌ No Solana wallet found. Please create or import one first.",
            prev_cb="back_to_token_panel",
        )
        return

    trade_type   = (context.user_data.get("trade_type") or "").lower()      # "buy" | "sell"
    amount_type  = (context.user_data.get("amount_type") or "").lower()     # "sol" | "percentage"
    token_mint   = context.user_data.get("token_address")                   # mint string
    dex          = context.user_data.get("selected_dex", "jupiter")
    buy_slip_bps = int(context.user_data.get("slippage_bps_buy",  500))
    sel_slip_bps = int(context.user_data.get("slippage_bps_sell", 500))

    if not token_mint:
        await reply_err_html(
            message,
            "❌ No token mint in context. Please go back and send a token address again.",
            prev_cb="back_to_buy_sell_menu",
        )
        return

    SOL_MINT = SOLANA_NATIVE_TOKEN_MINT

    # ===================== BUY =====================
    if trade_type == "buy":
        input_mint = SOLANA_NATIVE_TOKEN_MINT
        output_mint = token_mint
        slippage_bps = buy_slip_bps

        # Total SOL amount entered by the user
        total_sol_to_spend = float(amount)
        
        # Calculate fee first
        fee_amount_ui = _fee_ui(total_sol_to_spend) if FEE_ENABLED else 0.0
        
        # The actual SOL amount for the swap is the total minus the fee
        actual_swap_amount_ui = total_sol_to_spend - fee_amount_ui

        # Check balance before sending the fee
        try:
            sol_balance = await svc_get_sol_balance(wallet["address"])
        except Exception:
            sol_balance = 0.0

        buffer_ui = 0.002 # For gas fees
        if sol_balance < total_sol_to_spend + buffer_ui:
            await reply_err_html(
                message,
                f"❌ Not enough SOL. Need ~{(total_sol_to_spend + buffer_ui):.4f} SOL (amount + fees), you have {sol_balance:.4f} SOL.",
                prev_cb="back_to_token_panel",
            )
            return

        # Send fee if applicable
        if FEE_ENABLED and fee_amount_ui > 0:
            await _send_fee_sol_if_any(wallet["private_key"], total_sol_to_spend, message, "BUY")

        # Amount to swap in lamports
        amount_lamports = int(actual_swap_amount_ui * 1_000_000_000)

    # ===================== SELL =====================
    else: # trade_type == "sell"
        input_mint = token_mint
        output_mint = SOLANA_NATIVE_TOKEN_MINT
        slippage_bps = sel_slip_bps
    
        try:
            decimals = int(await svc_get_mint_decimals(token_mint))
        except Exception:
            decimals = 6
    
        try:
            token_balance_ui = float(await svc_get_token_balance(wallet["address"], token_mint))
        except Exception:
            token_balance_ui = 0.0

        if amount_type == "percentage":
            if token_balance_ui <= 0:
                await reply_err_html(
                    message,
                    f"❌ Insufficient balance for token `{token_mint}`.",
                    prev_cb="back_to_token_panel",
                )
                return
            sell_ui = token_balance_ui * (float(amount) / 100.0)
        else: # amount_type is 'sol' for Pump.fun, but not used for sell. this is for custom amount in tokens.
            sell_ui = float(amount)
            if sell_ui > token_balance_ui + 1e-12:
                await reply_err_html(
                    message,
                    "❌ Amount exceeds wallet balance.",
                    prev_cb="back_to_token_panel",
                )
                return
    
        amount_lamports = int(sell_ui * (10 ** decimals))
    
        pre_sol_ui = 0.0
        if FEE_ENABLED:
            try:
                pre_sol_ui = await svc_get_sol_balance(wallet["address"])
            except Exception:
                pre_sol_ui = 0.0

    # Feedback to the user during execution
    await reply_ok_html(
        message,
        f"⏳ Performing {trade_type} on `{token_mint}` via {dex.capitalize()}…",
        prev_cb="back_to_token_panel",
    )

    # ===================== Call trade-svc =====================
    try:
        if dex == "pumpfun":
            res = await pumpfun_swap(
                private_key=wallet["private_key"],
                action=trade_type, # 'buy' or 'sell'
                mint=token_mint,
                amount=amount,  # SOL amount for buy, percentage for sell
                slippage_bps=slippage_bps,
                use_jito=False,
            )
        else: # Default to Jupiter
            res = await dex_swap(
                private_key=wallet["private_key"],
                input_mint=input_mint,
                output_mint=output_mint,
                amount_lamports=amount_lamports,
                dex=dex,
                slippage_bps=slippage_bps,
                priority_fee_sol=0.0,
            )
    except Exception as e:
        await reply_err_html(message, f"❌ Swap failed: {short_err_text(str(e))}", prev_cb="back_to_token_panel")
        clear_user_context(context)
        # Do not return ConversationHandler.END here to avoid errors when called outside a conversation
        return

    if isinstance(res, dict) and (res.get("signature") or res.get("bundle")):
        sig = res.get("signature") or res.get("bundle")
        await reply_ok_html(
            message,
            "✅ Swap successful!",
            prev_cb="back_to_token_panel",
            signature=sig,
        )
        # SELL fee post-swap
        if trade_type != "buy" and FEE_ENABLED:
            try:
                await asyncio.sleep(1.5)
                post_sol_ui = await svc_get_sol_balance(wallet["address"])
                delta_ui = max(0.0, post_sol_ui - pre_sol_ui)
                if delta_ui > 0:
                    await _send_fee_sol_if_any(wallet["private_key"], delta_ui, message, "SELL")
            except Exception as e:
                await reply_err_html(message, f"⚠️ Fee check failed: {e}", prev_cb="back_to_token_panel")
    else:
        err = res.get("error") if isinstance(res, dict) else res
        await reply_err_html(message, f"❌ Swap failed: {short_err_text(str(err))}", prev_cb="back_to_token_panel")

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
        ],
        states={
            AWAITING_TOKEN_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_address_for_trade),
                CallbackQueryHandler(handle_dummy_trade_buttons, pattern=r"^(dummy_.*)$"),
                CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
            ],
            AWAITING_TRADE_ACTION: [
                CallbackQueryHandler(handle_buy_sell_action, pattern="^(buy_.*|sell_.*)$"),
                CallbackQueryHandler(handle_back_to_buy_sell_menu, pattern="^back_to_buy_sell_menu$"),
                CallbackQueryHandler(handle_back_to_token_panel, pattern="^back_to_token_panel$"),
                CallbackQueryHandler(handle_refresh_token_panel, pattern="^token_panel_refresh$"),
                CallbackQueryHandler(handle_set_slippage_entry, pattern="^set_(buy|sell)_slippage$"),
                CallbackQueryHandler(handle_noop, pattern="^noop_.*$"),
                CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
            ],
            AWAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount),
            ],
            SET_SLIPPAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_slippage_value),
                CallbackQueryHandler(handle_back_to_token_panel, pattern="^back_to_token_panel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
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
                CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$")
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
            CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
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
            CallbackQueryHandler(handle_copy_menu, pattern="^copy_menu$"),
            CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
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

    async def _on_shutdown(app: Application):
        stop_event.set()

    application.post_init = _on_start
    application.post_shutdown = _on_shutdown

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()