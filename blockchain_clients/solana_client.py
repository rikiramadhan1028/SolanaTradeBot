# blockchain_clients/solana_client.py
import json
import base58
import asyncio
from solders.transaction_status import TransactionConfirmationStatus
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction
from solders.system_program import TransferParams, transfer
from solana.rpc.types import TxOpts, TokenAccountOpts
from spl.token.instructions import transfer_checked, get_associated_token_address
from spl.token.constants import TOKEN_PROGRAM_ID
from solders.message import Message, MessageV0
from dex_integrations.jupiter_aggregator import get_swap_route, get_swap_transaction

class SolanaClient:
    def __init__(self, rpc_url: str):
        self.client = Client(rpc_url)

    def get_balance(self, public_key_str: str) -> float:
        try:
            pubkey = Pubkey.from_string(public_key_str)
            balance_lamports = self.client.get_balance(pubkey).value
            return balance_lamports / 1_000_000_000
        except Exception as e:
            print(f"Error fetching Solana balance for {public_key_str}: {e}")
            return 0.0

    def _get_keypair_from_private_key(self, private_key_input: str) -> Keypair:
        """
        Helper method to create Keypair from various private key formats
        """
        try:
            # Jika input adalah JSON string
            if private_key_input.strip().startswith('['):
                key_data = json.loads(private_key_input)
                if not isinstance(key_data, list):
                    raise ValueError("JSON private key must be a list of integers.")
                key_bytes = bytes(key_data)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")
                return Keypair.from_bytes(key_bytes)
            
            # Jika input adalah Base58 string
            else:
                key_bytes = base58.b58decode(private_key_input)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")
                return Keypair.from_bytes(key_bytes)
                
        except Exception as e:
            raise ValueError(f"Invalid private key format: {e}")

    async def perform_swap(self, sender_private_key_json: str, amount_lamports: int,
                       input_mint: str, output_mint: str) -> str:
        try:
            # PERBAIKAN: Menggunakan method helper yang benar
            keypair = self._get_keypair_from_private_key(sender_private_key_json)
            public_key_str = str(keypair.pubkey())

            # Fetch swap route
            route = await get_swap_route(input_mint, output_mint, amount_lamports)
            if not route:
                return "Error: No swap route found."

            # Build transaction
            swap_transaction = await get_swap_transaction(route, public_key_str)
            if not swap_transaction:
                return "Error: Could not build swap transaction."
            
            raw_tx = base58.b58decode(swap_transaction)
            tx = VersionedTransaction.deserialize(raw_tx)
            tx.sign([keypair])
            
            # Send transaction
            tx_sig = self.client.send_transaction(tx)
            return str(tx_sig.value)
        except Exception as e:
            print(f"Swap error details: {e}")  # Debug logging
            return f"Error: {e}"

    def get_public_key_from_private_key_json(self, private_key_json: str) -> Pubkey:
        try:
            # PERBAIKAN: Menggunakan method helper
            keypair = self._get_keypair_from_private_key(private_key_json)
            return keypair.pubkey()
        except Exception as e:
            print(f"Error converting private key JSON to public key: {e}")
            return None

    def send_sol(self, private_key_base58: str, to_address: str, amount: float) -> str:
        try:
            # Menggunakan helper method untuk mendapatkan Keypair
            sender_keypair = self._get_keypair_from_private_key(private_key_base58)
            sender_pubkey = sender_keypair.pubkey()

            recipient_pubkey = Pubkey.from_string(to_address)
            lamports = int(amount * 1_000_000_000)

            # Periksa saldo
            current_balance = self.get_balance(str(sender_pubkey))
            if current_balance < amount:
                return f"Error: Insufficient balance. Current: {current_balance} SOL, Required: {amount} SOL"

            # Ambil blockhash
            latest_blockhash = self.client.get_latest_blockhash().value.blockhash

            # Buat instruksi transfer
            transfer_instruction = transfer(
                TransferParams(
                    from_pubkey=sender_pubkey,
                    to_pubkey=recipient_pubkey,
                    lamports=lamports
                )
            )

            # --- Bagian Perbaikan Penting ---
            # 1. Buat Message dari instruksi dan payer
            message = Message(
                instructions=[transfer_instruction],
                payer=sender_pubkey
            )
            
            # 2. Buat Transaction dari Message dan recent_blockhash
            tx = Transaction(
                message=message,
                recent_blockhash=latest_blockhash
            )
            
            # 3. Tandatangani transaksi menggunakan keypair
            tx.sign([sender_keypair])

            # Kirim transaksi
            result = self.client.send_transaction(tx)
            return str(result.value)
            
        except Exception as e:
            print(f"Error sending SOL: {e}")
            return f"Error: {e}"



    def send_spl_token(self, private_key_base58: str, token_mint_address: str, to_wallet_address: str, amount: float) -> str:
        try:
            sender_keypair = self._get_keypair_from_private_key(private_key_base58)
            sender_pubkey = sender_keypair.pubkey()

            mint = Pubkey.from_string(token_mint_address)
            recipient = Pubkey.from_string(to_wallet_address)

            sender_token_account = get_associated_token_address(sender_pubkey, mint)
            recipient_token_account = get_associated_token_address(recipient, mint)

            # Ambil blockhash
            latest_blockhash = self.client.get_latest_blockhash().value.blockhash

            decimals = 6  # Sesuaikan jika tokenmu pakai decimals lain
            token_amount = int(amount * (10 ** decimals))

            transfer_instruction = transfer_checked(
                program_id=TOKEN_PROGRAM_ID,
                source=sender_token_account,
                mint=mint,
                dest=recipient_token_account,
                owner=sender_pubkey,
                amount=token_amount,
                decimals=decimals
            )

            tx = Transaction([transfer_instruction], sender_pubkey, latest_blockhash)
            tx.sign([sender_keypair])

            result = self.client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
            return str(result.value)
        except Exception as e:
            print(f"Error sending SPL Token: {e}")
            return f"Error: {e}"
            
    def get_spl_token_balances(self, wallet_address: str) -> list:
        try:
            owner = Pubkey.from_string(wallet_address)
            opts = TokenAccountOpts(
                program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                encoding="jsonParsed"
            )

            response = self.client.get_token_accounts_by_owner(owner, opts)
            results = []

            for token_info in response.value:
                try:
                    data = token_info['account']['data']['parsed']['info']
                    token_amount = data['tokenAmount']
                    mint = data['mint']
                    amount_raw = int(token_amount['amount'])
                    decimals = int(token_amount['decimals'])
                    ui_amount = amount_raw / (10 ** decimals)

                    if ui_amount > 0:
                        results.append({
                            'mint': mint,
                            'amount': ui_amount,
                            'decimals': decimals
                        })
                except Exception as parse_error:
                    print(f"Error parsing token account: {parse_error}")
                    continue

            return results

        except Exception as e:
            print(f"[SPL Token Balance Error] {e}")
            return []