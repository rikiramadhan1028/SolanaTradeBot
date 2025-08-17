# -*- coding: utf-8 -*-
# main.py — RokuTrade Bot (secure revision)

import os
import json
import re
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional

# -------- env harus loaded SEBELUM os.getenv dipanggil --------
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
) = range(5)

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
    except Exception: # noqa
        return False

# hitung fee UI dari nilai UI
def _fee_ui(val_ui: float) -> float:
    try:
        return max(0.0, float(val_ui) * (FEE_BPS / 10000.0))
    except Exception:
        return 0.0

# pastikan fee yang dikirim tidak mengganggu biaya jaringan
def _safe_fee_amount_ui(total_ui: float, want_fee_ui: float, *, keep_buffer_ui: float = 0.002) -> float:
    # sisakan buffer untuk biaya jaringan & ATA (±0.002 SOL aman)
    max_send = max(0.0, float(total_ui) - float(keep_buffer_ui))
    return max(0.0, min(float(want_fee_ui), max_send))

async def _send_fee_sol_if_any(private_key: str, have_ui: float, gain_ui: float, message, reason: str):
    """
    - BUY : panggil dengan have_ui = amount SOL sebelum swap, gain_ui = amount SOL yang user input
    - SELL: panggil setelah swap; have_ui = post-swap SOL balance, gain_ui = (post - pre)
    """
    if not FEE_ENABLED:
        return None
    if not _is_valid_pubkey(FEE_WALLET):
        await reply_err_html(message, "⚠️ FEE_WALLET invalid; fee skipped.", prev_cb="back_to_token_panel")
        return None

    want = _fee_ui(gain_ui)
    fee_ui = _safe_fee_amount_ui(have_ui, want)
    if fee_ui <= 0.00001:  # hindari dust
        return None

    tx = solana_client.send_sol(private_key, FEE_WALLET, fee_ui)
    if isinstance(tx, str) and not tx.lower().startswith("error"):
        # info tambahan ke user; swap tetap lanjut walaupun ini gagal
        await reply_ok_html(message, f"💸 Platform fee ({reason}): {fee_ui:.6f} SOL sent.", signature=tx)
        return tx
    else:
        await reply_err_html(message, f"⚠️ Fee transfer failed: {tx}", prev_cb="back_to_token_panel")
        return None


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
            # pilih pair ber-liquidity tertinggi
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
            InlineKeyboardButton("📋 Copy Trading", callback_data="copy_trading"),
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
    wallet_info = database.get_user_wallet(user_id)
    solana_address = wallet_info.get("address", "--")
    sol_balance_str = "--"
    if solana_address and solana_address != "--":
        try:
            sol_balance = solana_client.get_balance(solana_address)
            sol_balance_str = f"{sol_balance:.4f} SOL"
        except Exception:
            sol_balance_str = "Error"
    return (
        f"👋 Hello {user_mention}! Welcome to <b>RokuTrade</b>\n\n"
        f"Wallet address: <code>{solana_address}</code>\n"
        f"Wallet balance: <code>{sol_balance_str}</code> ($~)\n\n"
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
        InlineKeyboardButton("🏠 Menu",  callback_data="back_to_main_menu"),
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
            bal = solana_client.get_balance(addr)
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
    lines.append(f"• Price: {price_text}  LP: {lp_text}  MC: {mc_text}")
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

    if solana_address:
        try:
            sol_amount = solana_client.get_balance(solana_address)
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

    # Pakai Node/web3.js agar Token-2022 juga kebaca
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
    # ⚠️ Tampilkan PK agar user bisa backup, tapi beri peringatan tegas
    await query.edit_message_text(
        "🔐 <b>New Solana wallet created & saved.</b>\n"
        f"Address:\n<code>{public_address}</code>\n\n"
        "⚠️ <b>Private Key (BACKUP & JANGAN BAGIKAN):</b>\n"
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

    # slippage flow teks
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
    # kembali ke mode input mint
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
        [InlineKeyboardButton("✈️ Copy Trade", callback_data="dummy_copy_trade")],
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
    tx = solana_client.send_sol(private_key, FEE_WALLET, fee_ui)
    if isinstance(tx, str) and not tx.lower().startswith("error"):
        await reply_ok_html(message, f"💸 Platform fee ({reason}): {fee_ui:.6f} SOL sent.", signature=tx)
        return tx
    else:
        # jangan gagalkan swap hanya karena fee tx gagal
        await reply_err_html(message, f"⚠️ Fee transfer failed: {tx}", prev_cb="back_to_token_panel")
        return None

# ------------------------- Trade core -------------------------
async def perform_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, amount):
    """
    - BUY  : SOL -> token (fee diambil dari input SOL sebelum swap)
    - SELL : token -> SOL (fee diambil dari hasil SOL sesudah swap)
    - Balances/decimals via trade-svc (web3.js) agar akurat.
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

    trade_type   = (context.user_data.get("trade_type") or "").lower()          # "buy" | "sell"
    amount_type  = (context.user_data.get("amount_type") or "").lower()         # "sol" | "percentage"
    token_mint   = context.user_data.get("token_address")                       # mint string
    dex          = "jupiter"
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
    if trade_type == "buy": # noqa
        input_mint       = SOLANA_NATIVE_TOKEN_MINT
        output_mint      = token_mint
        slippage_bps     = buy_slip_bps

        # total SOL yang di-input user
        amount_ui = float(amount)

        # cek saldo SOL awal (friendly)
        try:
            sol_ui_before = await svc_get_sol_balance(wallet["address"])
        except Exception:
            sol_ui_before = 0.0

        if FEE_ENABLED:
            # kirim fee dulu dari SOL sebelum swap
            await _send_fee_sol_if_any(
                private_key=wallet["private_key"],
                have_ui=sol_ui_before,
                gain_ui=amount_ui,
                message=message,
                reason="BUY"
            )

            # refresh saldo setelah fee
            try:
                sol_ui_before = await svc_get_sol_balance(wallet["address"])
            except Exception:
                pass

        # sisakan buffer jaringan ~0.002 SOL
        buffer_ui = 0.002
        if sol_ui_before + 1e-9 < amount_ui + buffer_ui:
            await reply_err_html(
                message,
                f"❌ Not enough SOL. Need ~{(amount_ui + buffer_ui):.4f} SOL (amount + fees), you have {sol_ui_before:.4f} SOL.",
                prev_cb="back_to_token_panel",
            )
            return

        amount_lamports = int(amount_ui * 1_000_000_000)

# ===================== SELL =====================
    else: # noqa
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
        else:
            sell_ui = float(amount)
            if sell_ui > token_balance_ui + 1e-12:
                await reply_err_html(
                    message,
                    "❌ Amount exceeds wallet balance.",
                    prev_cb="back_to_token_panel",
                )
                return

        amount_lamports = int(sell_ui * (10 ** decimals))

        # catat SOL sebelum swap (untuk fee post-swap)
        pre_sol_ui = 0.0
        if FEE_ENABLED:
            try:
                pre_sol_ui = await svc_get_sol_balance(wallet["address"])
            except Exception:
                pre_sol_ui = 0.0



    # Feedback ke user saat eksekusi
    await reply_ok_html(
        message,
        f"⏳ Performing {trade_type} on `{token_mint}` …",
        prev_cb="back_to_token_panel",
    )

    # ===================== Call trade-svc (Jupiter) =====================
    try:
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
        return ConversationHandler.END

    if isinstance(res, dict) and res.get("signature"):
        sig = res["signature"]
        await reply_ok_html(
            message,
            "✅ Swap successful!",
            prev_cb="back_to_token_panel",
            signature=sig,
        )
        # SELL fee post-swap (setelah dapat res signature berhasil)
        if trade_type != "buy" and FEE_ENABLED:
            try:
                await asyncio.sleep(1.5)  # beri waktu balance settle
                post_sol_ui = await svc_get_sol_balance(wallet["address"])
                delta_ui = max(0.0, post_sol_ui - pre_sol_ui)
                if delta_ui > 0:
                    # kirim fee dari hasil SOL yang baru masuk
                    await _send_fee_sol_if_any(
                        private_key=wallet["private_key"],
                        have_ui=post_sol_ui,   # saldo saat ini
                        gain_ui=delta_ui,      # hanya dari hasil swap
                        message=message,
                        reason="SELL"
                    )
            except Exception as e:
                await reply_err_html(message, f"⚠️ Fee check failed: {e}", prev_cb="back_to_token_panel")

    else:
        err = res.get("error") if isinstance(res, dict) else res
        await reply_err_html(message, f"❌ Swap failed: {short_err_text(str(err))}", prev_cb="back_to_token_panel")

    clear_user_context(context)
    await reply_ok_html(message, "Done! What's next?", prev_cb=None)
    return ConversationHandler.END

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
            # The reply_markup here should be the token_panel_keyboard, not back_markup
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

# ----- Pump.fun flow -----
async def handle_pumpfun_trade_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🤖 <b>Pump.fun Auto Trade</b>\n\n"
        "Please send the <b>token mint address</b> you want to auto trade.",
        parse_mode="HTML",
        reply_markup=back_markup("back_to_main_menu"),
    )
    return PUMPFUN_AWAITING_TOKEN

async def handle_pumpfun_trade_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token_address = update.message.text.strip()

    if len(token_address) < 32 or len(token_address) > 44:
        await update.message.reply_text(
            "❌ Invalid token address format. Please enter a valid Solana token address.",
            reply_markup=back_markup("pumpfun_trade"),
        )
        return PUMPFUN_AWAITING_TOKEN

    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    if not wallet or not wallet["private_key"]:
        await update.message.reply_text(
            "❌ No Solana wallet found. Please create or import one first.",
            reply_markup=back_markup("back_to_main_menu"),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"⏳ Performing a Pump.fun BUY for `{token_address}` using 0.1 SOL ...",
        reply_markup=back_markup("pumpfun_trade"),
    )

    res = await pumpfun_swap(
        private_key=wallet["private_key"],
        action="buy",
        mint=token_address,
        amount=0.1,
        use_jito=False,
    )

    if isinstance(res, dict) and not res.get("error") and (res.get("signature") or res.get("bundle")):
        if res.get("signature"):
            sig = res["signature"]
            await reply_ok_html(
                update.message,
                "✅ Pump.fun buy successful!",
                prev_cb="back_to_main_menu",
                signature=sig,
            )
        elif res.get("bundle"):
            await reply_ok_html(update.message, "✅ Pump.fun bundle submitted to Jito.", prev_cb="back_to_main_menu")
    else:
        err = res.get("error") if isinstance(res, dict) else res
        await reply_err_html(update.message, f"❌ Pump.fun buy failed: {short_err_text(str(err))}", prev_cb="back_to_main_menu")

    clear_user_context(context)
    return ConversationHandler.END

# ================== App bootstrap ==================
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    trade_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_sell, pattern="^buy_sell$"),
            CallbackQueryHandler(handle_pumpfun_trade_entry, pattern="^pumpfun_trade$"),
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
            PUMPFUN_AWAITING_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pumpfun_trade_token),
                CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
            CommandHandler("start", start),
        ],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(trade_conv_handler)
    application.add_handler(CallbackQueryHandler(handle_assets, pattern="^view_assets$"))
    application.add_handler(CallbackQueryHandler(handle_wallet_menu, pattern="^menu_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_create_wallet_callback, pattern=r"^create_wallet:.*$"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$"))
    application.add_handler(CallbackQueryHandler(handle_import_wallet, pattern="^import_wallet$"))
    application.add_handler(
        CallbackQueryHandler(
            dummy_response, pattern=r"^(invite_friends|copy_trading|limit_order|change_language|menu_help|menu_settings)$"
        )
    )
    application.add_handler(CallbackQueryHandler(handle_delete_wallet, pattern=r"^delete_wallet:solana$"))
    application.add_handler(CallbackQueryHandler(handle_send_asset, pattern="^send_asset$"))
    # Catch-all text commands after conversations
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
