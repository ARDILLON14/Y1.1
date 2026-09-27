"""Encrypted keystore for the bot's Solana keypair.

Format (JSON, version 1)::

    {
      "version": 1,
      "public_key": "<base58>",
      "kdf": "scrypt", "kdf_params": {"n": 2**17, "r": 8, "p": 1, "salt": "<b64>"},
      "cipher": "aes-256-gcm", "nonce": "<b64>", "ciphertext": "<b64>"
    }

* scrypt (memory-hard) derives a 256-bit key from the passphrase.
* AES-256-GCM authenticates the ciphertext; the public key is bound as
  associated data, so swapping files between wallets is detected.
* The plaintext secret only exists in memory of the signer process.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from copytrader.core.errors import SecurityError

KEYSTORE_VERSION = 1
DEFAULT_SCRYPT_N = 2**17
MIN_PASSPHRASE_LEN = 12


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode())


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class KeystoreFile:
    public_key: str
    data: dict[str, Any]


def encrypt_secret(secret: bytes, public_key: str, passphrase: str, scrypt_n: int = DEFAULT_SCRYPT_N) -> dict[str, Any]:
    if len(passphrase) < MIN_PASSPHRASE_LEN:
        raise SecurityError(f"passphrase must be at least {MIN_PASSPHRASE_LEN} characters")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive(passphrase, salt, scrypt_n, 8, 1)
    ciphertext = AESGCM(key).encrypt(nonce, secret, public_key.encode())
    return {
        "version": KEYSTORE_VERSION,
        "public_key": public_key,
        "kdf": "scrypt",
        "kdf_params": {"n": scrypt_n, "r": 8, "p": 1, "salt": _b64e(salt)},
        "cipher": "aes-256-gcm",
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ciphertext),
    }


def decrypt_secret(data: dict[str, Any], passphrase: str) -> bytes:
    if data.get("version") != KEYSTORE_VERSION or data.get("kdf") != "scrypt":
        raise SecurityError("unsupported keystore format")
    params = data["kdf_params"]
    key = _derive(passphrase, _b64d(params["salt"]), int(params["n"]), int(params["r"]), int(params["p"]))
    try:
        return AESGCM(key).decrypt(_b64d(data["nonce"]), _b64d(data["ciphertext"]), str(data["public_key"]).encode())
    except InvalidTag as exc:
        raise SecurityError("wrong passphrase or corrupted keystore") from exc


def write_keystore(path: str | Path, data: dict[str, Any]) -> None:
    """Write atomically with 0600 permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def read_keystore(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SecurityError(f"keystore not found: {p}")
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise SecurityError(f"keystore {p} permissions {oct(mode)} are too open (use chmod 600)")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SecurityError("invalid keystore")
    return data


def create_keystore_from_keypair_bytes(
    keypair_bytes: bytes, passphrase: str, scrypt_n: int = DEFAULT_SCRYPT_N
) -> dict[str, Any]:
    """Build a keystore from a 64-byte Solana keypair (secret || public)."""
    from solders.keypair import Keypair

    kp = Keypair.from_bytes(keypair_bytes)
    return encrypt_secret(bytes(kp), str(kp.pubkey()), passphrase, scrypt_n)


def load_keypair(path: str | Path, passphrase: str) -> Any:
    """Decrypt and return a ``solders.keypair.Keypair`` (signer process only)."""
    from solders.keypair import Keypair

    data = read_keystore(path)
    secret = decrypt_secret(data, passphrase)
    kp = Keypair.from_bytes(secret)
    if str(kp.pubkey()) != data["public_key"]:
        raise SecurityError("keystore public key mismatch")
    return kp


def rotate_passphrase(path: str | Path, old: str, new: str, scrypt_n: int = DEFAULT_SCRYPT_N) -> None:
    data = read_keystore(path)
    secret = decrypt_secret(data, old)
    write_keystore(path, encrypt_secret(secret, data["public_key"], new, scrypt_n))
