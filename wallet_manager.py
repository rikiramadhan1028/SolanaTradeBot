# wallet_manager.py
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import json
import base58

def create_solana_wallet():
    keypair = Keypair()
    private_key_bytes = keypair.to_bytes()
    public_key = keypair.pubkey()
    return json.dumps(list(private_key_bytes)), str(public_key)

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