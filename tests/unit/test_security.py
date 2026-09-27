import os
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from copytrader.core.errors import AuthError, SecurityError, SignerPolicyViolation
from copytrader.security.hmac_auth import HmacVerifier, sign_request
from copytrader.security.keystore import (
    create_keystore_from_keypair_bytes,
    decrypt_secret,
    load_keypair,
    read_keystore,
    rotate_passphrase,
    write_keystore,
)
from copytrader.security.passwords import (
    FieldCipher,
    hash_password,
    new_totp_secret,
    totp_code,
    verify_password,
    verify_totp,
)
from copytrader.security.signer import LocalSigner
from copytrader.security.signer_policy import (
    ATA_PROGRAM,
    JITO_TIP_ACCOUNTS,
    JUPITER_V6_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
    SignerLimits,
    SignerPolicy,
    SignIntent,
)

FAST_N = 2**10


# ------------------------------------------------------------------ keystore
def test_keystore_roundtrip(tmp_path):
    kp = Keypair()
    data = create_keystore_from_keypair_bytes(bytes(kp), "correct horse battery", scrypt_n=FAST_N)
    assert str(kp) not in str(data)
    path = tmp_path / "ks.json"
    write_keystore(path, data)
    assert os.stat(path).st_mode & 0o777 == 0o600
    loaded = load_keypair(path, "correct horse battery")
    assert loaded.pubkey() == kp.pubkey()


def test_keystore_wrong_passphrase(tmp_path):
    data = create_keystore_from_keypair_bytes(bytes(Keypair()), "correct horse battery", scrypt_n=FAST_N)
    with pytest.raises(SecurityError):
        decrypt_secret(data, "wrong passphrase!!")


def test_keystore_rejects_open_permissions(tmp_path):
    path = tmp_path / "ks.json"
    write_keystore(path, create_keystore_from_keypair_bytes(bytes(Keypair()), "correct horse battery",
                                                            scrypt_n=FAST_N))
    os.chmod(path, 0o644)
    with pytest.raises(SecurityError):
        read_keystore(path)


def test_keystore_rejects_short_passphrase():
    with pytest.raises(SecurityError):
        create_keystore_from_keypair_bytes(bytes(Keypair()), "short")


def test_keystore_tamper_detected():
    data = create_keystore_from_keypair_bytes(bytes(Keypair()), "correct horse battery", scrypt_n=FAST_N)
    data["public_key"] = str(Keypair().pubkey())  # bound as associated data
    with pytest.raises(SecurityError):
        decrypt_secret(data, "correct horse battery")


def test_rotate_passphrase(tmp_path):
    kp = Keypair()
    path = tmp_path / "ks.json"
    write_keystore(path, create_keystore_from_keypair_bytes(bytes(kp), "old passphrase 123", scrypt_n=FAST_N))
    rotate_passphrase(path, "old passphrase 123", "new passphrase 456", scrypt_n=FAST_N)
    assert load_keypair(path, "new passphrase 456").pubkey() == kp.pubkey()
    with pytest.raises(SecurityError):
        load_keypair(path, "old passphrase 123")


# ---------------------------------------------------------------------- HMAC
def test_hmac_accepts_valid_and_blocks_replay():
    key = b"k" * 32
    clock = {"t": 1_000_000.0}
    verifier = HmacVerifier(keys=[key], clock=lambda: clock["t"])
    headers = sign_request(key, "POST", "/v1/sign", b"{}", now=clock["t"])
    verifier.verify("POST", "/v1/sign", b"{}", headers)
    with pytest.raises(AuthError, match="replay"):
        verifier.verify("POST", "/v1/sign", b"{}", headers)


def test_hmac_rejects_tampered_body_skew_and_wrong_key():
    key = b"k" * 32
    verifier = HmacVerifier(keys=[key], clock=lambda: 1_000_000.0)
    headers = sign_request(key, "POST", "/v1/sign", b"{}", now=1_000_000.0)
    with pytest.raises(AuthError):
        verifier.verify("POST", "/v1/sign", b'{"x":1}', headers)
    old = sign_request(key, "POST", "/v1/sign", b"{}", now=1_000_000.0 - 120)
    with pytest.raises(AuthError, match="window"):
        verifier.verify("POST", "/v1/sign", b"{}", old)
    other = sign_request(b"x" * 32, "POST", "/v1/sign", b"{}", now=1_000_000.0)
    with pytest.raises(AuthError):
        verifier.verify("POST", "/v1/sign", b"{}", other)


def test_hmac_key_rotation_accepts_previous_key():
    old, new = b"o" * 32, b"n" * 32
    verifier = HmacVerifier(keys=[new, old], clock=lambda: 5.0)
    verifier.verify("GET", "/v1/pubkey", b"{}", sign_request(old, "GET", "/v1/pubkey", b"{}", now=5.0))


# ------------------------------------------------------------ signer policy
def _ata(owner: Pubkey, mint: str) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA_PROGRAM))[0]


def _cu_limit(units: int) -> Instruction:
    return Instruction(Pubkey.from_string("ComputeBudget111111111111111111111111111111"),
                       b"\x02" + struct.pack("<I", units), [])


def _cu_price(micro: int) -> Instruction:
    return Instruction(Pubkey.from_string("ComputeBudget111111111111111111111111111111"),
                       b"\x03" + struct.pack("<Q", micro), [])


def _jup_swap_tx(kp: Keypair, *, wrap: int, extra: list[Instruction] | None = None, payer: Keypair | None = None,
                 cu_price: int = 100_000) -> bytes:
    owner = kp.pubkey()
    wsol = _ata(owner, WSOL_MINT)
    token = Pubkey.from_string(TOKEN_PROGRAM)
    ixs = [
        _cu_limit(200_000),
        _cu_price(cu_price),
        Instruction(Pubkey.from_string(ATA_PROGRAM), b"\x01", [
            AccountMeta(owner, True, True), AccountMeta(wsol, False, True), AccountMeta(owner, False, False),
            AccountMeta(Pubkey.from_string(WSOL_MINT), False, False)]),
        transfer(TransferParams(from_pubkey=owner, to_pubkey=wsol, lamports=wrap)),
        Instruction(token, bytes([17]), [AccountMeta(wsol, False, True)]),
        Instruction(Pubkey.from_string(JUPITER_V6_PROGRAM), b"\xe5\x17\xcb\x97" + b"\x00" * 20,
                    [AccountMeta(owner, True, False), AccountMeta(wsol, False, True)]),
        Instruction(token, bytes([9]), [AccountMeta(wsol, False, True), AccountMeta(owner, False, True),
                                        AccountMeta(owner, True, False)]),
        *(extra or []),
    ]
    fee_payer = (payer or kp).pubkey()
    msg = MessageV0.try_compile(fee_payer, ixs, [], Hash.default())
    from solders.signature import Signature

    return bytes(VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures))


def _intent(amount=1_000_000_000, notional=50.0, cid="o1"):
    return SignIntent(client_order_id=cid, purpose="entry", input_mint=WSOL_MINT, output_mint="X" * 32,
                      amount_in_raw=amount, notional_usd=notional)


def test_policy_allows_standard_jupiter_swap():
    kp = Keypair()
    policy = SignerPolicy(owner=str(kp.pubkey()))
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=1_000_000_000))
    assert policy.inspect(tx, _intent()) == []


def test_policy_rejects_drain_via_system_transfer():
    kp = Keypair()
    attacker = Keypair().pubkey()
    extra = [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=attacker, lamports=5))]
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=1_000_000_000, extra=extra))
    violations = SignerPolicy(owner=str(kp.pubkey())).inspect(tx, _intent())
    assert any("non-allowed destination" in v for v in violations)


def test_policy_rejects_top_level_token_transfer_and_unknown_program():
    kp = Keypair()
    owner = kp.pubkey()
    evil_token_transfer = Instruction(Pubkey.from_string(TOKEN_PROGRAM), bytes([3]) + struct.pack("<Q", 10), [
        AccountMeta(Pubkey.new_unique(), False, True), AccountMeta(Pubkey.new_unique(), False, True),
        AccountMeta(owner, True, False)])
    unknown = Instruction(Pubkey.new_unique(), b"\x00", [AccountMeta(owner, True, True)])
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=1, extra=[evil_token_transfer, unknown]))
    violations = SignerPolicy(owner=str(owner)).inspect(tx, _intent())
    assert any("Transfer" in v for v in violations)
    assert any("program not allowed" in v for v in violations)


def test_policy_rejects_foreign_fee_payer_and_overwrap():
    kp = Keypair()
    other = Keypair()
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=10, payer=other))
    violations = SignerPolicy(owner=str(kp.pubkey())).inspect(tx, _intent())
    assert any("fee payer" in v for v in violations)
    tx2 = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=2_000_000_000))
    assert any("wraps more SOL" in v for v in SignerPolicy(owner=str(kp.pubkey())).inspect(tx2, _intent()))


def test_policy_priority_fee_cap_and_tip():
    kp = Keypair()
    limits = SignerLimits(max_priority_fee_lamports=10_000, max_tip_lamports=1000)
    tip = transfer(TransferParams(from_pubkey=kp.pubkey(),
                                  to_pubkey=Pubkey.from_string(sorted(JITO_TIP_ACCOUNTS)[0]), lamports=5000))
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=1, extra=[tip], cu_price=10_000_000))
    violations = SignerPolicy(owner=str(kp.pubkey()), limits=limits).inspect(tx, _intent())
    assert any("priority fee" in v for v in violations)
    assert any("tip" in v for v in violations)


def test_policy_notional_caps_and_rate_limit(tmp_path):
    kp = Keypair()
    limits = SignerLimits(max_notional_usd_per_tx=100, max_notional_usd_per_day=150, max_tx_per_minute=10,
                          state_file=str(tmp_path / "state.json"))
    policy = SignerPolicy(owner=str(kp.pubkey()), limits=limits)
    tx = VersionedTransaction.from_bytes(_jup_swap_tx(kp, wrap=1))
    with pytest.raises(SignerPolicyViolation, match="per-transaction"):
        policy.authorize(tx, _intent(notional=120, cid="a"), now=1000)
    policy.authorize(tx, _intent(notional=90, cid="b"), now=1000)
    policy.authorize(tx, _intent(notional=90, cid="b"), now=1001)  # re-sign same order: not counted twice
    with pytest.raises(SignerPolicyViolation, match="daily"):
        policy.authorize(tx, _intent(notional=90, cid="c"), now=1002)
    # state persisted across restarts
    reloaded = SignerPolicy(owner=str(kp.pubkey()), limits=limits)
    with pytest.raises(SignerPolicyViolation, match="daily"):
        reloaded.authorize(tx, _intent(notional=90, cid="d"), now=1003)


async def test_local_signer_signs_allowed_tx():
    kp = Keypair()
    signer = LocalSigner(kp, SignerPolicy(owner=str(kp.pubkey())))
    signed = await signer.sign(_jup_swap_tx(kp, wrap=1_000), _intent())
    tx = VersionedTransaction.from_bytes(signed.tx_bytes)
    assert str(tx.signatures[0]) == signed.signature
    assert all(tx.verify_with_results())


def test_jito_tip_accounts_are_valid_pubkeys():
    for acc in JITO_TIP_ACCOUNTS:
        Pubkey.from_string(acc)


# ---------------------------------------------------------- passwords / TOTP
def test_password_hashing():
    h = hash_password("a very long password")
    assert verify_password("a very long password", h)
    assert not verify_password("wrong password!!", h)
    with pytest.raises(SecurityError):
        hash_password("short")


def test_totp_rfc6238_vector():
    # RFC 6238 test secret "12345678901234567890" (SHA1), T=59 -> 94287082 (8 digits)
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_code(secret, at=59, digits=8) == "94287082"
    s = new_totp_secret()
    assert verify_totp(s, totp_code(s, at=1000), at=1000)
    assert not verify_totp(s, "000000", at=1000) or totp_code(s, at=1000) == "000000"


def test_field_cipher():
    c = FieldCipher(FieldCipher.generate_key())
    assert c.decrypt(c.encrypt("secret")) == "secret"
    with pytest.raises(SecurityError):
        FieldCipher(None).encrypt("x")
