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
    selected, scores, _ = bt._select(swaps, {}, T0 + timedelta(days=15), 30, {}, set(), 5)  # noqa: SLF001
    assert "late" not in selected
    assert scores["late"] < 50  # no data before t → neutral/low prior only
    params = BacktestParams(train_days=10, test_days=5)
    result = bt.run(swaps, params)
    assert "results" in result
