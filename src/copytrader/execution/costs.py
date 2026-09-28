"""Network cost model shared by paper trading, the backtester and the entry filter.

It mirrors what the live Jupiter builder pays per swap transaction:

* the base fee of one signature;
* EITHER the priority fee (``priorityLevelWithMaxLamports``) OR the Jito tip —
  the builder sends one or the other, never both;
* the rent of the token account created on a buy (~0.002 SOL) when empty
  accounts are NOT closed afterwards. With closing enabled the rent is
  refunded when the position is closed, so it is not a cost.

For small trades these fixed costs are a large fraction of the expected edge,
which is why the entry pipeline rejects trades whose round trip would cost more
than ``risk.max_round_trip_cost_pct`` of the position.
"""

from __future__ import annotations

from copytrader.config.models import AppConfig
from copytrader.providers.solana.constants import TOKEN_ACCOUNT_RENT_LAMPORTS

LAMPORTS_PER_SOL = 1_000_000_000


def expected_priority_fee_lamports(cfg: AppConfig) -> int:
    """Priority fee we expect to pay per transaction (conservative default: the configured maximum)."""
    ex = cfg.execution
    if ex.expected_priority_fee_lamports is not None:
        return ex.expected_priority_fee_lamports
    return ex.priority_fee_max_lamports


def swap_fee_lamports(cfg: AppConfig) -> int:
    """Network cost of one swap transaction."""
    base = round(cfg.paper.network_fee_sol * LAMPORTS_PER_SOL)
    if cfg.execution.jito_tip_lamports > 0:
        return base + cfg.execution.jito_tip_lamports
    return base + expected_priority_fee_lamports(cfg)


def entry_rent_lamports(cfg: AppConfig) -> int:
    """Token-account rent that is lost on a buy (0 when empty accounts are closed and refunded)."""
    return 0 if cfg.execution.close_empty_token_accounts else TOKEN_ACCOUNT_RENT_LAMPORTS


def round_trip_cost_lamports(cfg: AppConfig) -> int:
    """Fixed cost of buying and later selling one position (excluding price impact/slippage)."""
    return 2 * swap_fee_lamports(cfg) + entry_rent_lamports(cfg)


def lamports_to_usd(lamports: int, sol_price_usd: float) -> float:
    return lamports / LAMPORTS_PER_SOL * sol_price_usd
