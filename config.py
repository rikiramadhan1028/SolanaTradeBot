# file: config.py
import os

# Secrets wajib via ENV (hindari hardcode)
PUMPPORTAL_API_KEY = os.getenv("a98n2mvra1jn6vhf6h63jt2hehgm6d2r6t5mgebda5274wjgahamjp9n618mey1tdctq2vjm8x53jwad8naq6njqart2pw3me9m4evkhax65euanccvn2nhratvpawjad5t4gbtta4ykub4u30va4cn5ngd3161n46jjhdmb93m6rbn5x7puuk875r4ep2fa5x46jb58nvkuf8")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
