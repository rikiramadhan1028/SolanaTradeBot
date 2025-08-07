# wallet_manager.py

from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58
import json

def create_solana_wallet():
    """
    Generates a new Solana keypair and returns a tuple of:
    (base58 encoded 64-byte private key, public key string)
    """
    keypair = Keypair()
    private_key_bytes = keypair.to_bytes()  # 64 bytes: secret + pubkey
    private_key_b58 = base58.b58encode(private_key_bytes).decode()
    public_key = str(keypair.pubkey())
    return private_key_b58, public_key

def get_solana_pubkey_from_private_key_json(private_key_json: str) -> Pubkey:
    """
    Validates and retrieves the public key from a private key.
    Supports both base58-encoded 64-byte string and JSON array.
    """
    key_data = private_key_json.strip()

    # JSON array format
    if key_data.startswith("[") and key_data.endswith("]"):
        try:
            key_array = json.loads(key_data)
            if not isinstance(key_array, list):
                raise ValueError("JSON private key must be a list of integers.")
            key_bytes = bytes(key_array)
            if len(key_bytes) != 64:
                raise ValueError("JSON private key must be 64 bytes (private + public key).")
            return Keypair.from_bytes(key_bytes).pubkey()
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Invalid JSON private key: {e}")

    # Base58 format
    try:
        key_bytes = base58.b58decode(key_data)
        if len(key_bytes) != 64:
            raise ValueError("Base58 private key must be 64 bytes (private + public key).")
        return Keypair.from_bytes(key_bytes).pubkey()
    except Exception as e:
        raise ValueError(f"Invalid Base58 private key: {e}")

def validate_and_clean_private_key(key_data: str) -> str:
    """
    Takes input (Base58 or JSON) and returns base58-encoded 64-byte private key string.
    """
    key_data = key_data.strip()

    # If JSON
    if key_data.startswith("["):
        try:
            key_array = json.loads(key_data)
            if not isinstance(key_array, list):
                raise ValueError("JSON must be a list of integers.")
            key_bytes = bytes(key_array)
            if len(key_bytes) != 64:
                raise ValueError("JSON private key must be 64 bytes.")
            return base58.b58encode(key_bytes).decode()
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format: {e}")
    
    # If Base58
    try:
        key_bytes = base58.b58decode(key_data)
        if len(key_bytes) != 64:
            raise ValueError("Base58 private key must be 64 bytes.")
        return key_data
    except Exception as e:
        raise ValueError(f"Invalid Base58 private key: {e}")

def get_solana_pubkey_from_base58(base58_private_key: str) -> str:
    """
    Returns the public key string from a base58 private key.
    """
    key_bytes = base58.b58decode(base58_private_key)
    if len(key_bytes) != 64:
        raise ValueError("Private key must decode to 64 bytes.")
    kp = Keypair.from_bytes(key_bytes)
    return str(kp.pubkey())

# Placeholder
def create_evm_wallet():
    raise NotImplementedError("EVM wallet creation not yet implemented.")

def import_evm_wallet_from_mnemonic(mnemonic: str, index: int = 0):
    raise NotImplementedError("EVM from mnemonic not yet implemented.")
    
def import_solana_wallet_from_mnemonic(mnemonic_phrase: str):
    raise NotImplementedError("Solana from mnemonic not yet implemented.")
