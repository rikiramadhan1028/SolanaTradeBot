# -*- coding: utf-8 -*-
# main.py — RokuTrade Bot (secure + realtime SOL price)
import os
import json
import re
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional
from enum import Enum
from dotenv import load_dotenv

# Load environment variables first
load_dotenv()

from copy_trading import copytrading_loop


# Import CU price configuration and user settings
from cu_config import (
    choose_cu_price, 
    cu_to_sol_priority_fee,
    choose_priority_fee_sol,
    DEX_CU_PRICE_MICRO_DEFAULT, 
    DEX_CU_PRICE_MICRO_FAST, 
    DEX_CU_PRICE_MICRO_TURBO, 
    DEX_CU_PRICE_MICRO_ULTRA,
    PRIORITY_FEE_SOL_FAST,
    PRIORITY_FEE_SOL_TURBO, 
    PRIORITY_FEE_SOL_ULTRA,
    PriorityTier
)
from user_settings import UserSettings

# Telegram imports
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

# Global default CU price (fallback)
cu_price = choose_cu_price(os.getenv("PRIORITY_TIER"))

def get_user_cu_price(user_id: str) -> Optional[int]:
    """Get CU price for specific user, with fallback to global default."""
    user_cu = UserSettings.get_user_cu_price(str(user_id))
    # If user has explicitly set OFF (None/0), respect that choice
    # Only use global default if no user preference is stored at all
    if user_cu is not None:
        return user_cu if user_cu > 0 else None  # Treat 0 as None/OFF
    # Check if user has any stored preference (including OFF)
    user_settings = UserSettings.get_user_settings_summary(str(user_id))
    if 'cu_price' in user_settings:
        # User has a stored preference, even if it's None/OFF
        return None
    # No stored preference at all, use global default
    return cu_price

def get_user_priority_tier(user_id: str) -> Optional[str]:
    """Get user's priority tier from database settings."""
    # First try to get stored priority tier
    tier = UserSettings.get_user_priority_tier(user_id)
    if tier:
        return tier
    
    # Check if user has explicitly chosen OFF
    user_settings = UserSettings.get_user_settings_summary(str(user_id))
    if 'priority_tier' in user_settings and user_settings['priority_tier'] is None:
        # User explicitly set OFF
        return None
    
    # Fallback: Map from legacy CU price setting (avoid recursion)
    user_cu = UserSettings.get_user_cu_price(str(user_id))
    if user_cu is None or user_cu == 0:
        return None
    elif user_cu == DEX_CU_PRICE_MICRO_FAST:
        return "fast"
    elif user_cu == DEX_CU_PRICE_MICRO_TURBO:
        return "turbo"
    elif user_cu == DEX_CU_PRICE_MICRO_ULTRA:
        return "ultra"
    else:
        return "custom"  # Custom CU price

def is_admin(user_id: int) -> bool:
    """Check if user is an admin."""
    admin_ids = os.getenv("ADMIN_USER_IDS", "").split(",")
    return str(user_id) in [admin_id.strip() for admin_id in admin_ids if admin_id.strip()]

async def handle_admin_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to view user settings statistics."""
    if not is_admin(update.effective_user.id):
        return
    
    try:
        # Import database functions directly for admin stats
        from database import user_settings_count, user_settings_list_all
        
        total_count = user_settings_count()
        if total_count == 0:
            await update.message.reply_html("📊 No user settings found in MongoDB.")
            return
        
        stats_msg = "📊 <b>User Settings Statistics (MongoDB)</b>\n\n"
        
        tier_counts = {"off": 0, "fast": 0, "turbo": 0, "ultra": 0, "custom": 0}
        
        all_docs = user_settings_list_all()
        for doc in all_docs:
            cu_price = doc.get("cu_price")
            tier = doc.get("priority_tier", "off")
            
            if cu_price is None:
                tier_counts["off"] += 1
            elif tier in tier_counts:
                tier_counts[tier] += 1
            else:
                tier_counts["custom"] += 1
        
        stats_msg += f"👥 <b>Total Users:</b> {total_count}\n\n"
        stats_msg += f"🔴 OFF: {tier_counts['off']} users\n"
        stats_msg += f"🟡 FAST: {tier_counts['fast']} users\n"
        stats_msg += f"🟠 TURBO: {tier_counts['turbo']} users\n" 
        stats_msg += f"🔥 ULTRA: {tier_counts['ultra']} users\n"
        stats_msg += f"✏️ CUSTOM: {tier_counts['custom']} users\n\n"
        
        # Show sample users
        stats_msg += "<b>Sample Users:</b>\n"
        for i, doc in enumerate(all_docs[:5]):
            user_id = doc.get("user_id")
            cu_price = doc.get("cu_price")
            tier_display = _tier_of(cu_price)
            stats_msg += f"• {user_id}: {tier_display}\n"
        
        if len(all_docs) > 5:
            stats_msg += f"... and {len(all_docs) - 5} more users\n"
        
        await update.message.reply_html(stats_msg)
        
    except ImportError:
        response = await update.message.reply_html("❌ MongoDB database not available.")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
    except Exception as e:
        response = await update.message.reply_html(f"❌ Error getting user stats: {e}")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))

# CU Settings conversation states
SET_CU_PRICE = 1
# -------- ENV --------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRADE_SVC_URL      = os.getenv("TRADE_SVC_URL", "http://localhost:8080").rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://yourdomain.com/webhook")
# Optional platform fee (default OFF)
# FEE_BPS: basis points, 100 = 1%
FEE_BPS     = int(os.getenv("FEE_BPS", "0"))
FEE_WALLET  = (os.getenv("FEE_WALLET") or "").strip()
FEE_ENABLED = FEE_BPS > 0 and len(FEE_WALLET) >= 32
FEE_MIN_SOL = float(os.getenv("FEE_MIN_SOL", "0.000000001")) if FEE_ENABLED else 0.0

# Jito configuration (default ON for faster transactions)
JITO_ENABLED = os.getenv("JITO_ENABLED", "true").lower() in ("true", "1", "yes", "on")


import config
import database
from database import (
    get_user_slippage_buy, get_user_slippage_sell, get_user_language, 
    get_user_anti_mev, get_user_jupiter_versioned_tx, get_user_jupiter_skip_preflight,
    user_settings_upsert
)
import wallet_manager
from blockchain_clients.solana_client import SolanaClient

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
    _refreshing: set[str] = set()  # Track ongoing refreshes to prevent duplicates

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
    async def force_refresh(cls, mint: str) -> dict:
        """Force refresh a single mint for user-triggered refresh. ALWAYS fetches fresh data, ignores cache."""
        # Don't check _refreshing - allow concurrent requests for different users
        # Each user's refresh should get truly fresh data
        
        try:
            # ALWAYS fetch fresh data, bypassing all cache logic
            result = await cls._fetch_bulk([mint])
            return result.get(mint, {"price": 0.0, "lp": 0.0, "mc": 0.0})
        except Exception:
            # Fallback to cache only if network fails
            hit = cls._store.get(mint)
            if hit:
                return hit[1]
            return {"price": 0.0, "lp": 0.0, "mc": 0.0}

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
        """Warm cache setiap 1s untuk semua mint yang pernah dirender - ultra-fast refresh."""
        while not stop_event.is_set():
            try:
                if cls._watch:
                    await cls._fetch_bulk(list(cls._watch))
            except Exception:
                pass
            await asyncio.sleep(1.0)  # Faster refresh for real-time updates

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
(WITHDRAW_AMOUNT, WITHDRAW_ADDRESS) = range(11, 13)

# ================== UI Helpers ==================

# ================== Message Cleanup Helpers ==================
async def delete_user_message(update: Update) -> None:
    """Auto-delete user input message to keep chat clean - DEPRECATED, use delete_sensitive_user_message"""
    try:
        await update.message.delete()
    except Exception:
        pass

async def delete_sensitive_user_message(update: Update) -> None:
    """Delete user message only if it contains sensitive data like private keys"""
    try:
        message_text = update.message.text or ""
        # Only delete if message contains sensitive information
        if ("import" in message_text.lower() and len(message_text) > 20) or \
           any(word in message_text.lower() for word in ["private", "secret", "key"]):
            await update.message.delete()
    except Exception:
        pass

async def delete_previous_bot_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Delete previous bot message stored in context"""
    if context.user_data.get("last_bot_message_id"):
        try:
            bot = context.bot
            await bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data["last_bot_message_id"]
            )
        except Exception:
            pass

async def store_bot_message(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    """Store bot message ID for later cleanup - DEPRECATED, use track_bot_message"""
    context.user_data["last_bot_message_id"] = message_id
    await track_bot_message(context, message_id)

async def clear_message_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all message-related context data"""
    context.user_data.pop("last_bot_message_id", None)
    context.user_data.pop("last_bot_message", None)
    context.user_data.pop("bot_messages_to_delete", None)

async def delete_all_bot_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Delete all bot messages tracked in context"""
    try:
        bot = context.bot
        
        # Delete the last bot message
        if context.user_data.get("last_bot_message_id"):
            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=context.user_data["last_bot_message_id"]
                )
            except:
                pass
        
        # Delete all tracked bot messages
        bot_messages = context.user_data.get("bot_messages_to_delete", [])
        for message_id in bot_messages:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except:
                pass
                
        # Clear the tracking
        await clear_message_context(context)
    except Exception as e:
        print(f"Error deleting bot messages: {e}")

async def delete_all_bot_messages_except_current(context: ContextTypes.DEFAULT_TYPE, chat_id: int, current_message_id: int) -> None:
    """Delete all bot messages tracked in context except the current one"""
    try:
        bot = context.bot
        
        # Delete the last bot message if it's not the current one
        last_message_id = context.user_data.get("last_bot_message_id")
        if last_message_id and last_message_id != current_message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=last_message_id)
            except:
                pass
        
        # Delete all tracked bot messages except current one
        bot_messages = context.user_data.get("bot_messages_to_delete", [])
        for message_id in bot_messages:
            if message_id != current_message_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except:
                    pass
                    
        # Update tracking to keep only current message
        context.user_data["bot_messages_to_delete"] = [current_message_id]
        context.user_data["last_bot_message_id"] = current_message_id
    except Exception as e:
        print(f"Error deleting bot messages: {e}")

async def track_bot_message(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    """Track bot message for automatic deletion"""
    if "bot_messages_to_delete" not in context.user_data:
        context.user_data["bot_messages_to_delete"] = []
    context.user_data["bot_messages_to_delete"].append(message_id)
    # Also store as last message for backward compatibility
    context.user_data["last_bot_message_id"] = message_id

async def auto_reply_html(message, text: str, context: ContextTypes.DEFAULT_TYPE, **kwargs):
    """Reply with HTML and automatically track the message for deletion"""
    response = await message.reply_html(text, **kwargs)
    await track_bot_message(context, response.message_id)
    return response

async def auto_reply_text(message, text: str, context: ContextTypes.DEFAULT_TYPE, **kwargs):
    """Reply with text and automatically track the message for deletion"""
    response = await message.reply_text(text, **kwargs)
    await track_bot_message(context, response.message_id)
    return response

async def safe_reply_text(message, text: str, context: ContextTypes.DEFAULT_TYPE = None, **kwargs):
    """Safe reply with automatic tracking - use this instead of direct reply_text"""
    response = await message.reply_text(text, **kwargs)
    if context:
        await track_bot_message(context, response.message_id)
    return response

async def safe_reply_html(message, text: str, context: ContextTypes.DEFAULT_TYPE = None, **kwargs):
    """Safe reply with automatic tracking - use this instead of direct reply_html"""
    response = await message.reply_html(text, **kwargs)
    if context:
        await track_bot_message(context, response.message_id)
    return response

async def auto_edit_message_text(query, text: str, context: ContextTypes.DEFAULT_TYPE, **kwargs):
    """Edit message text and automatically track the message for deletion"""
    await query.edit_message_text(text, **kwargs)
    await track_bot_message(context, query.message.message_id)
    return query

async def safe_edit_with_tracking(query, text: str, context: ContextTypes.DEFAULT_TYPE, **kwargs):
    """Edit message text with tracking and fallback to new message if edit fails"""
    try:
        await query.edit_message_text(text, **kwargs)
        await track_bot_message(context, query.message.message_id)
    except Exception:
        # If edit fails, send new message
        response = await query.message.reply_text(text, **kwargs)
        await track_bot_message(context, response.message_id)
        # Try to delete the original message
        try:
            await query.message.delete()
        except:
            pass

async def safe_edit_message(query, text: str, **kwargs):
    """Safely edit message and store ID for cleanup"""
    try:
        response = await query.edit_message_text(text, **kwargs)
        return response
    except Exception:
        # If edit fails, send new message
        response = await query.message.reply_text(text, **kwargs)
        try:
            await query.message.delete()
        except Exception:
            pass
        return response

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

async def reply_ok_html(message, text: str, prev_cb: str | None = None, signature: str | None = None, context: ContextTypes.DEFAULT_TYPE = None):
    extra = ""
    if signature:
        extra = f'\n🔗 <a href="{solscan_tx(signature)}">Solscan</a>\n<code>{signature}</code>'
    response = await message.reply_html(text + extra, reply_markup=back_markup(prev_cb))
    if context:
        await track_bot_message(context, response.message_id)
        # Schedule automatic cleanup for success messages after 5 minutes
        if text.startswith("✅"):
            chat_id = message.chat_id
            asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))
    return response

async def reply_loading_html(message, text: str, context: ContextTypes.DEFAULT_TYPE = None):
    """Send loading message without buttons and auto-delete after 0.5 seconds for instant UX"""
    # Send message without any buttons
    response = await message.reply_html(text)
    if context:
        await track_bot_message(context, response.message_id)
        # Auto-delete loading message after 0.5 seconds for instant results
        chat_id = message.chat_id
        asyncio.create_task(auto_delete_loading_message(context, chat_id, response.message_id))
    return response

async def auto_delete_loading_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Delete loading message after 0.5 seconds for instant UX"""
    await asyncio.sleep(0.5)  # Wait 0.5 seconds for max 1s total UX
    try:
        bot = context.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Message might already be deleted or edited

async def reply_err_html(message, text: str, prev_cb: str | None = None, context: ContextTypes.DEFAULT_TYPE = None):
    response = await message.reply_html(text, reply_markup=back_markup(prev_cb))
    if context:
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error messages after 5 minutes
        chat_id = message.chat_id
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))
    return response

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

def get_pnl_image_url(pnl_pct: float) -> str:
    """Returns image URL based on PnL percentage for modern CEX-like sharing"""
    if pnl_pct >= 1.0:  # +100% or more
        return "https://example.com/images/pnl_100plus.png"  # Replace with actual URL
    elif pnl_pct >= 0.5:  # +50% to +99.99%
        return "https://example.com/images/pnl_50plus.png"   # Replace with actual URL
    elif pnl_pct >= 0.25:  # +25% to +49.99%
        return "https://example.com/images/pnl_25plus.png"   # Replace with actual URL
    elif pnl_pct >= 0.0:   # 0% to +24.99%
        return "https://example.com/images/pnl_positive.png" # Replace with actual URL
    elif pnl_pct >= -0.25:  # -25% to -0.01%
        return "https://example.com/images/pnl_negative.png" # Replace with actual URL
    elif pnl_pct >= -0.5:   # -50% to -25.01%
        return "https://example.com/images/pnl_minus25.png"  # Replace with actual URL
    else:  # -50% or worse
        return "https://example.com/images/pnl_minus50.png"  # Replace with actual URL

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

async def clear_user_context_with_cleanup(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Clear user context and delete all bot messages"""
    await delete_all_bot_messages(context, chat_id)
    clear_user_context(context)

async def auto_cleanup_success_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay_minutes: int = 5):
    """Schedule automatic cleanup of success messages after delay"""
    await asyncio.sleep(delay_minutes * 60)  # Convert minutes to seconds
    try:
        bot = context.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Message might already be deleted

async def auto_cleanup_user_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay_minutes: int = 5):
    """Schedule automatic cleanup of user messages after delay for clean chat"""
    await asyncio.sleep(delay_minutes * 60)  # Convert minutes to seconds
    try:
        bot = context.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Message might already be deleted

async def track_and_schedule_user_message_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track user message and schedule it for auto-deletion after 5 minutes"""
    if update.message and update.message.from_user.id != context.bot.id:
        # This is a user message, schedule it for cleanup
        chat_id = update.effective_chat.id
        message_id = update.message.message_id
        asyncio.create_task(auto_cleanup_user_message(context, chat_id, message_id, 5))

async def ensure_message_cleanup_on_user_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Cleanup all bot messages when user performs any action"""
    await delete_all_bot_messages(context, chat_id)

async def handle_callback_with_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE, handler_func):
    """Universal callback handler that cleans up messages before executing the actual handler"""
    chat_id = update.effective_chat.id
    # Don't cleanup immediately for callback queries to avoid deleting the message being interacted with
    # Just clean up old messages, let the handler manage the current message
    await delete_all_bot_messages_except_current(context, chat_id, update.callback_query.message.message_id)
    return await handler_func(update, context)

def get_start_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("💰 Buy/Sell", callback_data="buy_sell"),
            InlineKeyboardButton("🧾 Asset", callback_data="view_assets"),
        ],
        [
            InlineKeyboardButton("📉 Limit Order", callback_data="limit_order"),
            InlineKeyboardButton("🏆 Invite Friends", callback_data="invite_friends"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
            InlineKeyboardButton("👛 Wallet", callback_data="menu_wallet"),
        ],
        [
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
def token_panel_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> InlineKeyboardMarkup:
    # Get slippage from database instead of context
    buy_bps = get_user_slippage_buy(user_id)
    sell_bps = get_user_slippage_sell(user_id)
    kb: list[list[InlineKeyboardButton]] = []
    kb.append([
        InlineKeyboardButton("↻ Refresh", callback_data="token_panel_refresh"),
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
    # Slippage moved to settings menu - no longer shown in trading panel
    kb.append([
        InlineKeyboardButton("⬅️ Change Token", callback_data="back_to_buy_sell_menu"),
        InlineKeyboardButton("🏠 Menu",   callback_data="back_to_main_menu"),
    ])
    return InlineKeyboardMarkup(kb)

async def build_token_panel(user_id: int, mint: str, *, force_fresh: bool = False) -> str:
    """Compact summary with price & LP from Dexscreener; unknown -> N/A."""
    wallet_info = database.get_user_wallet(user_id)
    addr = wallet_info.get("address", "--") if wallet_info else "--"

    # SOL Balance & Token Balance for PnL
    balance_text = "N/A"
    token_balance = 0.0
    if addr and addr != "--":
        try:
            bal = await svc_get_sol_balance(addr)
            balance_text = f"{bal:.4f} SOL"
            # Get token balance for PnL calculation
            token_balance = await svc_get_token_balance(addr, mint)
        except Exception:
            balance_text = "Error"

    # Price + meta using FAST cache system
    price_text = "N/A"
    mc_text = "N/A"
    lp_text = "N/A"
    display_name = None

    # Get price data - either fresh or cached
    current_price_data = None
    meta = await MetaCache.get(mint)  # Meta rarely changes, cache OK
    
    if force_fresh:
        # Get absolutely fresh data for user-triggered refresh
        current_price_data = await DexCache.force_refresh(mint)  # ALWAYS fresh price data
    else:
        # Use optimized cache system for normal loading
        pack = await DexCache.get_bulk([mint], prefer_cache=True)
        if pack and pack.get(mint):
            current_price_data = pack[mint]
    
    # Format price data
    if current_price_data:
        price_text = format_usd(current_price_data.get("price", 0))
        mc_text = format_usd(current_price_data.get("mc", 0))
        lp_text = format_usd(current_price_data.get("lp", 0))
    
    # Get symbol/name from meta cache
    if meta:
        symbol = (meta.get("symbol") or "") or ""
        name = (meta.get("name") or "") or ""
        if symbol:
            display_name = symbol if symbol.startswith("$") else f"${symbol}"
        elif name:
            display_name = name

    # Fallback to old methods only if cache completely fails
    if price_text == "N/A" or price_text == "$0.00":
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

    # Real-time PnL calculation if user holds tokens
    pnl_line = ""
    if token_balance > 0 and current_price_data:
        try:
            # Get position data from database
            positions = database.get_user_tokens(user_id)
            position = next((p for p in positions if p.get("mint") == mint), None)
            
            if position and position.get("avg_entry_price_usd"):
                current_price = current_price_data.get("price", 0)
                avg_entry_price = position["avg_entry_price_usd"]
                
                if current_price > 0 and avg_entry_price > 0:
                    current_value_usd = token_balance * current_price
                    entry_value_usd = token_balance * avg_entry_price
                    pnl_usd = current_value_usd - entry_value_usd
                    pnl_pct = (pnl_usd / entry_value_usd) if entry_value_usd > 0 else 0
                    
                    pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
                    pnl_line = f"• PnL: {pnl_emoji} {pnl_pct*100:+.1f}% ({format_usd(pnl_usd)})"
        except Exception:
            pass

    # High-precision timestamp for real-time feedback
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-2]  # Show milliseconds
    
    # Add refresh indicator for spam-click feedback
    refresh_indicator = "🔴 LIVE" if force_fresh else "📊"

    lines = []
    lines.append(f"Swap <b>{display_name}</b> 📈")
    lines.append("")
    lines.append(f"<a href=\"{dexscreener_url(mint)}\">{mint[:4]}…{mint[-4:]}</a>")
    lines.append(f"• SOL Balance: {balance_text}")
    if token_balance > 0:
        lines.append(f"• Token Balance: {token_balance:.4f}")
    lines.append(f"• Price: {price_text}   LP: {lp_text}   MC: {mc_text}")
    if pnl_line:
        lines.append(pnl_line)
    lines.append("• Raydium CPMM")
    lines.append(f'• <a href="{dexscreener_url(mint)}">DEX Screener</a>')
    lines.append("")
    lines.append(f"🕒 {refresh_indicator} Updated: {ts} UTC")
    return "\n".join(lines)

# ================== Bot Handlers ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    user_id = update.effective_user.id
    user_mention = update.effective_user.mention_html()
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    response = await update.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))
    await track_bot_message(context, response.message_id)

async def handle_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    
    # init state default agar tidak menyaring dust & tampilkan detail
    context.user_data.setdefault("assets_state", {
        "page": 1, "sort": "value", "hide_dust": False,
        "dust_usd": DEFAULT_DUST_USD, "detail": True, "hidden_mints": set()
    })
    
    # Clean up other tracked messages first, then render assets
    await delete_all_bot_messages_except_current(context, chat_id, q.message.message_id)
    clear_user_context(context)
    
    await _render_assets_detailed_view(q, context)


async def _render_assets_detailed_view(q_or_msg, context: ContextTypes.DEFAULT_TYPE):
    """Render SPL tokens sebagai kartu detail ala screenshot."""
    user_id = q_or_msg.from_user.id if hasattr(q_or_msg, "from_user") else context._user_id
    w = database.get_user_wallet(user_id)
    addr = (w or {}).get("address")
    if not addr:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]])
        await q_or_msg.edit_message_text("📊 <b>Your Asset Balances</b>\n\nNo wallet yet.", parse_mode="HTML", reply_markup=kb)
        # Track for cleanup if it's a callback query
        if hasattr(q_or_msg, "message"):
            context = q_or_msg  # This should be context, but let's try to handle it
            try:
                await track_bot_message(context, q_or_msg.message.message_id)
            except:
                pass
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

    # Calculate portfolio stats for modern CEX-like interface
    total_positions = len(filtered)
    profitable_positions = sum(1 for x in filtered if x.get("pos", {}).get("avg_entry_price_usd") and x["price_usd"] > x["pos"].get("avg_entry_price_usd", 0))
    total_pnl_usd = 0
    total_cost_usd = 0
    
    for x in filtered:
        pos = x.get("pos", {})
        if pos and x["price_usd"] > 0:
            avg_px = pos.get("avg_entry_price_usd")
            if isinstance(avg_px, (int, float)) and avg_px > 0:
                cost_usd = x["amount"] * avg_px
                current_usd = x["amount"] * x["price_usd"]
                total_pnl_usd += (current_usd - cost_usd)
                total_cost_usd += cost_usd
    
    portfolio_pnl_pct = (total_pnl_usd / total_cost_usd) if total_cost_usd > 0 else None
    win_rate = (profitable_positions / total_positions * 100) if total_positions > 0 else 0
    
    # Modern portfolio header
    lines = []
    lines.append("💼 <b>Portfolio Overview</b>")
    lines.append("─────────────────────")
    lines.append(f"🏦 <b>Total Value:</b> {format_usd(total_usd)}")
    
    if portfolio_pnl_pct is not None:
        pnl_emoji = "🟢" if portfolio_pnl_pct >= 0 else "🔴"
        lines.append(f"📊 <b>Total PnL:</b> {pnl_emoji} {format_pct(portfolio_pnl_pct)} ({format_usd(total_pnl_usd)})")
    
    lines.append(f"🎯 <b>Positions:</b> {total_positions} | Win Rate: {win_rate:.1f}%")
    lines.append(f"💰 <b>SOL:</b> {sol_amount:.3f} ({format_usd(sol_usd)})")
    lines.append(f"🪙 <b>Tokens:</b> {format_usd(tokens_total_usd)}")
    lines.append(f"👛 <code>{addr[:4]}...{addr[-4:]}</code>")
    lines.append("─────────────────────\n")

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

        lines.append(f"<b><a href='tg://callback?data=trade_token_{mint}'>${sym}</a></b> {indicator} : <code>{val_sol:.3f} SOL</code> ({format_usd(val_usd)}) "
                     f"[<a href='tg://callback?data=assets_hide_{mint}'>hide</a>]")  # Make symbol clickable to trade
        lines.append(f"<code>{mint}</code>")
        if pnl_pct is not None:
            lines.append(f"• PNL: {format_pct(pnl_pct)} "
                         f"({(pnl_sol or 0):.3f} SOL/{format_usd((pnl_sol or 0)*sol_price)}) {danger}")
        else:
            lines.append("• PNL: —")
        lines.append(f"[<a href='tg://callback?data=assets_share_pnl_{mint}'>Share PNL</a>]")
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
    row1_5 = [
        InlineKeyboardButton("📸 Share Portfolio", callback_data="assets_share_portfolio"),
    ]
    row2 = [b for b in (prev_btn, InlineKeyboardButton("↻ Refresh", callback_data="assets_refresh"), next_btn) if b]
    # Remove individual trade and share buttons - now using clickable symbols and inline links

    back = [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")]

    kb_rows = [row0, row1, row1_5]
    if row2: kb_rows.append(row2)
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

    elif data.startswith("assets_share_pnl_"):
        mint = data.split("_", 3)[3]
        await handle_share_portfolio_pnl(q, context, mint)
        return  # Don't re-render assets view
        
    elif data == "assets_share_portfolio":
        await handle_share_full_portfolio(q, context)
        return  # Don't re-render assets view
        
    elif data.startswith("trade_"):
        # Handle both "trade_{mint}" and "trade_token_{mint}" patterns
        if data.startswith("trade_token_"):
            mint = data.split("_", 2)[2]  # Extract mint from "trade_token_{mint}"
        else:
            mint = data.split("_", 1)[1]  # Extract mint from "trade_{mint}"
        
        # Navigate to trade screen for this token
        context.user_data["trade_mint"] = mint  # Set mint for trading
        await handle_trade(q, context)
        return  # Don't re-render assets view
        
    elif data.startswith("assets_share_"):
        mint = data.split("_", 2)[2]
        # kirim share card terpisah
        try:
            # cari data terakhir di state render dengan fetch ulang singkat hanya token itu
            await q.message.reply_text(f"Mint: {mint}\nDexScreener: {dexscreener_url(mint)}\n(RokuTrade)", disable_web_page_preview=True)
        except Exception:
            pass

    await _render_assets_detailed_view(q, context)

async def handle_share_portfolio_pnl(q, context: ContextTypes.DEFAULT_TYPE, mint: str):
    """Share PnL with modern CEX-like image based on performance"""
    user_id = q.from_user.id
    w = database.get_user_wallet(user_id)
    addr = (w or {}).get("address")
    
    if not addr:
        response = await q.message.reply_text("❌ No wallet found")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        chat_id = q.message.chat_id
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))
        return
    
    try:
        # Get token data for this specific mint
        meta = await MetaCache.get(mint)
        pack = await DexCache.get(mint, prefer_cache=True)
        pos = database.position_get(user_id, mint) or {}
        
        symbol = (meta.get("symbol") or "").strip() or mint[:6].upper()
        price = float(pack.get("price") or 0.0)
        
        # Calculate PnL
        pnl_pct = None
        pnl_usd = 0
        if pos and price > 0:
            avg_px = pos.get("avg_entry_price_usd")
            if isinstance(avg_px, (int, float)) and avg_px > 0:
                # Get current balance
                tokens = await svc_get_token_balances(addr, min_amount=0.0)
                current_amount = 0
                for t in tokens:
                    if (t.get("mint") or t.get("mintAddress")) == mint:
                        current_amount = float(t.get("amount") or t.get("uiAmount") or 0)
                        break
                
                if current_amount > 0:
                    cost_usd = current_amount * avg_px
                    current_usd = current_amount * price
                    pnl_usd = current_usd - cost_usd
                    pnl_pct = (pnl_usd / cost_usd) if cost_usd > 0 else 0
        
        if pnl_pct is not None:
            # Get current token balance for detailed PnL card
            tokens = await svc_get_token_balances(addr, min_amount=0.0)
            current_amount = 0
            for t in tokens:
                if (t.get("mint") or t.get("mintAddress")) == mint:
                    current_amount = float(t.get("amount") or t.get("uiAmount") or 0)
                    break
            
            # Calculate detailed PnL data
            pnl_text = f"{pnl_pct*100:+.1f}%" if pnl_pct else "0.0%"
            avg_entry_price = pos.get('avg_entry_price_usd', 0)
            total_invested = current_amount * avg_entry_price if avg_entry_price > 0 else 0
            current_value = current_amount * price
            
            # Create PnL card similar to the example
            card = "🎯 <b>PnL CARD</b> 🎯\n"
            card += "━━━━━━━━━━━━━━━━━━\n\n"
            
            # Token info header
            card += f"<b>${symbol}</b>\n"
            card += f"<code>{mint[:8]}...{mint[-8:]}</code>\n\n"
            
            # Main PnL display (large)
            if pnl_pct >= 0:
                emoji = "🟢" if pnl_pct < 0.25 else "🚀" if pnl_pct >= 0.5 else "📈"
                card += f"{emoji} <b>{pnl_text}</b> {emoji}\n\n"
            else:
                emoji = "🟡" if pnl_pct >= -0.25 else "🔴" if pnl_pct >= -0.5 else "💀"
                card += f"{emoji} <b>{pnl_text}</b> {emoji}\n\n"
            
            # Investment details
            card += f"💎 <b>Total Invested</b>\n{format_usd(total_invested)}\n\n"
            card += f"💰 <b>Current Value</b>\n{format_usd(current_value)}\n\n"
            
            pnl_label = "Profit" if pnl_usd >= 0 else "Loss"
            card += f"{'📈' if pnl_usd >= 0 else '📉'} <b>{pnl_label}</b>\n{format_usd(abs(pnl_usd))}\n\n"
            
            # Additional info
            card += f"📊 <b>Entry Price:</b> {format_usd(avg_entry_price)}\n"
            card += f"💎 <b>Current Price:</b> {format_usd(price)}\n"
            card += f"🪙 <b>Balance:</b> {current_amount:,.4f}\n\n"
            
            card += "━━━━━━━━━━━━━━━━━━\n"
            card += "🤖 <i>RokuTrade - Solana Trading Bot</i>"
            
            # Send the PnL card
            await q.message.reply_text(
                card,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        else:
            response = await q.message.reply_text("❌ No PnL data available for this token")
            await track_bot_message(context, response.message_id)
            # Auto-cleanup error message after 5 minutes
            chat_id = q.message.chat_id
            asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))
            
    except Exception as e:
        response = await q.message.reply_text(f"❌ Error sharing PnL: {str(e)}")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        chat_id = q.message.chat_id
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))

async def handle_trade(q, context: ContextTypes.DEFAULT_TYPE):
    """Handle trade button from assets view - navigate to token panel for specific token"""
    await q.answer()
    user_id = q.from_user.id
    
    # Get the token mint from context
    mint = context.user_data.get("trade_mint")
    if not mint:
        await q.edit_message_text(
            "❌ No token selected for trading.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Assets", callback_data="view_assets")
            ]])
        )
        return
    
    # Set token address in context for trading flow
    context.user_data["token_address"] = mint
    
    try:
        # Build and display token panel
        panel = await build_token_panel(user_id, mint)
        await q.edit_message_text(
            panel, 
            reply_markup=token_panel_keyboard(context), 
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Error building token panel for {mint}: {e}")
        await q.edit_message_text(
            f"❌ Error loading token information.\n\nToken: <code>{mint}</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Assets", callback_data="view_assets")
            ]]),
            parse_mode="HTML"
        )

async def handle_share_full_portfolio(q, context: ContextTypes.DEFAULT_TYPE):
    """Share full portfolio summary with CEX-like interface"""
    user_id = q.from_user.id
    w = database.get_user_wallet(user_id)
    addr = (w or {}).get("address")
    
    if not addr:
        response = await q.message.reply_text("❌ No wallet found")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        chat_id = q.message.chat_id
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))
        return
    
    try:
        # Recalculate portfolio stats (similar to assets view)
        sol_amount = await svc_get_sol_balance(addr)
        sol_price = await get_sol_price_usd()
        sol_usd = sol_amount * sol_price if sol_price > 0 else 0.0
        
        tokens = await svc_get_token_balances(addr, min_amount=0.0)
        items = []
        mints = []
        
        for t in tokens or []:
            mint = t.get("mint") or t.get("mintAddress")
            amt = float(t.get("amount") or t.get("uiAmount") or 0)
            if mint and amt > 0:
                mints.append(mint)
                items.append({"mint": mint, "amount": amt})
        
        # Get metadata and pricing
        packs_by_mint = await DexCache.get_bulk(mints, prefer_cache=True)
        metas = await asyncio.gather(*(MetaCache.get(m) for m in mints), return_exceptions=True)
        
        # Calculate portfolio totals
        total_pnl_usd = 0
        total_cost_usd = 0
        tokens_total_usd = 0
        profitable_positions = 0
        total_positions = 0
        
        for it, meta in zip(items, metas):
            pack = packs_by_mint.get(it["mint"], {"price": 0.0})
            meta = meta if isinstance(meta, dict) else {}
            px = float(pack.get("price") or 0.0)
            usd = it["amount"] * px if px > 0 else 0.0
            tokens_total_usd += usd
            
            if usd >= 1.0:  # Only count positions > $1
                total_positions += 1
                pos = database.position_get(user_id, it["mint"]) or {}
                if pos and px > 0:
                    avg_px = pos.get("avg_entry_price_usd")
                    if isinstance(avg_px, (int, float)) and avg_px > 0:
                        cost_usd = it["amount"] * avg_px
                        pnl_usd = usd - cost_usd
                        total_pnl_usd += pnl_usd
                        total_cost_usd += cost_usd
                        if px > avg_px:
                            profitable_positions += 1
        
        total_usd = sol_usd + tokens_total_usd
        portfolio_pnl_pct = (total_pnl_usd / total_cost_usd) if total_cost_usd > 0 else None
        win_rate = (profitable_positions / total_positions * 100) if total_positions > 0 else 0
        
        # Select appropriate image based on portfolio PnL
        if portfolio_pnl_pct is not None:
            image_url = get_pnl_image_url(portfolio_pnl_pct)
            emoji = "🚀" if portfolio_pnl_pct >= 0.5 else "📈" if portfolio_pnl_pct >= 0 else "📉" if portfolio_pnl_pct >= -0.25 else "💀"
            pnl_text = f"{portfolio_pnl_pct*100:+.1f}%" if portfolio_pnl_pct else "0.0%"
        else:
            # Default to positive image for portfolios without PnL data
            image_url = get_pnl_image_url(0.1)
            emoji = "💼"
            pnl_text = "N/A"
        
        # Create comprehensive portfolio share message
        caption = f"{emoji} <b>My Portfolio Performance</b>\n\n"
        caption += f"💼 <b>Total Value:</b> {format_usd(total_usd)}\n"
        if portfolio_pnl_pct is not None:
            caption += f"📊 <b>Total PnL:</b> {pnl_text} ({format_usd(total_pnl_usd)})\n"
        caption += f"🎯 <b>Positions:</b> {total_positions} | Win Rate: {win_rate:.1f}%\n"
        caption += f"💰 <b>SOL:</b> {sol_amount:.3f} ({format_usd(sol_usd)})\n"
        caption += f"🪙 <b>Tokens:</b> {format_usd(tokens_total_usd)}\n\n"
        caption += f"👛 <code>{addr[:8]}...{addr[-8:]}</code>\n\n"
        caption += f"🤖 <i>Trading with RokuTrade</i>"
        
        # Send portfolio summary with image
        await q.message.reply_photo(
            photo=image_url,
            caption=caption,
            parse_mode="HTML"
        )
        
    except Exception as e:
        response = await q.message.reply_text(f"❌ Error sharing portfolio: {str(e)}")
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        chat_id = q.message.chat_id
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 5))

# ===== Copy Trading UI =====
async def handle_copy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
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
    
    try:
        # Try to edit current message first
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        # Track this message for cleanup
        await track_bot_message(context, q.message.message_id)
    except Exception:
        # If edit fails, send new message
        response = await q.message.reply_html(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await track_bot_message(context, response.message_id)
    
    # Clean up other tracked messages (but not the current one)
    await delete_all_bot_messages_except_current(context, chat_id, q.message.message_id)
    clear_user_context(context)
    
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
            response = await update.message.reply_html("❌ Invalid leader pubkey.", reply_markup=back_markup("back_to_main_menu"))
            await track_bot_message(context, response.message_id)
            # Auto-cleanup error message after 5 minutes
            asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
            return
        try:
            ratio = float(parts[2])
            max_sol = float(parts[3])
        except Exception:
            response = await update.message.reply_html("❌ Usage: <code>copyadd LEADER_PUBKEY RATIO MAX_SOL</code>", reply_markup=back_markup("back_to_main_menu"))
            await track_bot_message(context, response.message_id)
            # Auto-cleanup error message after 5 minutes
            asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
            return
        database.copy_follow_upsert(user_id, leader, ratio=ratio, max_sol_per_trade=max_sol, active=True)
        response = await update.message.reply_html("✅ Copy-follow added/updated.", reply_markup=back_markup("back_to_main_menu"))
        await track_bot_message(context, response.message_id)
        return

    if cmd == "copyon" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_upsert(user_id, leader, active=True)
        response = await update.message.reply_html("✅ Copy-follow turned ON.", reply_markup=back_markup("back_to_main_menu"))
        await track_bot_message(context, response.message_id)
        return

    if cmd == "copyoff" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_upsert(user_id, leader, active=False)
        response = await update.message.reply_html("✅ Copy-follow turned OFF.", reply_markup=back_markup("back_to_main_menu"))
        await track_bot_message(context, response.message_id)
        return

    if cmd == "copyrm" and len(parts) == 2:
        leader = parts[1].strip()
        database.copy_follow_remove(user_id, leader)
        response = await update.message.reply_html("🗑️ Copy-follow removed.", reply_markup=back_markup("back_to_main_menu"))
        await track_bot_message(context, response.message_id)
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
    # Keep user message visible for transparency
    
    leader = (update.message.text or "").strip()
    if not _is_pubkey(leader):
        response = await update.message.reply_html("❌ Invalid pubkey. Please try again.", reply_markup=back_markup("copy_menu"))
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
        return COPY_AWAIT_LEADER
    context.user_data["copy_leader"] = leader
    
    # Delete previous bot message
    await delete_previous_bot_message(context, update.effective_chat.id)
    
    response = await update.message.reply_html(
        "✅ Leader accepted.\n\nNow send the <b>ratio</b> (e.g. <code>1</code> for 1:1, <code>0.5</code> for half).",
        reply_markup=back_markup("copy_menu"),
    )
    await track_bot_message(context, response.message_id)
    return COPY_AWAIT_RATIO

async def copy_add_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Keep user message visible for transparency
    
    try:
        ratio = float((update.message.text or "").strip())
        if ratio <= 0 or ratio > 100:
            raise ValueError()
    except Exception:
        response = await update.message.reply_html("❌ Invalid ratio. Example: <code>1</code> or <code>0.5</code>.",
                                        reply_markup=back_markup("copy_menu"))
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
        return COPY_AWAIT_RATIO
    context.user_data["copy_ratio"] = ratio
    
    # Delete previous bot message
    await delete_previous_bot_message(context, update.effective_chat.id)
    
    response = await update.message.reply_html(
        "👌 Now send the <b>max SOL per trade</b> (e.g. <code>0.25</code>).",
        reply_markup=back_markup("copy_menu"),
    )
    await store_bot_message(context, response.message_id)
    return COPY_AWAIT_MAX

async def copy_add_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Keep user message visible for transparency
    
    try:
        max_sol = float((update.message.text or "").strip())
        if max_sol <= 0 or max_sol > 1000:
            raise ValueError()
    except Exception:
        response = await update.message.reply_html("❌ Invalid max SOL. Example: <code>0.25</code>.",
                                        reply_markup=back_markup("copy_menu"))
        await track_bot_message(context, response.message_id)
        # Auto-cleanup error message after 5 minutes
        asyncio.create_task(auto_cleanup_success_message(context, update.effective_chat.id, response.message_id, 5))
        return COPY_AWAIT_MAX

    user_id = update.effective_user.id
    leader = context.user_data.get("copy_leader")
    ratio  = context.user_data.get("copy_ratio", 1.0)

    database.copy_follow_upsert(user_id, leader, ratio=ratio, max_sol_per_trade=max_sol, active=True)

    # clear context & return to menu
    context.user_data.pop("copy_leader", None)
    context.user_data.pop("copy_ratio", None)
    
    # Delete previous bot message
    await delete_previous_bot_message(context, update.effective_chat.id)
    await clear_message_context(context)

    await update.message.reply_html("✅ Leader added & activated.", reply_markup=back_markup("copy_menu"))
    # refresh menu
    #fake_cb = Update(update.update_id, callback_query=update.to_dict().get("callback_query"))
    #await handle_copy_menu(update, context)  # or just let the user click Back
    

async def handle_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    keyboard_buttons = []
    keyboard_buttons.append(
        [
            InlineKeyboardButton("Create Solana Wallet", callback_data="create_wallet:solana"),
            InlineKeyboardButton("🗑️ Delete", callback_data="delete_wallet:solana"),
        ]
    )
    keyboard_buttons.append([InlineKeyboardButton("📤 Export Private Key", callback_data="export_private_key")])
    keyboard_buttons.append([InlineKeyboardButton("💸 Withdraw SOL", callback_data="withdraw_sol")])
    keyboard_buttons.append([InlineKeyboardButton("Import Wallet", callback_data="import_wallet")])
    keyboard_buttons.append([InlineKeyboardButton("Back to Menu", callback_data="back_to_main_menu")])
    
    try:
        # Try to edit current message first
        await query.edit_message_text("Wallet Options:", reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        # Track this message for cleanup
        await track_bot_message(context, query.message.message_id)
    except Exception:
        # If edit fails, send new message
        response = await query.message.reply_text("Wallet Options:", reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        await track_bot_message(context, response.message_id)
    
    # Clean up other tracked messages (but not the current one)
    await delete_all_bot_messages_except_current(context, chat_id, query.message.message_id)
    clear_user_context(context)

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

async def handle_export_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # Get user wallet
    wallet_info = database.get_user_wallet(user_id)
    if not wallet_info.get("address") or not wallet_info.get("private_key"):
        await query.edit_message_text(
            "❌ No wallet found. Please create or import a wallet first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return
    
    # Security confirmation first
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Export My Private Key", callback_data="confirm_export_pk")],
        [InlineKeyboardButton("❌ Cancel", callback_data="menu_wallet")]
    ]
    await query.edit_message_text(
        "⚠️ <b>SECURITY WARNING</b>\n\n"
        "You are about to export your private key.\n"
        "• Keep it safe and private\n"
        "• Anyone with this key can access your wallet\n"
        "• Never share it with anyone\n\n"
        "Are you sure you want to proceed?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_confirm_export_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # Get wallet private key
    wallet_info = database.get_user_wallet(user_id)
    private_key = wallet_info.get("private_key")
    address = wallet_info.get("address")
    
    if not private_key or not address:
        await query.edit_message_text(
            "❌ Error: Could not retrieve wallet information.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return
    
    response = await query.edit_message_text(
        "🔐 <b>Your Wallet Export</b>\n\n"
        f"Address:\n<code>{address}</code>\n\n"
        "⚠️ <b>Private Key (BACKUP & DO NOT SHARE):</b>\n"
        f"<code>{private_key}</code>\n\n"
        "💡 <b>Important:</b>\n"
        "• Save this private key in a secure location\n"
        "• Never share it with anyone\n"
        "• This message will auto-delete in 2 minutes for security",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete Now", callback_data="delete_private_key_msg"), InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
    )
    
    # Auto-delete after 2 minutes for security
    async def delayed_delete():
        await asyncio.sleep(120)  # 2 minutes
        try:
            await response.delete()
        except Exception:
            pass
    
    # Start the delayed delete task
    asyncio.create_task(delayed_delete())

async def handle_delete_private_key_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete private key message immediately"""
    query = update.callback_query
    await query.answer("Message deleted for security 🗑️")
    try:
        await query.message.delete()
    except Exception:
        pass

async def handle_withdraw_sol_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start withdraw conversation"""
    clear_user_context(context)
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # Get user wallet and balance
    wallet_info = database.get_user_wallet(user_id)
    if not wallet_info.get("address") or not wallet_info.get("private_key"):
        await query.edit_message_text(
            "❌ No wallet found. Please create or import a wallet first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return ConversationHandler.END
    
    address = wallet_info.get("address")
    context.user_data["withdraw_wallet_info"] = wallet_info
    
    # Get current SOL balance
    try:
        balance = solana_client.get_balance(address)
        context.user_data["current_balance"] = balance
        
        await query.edit_message_text(
            f"💰 <b>Withdraw SOL</b>\n\n"
            f"Current Balance: <b>{balance:.6f} SOL</b>\n"
            f"Wallet: <code>{address}</code>\n\n"
            "How much SOL do you want to withdraw?\n"
            "Send a number (e.g., <code>0.5</code>) or <code>all</code> to withdraw everything.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
        )
        return WITHDRAW_AMOUNT
    except Exception as e:
        await query.edit_message_text(
            f"❌ Error getting balance: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return ConversationHandler.END

async def handle_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle amount input"""
    # Clean up bot messages on user text input
    chat_id = update.effective_chat.id
    await ensure_message_cleanup_on_user_action(context, chat_id)
    
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    amount_str = update.message.text.strip()
    current_balance = context.user_data.get("current_balance", 0)
    
    # Auto-delete user input message
    try:
        await update.message.delete()
    except Exception:
        pass
    
    try:
        if amount_str.lower() == "all":
            # Reserve some for transaction fee
            amount = max(0, current_balance - 0.005)
            if amount <= 0:
                response = await update.message.reply_text(
                    "❌ Insufficient balance for withdrawal (need to reserve for transaction fee).",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
                )
                # Store message for potential cleanup
                context.user_data["last_bot_message"] = response.message_id
                return WITHDRAW_AMOUNT
        else:
            amount = float(amount_str)
            if amount <= 0:
                response = await update.message.reply_text(
                    "❌ Amount must be greater than 0.\nPlease send a valid amount or 'all':",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
                )
                context.user_data["last_bot_message"] = response.message_id
                return WITHDRAW_AMOUNT
            
            if amount > current_balance:
                response = await update.message.reply_text(
                    f"❌ Insufficient balance. You have {current_balance:.6f} SOL.\nPlease send a valid amount:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
                )
                context.user_data["last_bot_message"] = response.message_id
                return WITHDRAW_AMOUNT
        
        context.user_data["withdraw_amount"] = amount
        
        # Delete previous bot message if exists
        if context.user_data.get("last_bot_message"):
            try:
                await update.message.get_bot().delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data["last_bot_message"]
                )
            except Exception:
                pass
        
        response = await update.message.reply_text(
            f"✅ Amount: <b>{amount:.6f} SOL</b>\n\n"
            "Now send the destination address:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
        )
        context.user_data["last_bot_message"] = response.message_id
        return WITHDRAW_ADDRESS
        
    except ValueError:
        response = await update.message.reply_text(
            "❌ Invalid amount. Please enter a number or 'all':",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
        )
        context.user_data["last_bot_message"] = response.message_id
        return WITHDRAW_AMOUNT

async def handle_withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle address input and execute withdrawal"""
    to_addr = update.message.text.strip()
    amount = context.user_data.get("withdraw_amount")
    wallet_info = context.user_data.get("withdraw_wallet_info")
    
    # Auto-delete user input message
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if not wallet_info or not amount:
        response = await update.message.reply_text(
            "❌ Session expired. Please start over.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return ConversationHandler.END
    
    private_key = wallet_info.get("private_key")
    
    # Validate address format (basic check)
    if len(to_addr) < 32 or len(to_addr) > 44:
        # Delete previous bot message if exists
        if context.user_data.get("last_bot_message"):
            try:
                await update.message.get_bot().delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data["last_bot_message"]
                )
            except Exception:
                pass
                
        response = await update.message.reply_text(
            "❌ Invalid address format. Please send a valid Solana address:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
        )
        context.user_data["last_bot_message"] = response.message_id
        return WITHDRAW_ADDRESS
    
    # Delete previous bot message if exists
    if context.user_data.get("last_bot_message"):
        try:
            await update.message.get_bot().delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data["last_bot_message"]
            )
        except Exception:
            pass
    
    # Show confirmation
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Withdrawal", callback_data="withdraw_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]
    ]
    
    response = await update.message.reply_text(
        f"🔍 <b>Confirm Withdrawal</b>\n\n"
        f"Amount: <b>{amount:.6f} SOL</b>\n"
        f"To: <code>{to_addr}</code>\n"
        f"Estimated Fee: ~0.005 SOL\n\n"
        "Are you sure you want to proceed?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    context.user_data["withdraw_to_address"] = to_addr
    context.user_data["last_bot_message"] = response.message_id
    return WITHDRAW_ADDRESS

async def handle_withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute the withdrawal"""
    query = update.callback_query
    await query.answer()
    
    amount = context.user_data.get("withdraw_amount")
    to_addr = context.user_data.get("withdraw_to_address")
    wallet_info = context.user_data.get("withdraw_wallet_info")
    
    if not wallet_info or not amount or not to_addr:
        await query.edit_message_text(
            "❌ Session expired. Please start over.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
        return ConversationHandler.END
    
    private_key = wallet_info.get("private_key")
    
    # Execute withdrawal
    await query.edit_message_text("⏳ Processing withdrawal...", parse_mode="HTML")
    
    result = solana_client.send_sol(private_key, to_addr, amount)
    
    if result.startswith("Error"):
        await query.edit_message_text(
            f"❌ <b>Withdrawal Failed</b>\n\n{result}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
    else:
        await query.edit_message_text(
            f"✅ <b>Withdrawal Successful!</b>\n\n"
            f"Amount: <b>{amount:.6f} SOL</b>\n"
            f"To: <code>{to_addr}</code>\n"
            f"Transaction: <code>{result}</code>\n\n"
            f"🔗 <a href='https://solscan.io/tx/{result}'>View on Solscan</a>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
        )
    
    # Clear context
    context.user_data.pop("withdraw_amount", None)
    context.user_data.pop("withdraw_to_address", None)
    context.user_data.pop("withdraw_wallet_info", None)
    context.user_data.pop("current_balance", None)
    context.user_data.pop("last_bot_message", None)
    
    return ConversationHandler.END

async def handle_withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel withdrawal"""
    query = update.callback_query
    await query.answer()
    
    # Clear context
    context.user_data.pop("withdraw_amount", None)
    context.user_data.pop("withdraw_to_address", None)
    context.user_data.pop("withdraw_wallet_info", None)
    context.user_data.pop("current_balance", None)
    context.user_data.pop("last_bot_message", None)
    
    await query.edit_message_text(
        "❌ Withdrawal cancelled.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="menu_wallet")]])
    )
    return ConversationHandler.END

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clean up bot messages on any user text input
    chat_id = update.effective_chat.id
    await ensure_message_cleanup_on_user_action(context, chat_id)
    
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    user_id = update.effective_user.id
    text = update.message.text.strip().replace("\n", " ")
    command, *args = text.split(maxsplit=1)
    command = command.lower()

    if command == "import":
        # Auto-delete user message containing private key for security
        await delete_sensitive_user_message(update)
        
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
            # Message already deleted at start for security
            pass
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
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = query.from_user.id
    user_mention = query.from_user.mention_html()
    
    # Clear hidden assets when returning to main menu
    if "assets_state" in context.user_data:
        context.user_data["assets_state"]["hidden_mints"] = set()
    
    welcome_text = await get_dynamic_start_message_text(user_id, user_mention)
    
    try:
        # Try to edit current message first
        await query.edit_message_text(welcome_text, reply_markup=get_start_menu_keyboard(user_id), parse_mode="HTML")
        # Track this message for cleanup
        await track_bot_message(context, query.message.message_id)
    except Exception:
        # If edit fails, send new message and clean up old ones
        response = await query.message.reply_html(welcome_text, reply_markup=get_start_menu_keyboard(user_id))
        await track_bot_message(context, response.message_id)
    
    # Clean up other tracked messages (but not the current one)
    await delete_all_bot_messages_except_current(context, chat_id, query.message.message_id)
    clear_user_context(context)

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

async def handle_back_to_token_panel_outside_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper for back_to_token_panel that works outside conversations."""
    # Clear any stale context first
    
    # Ensure we have token_address
    mint = context.user_data.get("token_address")
    if not mint:
        return await handle_back_to_buy_sell_menu(update, context)
    
    # Build fresh token panel
    q = update.callback_query
    await q.answer()
    panel = await build_token_panel(q.from_user.id, mint)
    
    # Reset conversation state cleanly - force restart conversation
    # This ensures buttons will work properly
    context.user_data["in_trade_conversation"] = True
    
    try:
        await q.edit_message_text(panel, reply_markup=token_panel_keyboard(context), parse_mode="HTML")
    except Exception:
        # Fallback: send new message if edit fails
        await q.message.reply_html(panel, reply_markup=token_panel_keyboard(context))

async def dummy_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"🛠️ Feature `{query.data}` is under development.",
        reply_markup=back_markup("back_to_main_menu"),
    )

# --- CU Settings UI Functions ---
def _tier_of(cu_val: Optional[int]) -> str:
    """Return a display string for the current CU price with SOL and lamports values."""
    from cu_config import cu_to_sol_priority_fee, DEX_CU_PRICE_MICRO_FAST, DEX_CU_PRICE_MICRO_TURBO, DEX_CU_PRICE_MICRO_ULTRA
    
    if cu_val is None or cu_val == 0:
        return "OFF (0 SOL)"
    
    # Use current environment values for comparison
    current_fast = DEX_CU_PRICE_MICRO_FAST
    current_turbo = DEX_CU_PRICE_MICRO_TURBO  
    current_ultra = DEX_CU_PRICE_MICRO_ULTRA
    
    sol_fee = cu_to_sol_priority_fee(cu_val, 200000)
    lamports = int(sol_fee * 1_000_000_000)
    
    if cu_val == current_fast:
        return f"FAST ({sol_fee:.3f} SOL = {lamports:,} lamports)"
    elif cu_val == current_turbo:
        return f"TURBO ({sol_fee:.3f} SOL = {lamports:,} lamports)"
    elif cu_val == current_ultra:
        return f"ULTRA ({sol_fee:.3f} SOL = {lamports:,} lamports)"
    else:
        # Add warning for excessive custom values
        if cu_val >= 250000:  # Safety cap threshold
            return f"CUSTOM ({sol_fee:.3f} SOL = {lamports:,} lamports) ⚠️ CAPPED"
        else:
            return f"CUSTOM ({sol_fee:.3f} SOL = {lamports:,} lamports)"

def _settings_keyboard(user_id: int):
    """Return the elegant settings menu keyboard with all options."""
    # Get current settings
    buy_slip = get_user_slippage_buy(user_id)
    sell_slip = get_user_slippage_sell(user_id)
    anti_mev = get_user_anti_mev(user_id)
    
    return InlineKeyboardMarkup([
        # Priority Fee Settings
        [InlineKeyboardButton("⚡ Priority Fees", callback_data="settings_priority_fees")],
        
        # Slippage Settings
        [InlineKeyboardButton(f"📈 Buy Slippage: {buy_slip/100:.1f}%", callback_data="settings_slippage_buy"),
         InlineKeyboardButton(f"📉 Sell Slippage: {sell_slip/100:.1f}%", callback_data="settings_slippage_sell")],
        
        # Anti-MEV & Optimization
        [InlineKeyboardButton(f"🛡️ Anti-MEV: {'✅ ON' if anti_mev else '❌ OFF'}", callback_data="settings_toggle_antimev")],
        [InlineKeyboardButton("🚀 Jupiter Optimization", callback_data="settings_jupiter_opts")],
        
        # Language & Back  
        [InlineKeyboardButton("🌐 Language", callback_data="change_language")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ])

async def handle_menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the elegant settings menu callback."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    # Get current priority tier for display
    user_cu_price = get_user_cu_price(str(user_id))
    current_tier = _tier_of(user_cu_price)
    
    anti_mev_status = get_user_anti_mev(user_id)
    
    text = f"⚙️ <b>Bot Settings</b>\n\n"
    text += f"Priority Tier: <code>{current_tier}</code>\n"
    text += f"🛡️ Anti-MEV: {'✅ <b>ACTIVE</b>' if anti_mev_status else '❌ DISABLED'}\n\n"
    text += "Configure your trading preferences below:"
    
    await q.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=_settings_keyboard(user_id)
    )

# New settings handlers
async def handle_settings_priority_fees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle priority fees submenu."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    user_cu_price = get_user_cu_price(str(user_id))
    current = _tier_of(user_cu_price)
    text = f"⚡ <b>Priority Fees</b>\n\n📊 Current: <code>{current}</code>\n\nSelect priority level:"
    
    keyboard = [
        [InlineKeyboardButton("🔴 OFF", callback_data="set_cu:off"), 
         InlineKeyboardButton("🟡 FAST", callback_data="set_cu:fast")],
        [InlineKeyboardButton("🟠 TURBO", callback_data="set_cu:turbo"), 
         InlineKeyboardButton("🔥 ULTRA", callback_data="set_cu:ultra")],
        [InlineKeyboardButton("✏️ Custom", callback_data="set_cu:custom")],
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
    ]
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_settings_slippage_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy slippage settings."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    current = get_user_slippage_buy(user_id)
    text = f"📈 <b>Buy Slippage</b>\n\nCurrent: <code>{current/100:.1f}%</code>\n\nSelect slippage tolerance:"
    
    keyboard = [
        [InlineKeyboardButton("0.5%", callback_data="set_slippage_buy:50"),
         InlineKeyboardButton("1%", callback_data="set_slippage_buy:100"),
         InlineKeyboardButton("3%", callback_data="set_slippage_buy:300")],
        [InlineKeyboardButton("5%", callback_data="set_slippage_buy:500"),
         InlineKeyboardButton("10%", callback_data="set_slippage_buy:1000"),
         InlineKeyboardButton("20%", callback_data="set_slippage_buy:2000")],
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
    ]
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_settings_slippage_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell slippage settings."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    current = get_user_slippage_sell(user_id)
    text = f"📉 <b>Sell Slippage</b>\n\nCurrent: <code>{current/100:.1f}%</code>\n\nSelect slippage tolerance:"
    
    keyboard = [
        [InlineKeyboardButton("0.5%", callback_data="set_slippage_sell:50"),
         InlineKeyboardButton("1%", callback_data="set_slippage_sell:100"),
         InlineKeyboardButton("3%", callback_data="set_slippage_sell:300")],
        [InlineKeyboardButton("5%", callback_data="set_slippage_sell:500"),
         InlineKeyboardButton("10%", callback_data="set_slippage_sell:1000"),
         InlineKeyboardButton("20%", callback_data="set_slippage_sell:2000")],
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
    ]
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_settings_toggle_antimev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Anti-MEV protection."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    current = get_user_anti_mev(user_id)
    new_value = not current
    
    # Update database
    user_settings_upsert(user_id, anti_mev=new_value)
    
    status = "✅ ENABLED" if new_value else "❌ DISABLED"
    await q.answer(f"Anti-MEV protection {status}", show_alert=True)
    
    # Refresh settings menu
    await handle_menu_settings(update, context)

async def handle_settings_jupiter_opts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Jupiter optimization settings."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    versioned_tx = get_user_jupiter_versioned_tx(user_id)
    skip_preflight = get_user_jupiter_skip_preflight(user_id)
    
    text = f"🚀 <b>Jupiter Optimization</b>\n\n"
    text += f"Versioned Transactions: {'✅ ON' if versioned_tx else '❌ OFF'}\n"
    text += f"Skip Preflight: {'✅ ON' if skip_preflight else '❌ OFF'}\n\n"
    text += "<i>Versioned TX = Faster processing\nSkip Preflight = Higher speed, higher risk</i>"
    
    keyboard = [
        [InlineKeyboardButton(f"📦 Versioned TX: {'ON' if versioned_tx else 'OFF'}", 
                            callback_data="toggle_jupiter_versioned")],
        [InlineKeyboardButton(f"⚡ Skip Preflight: {'ON' if skip_preflight else 'OFF'}", 
                            callback_data="toggle_jupiter_preflight")],
        [InlineKeyboardButton("⬅️ Back to Settings", callback_data="menu_settings")]
    ]
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_set_priority_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle priority tier selection."""
    q = update.callback_query
    await q.answer()
    
    user_id = str(update.effective_user.id)
    choice = q.data.split(":", 1)[1]  # "set_cu:off" -> "off"

# Slippage handlers
async def handle_set_slippage_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy slippage selection."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    slippage_bps = int(q.data.split(":", 1)[1])  # "set_slippage_buy:500" -> 500
    
    # Update database
    user_settings_upsert(user_id, slippage_buy=slippage_bps)
    
    await q.answer(f"Buy slippage set to {slippage_bps/100:.1f}%", show_alert=True)
    
    # Refresh settings menu
    await handle_menu_settings(update, context)

async def handle_set_slippage_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sell slippage selection."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    slippage_bps = int(q.data.split(":", 1)[1])  # "set_slippage_sell:500" -> 500
    
    # Update database
    user_settings_upsert(user_id, slippage_sell=slippage_bps)
    
    await q.answer(f"Sell slippage set to {slippage_bps/100:.1f}%", show_alert=True)
    
    # Refresh settings menu
    await handle_menu_settings(update, context)

# Jupiter optimization toggles
async def handle_toggle_jupiter_versioned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Jupiter versioned transactions."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    current = get_user_jupiter_versioned_tx(user_id)
    new_value = not current
    
    # Update database
    user_settings_upsert(user_id, jupiter_versioned_tx=new_value)
    
    status = "ENABLED" if new_value else "DISABLED"
    await q.answer(f"Versioned transactions {status}", show_alert=True)
    
    # Refresh Jupiter settings
    await handle_settings_jupiter_opts(update, context)

async def handle_toggle_jupiter_preflight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Jupiter skip preflight."""
    q = update.callback_query
    user_id = q.from_user.id
    await q.answer()
    
    current = get_user_jupiter_skip_preflight(user_id)
    new_value = not current
    
    # Update database
    user_settings_upsert(user_id, jupiter_skip_preflight=new_value)
    
    status = "ENABLED" if new_value else "DISABLED"
    await q.answer(f"Skip preflight {status}", show_alert=True)
    
    # Refresh Jupiter settings
    await handle_settings_jupiter_opts(update, context)
    
    if choice == "custom":
        context.user_data["awaiting_custom_cu"] = True
        await q.edit_message_text(
            "✏️ Send a number for <b>computeUnitPriceMicroLamports</b>\n"
            "Example: <code>2000</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_settings")]]),
        )
        return SET_CU_PRICE
    elif choice == "off":
        user_cu_price = None
        tier_name = "OFF"
        note = "Priority fee set to OFF (no extra fee)."
    elif choice == "fast":
        user_cu_price = DEX_CU_PRICE_MICRO_FAST
        tier_name = "FAST"
        fee_sol = PRIORITY_FEE_SOL_FAST
        note = f"FAST tier: {fee_sol} SOL priority fee ({user_cu_price} μ-lamports/CU)."
    elif choice == "turbo":
        user_cu_price = DEX_CU_PRICE_MICRO_TURBO
        tier_name = "TURBO"
        fee_sol = PRIORITY_FEE_SOL_TURBO
        note = f"TURBO tier: {fee_sol} SOL priority fee ({user_cu_price} μ-lamports/CU)."
    elif choice == "ultra":
        user_cu_price = DEX_CU_PRICE_MICRO_ULTRA
        tier_name = "ULTRA"
        fee_sol = PRIORITY_FEE_SOL_ULTRA
        note = f"ULTRA tier: {fee_sol} SOL priority fee ({user_cu_price} μ-lamports/CU)."
    else:
        note = "Unknown option."
        user_cu_price = None
        tier_name = "UNKNOWN"

    # Save to persistent storage
    UserSettings.set_user_cu_price(user_id, user_cu_price)
    UserSettings.set_user_priority_tier(user_id, tier_name.lower() if tier_name != "OFF" else None)

    await q.edit_message_text(
        f"✅ {note}\nCurrent: <code>{_tier_of(user_cu_price)}</code>",
        parse_mode="HTML",
        reply_markup=_settings_keyboard(),
    )

async def handle_custom_cu_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle custom CU price input."""
    # Keep user message visible for transparency
    
    if not context.user_data.get("awaiting_custom_cu"):
        return SET_CU_PRICE
    
    user_id = str(update.effective_user.id)
    txt = (update.message.text or "").strip()
    try:
        # accept plain int; also support units like '2_000'
        val = int(txt.replace("_", ""))
        if val < 0 or val > 10_000_000:
            raise ValueError("out_of_range")
        user_cu_price = val if val > 0 else None
        
        # Save to persistent storage
        UserSettings.set_user_cu_price(user_id, user_cu_price)
        UserSettings.set_user_priority_tier(user_id, "custom" if user_cu_price else None)
        
        context.user_data.pop("awaiting_custom_cu", None)
        # Delete previous bot message
        await delete_previous_bot_message(context, update.effective_chat.id)
        await clear_message_context(context)
        
        await update.message.reply_html(
            f"✅ Custom priority set to <code>{_tier_of(user_cu_price)}</code>.",
            reply_markup=_settings_keyboard(),
        )
        return ConversationHandler.END
    except Exception:
        response = await update.message.reply_html(
            "❌ Invalid number. Send an integer like <code>2500</code>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_settings")]]),
        )
        await store_bot_message(context, response.message_id)
        return SET_CU_PRICE

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
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    keyboard = [
        [InlineKeyboardButton("🤖 Auto Trade - Pump.fun", callback_data="pumpfun_trade")],
        [InlineKeyboardButton("📉 Limit Orders", callback_data="dummy_limit_orders")],
        [InlineKeyboardButton("📈 Positions", callback_data="view_assets"), InlineKeyboardButton("👛 Wallet", callback_data="menu_wallet")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"), InlineKeyboardButton("💰 Referrals", callback_data="dummy_referrals")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_main_menu")],
    ]

    message_text = "Choose a trading option or enter a token address to start trading."
    
    try:
        # Try to edit current message first
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        # Track this message for cleanup
        await track_bot_message(context, query.message.message_id)
    except Exception:
        # If edit fails, send new message and clean up old ones
        response = await query.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        await track_bot_message(context, response.message_id)
    
    # Clean up other tracked messages (but not the current one)
    await delete_all_bot_messages_except_current(context, chat_id, query.message.message_id)
    clear_user_context(context)
    
    return AWAITING_TOKEN_ADDRESS

async def handle_limit_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle limit orders UI"""
    query = update.callback_query
    await query.answer()
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Create Buy Limit Order", callback_data="create_buy_limit")],
        [InlineKeyboardButton("📉 Create Sell Limit Order", callback_data="create_sell_limit")],
        [InlineKeyboardButton("📋 View Active Orders", callback_data="view_limit_orders")],
        [InlineKeyboardButton("❌ Cancel All Orders", callback_data="cancel_all_limits")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_buy_sell_menu")]
    ])
    
    text = "🎯 <b>Limit Orders</b>\n\n"
    text += "Set specific price targets for automatic buying or selling.\n\n"
    text += "<i>Note: This feature is under development.</i>"
    
    await query.edit_message_text(
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

async def handle_dummy_trade_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    
    if query.data == "dummy_limit_orders":
        await handle_limit_orders(update, context)
        return AWAITING_TOKEN_ADDRESS
    elif query.data.startswith("create_") or query.data.startswith("view_limit") or query.data.startswith("cancel_"):
        await query.answer(f"Limit order feature is under development.", show_alert=True)
        return AWAITING_TOKEN_ADDRESS
    
    await query.answer(f"Feature '{query.data}' is under development.", show_alert=True)
    return AWAITING_TOKEN_ADDRESS

async def handle_token_address_for_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clean up bot messages on user text input
    chat_id = update.effective_chat.id
    await ensure_message_cleanup_on_user_action(context, chat_id)
    
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    message = update.message if update.message else update.callback_query.message
    token_address = message.text.strip()

    if not _is_valid_pubkey(token_address):
        response = await message.reply_text(
            "❌ Invalid token address format. Please enter a valid Solana token address.",
            reply_markup=back_markup("back_to_main_menu"),
        )
        await track_bot_message(context, response.message_id)
        return AWAITING_TOKEN_ADDRESS

    context.user_data["token_address"] = token_address
    context.user_data["selected_dex"] = "jupiter"  # fixed route
    # Slippage now managed through database settings

    panel = await build_token_panel(update.effective_user.id, token_address)
    response = await message.reply_html(panel, reply_markup=token_panel_keyboard(context, update.effective_user.id))
    await track_bot_message(context, response.message_id)
    return AWAITING_TRADE_ACTION

async def handle_refresh_token_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    
    mint = context.user_data.get("token_address")
    if not mint:
        await q.answer("No token selected")
        return await handle_back_to_buy_sell_menu(update, context)
    
    # Immediate feedback with unique ID - show refresh is happening
    refresh_id = str(int(time.time() * 1000))[-4:]  # Last 4 digits of timestamp
    await q.answer(f"🔄 Refreshing #{refresh_id}...", show_alert=False)
    
    try:
        # Build panel with FORCED fresh data - no cache used
        panel = await build_token_panel(q.from_user.id, mint, force_fresh=True)
        
        # Update message with fresh data
        await q.edit_message_text(panel, reply_markup=token_panel_keyboard(context), parse_mode="HTML")
        # Track this edited message for cleanup
        await track_bot_message(context, q.message.message_id)
    except Exception as e:
        # Handle any errors gracefully
        await q.answer("⚠️ Refresh failed, try again", show_alert=False)
        
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
        result = await perform_trade(update, context, amount)
        # Only end conversation if trade was successful 
        return ConversationHandler.END if result else AWAITING_TRADE_ACTION

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
        result = await perform_trade(update, context, percentage)
        # Only end conversation if trade was successful
        return ConversationHandler.END if result else AWAITING_TRADE_ACTION

    await query.message.reply_text(
        "This action is not yet implemented.",
        reply_markup=back_markup("back_to_token_panel"),
    )
    return AWAITING_TRADE_ACTION

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clean up bot messages on user text input
    chat_id = update.effective_chat.id
    await ensure_message_cleanup_on_user_action(context, chat_id)
    
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            response = await update.message.reply_text(
                "❌ Amount must be greater than 0.",
                reply_markup=back_markup("back_to_token_panel"),
            )
            await store_bot_message(context, response.message_id)
            return AWAITING_AMOUNT
        context.user_data["trade_type"] = context.user_data.get("trade_type", "buy")
        context.user_data["amount_type"] = "sol"
        result = await perform_trade(update, context, amount)
        # Only end conversation if trade was successful
        return ConversationHandler.END if result else AWAITING_TRADE_ACTION
    except (ValueError, IndexError):
        response = await update.message.reply_text(
            "❌ Invalid amount. Please enter a valid number.",
            reply_markup=back_markup("back_to_token_panel"),
        )
        await store_bot_message(context, response.message_id)
        return AWAITING_AMOUNT

# ------------------------- FEE helper -------------------------
def _fee_ui(val_ui: float) -> float:
    return max(0.0, float(val_ui) * (FEE_BPS / 10_000.0))

async def _send_fee_sol_if_any(private_key: str, ui_amount: float, reason: str):
    if not FEE_ENABLED:
        return None
    fee_ui = _fee_ui(ui_amount)
    if fee_ui <= 0:
        return None
    if fee_ui < FEE_MIN_SOL:
        fee_ui = FEE_MIN_SOL
    tx = solana_client.send_sol(private_key, FEE_WALLET, fee_ui)
    return tx if isinstance(tx, str) and not tx.lower().startswith("error") else None

async def _send_fee_sol_direct(private_key: str, fee_amount: float, reason: str):
    # Kenapa: direct fee untuk BUY—hilangkan threshold agar selalu terkirim jika > 0
    if not FEE_ENABLED:
        return None
    amt = float(fee_amount)
    if amt <= 0:
        return None
    if amt < FEE_MIN_SOL:
        amt = FEE_MIN_SOL
    tx = solana_client.send_sol(private_key, FEE_WALLET, amt)
    return tx if isinstance(tx, str) and not tx.lower().startswith("error") else None


async def _prepare_buy_trade(wallet: dict, amount: float, token_mint: str, slippage_bps: int, user_id: str = None) -> dict:
    total_sol_to_spend = float(amount)
    fee_amount_ui = _fee_ui(total_sol_to_spend) if FEE_ENABLED else 0.0
    actual_swap_amount_ui = total_sol_to_spend - fee_amount_ui
    if actual_swap_amount_ui <= 0:
        return {"status": "error", "message": "❌ Amount is too small after fee."}

    try:
        sol_balance = await svc_get_sol_balance(wallet["address"])
    except Exception:
        sol_balance = 0.0

    # buffer priority fee + base tx fee
    if user_id:
        user_priority_tier = get_user_priority_tier(user_id)
        if user_priority_tier:
            from cu_config import choose_priority_fee_sol
            buffer_ui = choose_priority_fee_sol(user_priority_tier)
        else:
            user_cu_price = get_user_cu_price(user_id)
            if user_cu_price and user_cu_price > 0:
                from cu_config import cu_to_sol_priority_fee
                buffer_ui = cu_to_sol_priority_fee(user_cu_price, 200000)
            else:
                from cu_config import PRIORITY_FEE_SOL_DEFAULT
                buffer_ui = PRIORITY_FEE_SOL_DEFAULT
    else:
        from cu_config import PRIORITY_FEE_SOL_DEFAULT
        buffer_ui = PRIORITY_FEE_SOL_DEFAULT

    buffer_ui += 0.001  # biaya dasar

    # ✅ Perbaikan: cek saldo harus mencakup (swap + fee + buffer)
    need_ui = actual_swap_amount_ui + fee_amount_ui + buffer_ui
    if sol_balance < need_ui:
        return {
            "status": "error",
            "message": f"❌ Not enough SOL. Need ~{need_ui:.4f} SOL (amount + platform fee + fees), you have {sol_balance:.4f} SOL.",
        }

    # Kirim fee dulu (jika ada)
    if FEE_ENABLED and fee_amount_ui > 0:
        await _send_fee_sol_direct(wallet["private_key"], fee_amount_ui, "BUY")

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
        # Silently handle fee-related failures
        pass

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
    context: ContextTypes.DEFAULT_TYPE = None,
) -> bool:  # Return True if successful, False if failed
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
                await reply_err_html(message, f"⚠️ Position update failed: {e}", prev_cb=prev_cb, context=context)
            except Exception:
                pass

        sig = res.get("signature") or res.get("bundle")
        
        # Get token symbol for better display
        try:
            meta = await MetaCache.get(token_mint)
            token_symbol = (meta.get("symbol") or "").strip() or (meta.get("name") or "").strip() or f"{token_mint[:6].upper()}"
            success_msg = f"✅ {trade_type.capitalize()} {token_symbol} successful!"
        except:
            success_msg = "✅ Swap successful!"
            
        # Clean up all tracked messages (loading message already auto-deleted)
        await delete_all_bot_messages(context, message.chat_id)
        
        # Send success message instantly after loading disappears  
        await reply_ok_html(message, success_msg, prev_cb=prev_cb, signature=sig, context=context)
        
        context.user_data.pop("loading_message_id", None)
        return True
    else:
        err = res.get("error") if isinstance(res, dict) else res
        
        # Get token symbol for better display in error message
        try:
            meta = await MetaCache.get(token_mint)
            token_symbol = (meta.get("symbol") or "").strip() or (meta.get("name") or "").strip() or f"{token_mint[:6].upper()}"
            error_msg = f"❌ {trade_type.capitalize()} {token_symbol} failed: {short_err_text(str(err))}"
        except:
            error_msg = f"❌ Swap failed: {short_err_text(str(err))}"
            
        # Clean up all tracked messages (loading message already auto-deleted)
        await delete_all_bot_messages(context, message.chat_id)
        
        # Send error message instantly after loading disappears
        await reply_err_html(message, error_msg, prev_cb=prev_cb, context=context)
        
        context.user_data.pop("loading_message_id", None)
        return False


# ------------------------- Trade core -------------------------
async def perform_trade(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    amount,
    prev_cb_on_end: str | None = None,   # <-- dibuat optional
) -> bool:  # Return True if successful, False if failed
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
            context=context,
        )
        return False

    # context
    trade_type   = (context.user_data.get("trade_type") or "").lower()      # buy|sell
    amount_type  = (context.user_data.get("amount_type") or "").lower()     # sol|percentage
    token_mint   = context.user_data.get("token_address")
    # Get slippage from database instead of context
    buy_slip_bps = get_user_slippage_buy(user_id)
    sel_slip_bps = get_user_slippage_sell(user_id)

    if not token_mint:
        await reply_err_html(message, "❌ No token mint in context.", prev_cb="back_to_buy_sell_menu", context=context)
        return False

    # snapshot pra-trade
    try:
        pre_sol_ui   = await svc_get_sol_balance(wallet["address"])
        pre_token_ui = await svc_get_token_balance(wallet["address"], token_mint)
    except Exception:
        pre_sol_ui, pre_token_ui = 0.0, 0.0

    # siapkan parameter
    if trade_type == "buy":
        prep = await _prepare_buy_trade(wallet, amount, token_mint, buy_slip_bps, str(user_id))
    else:
        prep = await _prepare_sell_trade(wallet, amount, amount_type, token_mint, sel_slip_bps)
        if isinstance(prep, dict) and prep.get("pre_sol_ui") is not None:
            pre_sol_ui = float(prep["pre_sol_ui"])

    if prep.get("status") == "error":
        await reply_err_html(message, prep["message"], prev_cb=prev_cb, context=context)
        return False

    # Get token symbol for better display in loading message
    try:
        meta = await MetaCache.get(token_mint)
        token_symbol = (meta.get("symbol") or "").strip() or (meta.get("name") or "").strip() or f"{token_mint[:6].upper()}"
        loading_msg = f"⏳ Performing {trade_type} {token_symbol} via {selected_dex.capitalize()}…"
    except:
        loading_msg = f"⏳ Performing {trade_type} `{token_mint}` via {selected_dex.capitalize()}…"
    
    # Send loading message without buttons that will auto-delete in 0.5s for instant UX
    loading_response = await reply_loading_html(
        message,
        loading_msg,
        context=context,
    )
    context.user_data["loading_message_id"] = loading_response.message_id

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

            # Use priority tier system for better fee management
            user_priority_tier = get_user_priority_tier(str(user_id))
            user_cu_price = get_user_cu_price(str(user_id))  # fallback for legacy
            anti_mev_enabled = get_user_anti_mev(user_id)
            
            # REAL Anti-MEV implementation: Use local Jito bundles when enabled
            if anti_mev_enabled and JITO_ENABLED:
                # Use local Jito bundle implementation for REAL MEV protection
                res = await solana_client.perform_pumpfun_jito_bundle(
                    sender_private_key_json=wallet["private_key"],
                    amount=amt_param,
                    action=trade_type,
                    mint=token_mint,
                    bundle_count=1,  # Single transaction bundle
                    compute_unit_price_micro_lamports=user_cu_price,
                )
                
                # Convert bundle result to expected format
                if isinstance(res, str) and not res.startswith("Error"):
                    res = {"bundle": res}  # Format as expected by _handle_trade_response
            else:
                # Standard swap via trade service
                res = await pumpfun_swap(
                    private_key=wallet["private_key"],
                    action=trade_type,
                    mint=token_mint,
                    amount=amt_param,
                    denominated_in_sol=denom_sol,
                    slippage_bps=slip_pct * 100,  # convert percentage to basis points
                    priority_tier=user_priority_tier,  # NEW: Use tier system
                    compute_unit_price_micro_lamports=user_cu_price,  # Fallback
                    pool="auto",
                    use_jito=False,  # Use service-side when not using local bundles
                )
        else:
            # Use priority tier system for DEX swaps too
            user_priority_tier = get_user_priority_tier(str(user_id))
            user_cu_price = get_user_cu_price(str(user_id))  # fallback for legacy
            
            
            # Get Jupiter optimization and Anti-MEV settings from database
            enable_versioned_tx = get_user_jupiter_versioned_tx(user_id)
            skip_preflight = get_user_jupiter_skip_preflight(user_id)
            anti_mev_enabled = get_user_anti_mev(user_id)
            
            # REAL Anti-MEV implementation for Jupiter/DEX
            if anti_mev_enabled:
                enable_versioned_tx = True  # Force versioned TX for faster processing
                skip_preflight = False  # Disable skip preflight for safety
                
                # Force minimum TURBO priority for real MEV protection
                if user_priority_tier in ["off", "fast"]:
                    user_priority_tier = "turbo"  # Higher priority for MEV protection
                    
                # Add max accounts limit to reduce transaction size (harder to front-run)
                max_accounts = 20
            
            res = await dex_swap(
                private_key=wallet["private_key"],
                **prep["params"],
                priority_tier=user_priority_tier,  # Use tier system (TURBO for Anti-MEV)
                compute_unit_price_micro_lamports=user_cu_price,  # Fallback for legacy
                enable_versioned_tx=enable_versioned_tx,  # Jupiter optimization
                skip_preflight=skip_preflight,  # Jupiter optimization
                max_accounts=max_accounts if anti_mev_enabled else None,  # MEV protection
            )

        # handle sukses/gagal + update posisi
        success = await _handle_trade_response(
            message,
            res,
            trade_type=trade_type,
            wallet=wallet,
            user_id=user_id,
            token_mint=token_mint,
            pre_sol_ui=pre_sol_ui,
            pre_token_ui=pre_token_ui,
            prev_cb=prev_cb,
            context=context,
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
                pass

        return success

    except Exception as e:
        # Get token symbol for better display in error message
        try:
            meta = await MetaCache.get(token_mint)
            token_symbol = (meta.get("symbol") or "").strip() or (meta.get("name") or "").strip() or f"{token_mint[:6].upper()}"
            error_msg = f"❌ {trade_type.capitalize()} {token_symbol} failed: {short_err_text(str(e))}"
        except:
            error_msg = f"❌ An unexpected error occurred: {short_err_text(str(e))}"
            
        # Clean up all tracked messages (loading message already auto-deleted)
        await delete_all_bot_messages(context, message.chat_id)
        
        # Send error message instantly after loading disappears
        await reply_err_html(message, error_msg, prev_cb=prev_cb, context=context)
        
        context.user_data.pop("loading_message_id", None)
        return False
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
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    txt = (update.message.text or "").strip().replace("%", "")
    try:
        pct = float(txt)
        if pct <= 0 or pct > 100:
            raise ValueError("out of range")
        bps = int(round(pct * 100))
        # Update database instead of context
        tgt = context.user_data.get("slippage_target", "buy")
        user_id = update.effective_user.id
        if tgt == "sell":
            user_settings_upsert(user_id, slippage_sell=bps)
        else:
            user_settings_upsert(user_id, slippage_buy=bps)
        context.user_data.pop("awaiting_slippage_input", None)
        context.user_data.pop("slippage_target", None)
        # Clean up bot messages first
        chat_id = update.effective_chat.id
        await ensure_message_cleanup_on_user_action(context, chat_id)
        
        panel = await build_token_panel(update.effective_user.id, context.user_data.get("token_address", ""))
        
        # Show success message with updated panel in ONE message
        success_msg = f"✅ Slippage {tgt.upper()} set to {pct:.0f}%.\n\n{panel}"
        response = await update.message.reply_html(
            success_msg,
            reply_markup=token_panel_keyboard(context),
        )
        await track_bot_message(context, response.message_id)
        
        # Schedule cleanup for success message
        asyncio.create_task(auto_cleanup_success_message(context, chat_id, response.message_id, 3))
        return AWAITING_TRADE_ACTION
    except Exception:
        # Clean up bot messages on error too
        chat_id = update.effective_chat.id
        await ensure_message_cleanup_on_user_action(context, chat_id)
        
        response = await update.message.reply_text(
            "❌ Invalid number. Enter % like `5` or `18`.",
            reply_markup=back_markup("back_to_token_panel"),
        )
        await track_bot_message(context, response.message_id)
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
    # Slippage now managed through database settings

    panel_text = f"🤖 <b>Pump.fun Trade</b>\n\nToken: <code>{token_address}</code>"
    keyboard = [
        [
            InlineKeyboardButton("Buy (SOL)", callback_data="pumpfun_buy"),
            InlineKeyboardButton("Sell (%)", callback_data="pumpfun_sell"),
        ],
        # Slippage moved to settings menu
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ]
    response = await message.reply_html(panel_text, reply_markup=InlineKeyboardMarkup(keyboard))
    await track_bot_message(context, response.message_id)
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
    # Clean up bot messages on user text input
    chat_id = update.effective_chat.id
    await ensure_message_cleanup_on_user_action(context, chat_id)
    
    # Schedule user message for auto-cleanup in 5 minutes
    await track_and_schedule_user_message_cleanup(update, context)
    
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError()
        context.user_data["trade_type"] = "buy"
        result = await perform_trade(update, context, amount)
        # Only end conversation if trade was successful 
        return ConversationHandler.END if result else AWAITING_TRADE_ACTION
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
        # Slippage moved to settings menu
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_main_menu")]
    ]
    await query.edit_message_text(panel_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return PUMPFUN_AWAITING_ACTION

async def pumpfun_back_to_panel_outside_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper for pumpfun_back_to_panel that works outside conversations."""
    await pumpfun_back_to_panel(update, context)

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

    # --- CU Settings conversation ---
    cu_settings_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_set_priority_tier, pattern=r"^set_cu:custom$")],
        states={
            SET_CU_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_cu_input)],
        },
        fallbacks=[
            CallbackQueryHandler(handle_menu_settings, pattern="^menu_settings$"),
            CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$"),
        ],
    )
    application.add_handler(cu_settings_conv_handler)

    # --- Withdraw SOL conversation ---
    withdraw_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_withdraw_sol_start, pattern="^withdraw_sol$")],
        states={
            WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_amount),
                CallbackQueryHandler(handle_withdraw_cancel, pattern="^withdraw_cancel$"),
            ],
            WITHDRAW_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_address),
                CallbackQueryHandler(handle_withdraw_confirm, pattern="^withdraw_confirm$"),
                CallbackQueryHandler(handle_withdraw_cancel, pattern="^withdraw_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_withdraw_cancel, pattern="^withdraw_cancel$"),
            CallbackQueryHandler(handle_wallet_menu, pattern="^menu_wallet$"),
        ],
    )
    application.add_handler(withdraw_conv_handler)

    # --- Copy menu & item actions (once only) ---
    application.add_handler(CallbackQueryHandler(handle_copy_menu, pattern="^copy_menu$"))
    application.add_handler(CallbackQueryHandler(handle_copy_toggle, pattern=r"^copy_toggle:.+$"))
    application.add_handler(CallbackQueryHandler(handle_copy_remove, pattern=r"^copy_remove:.+$"))

    # --- Command & other conversations ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("userstats", handle_admin_user_stats))
    application.add_handler(trade_conv_handler)
    application.add_handler(pumpfun_conv_handler)

    # --- Back button handlers (needed outside conversations) ---
    application.add_handler(CallbackQueryHandler(handle_back_to_token_panel_outside_conv, pattern="^back_to_token_panel$"))
    application.add_handler(CallbackQueryHandler(pumpfun_back_to_panel_outside_conv, pattern="^pumpfun_back_to_panel$"))

    # --- Other callback menus ---
    application.add_handler(CallbackQueryHandler(handle_assets, pattern="^view_assets$"))
    application.add_handler(CallbackQueryHandler(handle_assets_callbacks, pattern=r"^assets_.*$"))
    application.add_handler(CallbackQueryHandler(handle_wallet_menu, pattern="^menu_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_create_wallet_callback, pattern=r"^create_wallet:.*$"))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$"))
    application.add_handler(CallbackQueryHandler(handle_import_wallet, pattern="^import_wallet$"))
    application.add_handler(CallbackQueryHandler(handle_delete_wallet, pattern=r"^delete_wallet:solana$"))
    application.add_handler(CallbackQueryHandler(handle_export_private_key, pattern="^export_private_key$"))
    application.add_handler(CallbackQueryHandler(handle_confirm_export_private_key, pattern="^confirm_export_pk$"))
    application.add_handler(CallbackQueryHandler(handle_delete_private_key_msg, pattern="^delete_private_key_msg$"))
    application.add_handler(CallbackQueryHandler(handle_send_asset, pattern="^send_asset$"))
    # Settings handlers - Enhanced settings UI
    application.add_handler(CallbackQueryHandler(handle_menu_settings, pattern=r"^menu_settings$"))
    application.add_handler(CallbackQueryHandler(handle_settings_priority_fees, pattern=r"^settings_priority_fees$"))
    application.add_handler(CallbackQueryHandler(handle_settings_slippage_buy, pattern=r"^settings_slippage_buy$"))
    application.add_handler(CallbackQueryHandler(handle_settings_slippage_sell, pattern=r"^settings_slippage_sell$"))
    application.add_handler(CallbackQueryHandler(handle_settings_toggle_antimev, pattern=r"^settings_toggle_antimev$"))
    application.add_handler(CallbackQueryHandler(handle_settings_jupiter_opts, pattern=r"^settings_jupiter_opts$"))
    
    # Priority tier handlers
    application.add_handler(CallbackQueryHandler(handle_set_priority_tier, pattern=r"^set_cu:(off|fast|turbo|ultra)$"))
    
    # Slippage handlers
    application.add_handler(CallbackQueryHandler(handle_set_slippage_buy, pattern=r"^set_slippage_buy:\d+$"))
    application.add_handler(CallbackQueryHandler(handle_set_slippage_sell, pattern=r"^set_slippage_sell:\d+$"))
    
    # Jupiter optimization handlers
    application.add_handler(CallbackQueryHandler(handle_toggle_jupiter_versioned, pattern=r"^toggle_jupiter_versioned$"))
    application.add_handler(CallbackQueryHandler(handle_toggle_jupiter_preflight, pattern=r"^toggle_jupiter_preflight$"))
    
    # Other dummy handlers
    application.add_handler(
        CallbackQueryHandler(
            dummy_response,
            pattern=r"^(invite_friends|copy_trading|limit_order|change_language|menu_help)$",
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

    async def set_webhook_and_run():
        asyncio.run(set_webhook_and_run())
        await application.bot.set_webhook(url=WEBHOOK_URL)
        application.run_webhook(
            listen="0.0.0.0",
            port=8443,
            webhook_url="https://yourdomain.com/webhook"
        )

    application.post_init = _on_start
    application.post_shutdown = _on_shutdown

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
     
if __name__ == "__main__":
    main()
        