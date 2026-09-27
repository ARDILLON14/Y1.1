"""Preflight checks required before arming real-money trading (levels 4-5)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from copytrader.container import Container
from copytrader.core.models import CheckResult
from copytrader.core.types import TradeMode
from copytrader.db.models import Position


async def run_preflight(c: Container) -> list[CheckResult]:
    cfg = c.cfg
    checks: list[CheckResult] = []

    def add(name: str, label: str, ok: bool, message: str = "", critical: bool = True) -> None:
        checks.append(CheckResult(name, label, ok, message=message, critical=critical))

    add("providers_live", "Proveedores reales (no simulados)", cfg.providers.mode == "live",
        f"providers.mode = {cfg.providers.mode}")
    add("live_enabled", "levels.live_trading_enabled activo", cfg.levels.live_trading_enabled)
    add("level", "Nivel máximo configurado ≥ 4", cfg.app.operating_level >= 4, f"{int(cfg.app.operating_level)}")
    try:
        await c.db.ping()
        add("database", "Base de datos accesible", True)
    except Exception as exc:
        add("database", "Base de datos accesible", False, type(exc).__name__)
    add("kill_switch", "Kill switches inactivos", c.kill.blocking_reason() is None, c.kill.blocking_reason() or "")

    live = c.providers.live_executor
    add("live_executor", "Ejecutor real configurado (signer + wallet)", live is not None,
        "configura security.signer_mode y execution.wallet_public_key")
    rpc = c.providers.rpc
    if rpc is not None:
        healthy = await rpc.get_health()
        add("rpc", "RPC de Solana saludable", healthy)
    signer = c.providers.signer
    if signer is not None and live is not None:
        try:
            pubkey = await signer.public_key()
            add("signer", "Firmador accesible y clave correcta", pubkey == cfg.execution.wallet_public_key, pubkey)
        except Exception as exc:
            add("signer", "Firmador accesible y clave correcta", False, str(exc)[:200])
        try:
            balance = await live.chain.get_balance(live.wallet) / 1e9
            sol = await c.tokens.sol_price() or 0.0
            needed = cfg.risk.reserve_sol + cfg.risk.min_trade_usd / sol if sol else float("inf")
            add("balance", "Saldo suficiente en la wallet del bot", balance >= needed,
                f"{balance:.4f} SOL (mínimo {needed:.4f})")
            add("balance_cap", "Saldo de la wallet no excesivo (capital mínimo necesario)",
                balance * sol <= cfg.risk.capital_usd * 1.5,
                f"{balance * sol:,.0f} USD en la wallet vs capital configurado {cfg.risk.capital_usd:,.0f}",
                critical=False)
        except Exception as exc:
            add("balance", "Saldo suficiente en la wallet del bot", False, str(exc)[:200])
    feed_ok = any(h["kind"] == "websocket" and h["status"] == "ok" for h in c.health.snapshot())
    add("stream", "Stream en tiempo real conectado", feed_ok, critical=False)
    add("notifications", "Canal de notificaciones configurado", c.notifier.enabled, critical=False)

    async with c.db.session() as s:
        first = (await s.execute(select(func.min(Position.opened_at)).where(
            Position.mode == TradeMode.PAPER.value))).scalar_one_or_none()
        n_paper = (await s.execute(select(func.count()).select_from(Position).where(
            Position.mode == TradeMode.PAPER.value))).scalar_one()
    days = (c.clock.now() - first).total_seconds() / 86400 if first else 0.0
    need = cfg.levels.preflight_min_paper_days
    add("paper_history", f"≥ {need} días de paper trading", days >= need or need == 0,
        f"{days:.1f} días, {n_paper} posiciones paper", critical=False)
    add("capital", "Límites de nivel 4 más estrictos que la config",
        cfg.levels.level4.max_trade_usd <= cfg.risk.max_trade_usd, critical=False)
    _ = timedelta
    return checks


def preflight_passed(checks: list[CheckResult]) -> bool:
    return all(c.passed for c in checks if c.critical)
