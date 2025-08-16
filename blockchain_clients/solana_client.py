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

    # ---------- Helpers kompatibilitas & error ----------
    @staticmethod
    def _vtx_from_bytes(buf: bytes) -> VersionedTransaction:
        try:
            return VersionedTransaction.from_bytes(buf)  # solders baru
        except AttributeError:
            return VersionedTransaction.deserialize(buf)  # solders lama  # type: ignore[attr-defined]

    @staticmethod
    def _tx_bytes(tx: VersionedTransaction) -> bytes:
        try:
            return tx.to_bytes()  # solders baru
        except AttributeError:
            try:
                return tx.serialize()  # solders lama  # type: ignore[attr-defined]
            except AttributeError:
                return bytes(tx)

    @staticmethod
    def _format_exc(e: Exception) -> str:
        msg = str(e)
        if not msg and getattr(e, "args", None):
            try:
                msg = json.dumps(e.args[0], ensure_ascii=False)
            except Exception:
                msg = repr(e.args[0])
        if not msg:
            msg = f"{e.__class__.__name__}"
        return msg

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

    # ---------- Jupiter/Raydium generic swap ----------
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
            unsigned = self._vtx_from_bytes(raw_tx)
            tx = VersionedTransaction(unsigned.message, [keypair])  # sign by constructing

            try:
                resp = self.client.send_raw_transaction(
                    self._tx_bytes(tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
                )
            except Exception as e:
                return f"Error: {self._format_exc(e)}"

            sig = getattr(resp, "value", None)
            if not sig:
                return f"Error: RPC returned no signature: {resp}"
            try:
                self.client.confirm_transaction(sig, commitment="confirmed")
            except Exception:
                pass
            return str(sig)
        except Exception as e:
            return f"Error: {self._format_exc(e)}"

    # ---------- Pumpfun local signing ----------
    async def perform_pumpfun_swap(
        self, sender_private_key_json: str, amount, action: str, mint: str
    ) -> str:
        try:
            keypair = self._get_keypair_from_private_key(sender_private_key_json)
            public_key_str = str(keypair.pubkey())

            tx_b64 = await get_pumpfun_swap_transaction(
                public_key_str,
                action,
                mint,
                amount,
                slippage=10,
                priority_fee=0.00005,
                pool="auto",
            )
            if not tx_b64:
                return "Error: Could not build Pumpfun transaction (empty response)."

            tx_bytes = base64.b64decode(tx_b64)
            unsigned = self._vtx_from_bytes(tx_bytes)
            tx = VersionedTransaction(unsigned.message, [keypair])  # signed

            try:
                sim = self.client.simulate_transaction(
                    tx,
                    sig_verify=False,
                    replace_recent_blockhash=True,
                )
                sim_val = getattr(sim, "value", None)
                if sim_val and getattr(sim_val, "err", None):
                    logs = (sim_val.logs or [])[-5:] if hasattr(sim_val, "logs") else []
                    return f"Error: Simulation failed: {sim_val.err}. Logs tail: {' | '.join(logs)}"
            except Exception as e:
                print(f"[Pumpfun simulate warn] {self._format_exc(e)}")

            try:
                resp = self.client.send_raw_transaction(
                    self._tx_bytes(tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
                )
            except Exception as e:
                return f"Error: {self._format_exc(e)}"

            sig = getattr(resp, "value", None)
            if not sig:
                return f"Error: RPC returned no signature: {resp}"
            try:
                self.client.confirm_transaction(sig, commitment="confirmed")
            except Exception:
                pass
            return str(sig)
        except Exception as e:
            return f"Error: {self._format_exc(e)}"

    async def perform_pumpfun_jito_bundle(
        self,
        sender_private_key_json: str,
        amount,
        action: str,
        mint: str,
        *,
        bundle_count: int = 1,
    ) -> str:
        """Build bundle via trade-local (array), sign locally, kirim ke Jito; auto-fallback ke local bila rate limited."""
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
                priority_fee=0.0001,
                pool="auto",
            )
            if not unsigned_base58_list:
                return "Error: Could not build Pumpfun bundle (empty response)."

            signed_b58_list = []
            signatures = []
            for enc in unsigned_base58_list:
                unsigned = self._vtx_from_bytes(bytes(base58.b58decode(enc)))
                vtx = VersionedTransaction(unsigned.message, [keypair])  # signed
                signed_b58_list.append(base58.b58encode(self._tx_bytes(vtx)).decode())
                signatures.append(str(vtx.signatures[0]))

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [signed_b58_list],
            }

            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    jr = await client.post(JITO_BUNDLE_ENDPOINT, json=payload)
                    if jr.status_code == 429:
                        fb = await self.perform_pumpfun_swap(sender_private_key_json, amount, action, mint)
                        return fb if not fb.startswith("Error") else f"Error: Jito rate-limited (429). Fallback failed: {fb}"
                    jr.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = e.response.text
                if e.response.status_code in (429, 503) or "rate limited" in body.lower():
                    fb = await self.perform_pumpfun_swap(sender_private_key_json, amount, action, mint)
                    return fb if not fb.startswith("Error") else f"Error: Jito rate-limited. Fallback failed: {fb}"
                return f"Error: Jito sendBundle failed {e.response.status_code}: {body}"
            except Exception as e:
                fb = await self.perform_pumpfun_swap(sender_private_key_json, amount, action, mint)
                return fb if not fb.startswith("Error") else f"Error: Jito error '{self._format_exc(e)}'. Fallback failed: {fb}"

            return signatures[0] if signatures else "OK"
        except Exception as e:
            return f"Error: {self._format_exc(e)}"

    # ---------- Misc ----------
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
            tx = VersionedTransaction(msg, [sender_keypair])  # signed

            try:
                resp = self.client.send_raw_transaction(
                    self._tx_bytes(tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
                )
            except Exception as e:
                return f"Error: {self._format_exc(e)}"

            sig = getattr(resp, "value", None)
            return str(sig) if sig else f"Error: RPC returned no signature: {resp}"
        except Exception as e:
            return f"Error: {self._format_exc(e)}"

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

            try:
                supply_resp = self.client.get_token_supply(mint)
                decimals = supply_resp.value.decimals
            except Exception:
                decimals = 6

            token_amount = int(amount * (10 ** decimals))

            ixs = []
            try:
                acc = self.client.get_account_info(recipient_ata)
                if acc.value is None:
                    ixs.append(create_associated_token_account(payer=sender_pubkey, owner=recipient, mint=mint))
            except Exception:
                ixs.append(create_associated_token_account(payer=sender_pubkey, owner=recipient, mint=mint))

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
            tx = VersionedTransaction(msg, [sender_keypair])  # signed

            try:
                resp = self.client.send_raw_transaction(
                    self._tx_bytes(tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
                )
            except Exception as e:
                return f"Error: {self._format_exc(e)}"

            sig = getattr(resp, "value", None)
            return str(sig) if sig else f"Error: RPC returned no signature: {resp}"
        except Exception as e:
            return f"Error: {self._format_exc(e)}"

    # ---------- BALANCES (fix utama) ----------
    def get_spl_token_balances(self, owner_address: str):
        """
        Return list of token balances for `owner_address`.
        Shape: [{ "mint": str, "amount": float_ui, "decimals": int }, ...]
        """
        try:
            owner = Pubkey(owner_address)
        except Exception as e:
            print(f"[get_spl_token_balances] invalid owner: {e}")
            return []

        out = []
        try:
            # Ambil semua ATA di program SPL Token standar (jsonParsed)
            resp = self.client.get_token_accounts_by_owner_json_parsed(
                owner,
                TokenAccountOpts(program_id=TOKEN_PROGRAM_ID),
            )
            for acc in (getattr(resp, "value", None) or []):
                try:
                    # dukung object maupun dict
                    acc_obj = getattr(acc, "account", None) or acc.get("account")
                    data = getattr(acc_obj, "data", None) or acc_obj.get("data")
                    parsed = getattr(data, "parsed", None) or data.get("parsed")
                    info = parsed["info"]
                    mint = info["mint"]
                    ta = info["tokenAmount"]
                    dec = int(ta.get("decimals", 0))

                    ui = ta.get("uiAmount")
                    if ui is None:
                        amt_str = ta.get("uiAmountString")
                        if amt_str is not None:
                            ui = float(amt_str)
                        else:
                            raw = float(ta.get("amount", "0"))
                            ui = raw / (10 ** dec if dec else 1)

                    out.append({"mint": mint, "amount": float(ui), "decimals": dec})
                except Exception:
                    continue
        except Exception as e:
            print(f"[get_spl_token_balances] error for {owner_address}: {e}")

        return out

    def get_token_decimals(self, mint_str: str) -> int:
        """Fallback helper if needed."""
        try:
            mint = Pubkey(mint_str)
            supply = self.client.get_token_supply(mint)
            return int(supply.value.decimals)
        except Exception:
            return 6

    def get_token_balance(self, owner_address: str, mint_address: str) -> float:
        """
        Total uiAmount untuk MINT tertentu pada OWNER.
        Pakai jsonParsed + filter mint agar akurat.
        """
        try:
            owner = Pubkey.from_string(owner_address)
            mint = Pubkey.from_string(mint_address)

            # gunakan endpoint jsonParsed versi mint-filtered
            resp = self.client.get_token_accounts_by_owner_json_parsed(
                owner,
                TokenAccountOpts(mint=mint),
            )
            total = 0.0
            for item in (getattr(resp, "value", None) or []):
                try:
                    acc = getattr(item, "account", None) or item.get("account")
                    data = getattr(acc, "data", None) or acc.get("data")
                    parsed = getattr(data, "parsed", None) or data.get("parsed")
                    info = parsed.get("info") if isinstance(parsed, dict) else None
                    if not isinstance(info, dict):
                        continue
                    ta = info.get("tokenAmount") or {}
                    dec = int(ta.get("decimals", 0))
                    ui = ta.get("uiAmount")
                    if ui is None:
                        amt_str = ta.get("uiAmountString")
                        if amt_str is not None:
                            ui = float(amt_str)
                        else:
                            raw = float(ta.get("amount", "0"))
                            ui = raw / (10 ** dec if dec else 1)
                    total += float(ui)
                except Exception:
                    continue
            return float(total)
        except Exception as e:
            print(f"[get_token_balance] error for {owner_address} mint {mint_address}: {e}")
            return 0.0
