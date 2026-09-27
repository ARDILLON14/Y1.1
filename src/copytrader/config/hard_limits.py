"""Absolute safety ceilings compiled into the program.

Configuration values are validated against these at load time and the
execution layer re-checks them independently right before sending an order.
They exist so that a typo in the YAML (``capital_usd: 100000`` instead of
``1000``) or a sizing bug can never put the whole bankroll into one trade.

Changing these numbers requires a code change and a new deployment — that is
intentional.
"""

from __future__ import annotations

# A single trade can never exceed this fraction of the configured capital.
HARD_MAX_TRADE_FRACTION = 0.25
# ...nor this absolute notional, whatever the capital.
HARD_MAX_TRADE_USD = 50_000.0
# Total simultaneous exposure can never exceed the capital (no leverage).
HARD_MAX_TOTAL_EXPOSURE_PCT = 100.0
# Slippage tolerance above this is never accepted for entries.
HARD_MAX_ENTRY_SLIPPAGE_PCT = 15.0
# Exits may need wider tolerance to get out of a collapsing token.
HARD_MAX_EXIT_SLIPPAGE_PCT = 50.0
# Loss limits cannot be disabled or set absurdly high.
HARD_MAX_DAILY_LOSS_PCT = 50.0
HARD_MAX_OPEN_POSITIONS = 100
# Live trading must always keep SOL for fees.
HARD_MIN_RESERVE_SOL = 0.01
# Level 4 ("small capital") can never exceed these, regardless of config.
HARD_LEVEL4_MAX_TRADE_USD = 100.0
HARD_LEVEL4_MAX_OPEN_POSITIONS = 5
