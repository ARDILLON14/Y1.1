"""Shared enumerations used across every layer.

Enum values are lowercase strings so they serialise cleanly to JSON/DB and are
stable across versions. Human-facing (Spanish) labels live in ``LABELS_ES``.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class WalletStatus(StrEnum):
    ACTIVE = "active"
    OBSERVE = "observe"
    BLOCKED = "blocked"


class ListType(StrEnum):
    NONE = "none"
    WHITELIST = "whitelist"
    WATCHLIST = "watchlist"
    BLACKLIST = "blacklist"


class OperatingLevel(IntEnum):
    """Gradual rollout levels (section 13 of the spec)."""

    ANALYSIS = 1  # analyse wallets only
    ALERTS = 2  # detect trades and alert
    PAPER = 3  # simulate trades automatically
    LIVE_SMALL = 4  # real trades with extremely strict caps
    LIVE = 5  # normal real trading

    @property
    def executes(self) -> bool:
        return self >= OperatingLevel.PAPER

    @property
    def is_live(self) -> bool:
        return self >= OperatingLevel.LIVE_SMALL


class ExitMode(StrEnum):
    MIRROR = "mirror"  # follow source entries and exits
    PROTECTED = "protected"  # follow source + own SL/TP
    SMART = "smart"  # source is only an entry signal; risk engine manages exit


class TradeMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SignalAction(StrEnum):
    COPY = "copy"  # candidate for copy trading
    EXIT = "exit"  # source sold: manage our copied position
    ALERT = "alert"  # notify only
    IGNORE = "ignore"  # record for analytics only


class SignalStatus(StrEnum):
    DETECTED = "detected"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"
    ALERTED = "alerted"
    IGNORED = "ignored"


class OrderStatus(StrEnum):
    CREATED = "created"
    QUOTED = "quoted"
    SIGNED = "signed"  # signature known and persisted, maybe not sent yet
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"  # blockhash expired without landing -> can never land
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.CONFIRMED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
        }

    @property
    def in_flight(self) -> bool:
        return self in {OrderStatus.SIGNED, OrderStatus.SUBMITTED}


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class AlertType(StrEnum):
    SIGNAL_DETECTED = "signal_detected"
    TRADE_COPIED = "trade_copied"
    TRADE_REJECTED = "trade_rejected"
    POSITION_CLOSED = "position_closed"
    WALLET_DEGRADED = "wallet_degraded"
    WALLET_STATUS = "wallet_status"
    RISK_EXCEEDED = "risk_exceeded"
    EXECUTION_ERROR = "execution_error"
    API_DISCONNECTED = "api_disconnected"
    RPC_PROBLEM = "rpc_problem"
    KILL_SWITCH = "kill_switch"
    SYSTEM = "system"


class KillSwitchScope(StrEnum):
    GLOBAL = "global"
    DAILY = "daily"


class TxSource(StrEnum):
    STREAM = "stream"
    BACKFILL = "backfill"
    CATCHUP = "catchup"
    SIMULATED = "simulated"


LABELS_ES: dict[str, str] = {
    "active": "ACTIVA",
    "observe": "OBSERVAR",
    "blocked": "BLOQUEADA",
    "buy": "COMPRA",
    "sell": "VENTA",
    "whitelist": "Whitelist",
    "watchlist": "Watchlist",
    "blacklist": "Blacklist",
    "none": "—",
    "mirror": "Espejo",
    "protected": "Protegido",
    "smart": "Inteligente",
}

LEVEL_NAMES_ES: dict[int, str] = {
    1: "Solo análisis",
    2: "Alertas",
    3: "Paper trading",
    4: "Ejecución con capital pequeño",
    5: "Ejecución normal",
}
