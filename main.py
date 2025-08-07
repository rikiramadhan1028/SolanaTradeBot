import os
import json
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
    ConversationHandler
)
from dotenv import load_dotenv
import re
import asyncio

# === Konfigurasi & Inisialisasi ===
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_NATIVE_TOKEN_MINT = "So11111111111111111111111111111111111111112"
solana_client = SolanaClient(config.SOLANA_RPC_URL)

# === States untuk ConversationHandler ===
AWAITING_TRADE_TYPE, AWAITING_TOKEN_ADDRESS, AWAITING_AMOUNT = range(3)

# === Fungsi helper untuk membersihkan context ===
def clear_user_context(context: ContextTypes.DEFAULT_TYPE):
    """Membersihkan user data context"""
    if hasattr(context, 'user_data'):
        context.user_data.clear()

# === Fungsi-fungsi Bot ===
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
        except Exception:
            sol_balance_str = "Error"
    welcome_text = (
        f"👋 Hello {user_mention}! Welcome to <b>TradeBeat Bot</b>\n\n"
        f"Wallet address: <code>{solana_address}</code>\n"
        f"Wallet balance: `{sol_balance_str}` ($~)\n\n"
        f"🔗 Referral link: https://t.me/TradeBeatBot?start=ref_{user_id}\n\n"
        f"✅ Send contract address to start trading. Please follow official accounts for more info and help."
    )
    return welcome_text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Reset context setiap kali start dipanggil
    clear_user_context(context)
    
    user_id = update.effective_user.id
    user_mention = update.effective_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await update.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))

async def handle_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)  # Reset context
    
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
    clear_user_context(context)  # Reset context
    
    query = update.callback_query
    await query.answer()
    keyboard_buttons = []
    keyboard_buttons.append([InlineKeyboardButton(f"Create Solana Wallet", callback_data=f"create_wallet:solana"),
                             InlineKeyboardButton(f"🗑️ Delete", callback_data=f"delete_wallet:solana")])
    keyboard_buttons.append([InlineKeyboardButton("Import Wallet", callback_data="import_wallet")])
    keyboard_buttons.append([InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")])
    await query.edit_message_text("Wallet Options:", reply_markup=InlineKeyboardMarkup(keyboard_buttons))

async def handle_create_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)  # Reset context
    
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
    clear_user_context(context)  # Reset context
    
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

def validate_and_clean_private_key(key_data: str) -> str:
    """
    Validasi dan bersihkan private key dari format yang berbeda
    """
    key_data = key_data.strip()
    
    # Jika dimulai dengan '[' berarti JSON array
    if key_data.startswith('['):
        try:
            # Pastikan JSON valid
            json.loads(key_data)
            return key_data
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format: {e}")
    
    # Jika berupa string Base58
    elif len(key_data) >= 32:
        # Cek apakah ini Base58 atau hex
        try:
            # Coba parse sebagai Base58 dulu
            import base58
            decoded = base58.b58decode(key_data)
            if len(decoded) == 64:  # Solana private key biasanya 64 bytes
                # Konversi ke format JSON array
                return json.dumps(list(decoded))
            else:
                raise ValueError("Invalid private key length")
        except:
            # Jika gagal Base58, coba sebagai hex
            try:
                if key_data.startswith('0x'):
                    key_data = key_data[2:]
                
                # Convert hex to bytes array
                key_bytes = bytes.fromhex(key_data)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes")
                
                return json.dumps(list(key_bytes))
            except:
                raise ValueError("Invalid private key format")
    else:
        raise ValueError("Private key too short or invalid format")

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().replace('\n', ' ')
    command, *args = text.split(maxsplit=1)
    command = command.lower()

    # Reset context untuk setiap command baru
    clear_user_context(context)

    if command == "import":
        if len(args) == 0:
            await update.message.reply_text("❌ Invalid Format Use: `import [private_key]`", parse_mode="Markdown")
            return
        try:
            key_data = args[0].strip()
            
            # Validasi dan bersihkan private key
            cleaned_key = validate_and_clean_private_key(key_data)
            
            old_wallet = database.get_user_wallet(user_id)
            already_exists = old_wallet.get("address") is not None
            
            # Test private key sebelum menyimpan
            try:
                pubkey = wallet_manager.get_solana_pubkey_from_private_key_json(cleaned_key)
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid private key: {e}")
                return
            
            database.set_user_wallet(user_id, cleaned_key, str(pubkey))
            
            msg = f"✅ Solana wallet {'replaced' if already_exists else 'imported'}!\nAddress: `{pubkey}`"
            if already_exists: 
                msg += "\n⚠️ Previous Solana wallet was overwritten."
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except ValueError as e:
            await update.message.reply_text(f"❌ Error importing Solana wallet: {e}")
        except Exception as e:
            print(f"Import error: {e}")  # Debug log
            await update.message.reply_text(f"❌ An unexpected error occurred during import. Please check your private key format.")
        return

    if command == "send":
        try:
            if len(args) == 0:
                await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
                return
                
            match = re.match(r'^(\w+)\s+([\d.]+)$', args[0].strip())
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
            if tx:
                await update.message.reply_text(f"✅ Sent {amount} SOL!\nTx: `{tx}`", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Failed to send SOL. Please check your balance or recipient address.")
        except (ValueError, AttributeError) as e:
            await update.message.reply_text("❌ Invalid format. Use `send [address] [amount]`")
        except Exception as e:
            print(f"Send error: {e}")  # Debug log
            await update.message.reply_text(f"❌ Error: {e}")
        return

    if command == "sendtoken":
        try:
            if len(args) == 0:
                await update.message.reply_text("❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`")
                return
                
            parts = args[0].strip().split()
            if len(parts) != 3:
                await update.message.reply_text("❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`")
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
            if tx:
                await update.message.reply_text(f"✅ Sent {amount} SPL Token!\nTx: `{tx}`", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Failed to send SPL token.")
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid format. Use `sendtoken [token_address] [to_address] [amount]`")
        except Exception as e:
            print(f"SendToken error: {e}")  # Debug log
            await update.message.reply_text(f"❌ Error: {e}")
        return

    await update.message.reply_text("❌ Unrecognized command. Please use `import`, `send`, or `sendtoken`.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]))

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)  # Reset context
    
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_mention = query.from_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    await query.edit_message_text(welcome_text, reply_markup=get_start_menu_keyboard(user_id), parse_mode='HTML')

async def dummy_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)  # Reset context
    
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]]
    await query.edit_message_text(f"🛠️ Feature `{query.data}` is under development.", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)  # Reset context
    
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    database.delete_user_wallet(user_id)
    await query.edit_message_text(f"🗑️ Your Solana wallet has been deleted.",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")]])
    )

async def handle_send_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_context(context)  # Reset context
    
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

async def handle_cancel_in_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)  # Reset context
    await update.message.reply_text("Trade has been cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]))
    return ConversationHandler.END

# === Fungsi untuk Alur Percakapan Trading ===
async def buy_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_user_context(context)  # Reset context
    
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Buy with SOL", callback_data="trade_buy")],
        [InlineKeyboardButton("Sell to SOL", callback_data="trade_sell")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]
    ]
    await query.edit_message_text("💰 Choose your trading action:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_TRADE_TYPE

async def handle_trade_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trade_type = query.data.split('_')[1]
    context.user_data['trade_type'] = trade_type
    
    keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
    
    if trade_type == "buy":
        await query.edit_message_text("📄 Please send the **token contract address** you want to buy.", reply_markup=InlineKeyboardMarkup(keyboard))
    else: # sell
        await query.edit_message_text("📄 Please send the **token contract address** you want to sell.", reply_markup=InlineKeyboardMarkup(keyboard))
    
    return AWAITING_TOKEN_ADDRESS

async def handle_token_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token_address = update.message.text.strip()
    
    # Validasi token address (basic validation)
    if len(token_address) < 32 or len(token_address) > 44:
        await update.message.reply_text("❌ Invalid token address format. Please enter a valid Solana token address.")
        return AWAITING_TOKEN_ADDRESS
    
    context.user_data['token_address'] = token_address
    
    keyboard = [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_to_main_menu")]]
    
    trade_type = context.user_data.get('trade_type')
    if trade_type == "buy":
        await update.message.reply_text(f"💰 You chose to **buy** `{token_address}`.\n\n"
                                        f"Please send the amount of **SOL** you want to use.", reply_markup=InlineKeyboardMarkup(keyboard))
    else: # sell
        await update.message.reply_text(f"💰 You chose to **sell** `{token_address}`.\n\n"
                                        f"Please send the amount of **tokens** you want to sell.", reply_markup=InlineKeyboardMarkup(keyboard))
        
    return AWAITING_AMOUNT

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    wallet = database.get_user_wallet(user_id)
    if not wallet or not wallet["private_key"]:
        await update.message.reply_text("❌ No Solana wallet found. Please create or import one first.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]))
        return ConversationHandler.END
    
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("❌ Amount must be greater than 0.")
            return AWAITING_AMOUNT
            
        trade_type = context.user_data.get('trade_type')
        token_address = context.user_data.get('token_address')

        if trade_type == "buy":
            input_mint = SOLANA_NATIVE_TOKEN_MINT
            output_mint = token_address
            amount_lamports = int(amount * 1_000_000_000)
            await update.message.reply_text(f"⏳ Buying token `{output_mint}` with `{amount}` SOL...")
        else: # sell
            input_mint = token_address
            output_mint = SOLANA_NATIVE_TOKEN_MINT
            amount_lamports = int(amount * 1_000_000) # Asumsi 6 desimal untuk SPL tokens
            await update.message.reply_text(f"⏳ Selling token `{input_mint}` for `{amount}` SOL...")
        
        tx_sig = await solana_client.perform_swap(
            sender_private_key_json=wallet["private_key"],
            amount_lamports=amount_lamports,
            input_mint=input_mint,
            output_mint=output_mint
        )

        if tx_sig.startswith("Error"):
            await update.message.reply_text(f"❌ Swap failed: {tx_sig}")
        else:
            await update.message.reply_text(
                f"✅ Swap successful! View transaction: https://explorer.solana.com/tx/{tx_sig}?cluster=devnet",
                parse_mode='Markdown'
            )
            
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid amount. Please enter a valid number.")
        return AWAITING_AMOUNT
    except Exception as e:
        print(f"Swap error: {e}")  # Debug log
        await update.message.reply_text(f"❌ An unexpected error occurred: {e}")
    
    # Clear context setelah selesai
    clear_user_context(context)
    
    await update.message.reply_text("Done! What's next?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]]))
    return ConversationHandler.END

async def handle_assets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /assets command."""
    await handle_assets(update, context)

async def handle_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /wallet command."""
    await handle_wallet_menu(update, context)

async def handle_buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /buy command to start the trading flow."""
    await buy_sell(update, context)

async def handle_send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /send command to send SOL."""
    await handle_send_asset(update, context)

async def handle_sendtoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /sendtoken command to send an SPL token."""
    await handle_send_asset(update, context)

async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    # You can customize this help message
    help_message = "This is the help menu. Use the buttons to navigate, or contact an admin."
    await update.message.reply_text(help_message)

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env file")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    trade_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_sell, pattern="^buy_sell$")],
        states={
            AWAITING_TRADE_TYPE: [CallbackQueryHandler(handle_trade_type, pattern=r"trade_.*")],
            AWAITING_TOKEN_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_address)],
            AWAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel_in_conversation, pattern="^back_to_main_menu$"),
            CommandHandler("start", start)
        ]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(trade_conv_handler)
    application.add_handler(CallbackQueryHandler(handle_assets, pattern="^view_assets$"))
    application.add_handler(CallbackQueryHandler(handle_wallet_menu, pattern="^menu_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_create_wallet_callback, pattern=r"^create_wallet:.*$"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$"))
    application.add_handler(CallbackQueryHandler(handle_import_wallet, pattern="^import_wallet$"))
    application.add_handler(CallbackQueryHandler(dummy_response, pattern=r"^(invite_friends|copy_trading|limit_order|change_language|menu_help|menu_settings)$"))
    application.add_handler(CallbackQueryHandler(handle_delete_wallet, pattern=r"^delete_wallet:solana$"))
    application.add_handler(CallbackQueryHandler(handle_send_asset, pattern="^send_asset$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()