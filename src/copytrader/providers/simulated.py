"""Simulated market: every Data Layer contract backed by a synthetic world.

Purpose:
* run the whole application end-to-end with zero API keys (demo mode);
* deterministic integration tests: each wallet has a known *archetype*
  (skilled, scalper, random, lucky, wash trader, sniper, coordinated,
  degrading, inactive, low-liquidity), so we can assert that the analyzer,
  detectors and scoring classify them correctly.

It is NOT a market model for research: prices are geometric Brownian motion
with jumps and rugs, pools are constant-product AMMs.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import structlog

from copytrader.core.clock import Clock
from copytrader.core.errors import ProviderError
from copytrader.core.models import Quote, SwapEvent
from copytrader.core.types import Side, TxSource
from copytrader.providers.interfaces import MarketData, MintData, RiskData, SwapHandler
from copytrader.providers.solana.constants import SOL_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM

log = structlog.get_logger(__name__)

STEP = timedelta(minutes=5)
LIVE_STEP = timedelta(seconds=5)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
AMM_FEE = 0.0025

ARCHETYPES = (
    "skilled",
    "skilled",
    "skilled",
    "scalper",
    "scalper",
    "random",
    "random",
    "random",
    "lucky",
    "wash",
    "sniper",
    "coordinated",
    "coordinated",
    "coordinated",
    "degrading",
    "inactive",
    "low_liquidity",
)


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


@dataclass
class SimToken:
    mint: str
    symbol: str
    created_at: datetime
    init_price: float
    supply: float
    base_liquidity: float
    vol: float  # per 5-min step (log)
    drift: float  # per step (log)
    rug_at: datetime | None
    category: str
    risk_score: float
    mint_authority: str | None
    freeze_authority: str | None
    dangerous: list[str]
    hist: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))
    live_times: list[datetime] = field(default_factory=list, repr=False)
    live_prices: list[float] = field(default_factory=list, repr=False)
    biases: list[tuple[datetime, float]] = field(default_factory=list, repr=False)
    decimals: int = 6


@dataclass
class SimWallet:
    address: str
    archetype: str
    label: str
    group: int | None = None


class SimulatedMarket:
    def __init__(
        self,
        *,
        clock: Clock,
        seed: int = 7,
        n_wallets: int = 40,
        n_tokens: int = 60,
        history_days: int = 60,
        realtime_trades_per_minute: float = 6.0,
        speedup: float = 1.0,
    ) -> None:
        self.clock = clock
        self.rng = np.random.default_rng(seed)
        self.t0 = clock.now()
        self.start = self.t0 - timedelta(days=history_days)
        self.realtime_rate = realtime_trades_per_minute
        self.speedup = speedup
        self._sol_hist = self._gbm_path(150.0, 0.0, 0.04 / math.sqrt(288), self._steps(self.start, self.t0))
        self._sol_live: list[tuple[datetime, float]] = []
        self.tokens: dict[str, SimToken] = {}
        self._make_tokens(n_tokens)
        self.wallets: dict[str, SimWallet] = {}
        self._make_wallets(n_wallets)
        self.history: dict[str, list[SwapEvent]] = {w: [] for w in self.wallets}
        self._holdings: dict[tuple[str, str], float] = {}
        self._generate_history()

    # ----------------------------------------------------------------- utils
    def _address(self) -> str:
        return b58encode(self.rng.bytes(32))

    def _signature(self) -> str:
        return b58encode(self.rng.bytes(64))

    def _steps(self, a: datetime, b: datetime) -> int:
        return max(1, int((b - a) / STEP) + 1)

    def _gbm_path(self, p0: float, drift: float, vol: float, n: int) -> np.ndarray:
        shocks = self.rng.normal(drift, vol, n - 1) if n > 1 else np.zeros(0)
        jumps = self.rng.random(n - 1) < 0.002 if n > 1 else np.zeros(0, dtype=bool)
        shocks = shocks + jumps * self.rng.normal(0, vol * 15, n - 1)
        return p0 * np.exp(np.concatenate([[0.0], np.cumsum(shocks)]))

    def _slot(self, t: datetime) -> int:
        return 250_000_000 + int((t - self.start).total_seconds() / 0.4)

    # ---------------------------------------------------------------- tokens
    def _make_tokens(self, n: int) -> None:
        span = (self.t0 - self.start).total_seconds()
        for i in range(n):
            # Most tokens exist before the window; ~40 % launch inside it.
            if self.rng.random() < 0.6:
                created = self.start - timedelta(days=float(self.rng.uniform(1, 200)))
            else:
                created = self.start + timedelta(seconds=float(self.rng.uniform(0, span * 0.97)))
            micro = self.rng.random() < 0.3
            init_price = float(10 ** self.rng.uniform(-6, -1.5))
            supply = 1e9
            base_liq = float(10 ** self.rng.uniform(3.6, 4.3)) if micro else float(10 ** self.rng.uniform(4.6, 6.6))
            rug_at = None
            if self.rng.random() < 0.12:
                life = max(created, self.start) + timedelta(hours=float(self.rng.uniform(2, 24 * 30)))
                rug_at = life if life < self.t0 else None
            mint = self._address()
            if self.rng.random() < 0.5:
                mint = mint[:-4] + "pump"
            risky = self.rng.random()
            tok = SimToken(
                mint=mint,
                symbol=f"SIM{i:03d}",
                created_at=created,
                init_price=init_price,
                supply=supply,
                base_liquidity=base_liq,
                vol=float(self.rng.uniform(0.004, 0.02)),
                drift=float(self.rng.normal(0, 0.0004)),
                rug_at=rug_at,
                category="launchpad:pumpfun" if mint.endswith("pump") else ("cap:micro" if micro else "cap:mid"),
                risk_score=float(self.rng.uniform(55, 95) if rug_at or risky < 0.1 else self.rng.uniform(5, 45)),
                mint_authority=self._address() if risky < 0.06 else None,
                freeze_authority=self._address() if risky < 0.03 else None,
                dangerous=["transferFee"] if 0.03 <= risky < 0.05 else [],
            )
            start = max(created, self.start)
            tok.hist = self._gbm_path(init_price, tok.drift, tok.vol, self._steps(start, self.t0))
            self.tokens[mint] = tok

    def token_price(self, mint: str, t: datetime | None = None) -> float | None:
        tok = self.tokens.get(mint)
        if tok is None:
            return None
        t = t or self.clock.now()
        if t < tok.created_at:
            return None
        base = max(tok.created_at, self.start)
        if t <= self.t0:
            pos = max(0.0, (t - base) / STEP)
            i = min(int(pos), len(tok.hist) - 1)
            j = min(i + 1, len(tok.hist) - 1)
            frac = pos - int(pos)
            price = float(tok.hist[i] * (1 - frac) + tok.hist[j] * frac)
        else:
            price = self._live_price(tok, t)
        if tok.rug_at is not None and t >= tok.rug_at:
            price *= 0.01
        return price

    def _live_price(self, tok: SimToken, t: datetime) -> float:
        if not tok.live_times:
            tok.live_times.append(self.t0)
            tok.live_prices.append(float(tok.hist[-1]))
        while tok.live_times[-1] < t:
            nxt = tok.live_times[-1] + LIVE_STEP
            bias = sum(b for until, b in tok.biases if until >= nxt)
            vol = tok.vol / math.sqrt(60)
            shock = float(self.rng.normal(tok.drift / 60 + bias, vol))
            tok.live_times.append(nxt)
            tok.live_prices.append(tok.live_prices[-1] * math.exp(shock))
            if len(tok.live_times) > 50_000:  # bound memory in long demos
                del tok.live_times[:10_000]
                del tok.live_prices[:10_000]
        idx = max(0, len(tok.live_times) - 1 - int((tok.live_times[-1] - t) / LIVE_STEP))
        return tok.live_prices[idx]

    def liquidity(self, mint: str, t: datetime | None = None) -> float | None:
        tok = self.tokens.get(mint)
        price = self.token_price(mint, t)
        if tok is None or price is None:
            return None
        t = t or self.clock.now()
        if tok.rug_at is not None and t >= tok.rug_at:
            return tok.base_liquidity * 0.002
        age_h = (t - tok.created_at).total_seconds() / 3600
        ramp = min(1.0, 0.15 + age_h / 48)
        return tok.base_liquidity * ramp * min(4.0, max(0.25, math.sqrt(price / tok.init_price)))

    def sol_price(self, t: datetime | None = None) -> float:
        t = t or self.clock.now()
        if t <= self.t0:
            pos = max(0.0, (t - self.start) / STEP)
            return float(self._sol_hist[min(int(pos), len(self._sol_hist) - 1)])
        if not self._sol_live:
            self._sol_live.append((self.t0, float(self._sol_hist[-1])))
        while self._sol_live[-1][0] < t:
            ts, p = self._sol_live[-1]
            self._sol_live.append((ts + STEP, p * math.exp(float(self.rng.normal(0, 0.04 / math.sqrt(288))))))
        return self._sol_live[-1][1]

    # --------------------------------------------------------------- wallets
    def _make_wallets(self, n: int) -> None:
        group = 0
        coordinated_left = 0
        for i in range(n):
            arche = ARCHETYPES[i % len(ARCHETYPES)]
            w = SimWallet(address=self._address(), archetype=arche, label=f"sim-{arche}-{i:02d}")
            if arche == "coordinated":
                if coordinated_left == 0:
                    group += 1
                    coordinated_left = 3
                w.group = group
                coordinated_left -= 1
            self.wallets[w.address] = w

    def wallets_by_archetype(self, archetype: str) -> list[str]:
        return [w.address for w in self.wallets.values() if w.archetype == archetype]

    def _alive_tokens(self, t: datetime, *, low_liq: bool | None = None, young: bool = False) -> list[SimToken]:
        out = []
        for tok in self.tokens.values():
            if tok.created_at > t or (tok.rug_at is not None and tok.rug_at <= t):
                continue
            if young and (t - tok.created_at) > timedelta(hours=2):
                continue
            liq = self.liquidity(tok.mint, t) or 0.0
            if low_liq is True and liq >= 20_000:
                continue
            if low_liq is False and liq < 40_000:
                continue
            out.append(tok)
        return out

    def _swap(
        self,
        wallet: str,
        tok: SimToken,
        side: Side,
        t: datetime,
        usd: float,
        price: float,
        source: TxSource,
        qty: float | None = None,
    ) -> SwapEvent:
        sol = self.sol_price(t)
        qty = qty if qty is not None else usd / price
        key = (wallet, tok.mint)
        before = self._holdings.get(key, 0.0)
        after = before + qty if side is Side.BUY else max(0.0, before - qty)
        self._holdings[key] = after
        return SwapEvent(
            wallet=wallet,
            signature=self._signature(),
            slot=self._slot(t),
            block_time=t,
            token_mint=tok.mint,
            side=side,
            token_amount=qty,
            token_decimals=tok.decimals,
            quote_mint=SOL_MINT,
            quote_amount=usd / sol,
            price_quote=(usd / sol) / qty,
            price_usd=usd / qty,
            value_usd=usd,
            sol_price_usd=sol,
            fee_sol=0.000105,
            dex="pumpswap" if tok.mint.endswith("pump") else "raydium_amm",
            token_balance_before=before,
            token_balance_after=after,
            source=source,
            liquidity_usd=self.liquidity(tok.mint, t),
        )

    def _roundtrip(
        self,
        w: SimWallet,
        t: datetime,
        end: datetime,
        *,
        skill: float,
        hold: timedelta,
        size: float,
        source: TxSource,
        forced: SimToken | None = None,
        low_liq: bool | None = False,
        young: bool = False,
        exit_mult: float | None = None,
    ) -> list[SwapEvent]:
        candidates = [forced] if forced else self._alive_tokens(t, low_liq=low_liq, young=young)
        if not candidates:
            return []
        picks = [candidates[int(self.rng.integers(len(candidates)))] for _ in range(4)]
        tok = picks[0]
        if self.rng.random() < skill and not forced:
            # "Skill": the wallet picks the candidate with the best forward return.
            def fwd(tk: SimToken) -> float:
                a = self.token_price(tk.mint, t)
                b = self.token_price(tk.mint, min(t + hold, self.t0)) if t + hold <= self.t0 else a
                return (b / a) if a and b else 0.0

            tok = max(picks, key=fwd)
        entry_px = self.token_price(tok.mint, t)
        if not entry_px:
            return []
        entry_px *= 1 + float(self.rng.uniform(0.001, 0.01))
        events = [self._swap(w.address, tok, Side.BUY, t, size, entry_px, source)]
        t_exit = t + hold
        if t_exit >= end:
            return events  # still holding at the end of the window
        if exit_mult is not None:
            exit_px = entry_px * exit_mult
        else:
            raw = self.token_price(tok.mint, t_exit) or entry_px * 0.01
            exit_px = raw * (1 - float(self.rng.uniform(0.001, 0.01)))
        qty = events[0].token_amount
        if self.rng.random() < 0.3:  # partial exits
            t_mid = t + hold / 2
            mid_px = (self.token_price(tok.mint, t_mid) or exit_px) * 0.995
            events.append(
                self._swap(w.address, tok, Side.SELL, t_mid, qty * 0.5 * mid_px, mid_px, source, qty=qty * 0.5)
            )
            events.append(
                self._swap(w.address, tok, Side.SELL, t_exit, qty * 0.5 * exit_px, exit_px, source, qty=qty * 0.5)
            )
        else:
            events.append(self._swap(w.address, tok, Side.SELL, t_exit, qty * exit_px, exit_px, source, qty=qty))
        return events

    def _generate_history(self) -> None:
        end = self.t0
        for w in self.wallets.values():
            t = self.start + timedelta(hours=float(self.rng.uniform(0, 12)))
            out: list[SwapEvent] = []
            arche = w.archetype
            stop_at = end - timedelta(days=20) if arche == "inactive" else end
            split = self.start + (end - self.start) * 0.7
            lucky_done = False
            while t < stop_at:
                if arche in ("skilled", "inactive", "coordinated"):
                    rate, skill = 4.0, 0.75
                    hold = timedelta(hours=float(self.rng.uniform(1, 10)))
                    size = float(self.rng.uniform(200, 1500))
                    ev = self._roundtrip(w, t, end, skill=skill, hold=hold, size=size, source=TxSource.BACKFILL)
                elif arche == "scalper":
                    rate = 10.0
                    hold = timedelta(minutes=float(self.rng.uniform(15, 90)))
                    ev = self._roundtrip(
                        w,
                        t,
                        end,
                        skill=0.6,
                        hold=hold,
                        size=float(self.rng.uniform(100, 600)),
                        source=TxSource.BACKFILL,
                    )
                elif arche == "degrading":
                    rate = 4.0
                    skill = 0.8 if t < split else 0.0
                    hold = timedelta(hours=float(self.rng.uniform(1, 10)))
                    mult = None if t < split else float(self.rng.uniform(0.55, 1.02))
                    ev = self._roundtrip(
                        w,
                        t,
                        end,
                        skill=skill,
                        hold=hold,
                        size=float(self.rng.uniform(200, 1500)),
                        source=TxSource.BACKFILL,
                        exit_mult=mult,
                    )
                elif arche == "lucky":
                    rate = 2.0
                    hold = timedelta(hours=float(self.rng.uniform(1, 24)))
                    lucky_mult = float(self.rng.uniform(0.7, 1.08))
                    size = float(self.rng.uniform(100, 400))
                    if not lucky_done and t > self.start + (end - self.start) * 0.4:
                        lucky_mult, lucky_done, size = 40.0, True, 800.0
                    ev = self._roundtrip(
                        w, t, end, skill=0.0, hold=hold, size=size, source=TxSource.BACKFILL, exit_mult=lucky_mult
                    )
                elif arche == "wash":
                    rate = 30.0
                    hold = timedelta(seconds=float(self.rng.uniform(20, 120)))
                    ev = self._roundtrip(
                        w,
                        t,
                        end,
                        skill=0.0,
                        hold=hold,
                        size=float(self.rng.uniform(500, 3000)),
                        source=TxSource.BACKFILL,
                        exit_mult=float(self.rng.uniform(0.997, 1.003)),
                    )
                elif arche == "sniper":
                    break  # snipers act on launches, not on a Poisson clock: see _snipes()
                elif arche == "low_liquidity":
                    rate = 5.0
                    hold = timedelta(hours=float(self.rng.uniform(0.5, 6)))
                    ev = self._roundtrip(
                        w,
                        t,
                        end,
                        skill=0.5,
                        hold=hold,
                        size=float(self.rng.uniform(100, 600)),
                        source=TxSource.BACKFILL,
                        low_liq=True,
                    )
                else:  # random
                    rate = 5.0
                    hold = timedelta(hours=float(self.rng.uniform(0.2, 12)))
                    ev = self._roundtrip(
                        w,
                        t,
                        end,
                        skill=0.0,
                        hold=hold,
                        size=float(self.rng.uniform(100, 1000)),
                        source=TxSource.BACKFILL,
                    )
                out.extend(ev)
                t += timedelta(days=float(self.rng.exponential(1.0 / rate)))
            if arche == "sniper":
                out = self._snipes(w, end)
            self.history[w.address] = out
        self._coordinate_groups()
        for swaps in self.history.values():
            swaps.sort(key=lambda s: (s.block_time, s.slot))

    def _snipes(self, w: SimWallet, end: datetime) -> list[SwapEvent]:
        out: list[SwapEvent] = []
        launches = sorted(
            (t for t in self.tokens.values() if self.start <= t.created_at < end), key=lambda t: t.created_at
        )
        for tok in launches:
            for _ in range(2):
                if self.rng.random() > 0.8:
                    continue
                t_entry = tok.created_at + timedelta(seconds=float(self.rng.uniform(1, 15)))
                mult = float(self.rng.uniform(1.2, 3.0)) if self.rng.random() < 0.8 else 0.5
                out.extend(
                    self._roundtrip(
                        w,
                        t_entry,
                        end,
                        skill=0.0,
                        hold=timedelta(seconds=float(self.rng.uniform(15, 55))),
                        size=float(self.rng.uniform(200, 800)),
                        source=TxSource.BACKFILL,
                        forced=tok,
                        exit_mult=mult,
                    )
                )
        return out

    def _coordinate_groups(self) -> None:
        """Followers in a coordinated group mirror the leader within seconds."""
        groups: dict[int, list[SimWallet]] = {}
        for w in self.wallets.values():
            if w.group is not None:
                groups.setdefault(w.group, []).append(w)
        for members in groups.values():
            leader, followers = members[0], members[1:]
            for f in followers:
                for key in [k for k in self._holdings if k[0] == f.address]:
                    del self._holdings[key]
                copied: list[SwapEvent] = []
                for ev in self.history[leader.address]:
                    delay = timedelta(seconds=float(self.rng.uniform(1, 8)))
                    tok = self.tokens[ev.token_mint]
                    t = ev.block_time + delay
                    px = ev.price_usd * (1.003 if ev.side is Side.BUY else 0.997) if ev.price_usd else 0.0
                    if px <= 0:
                        continue
                    copied.append(
                        self._swap(
                            f.address,
                            tok,
                            ev.side,
                            t,
                            (ev.value_usd or 0) * 0.8,
                            px,
                            TxSource.BACKFILL,
                            qty=ev.token_amount * 0.8,
                        )
                    )
                self.history[f.address] = copied

    # ------------------------------------------------------------- realtime
    def schedule_realtime(self, wallets: Sequence[str], now: datetime) -> list[SwapEvent]:
        """Generate new live round trips starting ``now`` (holds of minutes, for demos)."""
        out: list[SwapEvent] = []
        eligible = [self.wallets[w] for w in wallets if w in self.wallets and self.wallets[w].archetype != "inactive"]
        if not eligible:
            return out
        w = eligible[int(self.rng.integers(len(eligible)))]
        hold = timedelta(minutes=float(self.rng.uniform(2, 15)) / self.speedup)
        size = float(self.rng.uniform(150, 1200))
        cands = self._alive_tokens(now, low_liq=w.archetype == "low_liquidity")
        if not cands:
            return out
        tok = cands[int(self.rng.integers(len(cands)))]
        skilled = w.archetype in ("skilled", "scalper", "coordinated") or (
            w.archetype == "degrading" and self.rng.random() < 0.2
        )
        if skilled and self.rng.random() < 0.7:
            # the skilled wallet "knows": inject a positive drift for the hold period
            tok.biases.append((now + hold, float(self.rng.uniform(0.0015, 0.004))))
        elif w.archetype in ("random", "degrading", "lucky"):
            tok.biases.append((now + hold, float(self.rng.uniform(-0.003, 0.001))))
        price = self.token_price(tok.mint, now)
        if not price:
            return out
        buy = self._swap(w.address, tok, Side.BUY, now, size, price * 1.004, TxSource.SIMULATED)
        out.append(buy)
        exit_t = now + hold
        # SELL is materialised later (price at exit time unknown yet): marker event.
        out.append(
            SwapEvent(
                wallet=w.address,
                signature="pending",
                slot=0,
                block_time=exit_t,
                token_mint=tok.mint,
                side=Side.SELL,
                token_amount=buy.token_amount,
                token_decimals=tok.decimals,
                quote_mint=SOL_MINT,
                quote_amount=0.0,
                price_quote=0.0,
                price_usd=None,
                value_usd=None,
                source=TxSource.SIMULATED,
            )
        )
        return out

    def materialize_sell(self, marker: SwapEvent) -> SwapEvent | None:
        tok = self.tokens[marker.token_mint]
        price = self.token_price(tok.mint, marker.block_time)
        if not price:
            return None
        px = price * 0.996
        return self._swap(
            marker.wallet,
            tok,
            Side.SELL,
            marker.block_time,
            marker.token_amount * px,
            px,
            TxSource.SIMULATED,
            qty=marker.token_amount,
        )

    # --------------------------------------------------------------- quotes
    def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> tuple[int, float]:
        """Constant-product quote. Returns (out_raw, price_impact_frac)."""
        now = self.clock.now()
        sol = self.sol_price(now)
        if input_mint == SOL_MINT:
            tok = self.tokens.get(output_mint)
            price, liq = self.token_price(output_mint, now), self.liquidity(output_mint, now)
            if tok is None or not price or not liq:
                raise ProviderError("sim: no route", provider="sim_quotes", retryable=False)
            usd_in = amount_raw / 1e9 * sol
            res_usd = liq / 2
            eff_usd = usd_in * (1 - AMM_FEE)
            tokens_out = (res_usd / price) * eff_usd / (res_usd + eff_usd)
            impact = 1 - (tokens_out * price) / usd_in
            return int(tokens_out * 10**tok.decimals), max(0.0, impact)
        tok = self.tokens.get(input_mint)
        price, liq = self.token_price(input_mint, now), self.liquidity(input_mint, now)
        if tok is None or not price or not liq:
            raise ProviderError("sim: no route", provider="sim_quotes", retryable=False)
        qty = amount_raw / 10**tok.decimals
        res_tok = (liq / 2) / price
        eff = qty * (1 - AMM_FEE)
        usd_out = (liq / 2) * eff / (res_tok + eff)
        impact = 1 - usd_out / (qty * price) if qty > 0 else 0.0
        return int(usd_out / sol * 1e9), max(0.0, impact)


# ============================================================ protocol adapters
class SimulatedHistorySource:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def fetch_swaps(
        self,
        wallet: str,
        *,
        since: datetime | None = None,
        until_signature: str | None = None,
        max_signatures: int = 1000,
        source: TxSource = TxSource.BACKFILL,
    ) -> list[SwapEvent]:
        swaps = self.m.history.get(wallet, [])
        if since is not None:
            swaps = [s for s in swaps if s.block_time >= since]
        if until_signature:
            sigs = [s.signature for s in swaps]
            if until_signature in sigs:
                swaps = swaps[sigs.index(until_signature) + 1 :]
        return swaps[-max_signatures:]


class SimulatedFeed:
    """Emits live synthetic swaps for tracked simulated wallets."""

    def __init__(self, market: SimulatedMarket, *, detection_latency: tuple[float, float] = (0.3, 1.5)) -> None:
        self.m = market
        self._wallets: set[str] = set()
        self._stopped = asyncio.Event()
        self._latency = detection_latency
        self._pending: list[tuple[datetime, int, SwapEvent]] = []
        self._seq = 0

    def set_wallets(self, wallets: set[str]) -> None:
        self._wallets = set(wallets)

    async def stop(self) -> None:
        self._stopped.set()

    async def run(self, on_swap: SwapHandler) -> None:
        rate = self.m.realtime_rate
        while not self._stopped.is_set():
            now = self.m.clock.now()
            if rate > 0 and self._wallets and self.m.rng.random() < rate / 60.0:
                for ev in self.m.schedule_realtime(sorted(self._wallets), now):
                    self._seq += 1
                    heapq.heappush(self._pending, (ev.block_time, self._seq, ev))
            while self._pending and self._pending[0][0] <= now:
                _, seq, ev = heapq.heappop(self._pending)
                if ev.signature == "pending":  # sell whose price is only known at exit time
                    real = self.m.materialize_sell(ev)
                    if real is None:
                        continue
                    ev = real
                if ev.detected_at is None:
                    # emulate network/indexing latency: deliver it once "detected"
                    lat = float(self.m.rng.uniform(*self._latency))
                    ev = _with(ev, detected_at=ev.block_time + timedelta(seconds=lat))
                    heapq.heappush(self._pending, (ev.detected_at or ev.block_time, seq, ev))
                    continue
                if ev.wallet not in self._wallets:
                    continue
                self.m.history.setdefault(ev.wallet, []).append(ev)
                await on_swap(ev)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=0.25)


def _with(ev: SwapEvent, **changes: Any) -> SwapEvent:
    from dataclasses import replace

    return replace(ev, **changes)


class SimulatedMarketData:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def token_market(self, mints: Sequence[str]) -> dict[str, MarketData]:
        now = self.m.clock.now()
        out: dict[str, MarketData] = {}
        for mint in mints:
            tok = self.m.tokens.get(mint)
            price = self.m.token_price(mint, now)
            if tok is None or price is None:
                continue
            changes: dict[str, float] = {}
            for key, delta in (
                ("m5", timedelta(minutes=5)),
                ("h1", timedelta(hours=1)),
                ("h6", timedelta(hours=6)),
                ("h24", timedelta(hours=24)),
            ):
                past = self.m.token_price(mint, now - delta)
                if past:
                    changes[key] = (price / past - 1) * 100
            out[mint] = MarketData(
                mint=mint,
                symbol=tok.symbol,
                name=f"Simulated {tok.symbol}",
                price_usd=price,
                liquidity_usd=self.m.liquidity(mint, now),
                market_cap_usd=price * tok.supply,
                fdv_usd=price * tok.supply,
                volume_24h_usd=(self.m.liquidity(mint, now) or 0) * 3,
                pair_created_at=tok.created_at,
                price_change_pct=changes,
                dex="pumpswap" if mint.endswith("pump") else "raydium",
                pair_address=None,
            )
        return out


class SimulatedTokenRisk:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def token_risk(self, mint: str) -> RiskData | None:
        tok = self.m.tokens.get(mint)
        if tok is None:
            return None
        rugged = tok.rug_at is not None and tok.rug_at <= self.m.clock.now()
        score = 100.0 if rugged else tok.risk_score
        level = "critical" if rugged else ("high" if score >= 60 else "medium" if score >= 30 else "low")
        return RiskData(
            mint=mint, score=score, level=level, flags=["danger:Rugged"] if rugged else [], is_rugged=rugged
        )


class SimulatedMintInfo:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def mint_info(self, mint: str) -> MintData | None:
        tok = self.m.tokens.get(mint)
        if tok is None:
            return None
        return MintData(
            mint=mint,
            decimals=tok.decimals,
            token_program=TOKEN_2022_PROGRAM if tok.dangerous else TOKEN_PROGRAM,
            mint_authority=tok.mint_authority,
            freeze_authority=tok.freeze_authority,
            dangerous_extensions=list(tok.dangerous),
        )


class SimulatedPrices:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def prices_usd(self, mints: Sequence[str]) -> dict[str, float]:
        now = self.m.clock.now()
        out: dict[str, float] = {}
        for mint in mints:
            if mint == SOL_MINT:
                out[mint] = self.m.sol_price(now)
            else:
                p = self.m.token_price(mint, now)
                if p:
                    out[mint] = p
        return out


class SimulatedSolHistory:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def sol_price_at(self, ts: datetime) -> float | None:
        return self.m.sol_price(ts)

    async def sol_series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        out = []
        t = max(start, self.m.start)
        while t <= end:
            out.append((t, self.m.sol_price(t)))
            t += timedelta(hours=1)
        return out


class SimulatedQuotes:
    def __init__(self, market: SimulatedMarket) -> None:
        self.m = market

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> Quote:
        out_raw, impact = self.m.quote(input_mint, output_mint, amount_raw)
        if out_raw <= 0:
            raise ProviderError("sim: zero output", provider="sim_quotes", retryable=False)
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=amount_raw,
            out_amount_raw=out_raw,
            min_out_amount_raw=int(out_raw * (1 - slippage_bps / 10_000)),
            slippage_bps=slippage_bps,
            price_impact_frac=impact,
            obtained_at=self.m.clock.now(),
            route_label="SimAMM",
            raw={"sim": True},
        )
