# file: blockchain_clients/solana_client.py
import json
import base64
import base58
import httpx

from solana.rpc.api import Client
from solana.rpc.types import TxOpts, TokenAccountOpts

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0

from spl.token.instructions import (
    transfer_checked,
    get_associated_token_address,
    create_associated_token_account,
)
from spl.token.constants import TOKEN_PROGRAM_ID

from dex_integrations.jupiter_aggregator import (
    get_swap_route as jupiter_get_route,
    get_swap_transaction as jupiter_get_tx,
)
from dex_integrations.pumpfun_aggregator import get_pumpfun_bundle_unsigned_base58, get_pumpfun_swap_transaction
from dex_integrations.raydium_aggregator import (
    get_swap_quote as raydium_get_quote,
    get_swap_transaction as raydium_get_tx,
)

from dex_integrations.pumpfun_aggregator import (
    get_pumpfun_swap_transaction,
    get_pumpfun_bundle_unsigned_base58,
)

JITO_BUNDLE_ENDPOINT = "https://mainnet.block-engine.jito.wtf/api/v1/bundles" 


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
        try:
            if private_key_input.strip().startswith("["):
                key_data = json.loads(private_key_input)
                if not isinstance(key_data, list):
                    raise ValueError("JSON private key must be a list of integers.")
                key_bytes = bytes(key_data)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")
                return Keypair.from_bytes(key_bytes)
            else:
                key_bytes = base58.b58decode(private_key_input)
                if len(key_bytes) != 64:
                    raise ValueError("Private key must be 64 bytes.")
                return Keypair.from_bytes(key_bytes)
        except Exception as e:
            raise ValueError(f"Invalid private key format: {e}")

    async def perform_swap(
        self,
        sender_private_key_json: str,
        amount_lamports: int,
        input_mint: str,
        output_mint: str,
        dex: str = "jupiter",
    ) -> str:
        try:
            keypair = self._get_keypair_from_private_key(sender_private_key_json)
            public_key_str = str(keypair.pubkey())

            swap_transaction_b64 = None
            if dex == "jupiter":
                route = await jupiter_get_route(input_mint, output_mint, amount_lamports)
                if not route:
                    return "Error: No swap route found on Jupiter."
                swap_transaction_b64 = await jupiter_get_tx(route, public_key_str)
                if not swap_transaction_b64:
                    return "Error: Could not build swap transaction on Jupiter."
            elif dex == "raydium":
                quote = await raydium_get_quote(input_mint, output_mint, amount_lamports)
                if not quote:
                    return "Error: Could not get a quote from Raydium."
                swap_transaction_b64 = await raydium_get_tx(quote, public_key_str)
                if not swap_transaction_b64:
                    return "Error: Could not build swap transaction on Raydium."
            else:
                return "Error: Unsupported DEX."

            raw_tx = base64.b64decode(swap_transaction_b64)
            tx = VersionedTransaction.deserialize(raw_tx)
            tx.sign([keypair])

            # penting: kirim serialized signed versioned tx
            sig_resp = self.client.send_raw_transaction(
                tx.serialize(),
                opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
            )
            try:
                self.client.confirm_transaction(sig_resp.value, commitment="confirmed")
            except Exception:
                pass
            return str(sig_resp.value)
        except Exception as e:
            print(f"Swap error details: {e}")
            return f"Error: {e}"

    async def perform_pumpfun_swap(self, sender_private_key_json: str, amount, action: str, mint: str) -> str:
        try:
            keypair = self._get_keypair_from_private_key(sender_private_key_json)
            public_key_str = str(keypair.pubkey())

            tx_b64 = await get_pumpfun_swap_transaction(
                public_key_str, action, mint, amount,
                slippage=10, priority_fee=0.00001, pool="auto"
            )
            if not tx_b64:
                return "Error: Could not build Pumpfun transaction."

            tx_bytes = base64.b64decode(tx_b64)
            tx = VersionedTransaction.deserialize(tx_bytes)
            tx.sign([keypair])

            sig_resp = self.client.send_raw_transaction(
                tx.serialize(),
                opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
            )
            try:
                self.client.confirm_transaction(sig_resp.value, commitment="confirmed")
            except Exception:
                pass
            return str(sig_resp.value)
        except Exception as e:
            print(f"Pumpfun Swap error details: {e}")
            return f"Error: {e}"

    async def perform_pumpfun_jito_bundle(
        self,
        sender_private_key_json: str,
        amount,
        action: str,
        mint: str,
        *,
        bundle_count: int = 1,
    ) -> str:
        """
        Bangun bundle via trade-local (array), sign, lalu kirim ke Jito sendBundle.
        Minimal 1 tx (boleh >1). Lihat contoh resmi. :contentReference[oaicite:5]{index=5}
        """
        try:
            if bundle_count < 1:
                bundle_count = 1
            keypair = self._get_keypair_from_private_key(sender_private_key_json)
            public_key_str = str(keypair.pubkey())

            unsigned_base58_list = await get_pumpfun_bundle_unsigned_base58(
                [public_key_str] * bundle_count,
                [action] * bundle_count,
                [mint] * bundle_count,
                [amount] * bundle_count,
                slippage=10,
                priority_fee=0.00005,  # tip/jito fee di tx pertama
                pool="auto",
            )
            if not unsigned_base58_list:
                return "Error: Could not build Pumpfun bundle."

            # Sign semua tx & encode base58 serialized bytes
            import base58 as b58
            signed_b58_list = []
            signatures = []
            for enc in unsigned_base58_list:
                vtx = VersionedTransaction.deserialize(bytes(b58.b58decode(enc)))
                vtx.sign([keypair])
                signed_b58_list.append(b58.b58encode(vtx.serialize()).decode())
                signatures.append(b58.b58encode(vtx.signatures[0]).decode())

            # Kirim ke Jito Block Engine
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [signed_b58_list],
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                jr = await client.post(JITO_BUNDLE_ENDPOINT, json=payload)
                jr.raise_for_status()
                # Tidak selalu ada signature tunggal; balas signature[0] utk Solscan
                return signatures[0] if signatures else "OK"
        except httpx.HTTPStatusError as e:
            return f"Error: Jito sendBundle failed {e.response.status_code}: {e.response.text}"
        except Exception as e:
            return f"Error: {e}"
        
    def get_public_key_from_private_key_json(self, private_key_json: str) -> Pubkey:
        try:
            keypair = self._get_keypair_from_private_key(private_key_json)
            return keypair.pubkey()
        except Exception as e:
            print(f"Error converting private key JSON to public key: {e}")
            return None

    def send_sol(self, private_key_base58: str, to_address: str, amount: float) -> str:
        try:
            sender_keypair = self._get_keypair_from_private_key(private_key_base58)
            sender_pubkey = sender_keypair.pubkey()
            try:
                recipient_pubkey = Pubkey.from_string(to_address)
            except ValueError:
                return "Error: Invalid recipient address format"

            lamports = int(amount * 1_000_000_000)
            estimated_fee_sol = 0.000005
            current_balance = self.get_balance(str(sender_pubkey))
            total_needed = amount + estimated_fee_sol
            if current_balance < total_needed:
                return (
                    "Error: Insufficient balance.\n"
                    f"Current: {current_balance} SOL, Required: {total_needed} SOL"
                )

            latest_blockhash = self.client.get_latest_blockhash().value.blockhash
            ix = transfer(
                TransferParams(
                    from_pubkey=sender_pubkey, to_pubkey=recipient_pubkey, lamports=lamports
                )
            )
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=[ix],
                recent_blockhash=latest_blockhash,
                address_lookup_table_accounts=[],
            )
            tx = VersionedTransaction(msg, [sender_keypair])

            # konsisten: kirim raw serialized
            result = self.client.send_raw_transaction(
                tx.serialize(),
                opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
            )
            if result.value:
                return f"Transaction successful! Signature: {result.value}"
            else:
                return "Error: Transaction failed to process"
        except Exception as e:
            print(f"Error sending SOL: {e}")
            return f"Error: {e}"

    def send_spl_token(
        self, private_key_base58: str, token_mint_address: str, to_wallet_address: str, amount: float
    ) -> str:
        try:
            sender_keypair = self._get_keypair_from_private_key(private_key_base58)
            sender_pubkey = sender_keypair.pubkey()

            mint = Pubkey.from_string(token_mint_address)
            recipient = Pubkey.from_string(to_wallet_address)

            sender_ata = get_associated_token_address(sender_pubkey, mint)
            recipient_ata = get_associated_token_address(recipient, mint)
            latest_blockhash = self.client.get_latest_blockhash().value.blockhash

            # decimals dari mint → akurat
            try:
                supply_resp = self.client.get_token_supply(mint)
                decimals = supply_resp.value.decimals
            except Exception:
                decimals = 6

            token_amount = int(amount * (10 ** decimals))

            ixs = []
            # Pastikan ATA penerima ada
            try:
                acc = self.client.get_account_info(recipient_ata)
                if acc.value is None:
                    ixs.append(
                        create_associated_token_account(
                            payer=sender_pubkey, owner=recipient, mint=mint
                        )
                    )
            except Exception:
                ixs.append(
                    create_associated_token_account(
                        payer=sender_pubkey, owner=recipient, mint=mint
                    )
                )

            ixs.append(
                transfer_checked(
                    program_id=TOKEN_PROGRAM_ID,
                    source=sender_ata,
                    mint=mint,
                    dest=recipient_ata,
                    owner=sender_pubkey,
                    amount=token_amount,
                    decimals=decimals,
                )
            )

            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=ixs,
                recent_blockhash=latest_blockhash,
                address_lookup_table_accounts=[],
            )
            tx = VersionedTransaction(msg, [sender_keypair])

            result = self.client.send_raw_transaction(
                tx.serialize(),
                opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
            )
            return str(result.value)
        except Exception as e:
            print(f"Error sending SPL Token: {e}")
            return f"Error: {e}"
