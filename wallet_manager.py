# wallet_manager.py
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import json
import base58

def create_solana_wallet():
    keypair = Keypair()
    # Mengembalikan private key dalam format Base58 string
    private_key_base58 = keypair.to_base58_string()
    public_key = keypair.pubkey()
    return private_key_base58, str(public_key)

def get_solana_pubkey_from_private_key_json(private_key_json: str) -> Pubkey:
    try:
        keypair = Keypair.from_json_keypair(private_key_json.strip())
        return keypair.pubkey()
    except Exception:
        try:
            keypair = Keypair.from_base58_string(private_key_json.strip())
            return keypair.pubkey()
        except Exception:
            raise ValueError("Invalid Solana private key format. Must be JSON array or base58 string.")