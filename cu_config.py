# cu_config.py - Compute Unit price configuration utilities
import os
from enum import Enum
from typing import Optional

# Default per-CU (micro-lamports) — bisa dioverride ENV
DEX_CU_PRICE_MICRO_DEFAULT = int(os.getenv("DEX_CU_PRICE_MICRO", "0"))
DEX_CU_PRICE_MICRO_FAST = int(os.getenv("DEX_CU_PRICE_MICRO_FAST", "500"))
DEX_CU_PRICE_MICRO_TURBO = int(os.getenv("DEX_CU_PRICE_MICRO_TURBO", "2000"))
DEX_CU_PRICE_MICRO_ULTRA = int(os.getenv("DEX_CU_PRICE_MICRO_ULTRA", "10000"))

class PriorityTier(str, Enum):
    FAST = "fast"
    TURBO = "turbo"
    ULTRA = "ultra"

def choose_cu_price(tier: Optional[str]) -> Optional[int]:
    """Choose compute unit price based on priority tier."""
    if not tier:
        return DEX_CU_PRICE_MICRO_DEFAULT or None
    t = str(tier).lower()
    if t == PriorityTier.FAST: return DEX_CU_PRICE_MICRO_FAST
    if t == PriorityTier.TURBO: return DEX_CU_PRICE_MICRO_TURBO
    if t == PriorityTier.ULTRA: return DEX_CU_PRICE_MICRO_ULTRA
    return DEX_CU_PRICE_MICRO_DEFAULT or None

def cu_to_sol_priority_fee(cu_price_micro: Optional[int], estimated_cu: int = 200000) -> float:
    """Convert CU price (micro-lamports per CU) to SOL-based priority fee."""
    if cu_price_micro is None or cu_price_micro <= 0:
        return 0.00005  # default fallback
    # Formula: (cu_price_micro * estimated_cu) / 1e9 = priority fee in SOL
    return max(0.00001, (cu_price_micro * estimated_cu) / 1e9)