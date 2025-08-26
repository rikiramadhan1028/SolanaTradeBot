# file: test_balance_after_websocket.py
"""
Test script to verify balance fetching still works after WebSocket integration
"""

import sys
import logging
from blockchain_clients.solana_client import SolanaClient
from config import SOLANA_RPC_URL

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_balance_functionality():
    """Test various balance-related functions"""
    logger.info("Testing balance functionality after WebSocket integration...")
    
    try:
        # Initialize client
        client = SolanaClient(SOLANA_RPC_URL)
        logger.info(f"✅ SolanaClient initialized with RPC: {SOLANA_RPC_URL}")
        
        # Test 1: Get SOL balance for system account (should have some balance)
        system_account = "11111111111111111111111111111112"  # System Program
        sol_balance = client.get_balance(system_account)
        logger.info(f"✅ System account balance: {sol_balance} SOL")
        
        # Test 2: Get balance for a wallet (if you want to test with specific wallet)
        # Replace with your wallet address for real testing
        # test_wallet = "YOUR_WALLET_ADDRESS_HERE"
        # wallet_balance = client.get_balance(test_wallet)
        # logger.info(f"Test wallet balance: {wallet_balance} SOL")
        
        # Test 3: Get SPL token balances (should return empty list for system account)
        spl_balances = client.get_spl_token_balances(system_account)
        logger.info(f"✅ SPL token balances for system account: {len(spl_balances)} tokens")
        
        # Test 4: Test invalid address handling
        try:
            invalid_balance = client.get_balance("invalid_address")
            logger.info(f"Invalid address handled: {invalid_balance}")
        except Exception as e:
            logger.info(f"✅ Invalid address properly rejected: {e}")
        
        logger.info("🎉 All balance tests passed! WebSocket integration doesn't break existing functionality.")
        return True
        
    except Exception as e:
        logger.error(f"❌ Balance test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_http_rpc_direct():
    """Test HTTP RPC directly to isolate issues"""
    logger.info("Testing direct HTTP RPC calls...")
    
    try:
        from solana.rpc.api import Client
        from solders.pubkey import Pubkey
        
        client = Client(SOLANA_RPC_URL)
        pubkey = Pubkey.from_string("11111111111111111111111111111112")
        
        # Direct RPC call
        response = client.get_balance(pubkey)
        balance = response.value / 1_000_000_000
        
        logger.info(f"✅ Direct HTTP RPC call successful: {balance} SOL")
        return True
        
    except Exception as e:
        logger.error(f"❌ Direct HTTP RPC failed: {e}")
        return False

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Testing Balance Functionality After WebSocket Integration")
    logger.info("=" * 50)
    
    # Test direct HTTP first
    http_ok = test_http_rpc_direct()
    
    # Test integrated functionality
    balance_ok = test_balance_functionality()
    
    if http_ok and balance_ok:
        logger.info("✅ All tests passed!")
        sys.exit(0)
    else:
        logger.error("❌ Some tests failed!")
        sys.exit(1)