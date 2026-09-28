"""Network cost model shared by paper trading, the backtester and the entry filter.

It mirrors what the live Jupiter builder pays per swap transaction:

* the base fee of one signature;
* EITHER the priority fee (``priorityLevelWithMaxLamports``) OR the Jito tip —
  the builder sends one or the other, never both;
* the rent of the token account created on a buy (~0.002 SOL) when empty
  accounts are NOT closed afterwards. With closing enabled the rent is
  refunded when the position is closed, so it is not a cost.

The priority fee (or tip) is bounded by the absolute cap and, when the trade
value is known, by ``execution.priority_fee_max_trade_pct`` of it (see
``execution/fees.py``). What we expect to pay, in order of preference: the
median actually paid by recent live swaps (``observed``), the configured
``expected_priority_fee_lamports``, the market estimate (Jito landed tips,
``market``) and finally the cap itself (conservative).

For small trades these fixed costs are a large fraction of the expected edge,
which is why the entry pipeline rejects trades whose round trip would cost more
than ``risk.max_round_trip_cost_pct`` of the position.
"""

from __future__ import annotations

from copytrader.config.models import AppConfig
from copytrader.providers.solana.constants import TOKEN_ACCOUNT_RENT_LAMPORTS

LAMPORTS_PER_SOL = 1_000_000_000


def base_fee_lamports(cfg: AppConfig) -> int:
    return round(cfg.paper.network_fee_sol * LAMPORTS_PER_SOL)


def priority_cap_lamports(cfg: AppConfig, notional_usd: float | None = None, sol_price: float | None = None) -> int:
    """Most we pay on top of the base fee (priority fee, or tip with Jito) for one swap."""
    ex = cfg.execution
    jito = ex.jito_tip_lamports > 0
    cap = ex.jito_tip_lamports if jito else ex.priority_fee_max_lamports
    if ex.priority_fee_max_trade_pct is not None and notional_usd and sol_price:
        by_size = int(notional_usd * ex.priority_fee_max_trade_pct / 100 / sol_price * LAMPORTS_PER_SOL)
        floor = ex.min_jito_tip_lamports if jito else ex.min_priority_fee_lamports
        cap = min(cap, max(by_size, floor))
    return cap


def expected_priority_fee_lamports(
    cfg: AppConfig,
    notional_usd: float | None = None,
    sol_price: float | None = None,
    *,
    market: int | None = None,
) -> int:
    """Priority fee (or tip) we expect to pay per transaction."""
    ex = cfg.execution
    cap = priority_cap_lamports(cfg, notional_usd, sol_price)
    if ex.expected_priority_fee_lamports is not None:
        return min(ex.expected_priority_fee_lamports, cap)
    if market is not None:
        return min(market, cap)
    return cap


def swap_fee_lamports(
    cfg: AppConfig,
    notional_usd: float | None = None,
    sol_price: float | None = None,
    *,
    observed: int | None = None,
    market: int | None = None,
) -> int:
    """Network cost of one swap transaction (``observed``: typical total fee actually paid)."""
    base = base_fee_lamports(cfg)
    if observed is not None:
        # never above what the current caps allow, even if we paid more in the past
        return min(observed, base + priority_cap_lamports(cfg, notional_usd, sol_price))
    return base + expected_priority_fee_lamports(cfg, notional_usd, sol_price, market=market)


def entry_rent_lamports(cfg: AppConfig) -> int:
    """Token-account rent that is lost on a buy (0 when empty accounts are closed and refunded)."""
    return 0 if cfg.execution.close_empty_token_accounts else TOKEN_ACCOUNT_RENT_LAMPORTS


def round_trip_cost_lamports(cfg: AppConfig, swap_fee: int | None = None) -> int:
    """Fixed cost of buying and later selling one position (excluding price impact/slippage).

    ``swap_fee``: expected cost of one swap (e.g. from ``FeePolicy``); default: the static model.
    """
    fee = swap_fee_lamports(cfg) if swap_fee is None else swap_fee
    return 2 * fee + entry_rent_lamports(cfg)


def lamports_to_usd(lamports: int, sol_price_usd: float) -> float:
    return lamports / LAMPORTS_PER_SOL * sol_price_usd
