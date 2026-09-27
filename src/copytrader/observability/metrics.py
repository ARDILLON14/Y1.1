"""Prometheus metrics. Names are prefixed ``ct_``.

Latency histograms cover the whole chain the spec cares about:
wallet tx in block → detection → decision → execution.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry(auto_describe=True)

_LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 13, 20, 30, 60, 120)

PROVIDER_REQUESTS = Counter(
    "ct_provider_requests_total", "External requests", ["provider", "outcome"], registry=REGISTRY
)
PROVIDER_LATENCY = Histogram(
    "ct_provider_latency_seconds", "External request latency", ["provider"],
    buckets=_LAT_BUCKETS, registry=REGISTRY,
)
CIRCUIT_STATE = Gauge(
    "ct_circuit_state", "0=closed 1=half_open 2=open", ["breaker"], registry=REGISTRY
)
WS_CONNECTED = Gauge("ct_ws_connected", "WebSocket connected (1/0)", ["stream"], registry=REGISTRY)
WS_RECONNECTS = Counter("ct_ws_reconnects_total", "WebSocket reconnects", ["stream"], registry=REGISTRY)
SWAPS_DETECTED = Counter("ct_swaps_detected_total", "Parsed wallet swaps", ["source"], registry=REGISTRY)
DETECTION_LATENCY = Histogram(
    "ct_detection_latency_seconds", "Block time -> detection", buckets=_LAT_BUCKETS, registry=REGISTRY
)
PIPELINE_LATENCY = Histogram(
    "ct_pipeline_latency_seconds", "Detection -> decision", buckets=_LAT_BUCKETS, registry=REGISTRY
)
EXECUTION_LATENCY = Histogram(
    "ct_execution_latency_seconds", "Decision -> fill", ["mode"], buckets=_LAT_BUCKETS,
    registry=REGISTRY,
)
END_TO_END_LATENCY = Histogram(
    "ct_end_to_end_latency_seconds", "Source block time -> our fill", ["mode"],
    buckets=_LAT_BUCKETS, registry=REGISTRY,
)
SIGNALS = Counter("ct_signals_total", "Signals by action/status", ["action", "status"], registry=REGISTRY)
DECISIONS = Counter("ct_decisions_total", "Copy decisions", ["result", "reason"], registry=REGISTRY)
ORDERS = Counter("ct_orders_total", "Orders", ["mode", "purpose", "status"], registry=REGISTRY)
ERRORS = Counter("ct_errors_total", "Errors by component", ["component"], registry=REGISTRY)
OPEN_POSITIONS = Gauge("ct_open_positions", "Open positions", ["mode"], registry=REGISTRY)
EQUITY = Gauge("ct_equity_usd", "Equity", ["mode"], registry=REGISTRY)
EXPOSURE = Gauge("ct_exposure_usd", "Open exposure", ["mode"], registry=REGISTRY)
DAILY_PNL = Gauge("ct_daily_pnl_usd", "PnL since start of day", ["mode"], registry=REGISTRY)
RISK_UTILIZATION = Gauge(
    "ct_risk_utilization_ratio", "Used / limit", ["limit"], registry=REGISTRY
)
KILL_SWITCH = Gauge("ct_kill_switch_active", "Kill switch (1/0)", ["scope"], registry=REGISTRY)
WALLETS = Gauge("ct_wallets", "Wallets by status", ["status"], registry=REGISTRY)
SELECTED_WALLETS = Gauge("ct_selected_wallets", "Wallets selected for copy", registry=REGISTRY)
NOTIFICATIONS = Counter(
    "ct_notifications_total", "Notifications", ["channel", "outcome"], registry=REGISTRY
)
QUEUE_DEPTH = Gauge("ct_signal_queue_depth", "Pending swap events", registry=REGISTRY)

_CIRCUIT_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def set_circuit_state(name: str, state: str) -> None:
    CIRCUIT_STATE.labels(breaker=name).set(_CIRCUIT_VALUES.get(state, -1))


def start_metrics_server(host: str, port: int) -> None:
    start_http_server(port, addr=host, registry=REGISTRY)
