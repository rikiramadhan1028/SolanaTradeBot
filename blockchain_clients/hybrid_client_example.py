# file: blockchain_clients/hybrid_client_example.py
"""
Example usage of hybrid HTTP/WebSocket Solana client
Shows how to use the enhanced SolanaClient with WebSocket confirmations
"""

import asyncio
import logging
from solana_client import SolanaClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def example_swap_with_fast_confirmation():
    """Example showing fast swap with WebSocket confirmation"""
    
    # Initialize client with your RPC URL
    rpc_url = "https://api.mainnet-beta.solana.com"  # or your preferred RPC
    client = SolanaClient(rpc_url)
    
    # Example swap parameters
    sender_private_key = "[1,2,3...]"  # Your wallet private key JSON format
    amount_lamports = 100000000  # 0.1 SOL
    input_mint = "So11111111111111111111111111111111111111112"  # SOL
    output_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    
    try:
        logger.info("Starting swap with WebSocket confirmation...")
        
        # Perform swap with automatic WebSocket confirmation
        signature = await client.perform_swap(
            sender_private_key_json=sender_private_key,
            amount_lamports=amount_lamports,
            input_mint=input_mint,
            output_mint=output_mint,
            dex="jupiter",
            slippage_bps=50,
            compute_unit_price_micro_lamports=10000  # 0.01 SOL priority fee
        )
        
        if signature.startswith("Error"):
            logger.error(f"Swap failed: {signature}")
        else:
            logger.info(f"Swap successful! Signature: {signature}")
            logger.info(f"View on Solscan: https://solscan.io/tx/{signature}")
            
    except Exception as e:
        logger.error(f"Swap error: {e}")
    
    # Clean up WebSocket connection
    if client.ws_manager:
        await client.ws_manager.disconnect()

async def example_manual_websocket_confirmation():
    """Example showing manual WebSocket confirmation monitoring"""
    
    rpc_url = "https://api.mainnet-beta.solana.com"
    client = SolanaClient(rpc_url)
    
    # Example signature to monitor (replace with actual signature)
    signature = "YOUR_TRANSACTION_SIGNATURE_HERE"
    
    try:
        logger.info(f"Monitoring signature: {signature}")
        
        # Manual WebSocket confirmation with timeout
        result = await client._confirm_transaction_ws(
            signature=signature,
            commitment="confirmed",
            timeout=30.0
        )
        
        if result:
            logger.info("Transaction confirmed successfully!")
        else:
            logger.error("Transaction confirmation failed or timed out")
            
    except Exception as e:
        logger.error(f"Confirmation error: {e}")
    
    # Clean up
    if client.ws_manager:
        await client.ws_manager.disconnect()

async def example_direct_websocket_usage():
    """Example showing direct WebSocket manager usage"""
    
    from websocket_manager import SolanaWebSocketManager
    
    ws_url = "wss://api.mainnet-beta.solana.com"
    
    async with SolanaWebSocketManager(ws_url) as ws_manager:
        signature = "YOUR_TRANSACTION_SIGNATURE_HERE"
        
        def on_signature_update(result):
            logger.info(f"Signature update: {result}")
            if result and result.get("value"):
                if result["value"].get("err") is None:
                    logger.info("✅ Transaction confirmed!")
                else:
                    logger.error(f"❌ Transaction failed: {result['value']['err']}")
        
        # Subscribe to signature updates
        sub_id = await ws_manager.subscribe_signature(
            signature=signature,
            callback=on_signature_update,
            commitment="confirmed"
        )
        
        if sub_id:
            logger.info(f"Subscribed with ID: {sub_id}")
            # Wait for updates (in real use, this would be event-driven)
            await asyncio.sleep(30)
            await ws_manager.unsubscribe_signature(sub_id)
        else:
            logger.error("Failed to subscribe to signature")

if __name__ == "__main__":
    # Run example
    asyncio.run(example_swap_with_fast_confirmation())
    
    # Uncomment to run other examples:
    # asyncio.run(example_manual_websocket_confirmation())
    # asyncio.run(example_direct_websocket_usage())