"""Plain-language verdicts on rejection filters."""

from __future__ import annotations

from datetime import UTC, datetime

from copytrader.db.models import SignalOutcome
from copytrader.measurement.attribution import AttributionService, verdict

T = datetime(2026, 1, 1, tzinfo=UTC)


def _outcome(status: str, check: str | None, ret_60: float) -> SignalOutcome:
    return SignalOutcome(
        signal_id=0,
        wallet_id=1,
        token_mint="M",
        status=status,
        failed_check=check,
        failed_label=check,
        reference_price_usd=1.0,
        reference_at=T,
        returns={"5": ret_60 / 2, "60": ret_60, "1440": None},
        completed=True,
    )


def test_filters_are_compared_with_what_was_executed():
    outcomes = (
        [_outcome("executed", None, 0.02) for _ in range(12)]
        + [_outcome("rejected", "liquidity", -0.10) for _ in range(12)]  # rejected tokens fall: protects
        + [_outcome("rejected", "market_cap", 0.30) for _ in range(12)]  # rejected tokens rise more: review
        + [_outcome("expired", "signal_age", 0.01) for _ in range(12)]
        + [_outcome("rejected", "slippage", 0.5) for _ in range(3)]
    )
    rows = {r["check"]: r for r in AttributionService._filters(outcomes, [5, 60, 1440])}
    assert next(iter(rows)) == "executed"
    assert rows["liquidity"]["verdict"].startswith("Protege")
    assert rows["market_cap"]["verdict"].startswith("Revisar")
    assert rows["signal_age"]["verdict"] == "Neutral"
    assert rows["slippage"]["verdict"] == "Muestra insuficiente"
    h = rows["liquidity"]["horizons"]
    assert h["60"]["n"] == 12 and h["60"]["up_frac"] == 0 and h["1440"]["n"] == 0


def test_verdict_needs_a_minimum_sample():
    group = {"horizons": {"60": {"n": 3, "mean_pct": -50.0}}}
    assert verdict(group, None, "60") == "Muestra insuficiente"
