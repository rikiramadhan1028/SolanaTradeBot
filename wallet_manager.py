# wallet_manager.py
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import json
import base58

def create_solana_wallet():
    keypair = Keypair()
    private_key_bytes = keypair.to_bytes()
    public_key = keypair.pubkey()
    return base58.b58encode(private_key_bytes).decode('utf-8'), str(public_key)

def get_solana_pubkey_from_private_key_json(private_key_json: str) -> Pubkey:
    """
    Validates and retrieves the public key from a private key.
    Handles both Base58 and JSON array formats.
    """
    key_data = private_key_json.strip()

    # Case 1: Try parsing as JSON array
    if key_data.startswith('[') and key_data.endswith(']'):
        try:
            key_list = json.loads(key_data)
            return Keypair.from_bytes(bytes(key_list)).pubkey()
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Invalid JSON format for private key: {e}")

    # Case 2: Try parsing as Base58 string
    try:
        return Keypair.from_base58_string(key_data).pubkey()
    except ValueError as e:
        raise ValueError(f"Invalid Base58 format for private key: {e}")

def create_evm_wallet():
    """Creates a new EVM account and returns its private key (hex) and public address."""
    account = Account.create()
    return account.key.hex(), account.address

def import_evm_wallet_from_mnemonic(mnemonic: str, index: int = 0):
    raise NotImplementedError("EVM from mnemonic not yet implemented.")
    
def import_solana_wallet_from_mnemonic(mnemonic_phrase: str):
    raise NotImplementedError("Solana from mnemonic not yet implemented.")