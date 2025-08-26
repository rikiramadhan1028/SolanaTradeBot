# cu_config.py - Compute Unit price configuration utilities
import os
from enum import Enum
from typing import Optional

# SOL-based priority fee tiers (direct SOL amounts)
PRIORITY_FEE_SOL_DEFAULT = float(os.getenv("PRIORITY_FEE_SOL_DEFAULT", "0.00005"))
PRIORITY_FEE_SOL_FAST = float(os.getenv("PRIORITY_FEE_SOL_FAST", "0.001"))     # 0.001 SOL
PRIORITY_FEE_SOL_TURBO = float(os.getenv("PRIORITY_FEE_SOL_TURBO", "0.005"))   # 0.005 SOL  
PRIORITY_FEE_SOL_ULTRA = float(os.getenv("PRIORITY_FEE_SOL_ULTRA", "0.01"))    # 0.01 SOL

# Legacy CU-based for backward compatibility
DEX_CU_PRICE_MICRO_DEFAULT = int(os.getenv("DEX_CU_PRICE_MICRO", "0"))
DEX_CU_PRICE_MICRO_FAST = int(os.getenv("DEX_CU_PRICE_MICRO_FAST", "5000"))    # ~0.001 SOL
DEX_CU_PRICE_MICRO_TURBO = int(os.getenv("DEX_CU_PRICE_MICRO_TURBO", "25000"))  # ~0.005 SOL
DEX_CU_PRICE_MICRO_ULTRA = int(os.getenv("DEX_CU_PRICE_MICRO_ULTRA", "50000"))  # ~0.01 SOL

class PriorityTier(str, Enum):
    FAST = "fast"
    TURBO = "turbo"
    ULTRA = "ultra"

def choose_priority_fee_sol(tier: Optional[str]) -> float:
    """Choose priority fee in SOL based on tier. Primary method."""
    if not tier:
        return PRIORITY_FEE_SOL_DEFAULT
    t = str(tier).lower()
    if t == PriorityTier.FAST: return PRIORITY_FEE_SOL_FAST
    if t == PriorityTier.TURBO: return PRIORITY_FEE_SOL_TURBO
    if t == PriorityTier.ULTRA: return PRIORITY_FEE_SOL_ULTRA
    return PRIORITY_FEE_SOL_DEFAULT

def choose_cu_price(tier: Optional[str]) -> Optional[int]:
    """Choose compute unit price based on priority tier. Legacy method."""
    if not tier:
        return DEX_CU_PRICE_MICRO_DEFAULT or None
    t = str(tier).lower()
    if t == PriorityTier.FAST: return DEX_CU_PRICE_MICRO_FAST
    if t == PriorityTier.TURBO: return DEX_CU_PRICE_MICRO_TURBO
    if t == PriorityTier.ULTRA: return DEX_CU_PRICE_MICRO_ULTRA
    return DEX_CU_PRICE_MICRO_DEFAULT or None

def sol_to_cu_price(priority_fee_sol: float, estimated_cu: int = 200000) -> int:
    """Convert SOL priority fee to CU price (micro-lamports per CU)."""
    if priority_fee_sol <= 0:
        return 0
    # Formula: (priority_fee_sol * 1e9) / estimated_cu = cu_price_micro
    return max(1, int((priority_fee_sol * 1e9) / estimated_cu))

def cu_to_sol_priority_fee(cu_price_micro: Optional[int], estimated_cu: int = 200000) -> float:
    """Convert CU price (micro-lamports per CU) to SOL-based priority fee."""
    if cu_price_micro is None or cu_price_micro <= 0:
        return 0.00005  # default fallback
    # Formula: (cu_price_micro * estimated_cu) / 1e9 = priority fee in SOL
    return max(0.00001, (cu_price_micro * estimated_cu) / 1e9)