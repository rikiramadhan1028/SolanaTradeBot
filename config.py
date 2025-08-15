# file: config.py
import os

# Secrets wajib via ENV (hindari hardcode)
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet.solana.com")
