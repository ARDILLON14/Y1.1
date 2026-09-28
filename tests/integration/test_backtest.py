from __future__ import annotations

from datetime import timedelta

from copytrader.backtest.engine import Backtester, BacktestParams
from copytrader.backtest.service import run_backtest
from copytrader.core.types import Side
from copytrader.db.repositories import BacktestRepo
from tests.helpers import T0, swap


async def test_backtest_runs_walk_forward_on_simulated_history(container):
    run_id = await run_backtest(container, {"train_days": 20, "test_days": 7})
    async with container.db.session() as s:
        run = await BacktestRepo(s).get(run_id)
    assert run.status == "done", run.error
    res = run.results
    assert set(res["results"]) == {"strategy", "copy_all", "top_pnl"}
    assert len(res["windows"]) >= 2
    for name, summary in res["results"].items():
        assert summary["n_trades"] >= 0, name
        assert summary["max_drawdown_pct"] >= 0
    # the strategy must not select blocked archetypes (wash traders, snipers)
    market = container.providers.simulated_market
    chosen = {sel["wallet"] for w in res["windows"] for sel in w["selected"]}
    assert not any(market.wallets[a].archetype in ("wash", "sniper") for a in chosen)


def test_no_look_ahead_in_selection():
    """A wallet that only becomes great AFTER t must not be selected at t."""
    from copytrader.config.models import AppConfig

    cfg = AppConfig()
    swaps = {"late": [], "early": []}
    for i in range(60):
        t = T0 + timedelta(hours=6 * i)
        swaps["early"].append(swap("early", f"E{i}", Side.BUY, t, 100, 100))
        swaps["early"].append(swap("early", f"E{i}", Side.SELL, t + timedelta(hours=1), 100, 90))
        t2 = T0 + timedelta(days=16, hours=6 * i)
        swaps["late"].append(swap("late", f"L{i}", Side.BUY, t2, 100, 100))
        swaps["late"].append(swap("late", f"L{i}", Side.SELL, t2 + timedelta(hours=1), 100, 150))
    bt = Backtester(lambda: cfg)
    selected, scores, _, _ = bt._select(swaps, {}, T0 + timedelta(days=15), 30, {}, set(), 5)
    assert "late" not in selected
    assert scores["late"] < 50  # no data before t → neutral/low prior only
    params = BacktestParams(train_days=10, test_days=5)
    result = bt.run(swaps, params)
    assert "results" in result


async def test_backtest_compares_configuration_variants_on_the_same_data(container):
    import pytest

    from copytrader.backtest.service import validate_variants
    from copytrader.core.errors import ConfigError

    variants = [
        {"name": "Solo top 3", "patch": {"selection": {"top_n": 3}}},
        {"name": "Salida inteligente", "patch": {"exits": {"default_mode": "smart"}}},
    ]
    run_id = await run_backtest(container, {"train_days": 20, "test_days": 7}, variants)
    async with container.db.session() as s:
        run = await BacktestRepo(s).get(run_id)
    assert run.status == "done", run.error
    compared = run.results["variants"]
    assert [v["name"] for v in compared] == ["Configuración actual", "Solo top 3", "Salida inteligente"]
    assert compared[0]["summary"] == {
        k: v for k, v in run.results["results"]["strategy"].items() if k != "equity_curve"
    }
    for v in compared:
        assert "roi_pct" in v["summary"] and v["equity_curve"]
    assert run.params["variants"][0]["patch"] == {"selection": {"top_n": 3}}

    with pytest.raises(ConfigError):  # same scope rules as a live change: no touching the operating level
        validate_variants(container, [{"name": "x", "patch": {"app": {"operating_level": 5}}}])
    with pytest.raises(ConfigError):  # hard limits still apply
        validate_variants(container, [{"name": "x", "patch": {"risk": {"max_trade_usd": 10**6}}}])
    with pytest.raises(ConfigError):
        validate_variants(container, [{"name": str(i), "patch": {"selection": {"top_n": i + 1}}} for i in range(3)])


def test_backtest_simulates_the_protective_exits():
    from copytrader.backtest.engine import WalletFacts, _Pos
    from copytrader.config.loader import build_config

    cfg = build_config({"exits": {"wallet_sells_exit_min": 2, "wallet_sells_exit_fraction": 0.5}})
    bt = Backtester(lambda: cfg)
    params = BacktestParams()
    pos = _Pos("M", "src", qty=10, cost=100, entry_price=10, peak=10, opened_at=T0, at_risk=20)
    closed: list[tuple[str, float]] = []

    def close(p, fraction, price, when, reason):
        closed.append((reason, fraction))

    facts = {w: WalletFacts(None, None, w != "blocked") for w in ("a", "b", "blocked")}

    def sell(wallet: str, minutes: float, before: float, after: float):
        ev = swap(wallet, "M", Side.SELL, T0 + timedelta(minutes=minutes), 1, 10, before=before, after=after)
        bt._wallet_sell(pos, ev, ev.block_time, 10.0, params, facts, close)

    sell("a", 5, 100, 10)
    sell("blocked", 6, 100, 0)  # not credible: does not count
    sell("b", 7, 100, 80)  # 20 %: not an exit yet
    assert not closed
    sell("b", 8, 80, 40)  # 60 % of its holding in the window: two credible wallets out
    assert closed == [("wallets_selling", 0.5)]
    sell("a", 9, 10, 0)
    assert len(closed) == 1  # once per position
    # a sell outside the window (60 min) no longer counts
    late = _Pos("M", "src", qty=10, cost=100, entry_price=10, peak=10, opened_at=T0, at_risk=20)
    closed.clear()
    pos = late
    sell("a", 5, 100, 0)
    sell("b", 120, 100, 0)
    assert not closed


def test_wallet_sells_exit_is_off_by_default():
    from copytrader.backtest.engine import WalletFacts, _Pos
    from copytrader.config.loader import build_config

    cfg = build_config({})
    bt = Backtester(lambda: cfg)
    pos = _Pos("M", "src", qty=10, cost=100, entry_price=10, peak=10, opened_at=T0, at_risk=20)
    closed: list[str] = []
    facts = {w: WalletFacts(None, None, True) for w in ("a", "b")}
    for i, w in enumerate(("a", "b")):
        ev = swap(w, "M", Side.SELL, T0 + timedelta(minutes=i + 1), 1, 10, before=100, after=0)
        bt._wallet_sell(pos, ev, ev.block_time, 10.0, BacktestParams(), facts, lambda *a: closed.append(a[-1]))
    assert not closed
