"""Network cost model shared by paper trading, the backtester and the entry filter."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copytrader.config.loader import build_config
from copytrader.core.errors import ConfigError
from copytrader.core.models import OrderRequest, Quote
from copytrader.core.types import OrderPurpose, Side, TradeMode
from copytrader.execution.costs import (
    entry_rent_lamports,
    expected_priority_fee_lamports,
    lamports_to_usd,
    round_trip_cost_lamports,
    swap_fee_lamports,
)
from copytrader.execution.paper import PaperExecutor
from copytrader.providers.solana.constants import SOL_MINT, TOKEN_ACCOUNT_RENT_LAMPORTS

MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


def cfg(**sections):
    return build_config(sections)


def test_priority_fee_defaults_to_the_configured_maximum():
    c = cfg(execution={"priority_fee_max_lamports": 800_000})
    assert expected_priority_fee_lamports(c) == 800_000
    assert swap_fee_lamports(c) == 5_000 + 800_000
    c = cfg(execution={"priority_fee_max_lamports": 800_000, "expected_priority_fee_lamports": 50_000})
    assert swap_fee_lamports(c) == 5_000 + 50_000


def test_jito_tip_replaces_the_priority_fee():
    c = cfg(execution={"jito_tip_lamports": 100_000, "priority_fee_max_lamports": 900_000})
    assert swap_fee_lamports(c) == 5_000 + 100_000


def test_rent_is_a_cost_only_when_empty_accounts_are_not_closed():
    assert entry_rent_lamports(cfg()) == 0
    kept = cfg(execution={"close_empty_token_accounts": False})
    assert entry_rent_lamports(kept) == TOKEN_ACCOUNT_RENT_LAMPORTS
    assert round_trip_cost_lamports(kept) == 2 * swap_fee_lamports(kept) + TOKEN_ACCOUNT_RENT_LAMPORTS


def test_expected_fee_cannot_exceed_the_maximum():
    with pytest.raises(ConfigError):
        cfg(execution={"priority_fee_max_lamports": 1_000, "expected_priority_fee_lamports": 2_000})


def test_small_trades_are_dominated_by_fixed_costs():
    # Defaults: 2 x (5,000 + 1,000,000) lamports ~ 0.002 SOL per round trip.
    usd = lamports_to_usd(round_trip_cost_lamports(cfg()), 200.0)
    assert usd == pytest.approx(0.402)
    assert usd / 10 * 100 > 3.0  # a 10 USD trade loses >3 % to network costs alone
    assert usd / 20 * 100 < 3.0


class _Tokens:
    async def sol_price(self) -> float:
        return 200.0


class _Quotes:
    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps, **_kw):
        return Quote(input_mint, output_mint, amount_raw, 1_000_000, 990_000, slippage_bps, 0.001, datetime.now(UTC))


class _Handle:
    async def mark(self, *_a, **_k):
        return None


class _Clock:
    def now(self):
        return datetime.now(UTC)


@pytest.mark.parametrize("close_accounts", [True, False])
async def test_paper_fill_charges_the_live_network_costs(close_accounts):
    c = cfg(
        paper={"simulated_latency_ms": 0, "extra_slippage_bps": 0},
        execution={"close_empty_token_accounts": close_accounts},
    )
    ex = PaperExecutor(_Quotes(), _Tokens(), _Clock(), lambda: c)
    req = OrderRequest(
        client_order_id="o1",
        purpose=OrderPurpose.ENTRY,
        side=Side.BUY,
        mode=TradeMode.PAPER,
        token_mint=MINT,
        token_decimals=6,
        input_mint=SOL_MINT,
        output_mint=MINT,
        amount_in_raw=100_000_000,
        slippage_bps=150,
    )
    result = await ex.run(_Handle(), req, None)
    # 0.1 SOL = 20 USD: the priority fee is capped at 0.5 % of the trade (execution.priority_fee_max_trade_pct)
    assert swap_fee_lamports(c, 20.0, 200.0) == 5_000 + 500_000 < swap_fee_lamports(c)
    expected = swap_fee_lamports(c, 20.0, 200.0) + (0 if close_accounts else TOKEN_ACCOUNT_RENT_LAMPORTS)
    assert result.success
    assert result.fees_usd == pytest.approx(lamports_to_usd(expected, 200.0))
    assert result.network_fee_lamports == 505_000
    assert result.fee_decision["source"] == "size_cap" and result.fee_decision["priority_level"] == "veryHigh"
