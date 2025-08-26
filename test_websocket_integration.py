# file: test_websocket_integration.py
"""
Test script for WebSocket integration with Solana RPC
"""

import asyncio
import logging
import sys
import os
from blockchain_clients.websocket_manager import SolanaWebSocketManager
from config import SOLANA_RPC_URL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def test_websocket_connection():
    """Test basic WebSocket connection"""
    logger.info("Testing WebSocket connection...")
    
    # Convert HTTP RPC URL to WebSocket URL
    if SOLANA_RPC_URL.startswith("https://"):
        ws_url = SOLANA_RPC_URL.replace("https://", "wss://")
    elif SOLANA_RPC_URL.startswith("http://"):
        ws_url = SOLANA_RPC_URL.replace("http://", "ws://")
    else:
        ws_url = SOLANA_RPC_URL
    
    logger.info(f"WebSocket URL: {ws_url}")
    
    try:
        async with SolanaWebSocketManager(ws_url) as ws_manager:
            logger.info("✅ WebSocket connection established successfully!")
            
            # Test ping/keep-alive
            await asyncio.sleep(2)
            logger.info("✅ WebSocket connection stable")
            
    except Exception as e:
        logger.error(f"❌ WebSocket connection failed: {e}")
        return False
    
    return True

async def test_signature_subscription():
    """Test signature subscription functionality"""
    logger.info("Testing signature subscription...")
    
    # Convert HTTP RPC URL to WebSocket URL
    if SOLANA_RPC_URL.startswith("https://"):
        ws_url = SOLANA_RPC_URL.replace("https://", "wss://")
    elif SOLANA_RPC_URL.startswith("http://"):
        ws_url = SOLANA_RPC_URL.replace("http://", "ws://")
    else:
        ws_url = SOLANA_RPC_URL
    
    # Use a properly formatted dummy signature for testing subscription mechanism
    # Valid base58 signature format but likely doesn't exist
    test_signature = "5" * 87 + "1"  # Valid length and base58 chars
    
    received_updates = []
    
    def on_signature_update(result):
        logger.info(f"Received signature update: {result}")
        received_updates.append(result)
    
    try:
        async with SolanaWebSocketManager(ws_url) as ws_manager:
            logger.info("Attempting to subscribe to test signature...")
            
            sub_id = await ws_manager.subscribe_signature(
                signature=test_signature,
                callback=on_signature_update,
                commitment="confirmed"
            )
            
            if sub_id:
                logger.info(f"✅ Successfully subscribed with ID: {sub_id}")
                
                # Wait a bit to see if we get any updates
                await asyncio.sleep(5)
                
                # Unsubscribe
                await ws_manager.unsubscribe_signature(sub_id)
                logger.info("✅ Successfully unsubscribed")
                
                return True
            else:
                logger.error("❌ Failed to subscribe to signature")
                return False
                
    except Exception as e:
        logger.error(f"❌ Signature subscription test failed: {e}")
        return False

async def test_confirmation_timeout():
    """Test confirmation timeout functionality"""
    logger.info("Testing confirmation timeout...")
    
    if SOLANA_RPC_URL.startswith("https://"):
        ws_url = SOLANA_RPC_URL.replace("https://", "wss://")
    elif SOLANA_RPC_URL.startswith("http://"):
        ws_url = SOLANA_RPC_URL.replace("http://", "ws://")
    else:
        ws_url = SOLANA_RPC_URL
    
    # Use a dummy signature that won't exist but is properly formatted
    dummy_signature = "3" * 87 + "2"  # Valid base58 format
    
    try:
        async with SolanaWebSocketManager(ws_url) as ws_manager:
            logger.info("Testing confirmation timeout with dummy signature...")
            
            # This should timeout since the signature doesn't exist
            result = await ws_manager.wait_for_signature_confirmation(
                signature=dummy_signature,
                timeout=5.0,  # Short timeout for testing
                commitment="confirmed"
            )
            
            if "error" in result and "timeout" in result["error"].lower():
                logger.info("✅ Timeout functionality working correctly")
                return True
            else:
                logger.warning(f"Unexpected result: {result}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Timeout test failed: {e}")
        return False

async def run_all_tests():
    """Run all WebSocket integration tests"""
    logger.info("=" * 50)
    logger.info("Starting WebSocket Integration Tests")
    logger.info("=" * 50)
    
    tests = [
        ("WebSocket Connection", test_websocket_connection),
        ("Signature Subscription", test_signature_subscription),
        ("Confirmation Timeout", test_confirmation_timeout),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        logger.info(f"\n🧪 Running: {test_name}")
        try:
            result = await test_func()
            results.append((test_name, result))
            status = "✅ PASSED" if result else "❌ FAILED"
            logger.info(f"{test_name}: {status}")
        except Exception as e:
            logger.error(f"{test_name}: ❌ FAILED with exception: {e}")
            results.append((test_name, False))
    
    # Summary
    logger.info("\n" + "=" * 50)
    logger.info("Test Results Summary")
    logger.info("=" * 50)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        logger.info(f"{test_name}: {status}")
    
    logger.info(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        logger.info("🎉 All tests passed! WebSocket integration is working correctly.")
    else:
        logger.warning(f"⚠️  {total - passed} test(s) failed. Please check the logs above.")
    
    return passed == total

if __name__ == "__main__":
    try:
        success = asyncio.run(run_all_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Test runner failed: {e}")
        sys.exit(1)