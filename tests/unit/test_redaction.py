from copytrader.security.redaction import REDACTED, Redactor


def test_registered_values_are_scrubbed():
    r = Redactor()
    r.register(["supersecretapikey123"])
    assert "supersecretapikey123" not in r.text("url?x=supersecretapikey123&y=1")


def test_patterns():
    r = Redactor()
    keypair = "[" + ",".join(["12"] * 64) + "]"
    assert r.text(f"key={keypair}") == f"key={REDACTED}"
    assert REDACTED in r.text("https://mainnet.helius-rpc.com/?api-key=abcd1234efgh")
    assert REDACTED in r.text("token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx")
    hexkey = "a" * 64
    assert hexkey not in r.text(f"pk {hexkey}")
    mnemonic = " ".join(["abandon"] * 11 + ["about"])
    assert r.text(mnemonic) == REDACTED


def test_signatures_are_not_redacted():
    r = Redactor()
    sig = "5" * 88
    assert r.text(sig) == sig


def test_sensitive_keys_in_structures():
    r = Redactor()
    data = {"password": "x" * 10, "token_mint": "abc", "nested": {"api_key": "zzz", "ok": 1}, "raw": list(range(64))}
    out = r.data(data)
    assert out["password"] == REDACTED
    assert out["token_mint"] == "abc"
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["raw"] == REDACTED
