from datetime import timedelta

from copytrader.analysis.analyzer import WalletAnalyzer
from copytrader.config.models import AppConfig, SelectionSection, StatusRulesSection
from copytrader.core.models import Flag
from copytrader.core.types import ListType, Severity, Side, WalletStatus
from copytrader.scoring.scorer import ScoringEngine
from copytrader.scoring.status import decide_status
from copytrader.selection.selector import Candidate, select_wallets
from tests.helpers import T0, swap

CFG = AppConfig()


def _swaps(returns, *, wallet="W", start=T0, gap_hours=6.0, size=100.0):
    out = []
    for i, r in enumerate(returns):
        t = start + timedelta(hours=i * gap_hours)
        mint = f"T{i % 9}"
        out.append(swap(wallet, mint, Side.BUY, t, 100, size, sig=f"b{i}"))
        out.append(swap(wallet, mint, Side.SELL, t + timedelta(hours=2), 100, size * (1 + r), sig=f"s{i}"))
    return out


def _analyse(returns, **kw):
    swaps = _swaps(returns, **kw)
    now = swaps[-1].block_time + timedelta(hours=1)
    return WalletAnalyzer(lambda: CFG).analyze(1, "W", swaps, now=now, tokens={}, current_prices={})


def test_small_perfect_sample_cannot_outrank_large_good_sample():
    small = _analyse([0.5] * 4)
    large = _analyse([0.3, 0.25, -0.1, 0.2, 0.15, -0.05, 0.2, 0.3, -0.1, 0.1] * 12)
    engine = ScoringEngine(lambda: CFG)
    s_small = engine.score(small, [])
    s_large = engine.score(large, [])
    assert s_small.confidence < 0.2
    assert s_large.score > s_small.score
    assert s_small.score < CFG.status_rules.min_score_active


def test_losing_wallet_scores_low():
    bad = _analyse([-0.2, 0.05, -0.3, -0.1, 0.02] * 20)
    assert ScoringEngine(lambda: CFG).score(bad, []).score < 40


def test_penalties_and_critical_cap():
    a = _analyse([0.3, 0.25, -0.1, 0.2] * 20)
    engine = ScoringEngine(lambda: CFG)
    clean = engine.score(a, []).score
    warned = engine.score(a, [Flag("LOW_LIQUIDITY", Severity.WARNING, "x")]).score
    critical = engine.score(a, [Flag("WASH_TRADING", Severity.CRITICAL, "x")]).score
    assert warned == clean - CFG.scoring.warning_penalty_points
    assert critical <= 20


def test_recent_deterioration_lowers_score():
    good = _analyse([0.3, 0.2, -0.05, 0.25] * 25)
    degraded = _analyse([0.3, 0.2, -0.05, 0.25] * 18 + [-0.2, -0.15, 0.02, -0.3] * 7)
    engine = ScoringEngine(lambda: CFG)
    assert engine.score(degraded, []).score < engine.score(good, []).score - 5


def test_status_rules():
    rules = StatusRulesSection()
    a = _analyse([0.3, 0.2, -0.05, 0.25] * 10)
    assert (
        decide_status(list_type=ListType.BLACKLIST, score=90, metrics=a.all, flags=[], rules=rules).status
        is WalletStatus.BLOCKED
    )
    crit = decide_status(
        list_type=ListType.NONE,
        score=90,
        metrics=a.all,
        flags=[Flag("SINGLE_TRADE", Severity.CRITICAL, "una sola op")],
        rules=rules,
    )
    assert crit.status is WalletStatus.BLOCKED and "una sola op" in crit.reasons[0]
    low = decide_status(list_type=ListType.NONE, score=30, metrics=a.all, flags=[], rules=rules)
    assert low.status is WalletStatus.OBSERVE and any("Score" in r for r in low.reasons)
    ok = decide_status(list_type=ListType.NONE, score=80, metrics=a.all, flags=[], rules=rules)
    assert ok.status is WalletStatus.ACTIVE
    watch = decide_status(list_type=ListType.WATCHLIST, score=80, metrics=a.all, flags=[], rules=rules)
    assert watch.status is WalletStatus.OBSERVE
    few = _analyse([0.3] * 5)
    assert (
        decide_status(list_type=ListType.NONE, score=80, metrics=few.all, flags=[], rules=rules).status
        is WalletStatus.OBSERVE
    )


def _cand(addr, score, status=WalletStatus.ACTIVE, lt=ListType.NONE):
    return Candidate(wallet_id=hash(addr) % 1000, address=addr, score=score, status=status, list_type=lt)


def test_selection_top_n_and_hard_exclusions():
    cfg = SelectionSection(top_n=2, min_score=50, rank_buffer=0, hysteresis_points=0)
    cands = [
        _cand("a", 90),
        _cand("b", 80),
        _cand("c", 70),
        _cand("bl", 99, lt=ListType.BLACKLIST),
        _cand("blk", 95, status=WalletStatus.BLOCKED),
        _cand("obs", 94, status=WalletStatus.OBSERVE),
        _cand("w", 93, lt=ListType.WATCHLIST),
    ]
    res = select_wallets(cands, set(), cfg)
    assert res.addresses == {"a", "b"}
    assert res.reasons["bl"] == "Blacklist"
    assert "Fuera del Top" in res.reasons["c"]


def test_selection_hysteresis_keeps_incumbent():
    cfg = SelectionSection(top_n=2, min_score=50, rank_buffer=1, hysteresis_points=5)
    cands = [_cand("a", 90), _cand("new", 80), _cand("inc", 79), _cand("low", 47)]
    res = select_wallets(cands, {"inc", "low"}, cfg)
    assert "inc" in res.addresses and "a" in res.addresses and "new" not in res.addresses
    assert "low" not in res.addresses  # below min − hysteresis? 47 < 45? no: rank 4 > top_n + buffer
    res2 = select_wallets(cands, set(), cfg)
    assert res2.addresses == {"a", "new"}
    assert "plaza retenida" in res.reasons["new"]
    # a newcomer that is clearly better (≥ hysteresis points) does displace the incumbent
    better = [_cand("a", 90), _cand("new", 86), _cand("inc", 79)]
    assert select_wallets(better, {"inc"}, cfg).addresses == {"a", "new"}


def test_selection_whitelist_priority_and_min_score():
    cfg = SelectionSection(top_n=2, min_score=60, whitelist_min_score=40)
    cands = [
        _cand("a", 90),
        _cand("b", 85),
        _cand("wl", 45, lt=ListType.WHITELIST),
        _cand("wl_low", 30, lt=ListType.WHITELIST),
    ]
    res = select_wallets(cands, set(), cfg)
    assert res.addresses == {"wl", "a"}
    assert "Whitelist pero score" in res.reasons["wl_low"]


def test_selection_whitelist_in_observe_is_not_selected():
    """A manual whitelist never overrides a risk flag: OBSERVE is never copied."""
    cfg = SelectionSection(top_n=2, min_score=60, whitelist_min_score=40)
    cands = [_cand("a", 90), _cand("wl_obs", 95, lt=ListType.WHITELIST, status=WalletStatus.OBSERVE)]
    res = select_wallets(cands, {"wl_obs"}, cfg)
    assert res.addresses == {"a"}
    assert "observación (whitelist)" in res.reasons["wl_obs"]


def test_selection_top_n_is_configurable_without_code():
    cands = [_cand(str(i), 60 + i) for i in range(30)]
    for n in (5, 10, 20):
        assert len(select_wallets(cands, set(), SelectionSection(top_n=n, min_score=50)).selected) == n
