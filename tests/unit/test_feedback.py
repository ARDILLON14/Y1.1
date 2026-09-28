"""Learning from our own copies: the estimate is corrected by what copying really returned."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copytrader.analysis.metrics import WalletMetrics
from copytrader.config.models import LearningSection, StatusRulesSection
from copytrader.core.types import ListType, WalletStatus
from copytrader.scoring.feedback import apply_feedback, realized_stats
from copytrader.scoring.status import decide_status

RULES = StatusRulesSection(min_trades_active=10, min_score_active=0)
LEARNING = LearningSection(losing_min_positions=10)


def metrics(estimate: float = 4.0) -> WalletMetrics:
    m = WalletMetrics(window="all", computed_at=datetime(2026, 1, 1, tzinfo=UTC), n_closed_trades=40)
    m.copy_n, m.copy_expectancy_pct, m.copy_expectancy_lb_pct = 40, estimate, estimate - 2
    m.replication = {"latency_seconds": 3.0}
    return m


def status(m: WalletMetrics):
    return decide_status(list_type=ListType.NONE, score=80, metrics=m, flags=[], rules=RULES, learning=LEARNING)


def test_realized_stats_bounds():
    r = realized_stats([0.1, -0.05, 0.02, 0.03])
    assert r is not None and r.n == 4
    assert r.mean_pct == pytest.approx(2.5)
    assert r.lb_pct < r.mean_pct < r.ub_pct
    assert realized_stats([]) is None
    single = realized_stats([0.1])
    assert single is not None and single.ub_pct == float("inf")


def test_without_real_copies_the_estimate_is_used():
    m = metrics(4.0)
    apply_feedback(m, None, prior_positions=10)
    assert m.effective_copy_expectancy_pct == 4.0 and m.realized_copy_n == 0
    assert status(m).status is WalletStatus.ACTIVE


def test_real_copies_progressively_replace_the_estimate():
    m = metrics(4.0)
    apply_feedback(m, realized_stats([-0.02] * 10), prior_positions=10)
    assert m.effective_copy_expectancy_pct == pytest.approx(1.0)  # (10·4 + 10·(-2)) / 20
    m2 = metrics(4.0)
    apply_feedback(m2, realized_stats([-0.02] * 90), prior_positions=10)
    assert m2.effective_copy_expectancy_pct == pytest.approx(-1.4)  # reality dominates


def test_a_wallet_that_looks_good_but_loses_when_copied_is_demoted():
    m = metrics(4.0)
    apply_feedback(m, realized_stats([-0.05, -0.03, -0.04, -0.06, -0.02, -0.05, -0.04, -0.03, -0.05, -0.04]), 10)
    decision = status(m)
    assert decision.status is WalletStatus.OBSERVE
    assert "pierde en la práctica" in decision.reasons[0] and "10 copias" in decision.reasons[0]


def test_noisy_losses_blend_before_demoting():
    m = metrics(4.0)
    # 10 very noisy copies with a slightly negative mean: not clearly losing, blended value still positive
    apply_feedback(m, realized_stats([0.4, -0.3, 0.2, -0.35, 0.1, -0.2, 0.3, -0.25, 0.05, -0.1]), 10)
    assert m.realized_copy_ub_pct is not None and m.realized_copy_ub_pct > 0
    assert status(m).status is WalletStatus.ACTIVE
    m3 = metrics(1.0)
    apply_feedback(m3, realized_stats([0.2, -0.3, 0.1, -0.25, 0.05, -0.2, 0.1, -0.15, 0.0, -0.1]), 10)
    decision = status(m3)  # effective (1·10 − 5.5·10)/20 < 0: insufficient copyable edge
    assert decision.status is WalletStatus.OBSERVE and "insuficiente" in decision.reasons[0]
