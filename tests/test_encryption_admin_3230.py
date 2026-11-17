"""#3230: recovery key pair, KMS-loss recovery and export work end to end."""
import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Base, StorageObject  # noqa: E402
from storage import encryption as E  # noqa: E402
from scripts import encryption_admin as A  # noqa: E402


def test_keygen_writes_private_0600_and_prints_public(tmp_path, capsys):
    out = tmp_path / "alex.key"
    assert A.main(["keygen", "--out", str(out)]) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    printed = capsys.readouterr().out
    assert "STORAGE_ENCRYPTION_RECOVERY_PUBKEY=" in printed
    assert out.read_text().strip() not in printed  # the private key never reaches stdout


def test_kms_loss_recovery_and_export(tmp_path, monkeypatch):
    from storage.service import generic_storage
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path / "up")
    keyfile = tmp_path / "alex.key"
    A.main(["keygen", "--out", str(keyfile)])
    priv = base64.b64decode(keyfile.read_text().strip())
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    pub = X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    old = E.LocalTestKeyWrapper(secrets.token_bytes(32), kid="test:old")
    new = E.LocalTestKeyWrapper(secrets.token_bytes(32), kid="test:new")

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    sealed = E.seal(b"geheim" * 1000, "x.xlsx", tenant="arkturian", config=E.EncryptionConfig(old, pub))
    generic_storage._write_ciphertext(generic_storage.encrypted_path_for_key(sealed.object_key, "arkturian"),
                                      sealed.ciphertext)
    obj = StorageObject(object_key=sealed.object_key, tenant_id="arkturian", encrypted=True, enc_kid=sealed.enc_kid,
                        enc_wrapped_dek=sealed.enc_wrapped_dek, enc_recovery_kid=sealed.enc_recovery_kid,
                        enc_wrapped_dek_recovery=sealed.enc_wrapped_dek_recovery, enc_filename=sealed.enc_filename,
                        plain_size=sealed.plain_size, is_public=False, metadata_json={"private_media": True})
    s.add(obj); s.commit(); oid = obj.id; s.close()

    monkeypatch.setattr(A, "_session", lambda: S())
    monkeypatch.setattr(E, "load_config", lambda: E.EncryptionConfig(new, pub))
    assert A.main(["recover-rewrap", "--private-key", str(keyfile)]) == 0
    s = S(); obj = s.get(StorageObject, oid)
    assert obj.enc_kid == "test:new"
    E.DEK_CACHE.clear()
    dek, _ = E.unwrap_dek(obj, E.EncryptionConfig(new, pub))
    path = generic_storage.encrypted_path_for_key(obj.object_key, "arkturian")
    assert E.decrypt_all(path, dek, object_key=obj.object_key, tenant="arkturian") == b"geheim" * 1000
    s.close()

    out = tmp_path / "plain.bin"
    assert A.main(["export", str(oid), "--private-key", str(keyfile), "--out", str(out)]) == 0
    assert out.read_bytes() == b"geheim" * 1000 and oct(out.stat().st_mode & 0o777) == "0o600"
