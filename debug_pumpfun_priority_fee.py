#!/usr/bin/env python3
"""
Debug script to check PumpFun priority fee implementation
"""

import asyncio
import base64
from solders.transaction import VersionedTransaction
from cu_config import cu_to_sol_priority_fee, choose_cu_price
from dex_integrations.pumpfun_aggregator import get_pumpfun_swap_transaction

async def debug_pumpfun_priority_fee():
    """Test PumpFun transaction with priority fee"""
    print("=== PumpFun Priority Fee Debug ===")
    
    # Test parameters
    public_key = "11111111111111111111111111111111"  # dummy
    action = "buy"
    mint = "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump"  # Example PumpFun token
    amount = 0.01  # 0.01 SOL
    
    # Get priority fee
    cu_price = choose_cu_price('fast')  # 5000 micro-lamports
    priority_fee_sol = cu_to_sol_priority_fee(cu_price, 200000)
    print(f"CU Price: {cu_price} micro-lamports")
    print(f"Priority Fee SOL: {priority_fee_sol}")
    
    try:
        print("\n1. Building PumpFun transaction...")
        tx_b64 = await get_pumpfun_swap_transaction(
            public_key=public_key,
            action=action,
            mint=mint,
            amount=amount,
            slippage=10,
            priority_fee=priority_fee_sol,
            pool="auto"
        )
        print(f"Transaction built: {bool(tx_b64)}")
        
        if tx_b64:
            # Decode and analyze transaction
            print("\n2. Analyzing transaction...")
            tx_bytes = base64.b64decode(tx_b64)
            vtx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Check instructions for compute budget program
            compute_budget_program = "ComputeBudget111111111111111111111111111111"
            has_priority_fee = False
            
            for i, instruction in enumerate(vtx.message.instructions):
                program_id = str(vtx.message.account_keys[instruction.program_id_index])
                print(f"Instruction {i}: Program {program_id}")
                
                if program_id == compute_budget_program:
                    has_priority_fee = True
                    # Decode instruction data to check if it's SetComputeUnitPrice
                    if len(instruction.data) > 0:
                        if instruction.data[0] == 3:  # SetComputeUnitPrice discriminator
                            if len(instruction.data) >= 9:
                                price = int.from_bytes(instruction.data[1:9], 'little')
                                print(f"  -> SetComputeUnitPrice: {price} micro-lamports")
                            else:
                                print(f"  -> SetComputeUnitPrice: Invalid data length")
                        elif instruction.data[0] == 2:  # SetComputeUnitLimit
                            if len(instruction.data) >= 5:
                                limit = int.from_bytes(instruction.data[1:5], 'little')
                                print(f"  -> SetComputeUnitLimit: {limit} CU")
                            else:
                                print(f"  -> SetComputeUnitLimit: Invalid data length")
                        else:
                            print(f"  -> Unknown compute budget instruction: {instruction.data[0]}")
            
            print(f"\nHas Priority Fee Instructions: {has_priority_fee}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(debug_pumpfun_priority_fee())