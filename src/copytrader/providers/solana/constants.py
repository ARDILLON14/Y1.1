"""Solana constants: mints, programs and DEX labels."""

from __future__ import annotations

SOL_MINT = "So11111111111111111111111111111111111111112"  # wrapped SOL
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000
STABLE_MINTS = frozenset({USDC_MINT, USDT_MINT})

# Rent-exempt minimum of a 165-byte SPL token account.
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280
# SOL movements smaller than this are treated as noise (fees, rent rounding).
SOL_DUST_LAMPORTS = 10_000

# Highest transaction format we read (getTransaction, transactionSubscribe). Transaction v1
# (larger transactions, compute budget in the message config) is live on mainnet since
# 2026-09-15; asking with a lower value makes the RPC REJECT every v1 transaction
# ("Transaction version (1) is not supported"). Must be the JSON integer, not a string.
MAX_SUPPORTED_TX_VERSION = 1

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Program id -> human label (for analytics only; not security relevant).
DEX_PROGRAMS: dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium_launchlab",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_amm",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora_damm_v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora_dbc",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "phoenix",
}
AGGREGATORS = frozenset({"jupiter"})

JITO_TIP_ACCOUNTS = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)

_B58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def is_valid_address(value: str) -> bool:
    """Validate a base58 Solana public key (32 bytes)."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or not set(value) <= _B58_ALPHABET:
        return False
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(value)
    except (ValueError, TypeError):
        return False
    return True
