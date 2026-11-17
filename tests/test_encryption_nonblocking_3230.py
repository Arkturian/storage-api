"""#3230 (Cloud review): a slow KMS must not block the event loop on the
write paths (seal on upload/replace, unwrap on replace)."""
import asyncio
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import encryption as E  # noqa: E402


class SlowWrapper(E.LocalTestKeyWrapper):
    def wrap(self, dek, aad):
        time.sleep(0.6)
        return super().wrap(dek, aad)

    def unwrap(self, kid, blob, aad):
        time.sleep(0.6)
        return super().unwrap(kid, blob, aad)


async def _ticks_while(coro):
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1
    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        t.cancel()
    return ticks, result


def _cfg(monkeypatch):
    _, pub = E.generate_recovery_keypair()
    cfg = E.EncryptionConfig(SlowWrapper(secrets.token_bytes(32)), pub)
    monkeypatch.setattr(E, "load_config", lambda: cfg)
    E.DEK_CACHE.clear()
    return cfg


def test_save_encrypted_does_not_block_loop(tmp_path, monkeypatch):
    from storage.service import generic_storage
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path)
    _cfg(monkeypatch)
    ticks, saved = asyncio.run(_ticks_while(generic_storage.save_encrypted(
        data=b"x" * 1000, original_filename="a.txt", tenant_id="arkturian")))
    assert saved["encrypted"] is True
    assert ticks >= 6, ticks  # ~0.6 s of KMS latency, the loop kept running


def test_replace_does_not_block_loop(tmp_path, monkeypatch):
    from storage import domain
    from storage.service import generic_storage
    monkeypatch.setattr(generic_storage, "upload_root", tmp_path)
    _cfg(monkeypatch)
    saved = asyncio.run(generic_storage.save_encrypted(data=b"alt", original_filename="a.txt", tenant_id="arkturian"))
    E.DEK_CACHE.clear()
    obj = SimpleNamespace(object_key=saved["object_key"], tenant_id="arkturian", encrypted=True,
                          enc_kid=saved["enc_kid"], enc_wrapped_dek=saved["enc_wrapped_dek"],
                          enc_filename=saved["enc_filename"], context=None)

    class _DB:
        def commit(self): pass
        def refresh(self, o): pass
    ticks, _ = asyncio.run(_ticks_while(domain.update_file_and_record(_DB(), storage_obj=obj, data=b"neu")))
    assert obj.plain_size == 3
    assert ticks >= 12, ticks  # unwrap (0.6 s) + wrap (0.6 s), both off the loop
