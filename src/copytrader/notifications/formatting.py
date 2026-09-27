"""Human-readable (Spanish) message templates for alerts and notifications."""

from __future__ import annotations

from typing import Any

from copytrader.core.events import (
    ExecutionFailed,
    KillSwitchChanged,
    PositionClosed,
    ProviderStatusChanged,
    RiskLimitHit,
    SignalAlert,
    SignalDecided,
    SystemMessage,
    WalletStatusChanged,
)
from copytrader.core.types import LABELS_ES


def short(addr: str | None, n: int = 4) -> str:
    if not addr:
        return "?"
    return addr if len(addr) <= 2 * n + 1 else f"{addr[:n]}…{addr[-n:]}"


def usd(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if abs(value) < 0.01 and value != 0:
        return f"${value:.8g}"
    return f"${value:,.{digits}f}"


def _token(symbol: str | None, mint: str) -> str:
    return f"{symbol} ({short(mint)})" if symbol else short(mint, 6)


def _wallet(label: str | None, addr: str) -> str:
    return f"{label} ({short(addr)})" if label else short(addr, 6)


_CHECK_LABELS_SHORT = {
    "liquidity": "Liquidity", "slippage": "Slippage", "total_exposure": "Exposure", "token_risk": "Token Risk",
    "price_deviation": "Price deviation", "signal_age": "Delay", "min_score": "Wallet score",
}


def format_signal_decided(ev: SignalDecided) -> tuple[str, str]:
    lines = [
        f"Wallet: {_wallet(ev.wallet_label, ev.wallet)}",
        f"Token: {_token(ev.token_symbol, ev.token_mint)}",
        f"Tipo: {ev.side.upper()}",
        f"Precio: {usd(ev.source_price_usd, 8)}",
        f"Liquidity: {usd(ev.liquidity_usd, 0)}",
        f"Wallet Score: {ev.wallet_score or 0:.0f}/100",
        "",
        "Risk Check:",
    ]
    for c in ev.checks:
        if c["name"] in _CHECK_LABELS_SHORT or not c["passed"]:
            mark = "✅" if c["passed"] else "❌"
            name = _CHECK_LABELS_SHORT.get(c["name"], c["label"])
            msg = f" — {c['message']}" if not c["passed"] and c.get("message") else ""
            lines.append(f"{mark} {name}{msg}")
    lines.append("")
    if ev.approved:
        lines += ["Resultado:", f"✅ COPIADA ({(ev.mode or '').upper()})", "", "Mi posición:", usd(ev.size_usd)]
        title = "🚨 NUEVA OPERACIÓN COPIADA"
    else:
        lines += ["Resultado:", "❌ RECHAZADA", "", f"Motivo: {ev.reason or '—'}"]
        title = "🚨 OPERACIÓN RECHAZADA"
    return title, "\n".join(lines)


def format_signal_alert(ev: SignalAlert) -> tuple[str, str]:
    body = "\n".join([
        f"Wallet: {_wallet(ev.wallet_label, ev.wallet)}",
        f"Token: {_token(ev.token_symbol, ev.token_mint)}",
        f"Tipo: {ev.side.upper()}",
        f"Precio: {usd(ev.price_usd, 8)}",
        f"Valor: {usd(ev.value_usd)}",
        f"Wallet Score: {ev.wallet_score or 0:.0f}/100",
        f"Origen: {ev.reason}",
    ])
    return "🚨 NUEVA OPERACIÓN DETECTADA", body


def format_position_closed(ev: PositionClosed) -> tuple[str, str]:
    icon = "🟢" if ev.realized_pnl_usd >= 0 else "🔴"
    ret = f" ({ev.return_pct:+.1f}%)" if ev.return_pct is not None else ""
    body = "\n".join([f"Token: {_token(ev.token_symbol, ev.token_mint)}", f"Modo: {ev.mode.upper()}",
                      f"Motivo: {ev.reason}", f"PnL: {usd(ev.realized_pnl_usd)}{ret}"])
    return f"{icon} POSICIÓN CERRADA", body


def format_execution_failed(ev: ExecutionFailed) -> tuple[str, str]:
    body = "\n".join([f"Orden: {ev.client_order_id}", f"Modo: {ev.mode.upper()} · {ev.purpose}",
                      f"Token: {short(ev.token_mint, 6)}", f"Error: {ev.error}"])
    return "⚠️ ERROR DE EJECUCIÓN", body


def format_wallet_status(ev: WalletStatusChanged) -> tuple[str, str]:
    old = LABELS_ES.get(ev.old_status or "", ev.old_status or "—")
    new = LABELS_ES.get(ev.new_status, ev.new_status)
    body = "\n".join([f"Wallet: {_wallet(ev.wallet_label, ev.wallet)}", f"Estado: {old} → {new}", "",
                      "Motivos:", *[f"• {r}" for r in ev.reasons[:6]]])
    return ("📉 WALLET DEGRADADA" if ev.degraded else "ℹ️ CAMBIO DE ESTADO DE WALLET"), body


def format_kill_switch(ev: KillSwitchChanged) -> tuple[str, str]:
    if ev.active:
        return (f"🛑 KILL SWITCH {ev.scope.value.upper()} ACTIVADO",
                f"Motivo: {ev.reason}\nPor: {ev.actor}\nNo se abrirán nuevas posiciones.")
    return f"✅ Kill switch {ev.scope.value.upper()} desactivado", f"Por: {ev.actor}"


def format_risk(ev: RiskLimitHit) -> tuple[str, str]:
    return "⛔ LÍMITE DE RIESGO SUPERADO", ev.message


def format_provider(ev: ProviderStatusChanged) -> tuple[str, str]:
    if ev.healthy:
        return f"✅ {ev.provider} recuperado", ev.detail or "Conexión restablecida"
    title = "🔌 PROBLEMAS DE RPC" if ev.kind == "rpc" else "🔌 API DESCONECTADA"
    return title, f"{ev.provider} ({ev.kind}): {ev.detail or 'sin detalle'}"


def format_system(ev: SystemMessage) -> tuple[str, str]:
    return ev.title, ev.body


def as_dict(ev: Any) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(ev)
    data.pop("checks", None)
    data.pop("explanation", None)
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()}
