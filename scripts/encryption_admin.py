#!/usr/bin/env python3
"""Key administration for encrypted objects (#3230, Content post 6192).

Subcommands
  keygen --out PRIVATE_FILE
      Create Alex's offline recovery key pair. Writes the PRIVATE key (raw
      X25519, base64) to PRIVATE_FILE with mode 0600 and prints only the
      PUBLIC key and its kid. Run it on Alex's own machine, not on a server;
      the public key goes into STORAGE_ENCRYPTION_RECOVERY_PUBKEY.

  rewrap-kms [--dry-run]
      After a KMS key rotation: unwrap every DEK through the KMS (which hands
      back the blob re-encrypted under its newest version) and store it.
      Afterwards the old KMS version can be disabled.

  recover-rewrap --private-key FILE [--dry-run]
      KMS account lost: unwrap every DEK with the offline recovery key and
      wrap it under the KMS currently configured. Ciphertext files stay as
      they are.

  export ID --private-key FILE --out PLAINTEXT_FILE
      Decrypt one object with the recovery key (no KMS needed). Writes the
      plaintext with mode 0600; prints nothing about its content or name.

Never prints a key, a DEK, a plaintext or an original filename.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import encryption as E  # noqa: E402


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _read_private(path: str) -> bytes:
    raw = base64.b64decode(Path(path).read_text().strip())
    if len(raw) != 32:
        raise SystemExit("private key file does not hold a raw 32-byte X25519 key")
    return raw


def cmd_keygen(args) -> int:
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite a recovery key")
    priv, pub = E.generate_recovery_keypair()
    _write_private(out, base64.b64encode(priv) + b"\n")
    print("STORAGE_ENCRYPTION_RECOVERY_PUBKEY=" + base64.b64encode(pub).decode())
    print("recovery_kid=" + E.recovery_kid_for(pub))
    print(f"private key written to {out} (0600) — keep it offline, two copies, never on a server")
    return 0


def _session():
    from database import SessionLocal
    return SessionLocal()


def _encrypted_rows(db):
    from models import StorageObject
    return db.query(StorageObject).filter(StorageObject.encrypted.is_(True)).order_by(StorageObject.id)


def cmd_rewrap_kms(args) -> int:
    cfg = E.load_config()
    if cfg.wrapper is None:
        raise SystemExit("no KMS configured")
    db = _session()
    changed = same = failed = 0
    try:
        for obj in _encrypted_rows(db):
            try:
                E.DEK_CACHE.drop(obj.object_key)
                _, new = cfg.wrapper.unwrap(obj.enc_kid, obj.enc_wrapped_dek,
                                            E.wrap_aad(obj.object_key, obj.tenant_id or "arkturian"))
            except E.EncryptionError as exc:
                failed += 1
                print(f"object {obj.id}: {exc.code}")
                continue
            if new and new != obj.enc_wrapped_dek:
                changed += 1
                if not args.dry_run:
                    obj.enc_wrapped_dek = new
            else:
                same += 1
        if not args.dry_run:
            db.commit()
    finally:
        db.close()
    print(f"rewrapped={changed} unchanged={same} failed={failed} dry_run={args.dry_run}")
    return 1 if failed else 0


def cmd_recover_rewrap(args) -> int:
    cfg = E.load_config()
    if cfg.wrapper is None:
        raise SystemExit("configure the new KMS first")
    priv = _read_private(args.private_key)
    db = _session()
    done = failed = 0
    try:
        for obj in _encrypted_rows(db):
            tenant = obj.tenant_id or "arkturian"
            try:
                dek = E.recovery_unwrap(obj.enc_wrapped_dek_recovery, priv, object_key=obj.object_key,
                                        tenant=tenant, recovery_kid=obj.enc_recovery_kid)
                blob = cfg.wrapper.wrap(dek, E.wrap_aad(obj.object_key, tenant))
            except E.EncryptionError as exc:
                failed += 1
                print(f"object {obj.id}: {exc.code}")
                continue
            done += 1
            if not args.dry_run:
                obj.enc_kid, obj.enc_wrapped_dek = cfg.wrapper.kid, blob
        if not args.dry_run:
            db.commit()
    finally:
        db.close()
    print(f"recovered={done} failed={failed} dry_run={args.dry_run}")
    return 1 if failed else 0


def cmd_export(args) -> int:
    from models import StorageObject
    from storage.service import generic_storage
    priv = _read_private(args.private_key)
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists")
    db = _session()
    try:
        obj = db.get(StorageObject, args.id)
        if obj is None or not obj.encrypted:
            raise SystemExit("no encrypted object with that id")
        tenant = obj.tenant_id or "arkturian"
        dek = E.recovery_unwrap(obj.enc_wrapped_dek_recovery, priv, object_key=obj.object_key,
                                tenant=tenant, recovery_kid=obj.enc_recovery_kid)
        path = generic_storage.encrypted_path_for_key(obj.object_key, tenant)
        data = E.decrypt_all(path, dek, object_key=obj.object_key, tenant=tenant)
    finally:
        db.close()
    _write_private(out, data)
    print(f"object {args.id}: {len(data)} bytes written to {out} (0600)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out", required=True); k.set_defaults(fn=cmd_keygen)
    r = sub.add_parser("rewrap-kms"); r.add_argument("--dry-run", action="store_true"); r.set_defaults(fn=cmd_rewrap_kms)
    rr = sub.add_parser("recover-rewrap"); rr.add_argument("--private-key", required=True)
    rr.add_argument("--dry-run", action="store_true"); rr.set_defaults(fn=cmd_recover_rewrap)
    ex = sub.add_parser("export"); ex.add_argument("id", type=int); ex.add_argument("--private-key", required=True)
    ex.add_argument("--out", required=True); ex.set_defaults(fn=cmd_export)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
