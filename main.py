# file: main.py
import os
import json
import re
from dotenv import load_dotenv

import config
import database
import wallet_manager

# --- Back buttons helpers ---
from typing import Optional

def back_markup(prev_cb: Optional[str] = None) -> InlineKeyboardMarkup:
    """
    Why: ensure every message has a way to go back.
    If prev_cb is given -> show both '⬅️ Back' (prev) and '🏠 Menu'.
    Else -> only '🏠 Menu'.
    """
    rows = []
    if prev_cb:
        rows.append(InlineKeyboardButton("⬅️ Back", callback_data=prev_cb))
    rows.append(InlineKeyboardButton("🏠 Menu", callback_data="back_to_main_menu"))
    return InlineKeyboardMarkup([rows])

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

# DEX & Pump.fun via Node microservice
from services.trade_service import dex_swap, pumpfun_swap
from dex_integrations.price_aggregator import (
    get_token_price_from_raydium,
    get_token_price_from_pumpfun,
)

# ================== Init ==================
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_NATIVE_TOKEN_MINT = "So11111111111111111111111111111111111111112"
solana_client = SolanaClient(config.SOLANA_RPC_URL)

# Conversation states
AWAITING_TOKEN_ADDRESS, AWAITING_TRADE_ACTION, AWAITING_AMOUNT, PUMPFUN_AWAITING_TOKEN = range(4)


# ================== Helpers ==================
def clear_user_context(context: ContextTypes.DEFAULT_TYPE):
    """Why: avoid stale state between flows."""
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

    welcome_text = (
        f"👋 Hello {user_mention}! Welcome to <b>TradeBeat Bot</b>\n\n"
        f"Wallet address: <code>{solana_address}</code>\n"
        f"Wallet balance: <code>{sol_balance_str}</code> ($~)\n\n"
        f"🔗 Referral link: https://t.me/TradeBeatBot?start=ref_{user_id}\n\n"
        f"✅ Send a contract address to start trading."
    )
    return welcome_text


def validate_and_clean_private_key(key_data: str) -> str:
    key_data = key_data.strip()

    if key_data.startswith("["):
        # JSON array (64 ints)
        parsed = json.loads(key_data)
        if not isinstance(parsed, list):
            raise ValueError("JSON key must be a list of integers.")
        if len(parsed) != 64:
            raise ValueError("Private key must be 64 bytes.")
        return key_data
    else:
        # Try Base58 first
        try:
            import base58

            decoded = base58.b58decode(key_data)
            if len(decoded) != 64:
                raise ValueError("Private key must be 64 bytes.")
            return key_data
        except Exception as decode_error:
            # Fallback Hex → Base58
            try:
                if key_data.startswith("0x"):
                    key_data = key_data[2:]
                key_bytes = bytes.fromhex(key_data)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")

                import base58

                return base58.b58encode(key_bytes).decode()
            except Exception:
                raise ValueError(
                    f"Invalid private key format. Not valid Base58 or Hex: {decode_error}"
                )


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
            sol_balance = f"{sol_amount:.4f} SOL"
        except Exception as e:
            sol_balance = "Error"
            print(f"[Solana Balance Error] {e}")

    msg = "📊 <b>Your Asset Balances</b>\n\n"
    msg += f"Solana: <code>{solana_address or '--'}</code>\n➡️ {sol_balance}\n"

    keyboard = [
        [InlineKeyboardButton("🔁 Withdraw/Send", callback_data="send_asset")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")],
    ]

    spl_tokens = []
    try:
        if solana_address:
            spl_tokens = solana_client.get_spl_token_balances(solana_address)  # repo-original
    except Exception as e:
        print(f"[SPL Token Balance Error] {e}")

    if spl_tokens:
        msg += "\n\n🔹 <b>SPL Tokens</b>\n"
        for token in spl_tokens:
            msg += f"{token['amount']:.4f} (mint: {token['mint'][:6]}...)\n"

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
    await query.edit_message_text(
        f"Your new Solana wallet has been created and saved.\n"
        f"Public Address: `{public_address}`\n"
        f"**Private Key (SAVE EXTREMELY SECURELY):** `{private_key_output}`\n\n"
        f"Return to the main menu to view balance.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]
        ),
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]),
    )


async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().replace("\n", " ")
    command, *args = text.split(maxsplit=1)
    command = command.lower()

    clear_user_context(context)

    if command == "import":
        if len(args) == 0:
            await update.message.reply_text("❌ Invalid format. Use: `import [private_key]`", parse_mode="Markdown")
            return
        try:
            key_data = args[0].strip()
            cleaned_key = validate_and_clean_private_key(key_data)

            old_wallet = database.get_user_wallet(user_id)
            already_exists = old_wallet.get("address") is not None

            try:
                pubkey = wallet_manager.get_solana_pubkey_from_private_key_json(cleaned_key)
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid private key: {e}")
                return

            database.set_user_wallet(user_id, cleaned_key, str(pubkey))

            msg = f"✅ Solana wallet {'replaced' if already_exists else 'imported'}!\nAddress: `{pubkey}`"
            if already_exists:
                msg += "\n⚠️ Previous Solana wallet was overwritten."
            await update.message.reply_text(msg, parse_mode="Markdown")

        except ValueError as e:
            await update.message.reply_text(f"❌ Error importing Solana wallet: {e}")
        except Exception as e:
            print(f"Import error: {e}")
            await update.message.reply_text(
                "❌ Unexpected error during import. Please check your private key format."
            )
        return

    if command == "send":
        try:
            if len(args) == 0:
                await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
                return

            match = re.match(r"^(\w+)\s+([\d.]+)$", args[0].strip())
            if not match:
                await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
                return

            to_addr, amount_str = match.groups()
            amount = float(amount_str)
            if amount <= 0:
                await update.message.reply_text("❌ Amount must be greater than 0")
                return

            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text("❌ No Solana wallet found.")
                return

            tx = solana_client.send_sol(wallet["private_key"], to_addr, amount)
            if tx and not tx.lower().startswith("error"):
                solscan_link = f"https://solscan.io/tx/{tx}"
                await update.message.reply_text(
                    f"✅ Sent {amount} SOL!\nTx: [`{tx}`]({solscan_link})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(f"❌ Failed to send SOL.\n{tx}", parse_mode="Markdown")
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
        except Exception as e:
            print(f"Send error: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
        return

    if command == "sendtoken":
        try:
            if len(args) == 0:
                await update.message.reply_text(
                    "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`"
                )
                return

            parts = args[0].strip().split()
            if len(parts) != 3:
                await update.message.reply_text(
                    "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`"
                )
                return

            token_addr, to_addr, amount_str = parts
            amount = float(amount_str)
            if amount <= 0:
                await update.message.reply_text("❌ Amount must be greater than 0")
                return

            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text("❌ No Solana wallet found.")
                return

            tx = solana_client.send_spl_token(wallet["private_key"], token_addr, to_addr, amount)
            if tx and not tx.lower().startswith("error"):
                solscan_link = f"https://solscan.io/tx/{tx}"
                await update.message.reply_text(
                    f"✅ Sent {amount} SPL Token!\nTx: [`{tx}`]({solscan_link})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(f"❌ Failed to send SPL token.\n{tx}", parse_mode="Markdown")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`"
            )
        except Exception as e:
            print(f"SendToken error: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
        return

    await update.message.reply_text(
        "❌ Unrecognized command. Please use `import`, `send`, or `sendtoken`.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]),
    )


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_mention = query.from_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await query.edit_message_text(welcome_text, reply_markup=get_start_menu_keyboard(user_id), parse_mode="HTML")


async def dummy_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]
    await query.edit_message_text(
        f"🛠️ Feature `{query.data}` is under development.", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    database.delete_user_wallet(user_id)
    await query.edit_message_text(
        "🗑️ Your Solana wallet has been deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]),
    )


async def handle_send_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
    await query.message.reply_text(
        "✉️ To send assets, use format:\n"
        "`send WALLET_ADDRESS AMOUNT` for native SOL\n"
        "`sendtoken TOKEN_ADDRESS TO_WALLET_ADDRESS AMOUNT` for SPL Tokens\n\n"
        "Example:\n"
        "`send Fk...9N 0.5`\n"
        "`sendtoken EPj...V1 G8...A7 0.01`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_cancel_in_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Trade has been cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]),
        )
    elif update.message:
        await update.message.reply_text(
            "Trade has been cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]),
        )
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
        await message.reply_text("❌ Invalid token address format. Please enter a valid Solana token address.")
        return AWAITING_TOKEN_ADDRESS

    context.user_data["token_address"] = token_address

    price_data = await get_token_price_from_raydium(token_address)
    if price_data["price"] <= 0:
        price_data = await get_token_price_from_pumpfun(token_address)

    price_str = f"${price_data['price']:.8f}" if price_data["price"] > 0 else "N/A"
    market_cap_str = f"${price_data['mc']:.2f}" if price_data["mc"] != "N/A" else "N/A"

    message_text = (
        f"**Token Address:** `{token_address}`\n"
        f"**Price:** {price_str}\n"
        f"**Market Cap:** {market_cap_str}\n\n"
        "**Choose a DEX:**"
    )

    keyboard = [
        [InlineKeyboardButton("Jupiter", callback_data="trade_dex_jupiter"), InlineKeyboardButton("Raydium", callback_data="trade_dex_raydium")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_buy_sell_menu")],
    ]

    await message.reply_text(message_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_TRADE_ACTION


async def handle_trade_dex_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dex_name = query.data.split("_")[-1]
    context.user_data["selected_dex"] = dex_name
    token_address = context.user_data.get("token_address")

    message_text = (
        f"✅ You chose to trade `{token_address}` on **{dex_name.capitalize()}**.\n"
        f"Select your action:"
    )

    keyboard = [
        [InlineKeyboardButton("Buy 0.2 SOL", callback_data="buy_fixed_0.2"),
         InlineKeyboardButton("Buy 0.5 SOL", callback_data="buy_fixed_0.5"),
         InlineKeyboardButton("Buy 1 SOL", callback_data="buy_fixed_1")],
        [InlineKeyboardButton("Buy 2 SOL", callback_data="buy_fixed_2"),
         InlineKeyboardButton("Buy 5 SOL", callback_data="buy_fixed_5"),
         InlineKeyboardButton("Buy X SOL...", callback_data="buy_custom")],
        [InlineKeyboardButton("Sell 10%", callback_data="sell_pct_10"),
         InlineKeyboardButton("Sell 25%", callback_data="sell_pct_25"),
         InlineKeyboardButton("Sell 50%", callback_data="sell_pct_50"),
         InlineKeyboardButton("Sell All", callback_data="sell_pct_100")],
        [InlineKeyboardButton("Anti-MEV Buy", callback_data="anti_mev_buy"),
         InlineKeyboardButton("Anti-MEV Sell", callback_data="anti_mev_sell")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_dex_selection")],
    ]

    await query.edit_message_text(message_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
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
        keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
        await query.edit_message_text(
            "Please enter the amount of SOL you want to buy with:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return AWAITING_AMOUNT

    elif action.startswith("sell_pct_"):
        percentage_str = action.split("_")[-1]
        percentage = int(percentage_str)
        context.user_data["trade_type"] = "sell"
        context.user_data["amount_type"] = "percentage"
        await perform_trade(update, context, percentage)
        return ConversationHandler.END

    await query.message.reply_text("This action is not yet implemented.")
    return AWAITING_TRADE_ACTION


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("❌ Amount must be greater than 0.")
            return AWAITING_AMOUNT
        await perform_trade(update, context, amount)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid amount. Please enter a valid number.")
        return AWAITING_AMOUNT
    return ConversationHandler.END


async def perform_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, amount):
    message = update.message if update.message else update.callback_query.message

    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    if not wallet or not wallet.get("private_key"):
        await message.reply_text(
            "❌ No Solana wallet found. Please create or import one first.",
            reply_markup=back_markup("back_to_dex_selection"),
        )
        return

    trade_type = (context.user_data.get("trade_type") or "").lower()
    amount_type = (context.user_data.get("amount_type") or "").lower()
    token_address = context.user_data.get("token_address")
    dex = (context.user_data.get("selected_dex") or "jupiter").lower()

    if not token_address:
        await message.reply_text(
            "❌ No token mint in context. Please tap Buy/Sell, paste the token address, then choose a DEX.",
            reply_markup=back_markup("back_to_buy_sell_menu"),
        )
        return

    # decimals helper (fallback 6 when unknown)
    def _decimals_or_default(mint: str, default: int = 6) -> int:
        try:
            if hasattr(solana_client, "get_token_decimals"):
                d = solana_client.get_token_decimals(mint)  # optional in your client
                if isinstance(d, int) and 0 <= d <= 18:
                    return d
        except Exception:
            pass
        return default

    if trade_type == "buy":
        input_mint = SOLANA_NATIVE_TOKEN_MINT
        output_mint = token_address
        amount_lamports = int(float(amount) * 1_000_000_000)  # SOL -> lamports
    else:
        input_mint = token_address
        output_mint = SOLANA_NATIVE_TOKEN_MINT
        if amount_type == "percentage":
            decimals = _decimals_or_default(token_address, 6)
            spl_tokens = solana_client.get_spl_token_balances(wallet["address"])
            token_balance = next((t["amount"] for t in spl_tokens if t["mint"] == token_address), 0)
            if token_balance <= 0:
                await message.reply_text(
                    f"❌ Insufficient balance for token `{token_address}`.",
                    reply_markup=back_markup("back_to_dex_selection"),
                )
                return
            amount_to_sell = float(token_balance) * (float(amount) / 100.0)
            amount_lamports = int(amount_to_sell * (10 ** decimals))
        else:
            decimals = _decimals_or_default(token_address, 6)
            amount_lamports = int(float(amount) * (10 ** decimals))

    dex_label = "Raydium (via Jupiter direct route)" if dex == "raydium" else "Jupiter"
    await message.reply_text(
        f"⏳ Performing {trade_type} on `{token_address}` via {dex_label} ...",
        reply_markup=back_markup("back_to_dex_selection"),
    )

    try:
        res = await dex_swap(
            private_key=wallet["private_key"],
            input_mint=input_mint,
            output_mint=output_mint,
            amount_lamports=amount_lamports,
            dex=dex,
            slippage_bps=50,
            priority_fee_sol=0.0,
        )
    except Exception as e:
        await message.reply_text(
            f"❌ Swap failed: {e}",
            reply_markup=back_markup("back_to_dex_selection"),
        )
        clear_user_context(context)
        return ConversationHandler.END

    if isinstance(res, dict) and res.get("signature"):
        await message.reply_text(
            f"✅ Swap successful! View: https://explorer.solana.com/tx/{res['signature']}",
            disable_web_page_preview=True,
            reply_markup=back_markup("back_to_dex_selection"),
        )
    else:
        err = res.get("error") if isinstance(res, dict) else res
        await message.reply_text(
            f"❌ Swap failed: {err}",
            reply_markup=back_markup("back_to_dex_selection"),
        )

    clear_user_context(context)
    await message.reply_text(
        "Done! What's next?",
        reply_markup=back_markup(None),
    )
    return ConversationHandler.END




async def handle_back_to_buy_sell_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
    await query.edit_message_text(
        "📄 Please send the **token contract address** you want to trade.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return AWAITING_TOKEN_ADDRESS


async def handle_back_to_dex_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    token_address = context.user_data.get("token_address")

    price_data = await get_token_price_from_raydium(token_address)
    if price_data["price"] <= 0:
        price_data = await get_token_price_from_pumpfun(token_address)

    price_str = f"${price_data['price']:.8f}" if price_data["price"] > 0 else "N/A"
    market_cap_str = f"${price_data['mc']:.2f}" if price_data["mc"] != "N/A" else "N/A"

    message_text = (
        f"**Token Address:** `{token_address}`\n"
        f"**Price:** {price_str}\n"
        f"**Market Cap:** {market_cap_str}\n\n"
        "**Choose a DEX:**"
    )

    keyboard = [
        [InlineKeyboardButton("Jupiter", callback_data="trade_dex_jupiter"), InlineKeyboardButton("Raydium", callback_data="trade_dex_raydium")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_buy_sell_menu")],
    ]

    await query.edit_message_text(message_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_TRADE_ACTION


async def handle_pumpfun_trade_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
    await query.edit_message_text(
        "🤖 **Pump.fun Auto Trade**\n\n"
        "Please send the **token mint address** you want to auto trade.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PUMPFUN_AWAITING_TOKEN


async def handle_pumpfun_trade_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token_address = update.message.text.strip()

    if len(token_address) < 32 or len(token_address) > 44:
        await update.message.reply_text("❌ Invalid token address format. Please enter a valid Solana token address.")
        return PUMPFUN_AWAITING_TOKEN

    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    if not wallet or not wallet["private_key"]:
        await update.message.reply_text("❌ No Solana wallet found. Please create or import one first.")
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Performing a Pump.fun BUY for `{token_address}` using 0.1 SOL ...")

    res = await pumpfun_swap(
        private_key=wallet["private_key"],
        action="buy",
        mint=token_address,
        amount=0.1,
        use_jito=False,
    )

    if isinstance(res, dict) and not res.get("error") and (res.get("signature") or res.get("bundle")):
        if res.get("signature"):
            await update.message.reply_text(
                f"✅ Pump.fun buy successful! View: https://explorer.solana.com/tx/{res['signature']}",
                disable_web_page_preview=True,
            )
        elif res.get("bundle"):
            await update.message.reply_text("✅ Pump.fun bundle submitted to Jito (no signature yet).")
    else:
        await update.message.reply_text(f"❌ Pump.fun buy failed.\n{res}")

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
                CallbackQueryHandler(handle_trade_dex_selection, pattern=r"^trade_dex_.*$"),
                CallbackQueryHandler(handle_buy_sell_action, pattern="^(buy_.*|sell_.*|anti_mev_.*)"),
                CallbackQueryHandler(handle_back_to_buy_sell_menu, pattern="^back_to_buy_sell_menu$"),
                CallbackQueryHandler(handle_back_to_dex_selection, pattern="^back_to_dex_selection$"),
            ],
            AWAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount),
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
