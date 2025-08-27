# cu_config.py - Compute Unit price configuration utilities
import os
from enum import Enum
from typing import Optional

# SOL-based priority fee tiers (direct SOL amounts)
PRIORITY_FEE_SOL_DEFAULT = float(os.getenv("PRIORITY_FEE_SOL_DEFAULT", "0.0001"))
PRIORITY_FEE_SOL_FAST = float(os.getenv("PRIORITY_FEE_SOL_FAST", "0.001"))     # 0.001 SOL
PRIORITY_FEE_SOL_TURBO = float(os.getenv("PRIORITY_FEE_SOL_TURBO", "0.005"))   # 0.005 SOL  
PRIORITY_FEE_SOL_ULTRA = float(os.getenv("PRIORITY_FEE_SOL_ULTRA", "0.01"))    # 0.01 SOL

# Lamports-based priority fee tiers (for new Jupiter API)
PRIORITY_FEE_LAMPORTS_DEFAULT = int(PRIORITY_FEE_SOL_DEFAULT * 1_000_000_000)  # 100,000 lamports
PRIORITY_FEE_LAMPORTS_FAST = int(PRIORITY_FEE_SOL_FAST * 1_000_000_000)        # 1,000,000 lamports
PRIORITY_FEE_LAMPORTS_TURBO = int(PRIORITY_FEE_SOL_TURBO * 1_000_000_000)      # 5,000,000 lamports
PRIORITY_FEE_LAMPORTS_ULTRA = int(PRIORITY_FEE_SOL_ULTRA * 1_000_000_000)      # 10,000,000 lamports

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

def choose_priority_fee_lamports(tier: Optional[str]) -> int:
    """Choose priority fee in lamports based on tier. For new Jupiter API."""
    if not tier:
        return PRIORITY_FEE_LAMPORTS_DEFAULT
    t = str(tier).lower()
    if t == PriorityTier.FAST: return PRIORITY_FEE_LAMPORTS_FAST
    if t == PriorityTier.TURBO: return PRIORITY_FEE_LAMPORTS_TURBO
    if t == PriorityTier.ULTRA: return PRIORITY_FEE_LAMPORTS_ULTRA
    return PRIORITY_FEE_LAMPORTS_DEFAULT

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
    """Convert SOL priority fee to CU price (micro-lamports per CU).
    
    Simplified calculation: 1 SOL = 5,000,000 micro-lamports/CU baseline
    """
    if priority_fee_sol <= 0:
        return 0
    # Simple baseline: 1 SOL = 5,000,000 micro-lamports/CU
    # Formula: priority_fee_sol * 5,000,000 = cu_price_micro
    BASELINE_CU_FOR_1_SOL = 5_000_000
    return max(1, int(priority_fee_sol * BASELINE_CU_FOR_1_SOL))

def cu_to_sol_priority_fee(cu_price_micro: Optional[int], estimated_cu: int = 200000) -> float:
    """Convert CU price (micro-lamports per CU) to SOL-based priority fee.
    
    Simplified calculation: 5,000,000 micro-lamports/CU = 1 SOL baseline
    Other values scale proportionally from this baseline.
    """
    if cu_price_micro is None or cu_price_micro <= 0:
        return PRIORITY_FEE_SOL_DEFAULT  # use consistent default
    
    # Simple baseline: 5,000,000 micro-lamports/CU = 1 SOL
    # Formula: (cu_price_micro / 5,000,000) = SOL priority fee
    BASELINE_CU_FOR_1_SOL = 5_000_000
    result = cu_price_micro / BASELINE_CU_FOR_1_SOL
    print(f"DEBUG cu_to_sol_priority_fee: {cu_price_micro} / {BASELINE_CU_FOR_1_SOL} = {result} SOL")
    return result