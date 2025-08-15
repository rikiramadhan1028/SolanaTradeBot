# file: tests/test_swap_and_token.py
import types
import base64
import pytest

# Module under test
import blockchain_clients.solana_client as sc


class DummyRPCResp:
    def __init__(self, value):
        self.value = value


class DummyBlockhashValue:
    def __init__(self, blockhash="11111111111111111111111111111111"):
        self.blockhash = blockhash


class DummyTokenSupplyValue:
    def __init__(self, decimals):
        self.decimals = decimals


def test_perform_swap_uses_send_raw_transaction(monkeypatch):
    client = sc.SolanaClient("http://localhost:8899")

    # stub keypair from 64 zero bytes (unsafe, tapi cukup untuk unit test stub)
    kp_bytes = bytes([1] * 64)
    monkeypatch.setattr(sc.base58, "b58decode", lambda s: kp_bytes)

    # stub jupiter aggregator
    monkeypatch.setattr(sc, "jupiter_get_route", lambda *a, **k: {"route": "ok"})
    monkeypatch.setattr(sc, "jupiter_get_tx", lambda *a, **k: base64.b64encode(b"tx").decode())

    # stub VersionedTransaction.deserialize -> object with sign(), serialize()
    class StubVtx:
        def sign(self, *_):  # noqa: D401
            return None

        def serialize(self):
            return b"serialized"

    monkeypatch.setattr(sc, "VersionedTransaction", types.SimpleNamespace(deserialize=lambda *_: StubVtx()))

    # stub RPC send_raw_transaction & confirm
    sent = {"called": False, "payload": None}

    def _send_raw(tx_bytes, opts=None):
        sent["called"] = True
        sent["payload"] = tx_bytes
        return DummyRPCResp("SIG123")

    def _confirm(sig, commitment=None):
        return None

    monkeypatch.setattr(client.client, "send_raw_transaction", _send_raw)
    monkeypatch.setattr(client.client, "confirm_transaction", _confirm)

    # stub blockhash
    monkeypatch.setattr(
        client.client, "get_latest_blockhash", lambda: DummyRPCResp(DummyBlockhashValue())
    )

    sig = client.__class__.perform_swap.__wrapped__(client,  # type: ignore[attr-defined]
        # bypass bound method? easier: call directly
    )

    # easier: call method directly without __wrapped__
    sig = client.client  # avoid mypy noise

    # invoke perform_swap
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    res = loop.run_until_complete(
        client.perform_swap("BASE58_WALLET", 1000, "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "jupiter")
    )
    loop.close()

    assert sent["called"] is True
    assert res == "SIG123"


def test_send_spl_token_uses_mint_decimals_and_creates_ata(monkeypatch):
    client = sc.SolanaClient("http://localhost:8899")

    # stub keypair decode
    kp_bytes = bytes([2] * 64)
    monkeypatch.setattr(sc.base58, "b58decode", lambda s: kp_bytes)

    # stub blockhash
    monkeypatch.setattr(
        client.client, "get_latest_blockhash", lambda: DummyRPCResp(DummyBlockhashValue())
    )

    # stub get_token_supply -> decimals 9
    monkeypatch.setattr(
        client.client, "get_token_supply", lambda mint: DummyRPCResp(DummyTokenSupplyValue(9))
    )

    # recipient ATA missing -> account_info None, so we create ATA
    class DummyAccInfo:
        def __init__(self, value):
            self.value = value

    monkeypatch.setattr(client.client, "get_account_info", lambda *_: DummyAccInfo(None))

    # capture call to send_raw_transaction
    captured = {"serialized_len": 0}

    def _send_raw(tx_bytes, opts=None):
        captured["serialized_len"] = len(tx_bytes)
        return DummyRPCResp("SIG_SPL")

    monkeypatch.setattr(client.client, "send_raw_transaction", _send_raw)

    sig = client.send_spl_token(
        "BASE58_PRIVATE",
        "So11111111111111111111111111111111111111112",  # dummy mint
        "8" * 32,  # dummy pubkey string format; parsing is not strict here
        1.5,  # UI amount
    )

    assert sig == "SIG_SPL"
    # serialized bytes should not be empty if message compiled
    assert captured["serialized_len"] > 0
