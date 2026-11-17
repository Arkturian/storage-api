"""#3230: container format, filename, recovery wrap, KMS adapters, DEK cache."""
import base64
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import encryption as E  # noqa: E402

SEG = E.SEGMENT_SIZE
SIZES = [0, 1, SEG - 1, SEG, SEG + 1, 3 * SEG + 5]


def _seal_file(tmp_path, data, key="enc_x.bin", tenant="arkturian"):
    dek = secrets.token_bytes(32)
    ct = E.encrypt_bytes(data, dek, object_key=key, tenant=tenant, kid="test:local")
    p = tmp_path / key
    p.write_bytes(ct)
    return p, dek


@pytest.mark.parametrize("n", SIZES)
def test_roundtrip(tmp_path, n):
    data = secrets.token_bytes(n)
    p, dek = _seal_file(tmp_path, data)
    assert E.decrypt_all(p, dek, object_key="enc_x.bin", tenant="arkturian") == data
    head = E.parse_header(p.read_bytes())
    assert E.plain_size_for(p.stat().st_size, head) == n


@pytest.mark.parametrize("start,end", [(0, 0), (SEG - 1, SEG), (SEG, SEG), (5, 3 * SEG + 4), (2 * SEG + 7, 3 * SEG + 4), (100, 10**9)])
def test_range(tmp_path, start, end):
    data = secrets.token_bytes(3 * SEG + 5)
    p, dek = _seal_file(tmp_path, data)
    got = b"".join(E.iter_decrypt(p, dek, object_key="enc_x.bin", tenant="arkturian", start=start, end=end))
    assert got == data[start:end + 1]


def test_no_plaintext_on_disk(tmp_path):
    data = b"Vertrauliche Tabelle " * 500
    p, _ = _seal_file(tmp_path, data)
    assert b"Vertrauliche" not in p.read_bytes()


def _expect_integrity(p, dek, key="enc_x.bin", tenant="arkturian"):
    with pytest.raises(E.IntegrityError):
        E.decrypt_all(p, dek, object_key=key, tenant=tenant)


def test_bitflip_detected(tmp_path):
    p, dek = _seal_file(tmp_path, secrets.token_bytes(2 * SEG))
    b = bytearray(p.read_bytes()); b[-5] ^= 1; p.write_bytes(bytes(b))
    _expect_integrity(p, dek)


def test_truncation_at_segment_boundary_detected(tmp_path):
    data = secrets.token_bytes(3 * SEG)
    p, dek = _seal_file(tmp_path, data)
    head = E.parse_header(p.read_bytes())
    p.write_bytes(p.read_bytes()[: head.length + 2 * (SEG + 16)])
    _expect_integrity(p, dek)


def test_empty_file_truncated_detected(tmp_path):
    p, dek = _seal_file(tmp_path, b"")
    head = E.parse_header(p.read_bytes())
    p.write_bytes(p.read_bytes()[: head.length])
    with pytest.raises(E.IntegrityError):
        E.decrypt_all(p, dek, object_key="enc_x.bin", tenant="arkturian")


def test_header_tamper_detected(tmp_path):
    p, dek = _seal_file(tmp_path, b"abc" * 100)
    raw = bytearray(p.read_bytes())
    raw[9] ^= 1  # nonce prefix byte
    p.write_bytes(bytes(raw))
    _expect_integrity(p, dek)


@pytest.mark.parametrize("key,tenant", [("enc_other.bin", "arkturian"), ("enc_x.bin", "oneal")])
def test_aad_binds_object_and_tenant(tmp_path, key, tenant):
    p, dek = _seal_file(tmp_path, b"payload")
    _expect_integrity(p, dek, key=key, tenant=tenant)


def test_filename_roundtrip_and_binding():
    dek = secrets.token_bytes(32)
    blob = E.encrypt_filename("Gehälter 2026.xlsx", dek, object_key="k", tenant="t")
    assert "Geh" not in blob
    assert E.decrypt_filename(blob, dek, object_key="k", tenant="t") == "Gehälter 2026.xlsx"
    with pytest.raises(E.IntegrityError):
        E.decrypt_filename(blob, dek, object_key="k2", tenant="t")


def test_recovery_roundtrip_and_binding():
    priv, pub = E.generate_recovery_keypair()
    dek = secrets.token_bytes(32)
    rkid, blob = E.recovery_wrap(dek, pub, object_key="k", tenant="t")
    assert E.recovery_unwrap(blob, priv, object_key="k", tenant="t", recovery_kid=rkid) == dek
    with pytest.raises(E.IntegrityError):
        E.recovery_unwrap(blob, priv, object_key="other", tenant="t", recovery_kid=rkid)
    other_priv, _ = E.generate_recovery_keypair()
    with pytest.raises(E.EncryptionError):
        E.recovery_unwrap(blob, other_priv, object_key="k", tenant="t", recovery_kid=rkid)


def test_local_wrapper_binds_aad():
    w = E.LocalTestKeyWrapper(secrets.token_bytes(32))
    dek = secrets.token_bytes(32)
    blob = w.wrap(dek, E.wrap_aad("k", "t"))
    assert w.unwrap(w.kid, blob, E.wrap_aad("k", "t"))[0] == dek
    with pytest.raises(E.IntegrityError):
        w.unwrap(w.kid, blob, E.wrap_aad("k2", "t"))


def test_scaleway_without_credential_is_unavailable(tmp_path):
    w = E.ScalewayKeyWrapper("kid-1", "fr-par", tmp_path / "missing")
    with pytest.raises(E.EncryptionUnavailable):
        w.wrap(secrets.token_bytes(32), b"aad")
    with pytest.raises(E.EncryptionUnavailable):
        w.unwrap(w.kid, b"blob", b"aad")


def test_scaleway_request_shape(monkeypatch, tmp_path):
    tok = tmp_path / "storage-kms-token"; tok.write_text("SECRET\n")
    seen = {}

    class R:
        status_code = 200
        def __init__(self, body): self._b = body
        def json(self): return self._b

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        if url.endswith("/encrypt"):
            return R({"key_id": "kid-1", "ciphertext": base64.b64encode(b"W" + base64.b64decode(json["plaintext"])).decode()})
        return R({"key_id": "kid-1", "plaintext": base64.b64encode(base64.b64decode(json["ciphertext"])[1:]).decode(),
                  "ciphertext": base64.b64encode(b"NEW").decode()})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    w = E.ScalewayKeyWrapper("kid-1", "fr-par", tok)
    dek = secrets.token_bytes(32)
    blob = w.wrap(dek, b"aad")
    assert seen["url"] == "https://api.scaleway.com/key-manager/v1alpha1/regions/fr-par/keys/kid-1/encrypt"
    assert seen["headers"]["X-Auth-Token"] == "SECRET"
    assert base64.b64decode(seen["json"]["associated_data"]) == b"aad"
    got, rewrapped = w.unwrap(w.kid, blob, b"aad")
    assert got == dek and rewrapped == b"NEW"
    assert seen["url"].endswith("/keys/kid-1/decrypt")


def test_scaleway_auth_failure_is_unavailable(monkeypatch, tmp_path):
    tok = tmp_path / "t"; tok.write_text("x")

    class R:
        status_code = 403
        def json(self): return {}
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    with pytest.raises(E.EncryptionUnavailable):
        E.ScalewayKeyWrapper("kid-1", "fr-par", tok).wrap(secrets.token_bytes(32), b"a")


def test_test_backend_needs_explicit_allow(monkeypatch):
    _, pub = E.generate_recovery_keypair()
    monkeypatch.setenv("STORAGE_ENCRYPTION_BACKEND", "test")
    monkeypatch.setenv("STORAGE_ENCRYPTION_TEST_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("STORAGE_ENCRYPTION_RECOVERY_PUBKEY", base64.b64encode(pub).decode())
    monkeypatch.delenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND", raising=False)
    assert not E.load_config().ready
    monkeypatch.setenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND", "1")
    assert E.load_config().ready


def test_not_ready_without_recovery_key(monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_BACKEND", "test")
    monkeypatch.setenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_TEST_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.delenv("STORAGE_ENCRYPTION_RECOVERY_PUBKEY", raising=False)
    cfg = E.load_config()
    assert not cfg.ready
    with pytest.raises(E.EncryptionUnavailable):
        E.seal(b"x", "a.txt", tenant="t", config=cfg)


def test_seal_and_unwrap_via_cache():
    priv, pub = E.generate_recovery_keypair()
    cfg = E.EncryptionConfig(E.LocalTestKeyWrapper(secrets.token_bytes(32)), pub)
    s = E.seal(b"hello", "x.txt", tenant="t", config=cfg)
    assert s.object_key.startswith("enc_") and "x.txt" not in s.object_key
    E.DEK_CACHE.clear()

    class O:
        object_key = s.object_key; tenant_id = "t"; enc_kid = s.enc_kid; enc_wrapped_dek = s.enc_wrapped_dek
    dek, _ = E.unwrap_dek(O(), cfg)
    assert E.DEK_CACHE.get(s.object_key) == dek
    rdek = E.recovery_unwrap(s.enc_wrapped_dek_recovery, priv, object_key=s.object_key, tenant="t",
                             recovery_kid=s.enc_recovery_kid)
    assert rdek == dek
    E.DEK_CACHE.drop(s.object_key)
    assert E.DEK_CACHE.get(s.object_key) is None
