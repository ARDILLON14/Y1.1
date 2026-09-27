"""Password hashing (scrypt), TOTP (RFC 6238) and field encryption helpers.

Only stdlib + ``cryptography`` are used to keep the dependency surface small.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

from cryptography.fernet import Fernet, InvalidToken

from copytrader.core.errors import SecurityError

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
MIN_PASSWORD_LEN = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise SecurityError(f"la contraseña debe tener al menos {MIN_PASSWORD_LEN} caracteres")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=32, maxmem=128 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                                p=int(p), dklen=32, maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded.upper())
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, at: float | None = None, window: int = 1) -> bool:
    now = at if at is not None else time.time()
    code = code.strip().replace(" ", "")
    return any(hmac.compare_digest(totp_code(secret_b32, now + i * 30), code)
               for i in range(-window, window + 1))


def totp_uri(secret_b32: str, account: str, issuer: str = "copytrader") -> str:
    return f"otpauth://totp/{issuer}:{account}?secret={secret_b32}&issuer={issuer}"


# ------------------------------------------------------------ field encryption
class FieldCipher:
    """Encrypts small sensitive DB fields (e.g. the TOTP secret) with Fernet."""

    def __init__(self, key: str | None) -> None:
        self._fernet = Fernet(key.encode()) if key else None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if self._fernet is None:
            raise SecurityError("DATA_ENCRYPTION_KEY is not configured")
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        if self._fernet is None:
            raise SecurityError("DATA_ENCRYPTION_KEY is not configured")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecurityError("cannot decrypt field (wrong DATA_ENCRYPTION_KEY?)") from exc
