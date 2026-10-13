"""Where a wallet's tokens come from: recognised buys, transfers, own tokens and blind spots."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from copytrader.analysis.flows import FlowReport, classify_token_flows
from copytrader.cli import _explain_wallet

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
POOL = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
MINT2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOL = 1_000_000_000


def tb(index: int, mint: str, owner: str, amount: int) -> dict[str, Any]:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(amount), "decimals": 6},
    }


def tx(keys, pre, post, pre_tok, post_tok, sig="sig1", fee=5000) -> dict[str, Any]:
    return {
        "slot": 1,
        "blockTime": 1_750_000_000,
        "meta": {
            "err": None,
            "fee": fee,
            "preBalances": pre,
            "postBalances": post,
            "preTokenBalances": pre_tok,
            "postTokenBalances": post_tok,
        },
        "transaction": {
            "signatures": [sig],
            "message": {"accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate(keys)]},
        },
    }


def kinds(raw: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(f.kind, f.counterpart) for f in classify_token_flows(raw, WALLET)]


def test_recognised_buy_and_sell():
    buy = tx(
        [WALLET, "ata", POOL, "vault"],
        [10 * SOL, 0, SOL, 0],
        [9 * SOL - 5000, 0, 2 * SOL, 0],
        [tb(1, MINT, WALLET, 0), tb(3, MINT, POOL, 1_000_000_000)],
        [tb(1, MINT, WALLET, 500_000_000), tb(3, MINT, POOL, 500_000_000)],
    )
    assert kinds(buy) == [("buy", None)]
    sell = tx(
        [WALLET, "ata", POOL, "vault"],
        [9 * SOL, 0, 2 * SOL, 0],
        [10 * SOL - 5000, 0, SOL, 0],
        [tb(1, MINT, WALLET, 500_000_000), tb(3, MINT, POOL, 500_000_000)],
        [tb(1, MINT, WALLET, 0), tb(3, MINT, POOL, 1_000_000_000)],
    )
    assert kinds(sell) == [("sell", None)]


def test_tokens_moved_between_wallets_without_paying():
    transfer_in = tx(
        [OTHER, WALLET, "src", "dst"],
        [SOL, SOL, 0, 0],
        [SOL - 5000, SOL, 0, 0],
        [tb(2, MINT, OTHER, 700), tb(3, MINT, WALLET, 0)],
        [tb(2, MINT, OTHER, 0), tb(3, MINT, WALLET, 700)],
    )
    assert kinds(transfer_in) == [("transfer_in", OTHER)]
    transfer_out = tx(
        [WALLET, OTHER, "src", "dst"],
        [SOL, SOL, 0, 0],
        [SOL - 5000, SOL, 0, 0],
        [tb(2, MINT, WALLET, 700), tb(3, MINT, OTHER, 0)],
        [tb(2, MINT, WALLET, 0), tb(3, MINT, OTHER, 700)],
    )
    assert kinds(transfer_out) == [("transfer_out", OTHER)]


def test_token_created_by_the_wallet_itself():
    create = tx(
        [WALLET, "ata", "curve", "curve_ata", MINT],
        [10 * SOL, 0, 0, 0, 0],
        [9 * SOL, 0, SOL, 0, 0],
        [],
        [tb(1, MINT, WALLET, 300), tb(3, MINT, "curve", 700)],
    )
    assert kinds(create) == [("created", None)]


def test_paid_but_not_understood_is_reported_as_a_blind_spot():
    two_tokens = tx(
        [WALLET, "a1", "a2", POOL, "v1", "v2"],
        [10 * SOL, 0, 0, SOL, 0, 0],
        [9 * SOL, 0, 0, 2 * SOL, 0, 0],
        [tb(4, MINT, POOL, 900), tb(5, MINT2, POOL, 900)],
        [tb(1, MINT, WALLET, 100), tb(2, MINT2, WALLET, 100), tb(4, MINT, POOL, 800), tb(5, MINT2, POOL, 800)],
    )
    assert kinds(two_tokens) == [("unparsed_buy", None), ("unparsed_buy", None)]


def test_airdrop_is_other():
    airdrop = tx([OTHER, WALLET, "dst"], [SOL, SOL, 0], [SOL - 5000, SOL, 0], [], [tb(2, MINT, WALLET, 50)])
    assert kinds(airdrop) == [("other_in", None)]
    mass_mint = tx(
        [OTHER, WALLET, "dst", "dst2"],
        [SOL, SOL, 0, 0],
        [SOL - 5000, SOL, 0, 0],
        [],
        [tb(2, MINT, WALLET, 50), tb(3, MINT, "someone", 50)],
    )
    assert kinds(mass_mint) == [("other_in", None)]  # new token, but the wallet paid nothing: not its creator
    spam = tx(
        [OTHER, WALLET, "dst", "src"],
        [SOL, SOL, 0, 0],
        [SOL - 5000, SOL, 0, 0],
        [tb(3, MINT, "minter", 0)],
        [tb(2, MINT, WALLET, 50), tb(3, MINT, "minter", 0)],
    )
    assert kinds(spam) == [("other_in", None)]


def test_report_counts_and_points_at_blind_spots():
    r = FlowReport(WALLET)
    when = datetime(2026, 10, 1, tzinfo=UTC)
    r.add(
        classify_token_flows(
            tx(
                [OTHER, WALLET, "s", "d"],
                [SOL, SOL, 0, 0],
                [SOL, SOL, 0, 0],
                [tb(2, MINT, OTHER, 7), tb(3, MINT, WALLET, 0)],
                [tb(2, MINT, OTHER, 0), tb(3, MINT, WALLET, 7)],
                sig="t1",
            ),
            WALLET,
        ),
        when,
    )
    r.add([], when)
    text = r.render()
    assert "Transacciones revisadas: 2" in text and "recibidos de otra wallet" in text
    assert f"{OTHER[:4]}…{OTHER[-4:]} (1)" in text


async def test_explain_command_reads_the_wallet_history(capsys):
    buy = tx(
        [WALLET, "ata", POOL, "vault"],
        [10 * SOL, 0, SOL, 0],
        [9 * SOL - 5000, 0, 2 * SOL, 0],
        [tb(1, MINT, WALLET, 0), tb(3, MINT, POOL, 1000)],
        [tb(1, MINT, WALLET, 500), tb(3, MINT, POOL, 500)],
    )

    class Rpc:
        async def get_signatures_for_address(self, address, limit=1000):
            return [
                {"signature": "a", "err": None},
                {"signature": "b", "err": {"x": 1}},
                {"signature": "c", "err": None},
            ]

        async def get_transaction(self, signature):
            return buy if signature == "a" else None

    container = SimpleNamespace(providers=SimpleNamespace(rpc=Rpc()))
    assert await _explain_wallet(container, WALLET, 50) == 0
    out = capsys.readouterr().out
    assert "Transacciones revisadas: 1" in out and "No se pudieron descargar: 1" in out
    assert "    1  compras reconocidas" in out


class TraceRpc:
    """Sell tx → wallet token account "ata" → its history (newest first) → the oldest is ``first``."""

    def __init__(self, first: dict[str, Any]) -> None:
        self.sell = tx(
            [WALLET, "ata", POOL, "vault"],
            [SOL, 0, 2 * SOL, 0],
            [2 * SOL, 0, SOL, 0],
            [tb(1, MINT, WALLET, 500), tb(3, MINT, POOL, 0)],
            [tb(1, MINT, WALLET, 0), tb(3, MINT, POOL, 500)],
            sig="sell",
        )
        self.first = first

    async def get_transaction(self, signature):
        return {"sell": self.sell, "old": self.first, "mid": self.sell}.get(signature)

    async def get_signatures_for_address(self, address, *, before=None, until=None, limit=1000):
        assert address == "ata"
        return [
            {"signature": "sell", "err": None},
            {"signature": "mid", "err": None},
            {"signature": "old", "err": None},
        ]


async def test_origin_of_a_token_received_from_another_wallet():
    from copytrader.analysis.flows import describe_origin, trace_origin

    first = tx(
        [OTHER, WALLET, "src", "ata"],
        [SOL, SOL, 0, 0],
        [SOL - 5000, SOL, 0, 0],
        [tb(2, MINT, OTHER, 500), tb(3, MINT, WALLET, 0)],
        [tb(2, MINT, OTHER, 0), tb(3, MINT, WALLET, 500)],
        sig="old",
    )
    origin = await trace_origin(TraceRpc(first), WALLET, MINT, "sell")
    assert origin.kind == "transfer_in" and origin.counterpart == OTHER
    kind, text = describe_origin(origin, analysed_since=datetime(2020, 1, 1, tzinfo=UTC), stored=set())
    assert kind == "transfer_in" and "sin pagar" in text


async def test_origin_of_a_bought_token_tells_window_and_gaps_apart():
    from copytrader.analysis.flows import describe_origin, trace_origin

    first = tx(
        [WALLET, "ata", POOL, "vault"],
        [10 * SOL, 0, SOL, 0],
        [9 * SOL - 5000, 0, 2 * SOL, 0],
        [tb(1, MINT, WALLET, 0), tb(3, MINT, POOL, 1000)],
        [tb(1, MINT, WALLET, 500), tb(3, MINT, POOL, 500)],
        sig="old",
    )
    origin = await trace_origin(TraceRpc(first), WALLET, MINT, "sell")
    assert origin.kind == "buy" and origin.signature == "old"
    bought = datetime.fromtimestamp(1_750_000_000, tz=UTC)
    before = describe_origin(origin, analysed_since=bought.replace(year=bought.year + 1), stored=set())
    missing = describe_origin(origin, analysed_since=bought.replace(year=bought.year - 1), stored=set())
    kept = describe_origin(origin, analysed_since=bought.replace(year=bought.year - 1), stored={"old"})
    assert before[0] == "before_window" and missing[0] == "buy_not_stored" and kept[0] == "buy_stored"
    assert "solscan.io/tx/old" in missing[1]


async def test_origin_not_found_never_raises():
    from copytrader.analysis.flows import trace_origin

    class Broken(TraceRpc):
        async def get_transaction(self, signature):
            return None

    assert (await trace_origin(Broken({}), WALLET, MINT, "sell")).kind == "not_found"
