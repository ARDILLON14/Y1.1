from datetime import UTC, datetime

from copytrader.core.types import Side, TxSource
from copytrader.providers.solana.constants import (
    JITO_TIP_ACCOUNTS,
    SOL_MINT,
    TOKEN_ACCOUNT_RENT_LAMPORTS,
    USDC_MINT,
)
from copytrader.providers.solana.parser import created_token_accounts, normalize_transaction, parse_swaps

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
MINT2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
BLOCK_TIME = 1_750_000_000


def tb(index, mint, owner, amount, decimals=6):
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(amount), "decimals": decimals},
    }


def make_tx(keys, pre, post, pre_tok, post_tok, fee=5000, err=None, parsed_keys=True):
    account_keys = (
        [{"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate(keys)] if parsed_keys else keys
    )
    return {
        "slot": 123,
        "blockTime": BLOCK_TIME,
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": pre,
            "postBalances": post,
            "preTokenBalances": pre_tok,
            "postTokenBalances": post_tok,
        },
        "transaction": {"signatures": ["sig1"], "message": {"accountKeys": account_keys}},
    }


def test_buy_with_new_token_account_on_pumpfun():
    spent = 1_000_000_000
    fee = 5000
    tx = make_tx(
        [WALLET, "ata1", PUMP, "curve"],
        [10_000_000_000, 0, 1, 5_000_000_000],
        [
            10_000_000_000 - spent - TOKEN_ACCOUNT_RENT_LAMPORTS - fee,
            TOKEN_ACCOUNT_RENT_LAMPORTS,
            1,
            5_000_000_000 + spent,
        ],
        [],
        [tb(1, MINT, WALLET, 1_000_000_000)],
        fee=fee,
    )
    swaps = parse_swaps(tx, WALLET, sol_price_usd=150.0)
    assert len(swaps) == 1
    s = swaps[0]
    assert s.side is Side.BUY
    assert s.token_mint == MINT
    assert s.token_amount == 1000.0
    assert s.quote_mint == SOL_MINT
    assert abs(s.quote_amount - 1.0) < 1e-9  # rent and fee excluded
    assert abs(s.value_usd - 150.0) < 1e-6
    assert abs(s.price_usd - 0.15) < 1e-9
    assert s.dex == "pumpfun"
    assert s.token_balance_before == 0 and s.token_balance_after == 1000
    assert s.block_time == datetime.fromtimestamp(BLOCK_TIME, tz=UTC)


def test_created_token_accounts_lists_only_new_accounts_of_the_wallet():
    tx = make_tx(
        [WALLET, "ata1", "ata2", "ata_other"],
        [10, 0, 5, 0],
        [9, 0, 5, 0],
        [tb(2, MINT2, WALLET, 7)],
        [tb(1, MINT, WALLET, 1_000), tb(2, MINT2, WALLET, 0), tb(3, MINT, OTHER, 5)],
    )
    assert created_token_accounts(tx, WALLET) == [MINT]


def test_sell_all_with_account_close_refund_excluded():
    received = 2_000_000_000
    fee = 5000
    tx = make_tx(
        [WALLET, "ata1", JUP],
        [1_000_000_000, TOKEN_ACCOUNT_RENT_LAMPORTS, 1],
        [1_000_000_000 + received + TOKEN_ACCOUNT_RENT_LAMPORTS - fee, 0, 1],
        [tb(1, MINT, WALLET, 500_000_000)],
        [],
        fee=fee,
    )
    s = parse_swaps(tx, WALLET, sol_price_usd=100.0)[0]
    assert s.side is Side.SELL
    assert abs(s.quote_amount - 2.0) < 1e-9
    assert s.token_amount == 500.0
    assert s.sold_fraction == 1.0
    assert s.dex == "jupiter"


def test_partial_sell_fraction():
    tx = make_tx(
        [WALLET, "ata1"],
        [1_000_000_000, 1],
        [1_500_000_000 - 5000, 1],
        [tb(1, MINT, WALLET, 1_000_000)],
        [tb(1, MINT, WALLET, 750_000)],
    )
    s = parse_swaps(tx, WALLET, sol_price_usd=100.0)[0]
    assert s.side is Side.SELL
    assert abs(s.sold_fraction - 0.25) < 1e-9


def test_wsol_is_folded_into_sol():
    # Wallet pays with pre-wrapped WSOL (token balance decreases), SOL only pays the fee.
    tx = make_tx(
        [WALLET, "wsol_ata", "ata1"],
        [1_000_000_000, TOKEN_ACCOUNT_RENT_LAMPORTS, TOKEN_ACCOUNT_RENT_LAMPORTS],
        [1_000_000_000 - 5000, TOKEN_ACCOUNT_RENT_LAMPORTS, TOKEN_ACCOUNT_RENT_LAMPORTS],
        [tb(1, SOL_MINT, WALLET, 3_000_000_000, 9), tb(2, MINT, WALLET, 0)],
        [tb(1, SOL_MINT, WALLET, 1_000_000_000, 9), tb(2, MINT, WALLET, 42_000_000)],
    )
    s = parse_swaps(tx, WALLET, sol_price_usd=100.0)[0]
    assert s.side is Side.BUY
    assert abs(s.quote_amount - 2.0) < 1e-9
    assert s.token_amount == 42.0


def test_usdc_quoted_buy():
    tx = make_tx(
        [WALLET, "usdc_ata", "ata1"],
        [1_000_000_000, 1, 1],
        [1_000_000_000 - 5000, 1, 1],
        [tb(1, USDC_MINT, WALLET, 500_000_000), tb(2, MINT, WALLET, 0)],
        [tb(1, USDC_MINT, WALLET, 400_000_000), tb(2, MINT, WALLET, 10_000_000)],
    )
    s = parse_swaps(tx, WALLET, sol_price_usd=None)[0]
    assert s.side is Side.BUY and s.quote_mint == USDC_MINT
    assert s.value_usd == 100.0
    assert s.price_usd == 10.0


def test_jito_tip_is_not_part_of_the_price():
    tip_account = sorted(JITO_TIP_ACCOUNTS)[0]
    tip = 1_000_000
    tx = make_tx(
        [WALLET, "ata1", tip_account],
        [5_000_000_000, 0, 10],
        [
            5_000_000_000 - 1_000_000_000 - tip - 5000 - TOKEN_ACCOUNT_RENT_LAMPORTS,
            TOKEN_ACCOUNT_RENT_LAMPORTS,
            10 + tip,
        ],
        [],
        [tb(1, MINT, WALLET, 100)],
    )
    s = parse_swaps(tx, WALLET, sol_price_usd=100.0)[0]
    assert abs(s.quote_amount - 1.0) < 1e-9
    assert abs(s.fee_sol - 0.001005) < 1e-12


def test_failed_transaction_is_ignored():
    tx = make_tx([WALLET, "ata1"], [1, 0], [0, 0], [], [tb(1, MINT, WALLET, 1)], err={"InstructionError": [0, 1]})
    assert parse_swaps(tx, WALLET, sol_price_usd=100.0) == []


def test_airdrop_or_transfer_is_not_a_swap():
    tx = make_tx([WALLET, "ata1"], [1_000_000_000, 0], [1_000_000_000 - 5000, 0], [], [tb(1, MINT, WALLET, 1_000_000)])
    assert parse_swaps(tx, WALLET, sol_price_usd=100.0) == []


def test_wallet_not_in_transaction():
    tx = make_tx([OTHER, "ata1"], [1, 0], [1, 0], [], [])
    assert parse_swaps(tx, WALLET, sol_price_usd=100.0) == []


def test_token_to_token_swap_uses_price_lookup():
    tx = make_tx(
        [WALLET, "a1", "a2"],
        [1_000_000_000, 1, 1],
        [1_000_000_000 - 5000, 1, 1],
        [tb(1, MINT, WALLET, 10_000_000), tb(2, MINT2, WALLET, 0)],
        [tb(1, MINT, WALLET, 0), tb(2, MINT2, WALLET, 5_000_000)],
    )
    swaps = parse_swaps(tx, WALLET, sol_price_usd=100.0, token_price_usd=lambda m: 2.0 if m == MINT else None)
    assert {s.side for s in swaps} == {Side.BUY, Side.SELL}
    sell = next(s for s in swaps if s.side is Side.SELL)
    buy = next(s for s in swaps if s.side is Side.BUY)
    assert sell.value_usd == 20.0 and buy.value_usd == 20.0
    assert buy.price_usd == 4.0
    # Without a price we refuse to guess
    assert parse_swaps(tx, WALLET, sol_price_usd=100.0) == []


def test_ambiguous_multi_token_is_rejected():
    tx = make_tx(
        [WALLET, "a1", "a2"],
        [2_000_000_000, 1, 1],
        [1_000_000_000, 1, 1],
        [],
        [tb(1, MINT, WALLET, 10), tb(2, MINT2, WALLET, 10)],
    )
    assert parse_swaps(tx, WALLET, sol_price_usd=100.0) == []


def test_json_encoding_with_loaded_addresses():
    tx = make_tx(
        [WALLET, "ata1"],
        [3_000_000_000, 0, 7],
        [2_000_000_000 - 5000 - TOKEN_ACCOUNT_RENT_LAMPORTS, TOKEN_ACCOUNT_RENT_LAMPORTS, 7],
        [],
        [tb(1, MINT, WALLET, 5)],
        parsed_keys=False,
    )
    tx["meta"]["loadedAddresses"] = {"writable": [], "readonly": [PUMP]}
    norm = normalize_transaction(tx)
    assert norm.account_keys[-1] == PUMP
    s = parse_swaps(tx, WALLET, sol_price_usd=100.0)[0]
    assert s.dex == "pumpfun"


def test_helius_notification_shape_and_fallback_time():
    inner = make_tx(
        [WALLET, "ata1"],
        [3_000_000_000, 0],
        [2_000_000_000 - 5000 - TOKEN_ACCOUNT_RENT_LAMPORTS, TOKEN_ACCOUNT_RENT_LAMPORTS],
        [],
        [tb(1, MINT, WALLET, 5)],
    )
    del inner["blockTime"]
    notification = {
        "signature": "sigH",
        "slot": 999,
        "transaction": {"transaction": inner["transaction"], "meta": inner["meta"]},
    }
    now = datetime(2025, 1, 1, tzinfo=UTC)
    s = parse_swaps(
        notification, WALLET, sol_price_usd=100.0, fallback_time=now, detected_at=now, source=TxSource.STREAM
    )[0]
    assert s.signature == "sigH" and s.slot == 999 and s.block_time == now
