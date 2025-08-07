# main.py
import os
import json
import config
import database
import wallet_manager
from blockchain_clients.solana_client import SolanaClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from dotenv import load_dotenv
import re
import asyncio

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

solana_client = SolanaClient(config.SOLANA_RPC_URL)

def get_current_multichain_status() -> bool:
    return database.get_setting("MULTICHAIN_ENABLED") == "True"

def get_start_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("⚡ Import Wallet", callback_data="import_wallet"),
         InlineKeyboardButton("🏆 Invite Friends", callback_data="invite_friends")],
        [InlineKeyboardButton("💰 Buy/Sell", callback_data="buy_sell"),
         InlineKeyboardButton("🧾 Asset", callback_data="view_assets")],
        [InlineKeyboardButton("📋 Copy Trading", callback_data="copy_trading"),
         InlineKeyboardButton("📉 Limit Order", callback_data="limit_order")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
         InlineKeyboardButton("👛 Wallet", callback_data="menu_wallet")],
        [InlineKeyboardButton("🌐 Language", callback_data="change_language"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_dynamic_start_message_text(user_id: int, user_mention: str) -> str:
    wallet_info = database.get_user_wallet(user_id)
    solana_address = wallet_info.get("address", "--")
    sol_balance_str = "--"

    if solana_address != "--":
        try:
            sol_balance = solana_client.get_balance(solana_address)
            sol_balance_str = f"{sol_balance:.4f} SOL"
        except Exception as e:
            sol_balance_str = "Error"
            print(f"Error fetching Solana balance for user {user_id}: {e}")

    welcome_text = (
        f"👋 Hello {user_mention}! Welcome to <b>TradeBeat Bot</b>\n\n"
        f"Wallet address: <code>{solana_address}</code>\n"
        f"Wallet balance: `{sol_balance_str}` ($~)\n\n"
        f"🔗 Referral link: https://t.me/TradeBeatBot?start=ref_{user_id}\n\n"
        f"✅ Send contract address to start trading. Please follow official accounts for more info and help."
    )
    return welcome_text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_mention = update.effective_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await update.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    is_enabled = get_current_multichain_status()
    status_text = "ENABLED" if is_enabled else "DISABLED"
    keyboard = [
        [InlineKeyboardButton(f"Multichain: {status_text}", callback_data=f"toggle_multichain:{'disable' if is_enabled else 'enable'}")],
        [InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]
    ]
    await query.edit_message_text("Bot Settings:", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_multichain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    admin_user_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    if query.from_user.id != admin_user_id:
        await query.edit_message_text("You do not have permission to do this.")
        return
    action = query.data.split(":")[1]
    new_status = "True" if action == "enable" else "False"
    database.set_setting("MULTICHAIN_ENABLED", new_status)
    status_text = "ENABLED" if new_status == "True" else "DISABLED"
    keyboard = [
        [InlineKeyboardButton(f"Multichain: {status_text}", callback_data=f"toggle_multichain:{'disable' if new_status == 'True' else 'enable'}")],
        [InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]
    ]
    await query.edit_message_text(f"Multichain system has been {status_text.lower()}.\nBot Settings:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_buy_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]
    await query.edit_message_text("Please enter the token contract address to start trading.", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    msg = f"📊 <b>Your Asset Balances</b>\n\n"
    msg += f"Solana: <code>{solana_address or '--'}</code>\n➡️ {sol_balance}\n"
    
    keyboard = [
        [InlineKeyboardButton("🔁 Withdraw/Send", callback_data="send_asset")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]
    ]
    spl_tokens = []
    try:
        if solana_address:
            spl_tokens = solana_client.get_spl_token_balances(solana_address)
    except Exception as e:
        print(f"[SPL Token Balance Error] {e}")

    if spl_tokens:
        msg += "\n\n🔹 <b>SPL Tokens</b>\n"
        for token in spl_tokens:
            msg += f"{token['amount']:.4f} (mint: {token['mint'][:6]}...)\n"

    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard_buttons = []
    keyboard_buttons.append([InlineKeyboardButton(f"Create Solana Wallet", callback_data=f"create_wallet:solana"),
                             InlineKeyboardButton(f"🗑️ Delete", callback_data=f"delete_wallet:solana")])
    keyboard_buttons.append([InlineKeyboardButton("Import Wallet", callback_data="import_wallet")])
    keyboard_buttons.append([InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")])
    await query.edit_message_text("Wallet Options:", reply_markup=InlineKeyboardMarkup(keyboard_buttons))

async def handle_create_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    private_key_output, public_address = wallet_manager.create_solana_wallet()
    database.set_user_wallet(user_id, private_key_output, public_address)
    await query.edit_message_text(f"Your new Solana wallet has been created and saved.\n"
                                  f"Public Address: `{public_address}`\n"
                                  f"**Private Key (SAVE EXTREMELY SECURELY):** `{private_key_output}`\n\n"
                                  f"To view your balance, please return to the main menu.",
                                  parse_mode='Markdown',
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]])
    )

async def handle_import_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🔐 Please send your private key in the format:\n"
        "`import [private_key]`\n\n"
        "Supported formats: **JSON array**, **Base58 string**\n"
        "Example: `import 3WbX...`\n\n"
        "⚠️ Keep it private and safe!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]
        ])
    )

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().replace('\n', ' ')
    
    if text.lower() == "cancel":
        user_mention = update.effective_user.mention_html()
        welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
        await update.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))
        return

    command, *args = text.split()
    command = command.lower()
    
    if command == "import":
        try:
            key_data = args[0]
            old_wallet = database.get_user_wallet(user_id)
            already_exists = old_wallet.get("address") is not None
            
            pubkey = wallet_manager.get_solana_pubkey_from_private_key_json(key_data)
            database.set_user_wallet(user_id, key_data, str(pubkey))
            
            msg = f"✅ Solana wallet {'replaced' if already_exists else 'imported'}!\nAddress: `{pubkey}`"
            if already_exists: msg += "\n⚠️ Previous Solana wallet was overwritten."
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except IndexError:
            await update.message.reply_text("❌ Invalid format. Use `import [private_key]`")
        except ValueError as e:
            await update.message.reply_text(f"❌ Error importing Solana wallet: {e}")
        except Exception as e:
            await update.message.reply_text(f"❌ An unexpected error occurred: {e}")
        return

    if command == "send":
        try:
            to_addr, amount_str = args
            amount = float(amount_str)
            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text("❌ No Solana wallet found.")
                return
            
            tx = solana_client.send_sol(wallet["private_key"], to_addr, amount)
            if tx:
                await update.message.reply_text(f"✅ Sent {amount} SOL!\nTx: `{tx}`", parse_mode='Markdown')
            else:
                 await update.message.reply_text("❌ Failed to send SOL. Please check your balance or recipient address.")
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        return
        
    if command == "sendtoken":
        try:
            token_addr, to_addr, amount_str = args
            amount = float(amount_str)
            wallet = database.get_user_wallet(user_id)
            if not wallet or not wallet["private_key"]:
                await update.message.reply_text("❌ No Solana wallet found.")
                return
            
            tx = solana_client.send_spl_token(wallet["private_key"], token_addr, to_addr, amount)
            if tx:
                await update.message.reply_text(f"✅ Sent {amount} SPL Token!\nTx: `{tx}`", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Failed to send SPL token.")
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        return

    # Fallback for unrecognized text commands
    await update.message.reply_text("❌ Unrecognized command. Please use `import`, `send`, or `sendtoken`.")
    
async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_mention = query.from_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await query.edit_message_text(welcome_text, reply_markup=get_start_menu_keyboard(user_id), parse_mode='HTML')

async def dummy_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]
    await query.edit_message_text(f"🛠️ Feature `{query.data}` is under development.", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    database.delete_user_wallet(user_id)
    await query.edit_message_text(f"🗑️ Your Solana wallet has been deleted.",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]))

async def handle_send_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env file")
        return
    if not os.getenv("ADMIN_TELEGRAM_ID"):
        print("Warning: ADMIN_TELEGRAM_ID not set in .env.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(settings, pattern="menu_settings"))
    application.add_handler(CallbackQueryHandler(toggle_multichain, pattern=r"^toggle_multichain:.*"))
    application.add_handler(CallbackQueryHandler(handle_buy_sell, pattern="buy_sell"))
    application.add_handler(CallbackQueryHandler(handle_assets, pattern="view_assets"))
    application.add_handler(CallbackQueryHandler(handle_wallet_menu, pattern="menu_wallet"))
    application.add_handler(CallbackQueryHandler(handle_create_wallet_callback, pattern=r"^create_wallet:.*"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="back_to_main_menu"))
    application.add_handler(CallbackQueryHandler(handle_import_wallet, pattern="import_wallet"))
    application.add_handler(CallbackQueryHandler(dummy_response, pattern=r"^(invite_friends|copy_trading|limit_order|change_language|menu_help)$"))
    application.add_handler(CallbackQueryHandler(handle_delete_wallet, pattern=r"^delete_wallet:(solana|ethereum)$"))
    application.add_handler(CallbackQueryHandler(handle_send_asset, pattern="send_asset"))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()