"""Secrets are loaded separately from the tunable configuration.

Sources, in priority order:
1. Docker/K8s secret files in ``/run/secrets`` (or ``COPYTRADER_SECRETS_DIR``)
   named after the field (``telegram_bot_token``...).
2. ``<NAME>_FILE`` environment variables pointing to a file.
3. Plain environment variables (``TELEGRAM_BOT_TOKEN``...) / ``.env``.

Every value is a ``SecretStr``: printing, logging or serialising the model
never reveals it. Secrets are never stored in the database nor exposed by the API.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secrets_dir() -> str | None:
    candidate = os.environ.get("COPYTRADER_SECRETS_DIR", "/run/secrets")
    return candidate if Path(candidate).is_dir() else None


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("COPYTRADER_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        secrets_dir=_secrets_dir(),
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./data/copytrader.db")
    solana_rpc_url: SecretStr | None = None
    solana_ws_url: SecretStr | None = None
    helius_api_key: SecretStr | None = None
    jupiter_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    signer_hmac_key: SecretStr | None = None
    signer_hmac_key_previous: SecretStr | None = None
    keystore_passphrase: SecretStr | None = None
    data_encryption_key: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _read_file_variants(cls, values: dict[str, object]) -> dict[str, object]:
        """Support ``FOO_FILE=/path`` for every field (Docker secrets convention)."""
        values = dict(values or {})
        for name in cls.model_fields:
            path = os.environ.get(f"{name.upper()}_FILE")
            if path and not values.get(name):
                content = Path(path).read_text(encoding="utf-8").strip()
                if content:
                    values[name] = content
        return values

    def rpc_http_url(self, default: str) -> str:
        return self.solana_rpc_url.get_secret_value() if self.solana_rpc_url else default

    def rpc_ws_url(self, default: str) -> str:
        return self.solana_ws_url.get_secret_value() if self.solana_ws_url else default

    def all_secret_values(self) -> list[str]:
        """Every configured secret value (for the log/notification redactor)."""
        out: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw and name != "database_url":
                    out.append(raw)
                elif raw and "@" in raw:
                    # only the credential part of a DB URL is secret
                    creds = raw.split("://", 1)[-1].split("@", 1)[0]
                    if ":" in creds:
                        out.append(creds.split(":", 1)[1])
        return [s for s in out if len(s) >= 6]
