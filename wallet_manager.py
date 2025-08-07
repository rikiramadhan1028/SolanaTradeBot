from solders.keypair import Keypair
from solders.pubkey import Pubkey
import json
import base58

def create_solana_wallet():
    keypair = Keypair()
    private_key_bytes = keypair.to_bytes()
    public_key = keypair.pubkey()
    return base58.b58encode(private_key_bytes).decode('utf-8'), str(public_key)

def validate_and_clean_private_key(key_data: str) -> str:
    """
    Validates input private key. Converts JSON array to base58 if needed.
    Always returns base58 format.
    """
    key_data = key_data.strip()
    if key_data.startswith("["):
        try:
            key_array = json.loads(key_data)
            if not isinstance(key_array, list):
                raise ValueError("JSON must be a list of integers.")
            key_bytes = bytes(key_array)
            return base58.b58encode(key_bytes).decode()
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format: {e}")
    else:
        try:
            _ = base58.b58decode(key_data)
            return key_data
        except Exception as e:
            raise ValueError(f"Invalid Base58 format: {e}")

def get_solana_pubkey_from_base58(base58_private_key: str) -> str:
    key_bytes = base58.b58decode(base58_private_key)
    kp = Keypair.from_secret_key(key_bytes)
    return str(kp.pubkey())

def get_keypair_from_base58(base58_private_key: str) -> Keypair:
    key_bytes = base58.b58decode(base58_private_key)
    return Keypair.from_secret_key(key_bytes)

# Placeholder for EVM (belum diimplementasi)
def create_evm_wallet():
    raise NotImplementedError("EVM wallet creation not yet implemented.")

def import_evm_wallet_from_mnemonic(mnemonic: str, index: int = 0):
    raise NotImplementedError("EVM from mnemonic not yet implemented.")

def import_solana_wallet_from_mnemonic(mnemonic_phrase: str):
    raise NotImplementedError("Solana from mnemonic not yet implemented.")
