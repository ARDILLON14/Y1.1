import asyncio

from copytrader.config.models import AppConfig
from copytrader.core.events import SignalDecided
from copytrader.core.types import Severity
from copytrader.notifications.channels import Notification, NotificationService
from copytrader.notifications.formatting import format_signal_decided
from copytrader.security.redaction import REDACTOR


class MemoryChannel:
    name = "memory"

    def __init__(self):
        self.sent: list[Notification] = []

    async def send(self, n: Notification) -> None:
        self.sent.append(n)


async def test_notifications_are_redacted_deduped_and_filtered():
    cfg = AppConfig.model_validate({"notifications": {"min_severity": "info", "dedupe_window_seconds": 60}})
    ch = MemoryChannel()
    svc = NotificationService([ch], lambda: cfg)
    svc.start()
    REDACTOR.register(["my-super-secret-token-123"])
    try:
        assert svc.submit(Notification("t", "key my-super-secret-token-123 leaked", Severity.INFO, "k1"))
        assert not svc.submit(Notification("t", "dup", Severity.INFO, "k1"))  # deduped
        await asyncio.sleep(0.05)
        await svc.stop()
        assert len(ch.sent) == 1
        assert "my-super-secret-token-123" not in ch.sent[0].body
    finally:
        REDACTOR.clear()


async def test_min_severity_filter():
    cfg = AppConfig.model_validate({"notifications": {"min_severity": "critical"}})
    svc = NotificationService([MemoryChannel()], lambda: cfg)
    assert not svc.submit(Notification("t", "b", Severity.WARNING))
    assert svc.submit(Notification("t", "b", Severity.CRITICAL))


def test_signal_notification_format_matches_spec():
    ev = SignalDecided(
        signal_id=1,
        wallet="7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
        wallet_label="Trader A",
        wallet_score=87,
        token_mint="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        token_symbol="TKN",
        side="buy",
        action="copy",
        approved=True,
        reason=None,
        explanation="",
        source_price_usd=0.012,
        liquidity_usd=250000,
        size_usd=42.5,
        mode="paper",
        checks=[
            {"name": "liquidity", "label": "Liquidez", "passed": True},
            {"name": "slippage", "label": "Slippage", "passed": True},
            {"name": "total_exposure", "label": "Exposición", "passed": True},
            {"name": "token_risk", "label": "Riesgo", "passed": True},
        ],
    )
    title, body = format_signal_decided(ev)
    assert "COPIADA" in title
    for fragment in (
        "Wallet: Trader A",
        "Token: TKN",
        "Tipo: BUY",
        "Wallet Score: 87/100",
        "✅ Liquidity",
        "✅ Slippage",
        "✅ Exposure",
        "✅ Token Risk",
        "Mi posición:",
        "$42.50",
    ):
        assert fragment in body
