"""Encrypted storage objects (#3230, concept: Content post 6192, product p-bb4917ce5da0).

Envelope encryption. Every object gets its own random 256-bit data key (DEK).
The file on disk is a STREAM-AEAD container (AES-256-GCM in 64 KiB segments);
the DEK is wrapped twice and only the wrapped forms are stored in storage.db:

  * by a managed KMS (Scaleway Key Manager) behind the KeyWrapper interface,
    with associated_data = object_key + tenant, so a wrapped DEK cannot be
    moved to another row;
  * by an offline recovery key (X25519, private half only with Alex), so the
    data survives the loss of the KMS account.

Nothing in here ever logs a key, a plaintext or an original filename.

Container layout (all integers big-endian):

  header  = MAGIC(4) | version(1) | segment_size(4) | nonce_prefix(7) | kid_len(1) | kid
  segment = AES-256-GCM(dek, nonce, plaintext_chunk, aad) -> chunk + 16-byte tag
  nonce   = nonce_prefix(7) | counter(4) | last(1)   (last = 0x01 only on the final segment)
  aad     = canon(object_key, tenant, header)

A 0-byte file still writes one (empty) final segment, so truncation at a
segment boundary is always detected: the segment that ends the file must
authenticate with last=0x01.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import serialization

MAGIC = b"AOSE"
VERSION = 1
SEGMENT_SIZE = 64 * 1024
TAG_SIZE = 16
NONCE_PREFIX_SIZE = 7
DEK_SIZE = 32
ENCRYPTED_DIRNAME = "media-enc"
FILENAME_PLACEHOLDER = "verschluesselt"
SIGNED_TTL_CAP_SECONDS = 300
DEK_CACHE_TTL_SECONDS = 300
DEK_CACHE_MAX = 256


class EncryptionError(Exception):
    """Base class. `code` is the stable API error code."""
    code = "encryption_error"
    status = 500


class EncryptionUnavailable(EncryptionError):
    """KMS or recovery key not configured / not reachable -> 503, never plaintext."""
    code = "encryption_key_unavailable"
    status = 503


class EncryptedObjectError(EncryptionError):
    """A plaintext consumer touched an encrypted object (fail closed)."""
    code = "encrypted_object"
    status = 409


class IntegrityError(EncryptionError):
    """Tag check failed, header tampered, file truncated."""
    code = "encrypted_integrity"
    status = 500


def canon(*parts) -> bytes:
    """Length-prefixed concatenation; no field can bleed into the next."""
    out = bytearray()
    for p in parts:
        b = p if isinstance(p, (bytes, bytearray)) else str(p or "").encode("utf-8")
        out += len(b).to_bytes(4, "big") + b
    return bytes(out)


def new_object_key() -> str:
    """Random on-disk name. The original filename never reaches the disk."""
    return f"enc_{secrets.token_hex(16)}.bin"


# --------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Header:
    version: int
    segment_size: int
    nonce_prefix: bytes
    kid: str
    raw: bytes

    @property
    def length(self) -> int:
        return len(self.raw)


def _build_header(kid: str, nonce_prefix: bytes, segment_size: int = SEGMENT_SIZE) -> Header:
    kid_b = kid.encode("utf-8")
    if len(kid_b) > 255:
        raise EncryptionError("kid too long")
    raw = MAGIC + bytes([VERSION]) + segment_size.to_bytes(4, "big") + nonce_prefix + bytes([len(kid_b)]) + kid_b
    return Header(VERSION, segment_size, nonce_prefix, kid, raw)


def parse_header(blob: bytes) -> Header:
    if len(blob) < 17 or blob[:4] != MAGIC:
        raise IntegrityError("not an encrypted container")
    version = blob[4]
    if version != VERSION:
        raise IntegrityError(f"unknown container version {version}")
    seg = int.from_bytes(blob[5:9], "big")
    if seg <= 0 or seg > 16 * 1024 * 1024:
        raise IntegrityError("bad segment size")
    prefix = blob[9:16]
    kid_len = blob[16]
    if len(blob) < 17 + kid_len:
        raise IntegrityError("short header")
    kid = blob[17:17 + kid_len].decode("utf-8")
    return Header(version, seg, prefix, kid, bytes(blob[:17 + kid_len]))


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    if counter >= 2 ** 32:
        raise EncryptionError("file too large for one container")
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt_bytes(data: bytes, dek: bytes, *, object_key: str, tenant: str, kid: str) -> bytes:
    """Whole-object encryption (uploads are already fully in RAM)."""
    if len(dek) != DEK_SIZE:
        raise EncryptionError("bad DEK size")
    header = _build_header(kid, secrets.token_bytes(NONCE_PREFIX_SIZE))
    aad = canon(object_key, tenant, header.raw)
    aes = AESGCM(dek)
    seg = header.segment_size
    n = max(1, -(-len(data) // seg))
    out = bytearray(header.raw)
    for i in range(n):
        chunk = data[i * seg:(i + 1) * seg]
        out += aes.encrypt(_nonce(header.nonce_prefix, i, i == n - 1), chunk, aad)
    return bytes(out)


def plain_size_for(cipher_len: int, header: Header) -> int:
    body = cipher_len - header.length
    unit = header.segment_size + TAG_SIZE
    if body < TAG_SIZE:
        raise IntegrityError("container too short")
    n = -(-body // unit)
    last_ct = body - (n - 1) * unit
    if last_ct < TAG_SIZE:
        raise IntegrityError("container truncated")
    return (n - 1) * header.segment_size + (last_ct - TAG_SIZE)


def iter_decrypt(path: Path, dek: bytes, *, object_key: str, tenant: str,
                 start: int = 0, end: Optional[int] = None,
                 expected_plain_size: Optional[int] = None) -> Iterator[bytes]:
    """Yield plaintext bytes [start, end] (inclusive). Every segment is
    authenticated BEFORE any of its bytes leave this function."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = parse_header(fh.read(17 + 255))
        plain = plain_size_for(size, head)
        if expected_plain_size is not None and plain != expected_plain_size:
            raise IntegrityError("size mismatch")
        if end is None or end >= plain:
            end = plain - 1
        if plain == 0:
            # Still authenticate the empty final segment.
            fh.seek(head.length)
            try:
                AESGCM(dek).decrypt(_nonce(head.nonce_prefix, 0, True), fh.read(TAG_SIZE),
                                    canon(object_key, tenant, head.raw))
            except Exception as exc:
                raise IntegrityError("empty final segment failed authentication") from exc
            return
        if start > end:
            return
        aad = canon(object_key, tenant, head.raw)
        aes = AESGCM(dek)
        seg = head.segment_size
        unit = seg + TAG_SIZE
        n = -(-plain // seg)
        first, last_i = start // seg, end // seg
        for i in range(first, last_i + 1):
            fh.seek(head.length + i * unit)
            ct = fh.read(unit)
            try:
                pt = aes.decrypt(_nonce(head.nonce_prefix, i, i == n - 1), ct, aad)
            except Exception as exc:  # InvalidTag
                raise IntegrityError(f"segment {i} failed authentication") from exc
            lo = start - i * seg if i == first else 0
            hi = end - i * seg + 1 if i == last_i else len(pt)
            yield pt[lo:hi]


def decrypt_all(path: Path, dek: bytes, *, object_key: str, tenant: str) -> bytes:
    return b"".join(iter_decrypt(path, dek, object_key=object_key, tenant=tenant))


# --------------------------------------------------------------------------
# Filename (own HKDF subkey, own nonce, own AAD — never the segment nonce space)
# --------------------------------------------------------------------------

def _filename_key(dek: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"aose-filename-v1").derive(dek)


def encrypt_filename(name: str, dek: bytes, *, object_key: str, tenant: str) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_filename_key(dek)).encrypt(nonce, (name or "").encode("utf-8"), canon(object_key, tenant, b"filename"))
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_filename(blob: str, dek: bytes, *, object_key: str, tenant: str) -> str:
    raw = base64.b64decode(blob)
    try:
        pt = AESGCM(_filename_key(dek)).decrypt(raw[:12], raw[12:], canon(object_key, tenant, b"filename"))
    except Exception as exc:
        raise IntegrityError("filename failed authentication") from exc
    return pt.decode("utf-8")


# --------------------------------------------------------------------------
# Recovery wrap (X25519 ephemeral ECDH + HKDF-SHA256 + AES-256-GCM)
# --------------------------------------------------------------------------

def recovery_kid_for(public_raw: bytes) -> str:
    return "rcv-" + hashlib.sha256(public_raw).hexdigest()[:16]


def _recovery_key(shared: bytes, eph_pub: bytes, rcpt_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + rcpt_pub,
                info=b"aose-recovery-v1").derive(shared)


def recovery_wrap(dek: bytes, public_raw: bytes, *, object_key: str, tenant: str) -> Tuple[str, bytes]:
    rcpt = X25519PublicKey.from_public_bytes(public_raw)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _recovery_key(eph.exchange(rcpt), eph_pub, public_raw)
    rkid = recovery_kid_for(public_raw)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, dek, canon(object_key, tenant, rkid))
    return rkid, eph_pub + nonce + ct


def recovery_unwrap(blob: bytes, private_raw: bytes, *, object_key: str, tenant: str, recovery_kid: str) -> bytes:
    priv = X25519PrivateKey.from_private_bytes(private_raw)
    rcpt_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if recovery_kid_for(rcpt_pub) != recovery_kid:
        raise EncryptionError("recovery key does not match this object")
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    key = _recovery_key(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, rcpt_pub)
    try:
        return AESGCM(key).decrypt(nonce, ct, canon(object_key, tenant, recovery_kid))
    except Exception as exc:
        raise IntegrityError("recovery wrap failed authentication") from exc


def generate_recovery_keypair() -> Tuple[bytes, bytes]:
    priv = X25519PrivateKey.generate()
    return (priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
            priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


# --------------------------------------------------------------------------
# KMS
# --------------------------------------------------------------------------

def wrap_aad(object_key: str, tenant: str) -> bytes:
    return canon(object_key, tenant, b"dek")


class KeyWrapper(abc.ABC):
    """Wraps/unwraps a 32-byte DEK. The master key never leaves the backend."""

    @property
    @abc.abstractmethod
    def kid(self) -> str: ...

    @abc.abstractmethod
    def wrap(self, dek: bytes, aad: bytes) -> bytes: ...

    @abc.abstractmethod
    def unwrap(self, kid: str, blob: bytes, aad: bytes) -> Tuple[bytes, Optional[bytes]]:
        """Return (dek, rewrapped_blob_or_None). A KMS that re-encrypts under its
        newest key version on decrypt hands the new blob back for a lazy rewrap."""


class LocalTestKeyWrapper(KeyWrapper):
    """AES-GCM with a local key. For tests and dev instances only; refuses to
    load unless STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND=1 (never set in prod)."""

    def __init__(self, key: bytes, kid: str = "test:local"):
        if len(key) != 32:
            raise EncryptionError("test key must be 32 bytes")
        self._aes = AESGCM(key)
        self._kid = kid

    @property
    def kid(self) -> str:
        return self._kid

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aes.encrypt(nonce, dek, aad)

    def unwrap(self, kid: str, blob: bytes, aad: bytes) -> Tuple[bytes, Optional[bytes]]:
        if kid != self._kid:
            raise EncryptionUnavailable(f"no key for kid {kid}")
        try:
            return self._aes.decrypt(blob[:12], blob[12:], aad), None
        except Exception as exc:
            raise IntegrityError("wrapped DEK failed authentication") from exc


class ScalewayKeyWrapper(KeyWrapper):
    """Scaleway Key Manager v1alpha1 over plain HTTPS (no SDK dependency, the
    API is alpha; this class is the whole surface we depend on).

    Credentials: the IAM application's secret key is read from the systemd
    credential `storage-kms-token` ($CREDENTIALS_DIRECTORY, LoadCredential=),
    never from .env. Without it every call raises EncryptionUnavailable (503).
    """

    API = "https://api.scaleway.com/key-manager/v1alpha1"

    def __init__(self, key_id: str, region: str, token_path: Optional[Path], timeout: float = 5.0):
        self.key_id = key_id
        self.region = region
        self.token_path = token_path
        self.timeout = timeout

    @property
    def kid(self) -> str:
        return f"scw:{self.region}:{self.key_id}"

    def _token(self) -> str:
        if not self.token_path or not self.token_path.is_file():
            raise EncryptionUnavailable("KMS credential not present")
        tok = self.token_path.read_text().strip()
        if not tok:
            raise EncryptionUnavailable("KMS credential empty")
        return tok

    def _call(self, key_id: str, op: str, body: dict) -> dict:
        import httpx
        try:
            r = httpx.post(f"{self.API}/regions/{self.region}/keys/{key_id}/{op}", json=body,
                           headers={"X-Auth-Token": self._token()}, timeout=self.timeout)
        except EncryptionUnavailable:
            raise
        except Exception as exc:  # network, DNS, TLS — no detail with secrets in it
            raise EncryptionUnavailable(f"KMS unreachable ({type(exc).__name__})") from None
        if r.status_code >= 500 or r.status_code in (401, 403, 429):
            raise EncryptionUnavailable(f"KMS answered {r.status_code}")
        if r.status_code != 200:
            raise EncryptionError(f"KMS {op} failed with {r.status_code}")
        return r.json()

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        res = self._call(self.key_id, "encrypt", {"plaintext": base64.b64encode(dek).decode(),
                                                  "associated_data": base64.b64encode(aad).decode()})
        return base64.b64decode(res["ciphertext"])

    def unwrap(self, kid: str, blob: bytes, aad: bytes) -> Tuple[bytes, Optional[bytes]]:
        parts = kid.split(":", 2)
        if len(parts) != 3 or parts[0] != "scw" or parts[1] != self.region:
            raise EncryptionUnavailable(f"no adapter for kid {kid}")
        res = self._call(parts[2], "decrypt", {"ciphertext": base64.b64encode(blob).decode(),
                                               "associated_data": base64.b64encode(aad).decode()})
        dek = base64.b64decode(res["plaintext"])
        new = res.get("ciphertext")
        return dek, (base64.b64decode(new) if new else None)


@dataclass
class EncryptionConfig:
    wrapper: Optional[KeyWrapper]
    recovery_public: Optional[bytes]

    @property
    def ready(self) -> bool:
        return self.wrapper is not None and self.recovery_public is not None


def load_config() -> EncryptionConfig:
    """Read the backend from the environment. Never raises: a missing piece
    just means encrypted uploads answer 503 encryption_key_unavailable."""
    backend = (os.getenv("STORAGE_ENCRYPTION_BACKEND") or "").strip().lower()
    wrapper: Optional[KeyWrapper] = None
    if backend == "scaleway":
        key_id = os.getenv("STORAGE_ENCRYPTION_SCW_KEY_ID", "").strip()
        region = os.getenv("STORAGE_ENCRYPTION_SCW_REGION", "fr-par").strip()
        cred_dir = os.getenv("CREDENTIALS_DIRECTORY")
        token_path = Path(cred_dir) / "storage-kms-token" if cred_dir else None
        if key_id:
            wrapper = ScalewayKeyWrapper(key_id, region, token_path)
    elif backend == "test" and os.getenv("STORAGE_ENCRYPTION_ALLOW_TEST_BACKEND") == "1":
        raw = os.getenv("STORAGE_ENCRYPTION_TEST_KEY", "")
        try:
            key = base64.b64decode(raw)
            wrapper = LocalTestKeyWrapper(key)
        except Exception:
            wrapper = None
    pub: Optional[bytes] = None
    raw_pub = os.getenv("STORAGE_ENCRYPTION_RECOVERY_PUBKEY", "").strip()
    if raw_pub:
        try:
            pub = base64.b64decode(raw_pub)
            X25519PublicKey.from_public_bytes(pub)
        except Exception:
            pub = None
    return EncryptionConfig(wrapper, pub)


# --------------------------------------------------------------------------
# DEK cache (RAM only, 5 min; the unit runs with MemorySwapMax=0, LimitCORE=0)
# --------------------------------------------------------------------------

class _DekCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict = {}

    def get(self, object_key: str) -> Optional[bytes]:
        with self._lock:
            hit = self._d.get(object_key)
            if not hit:
                return None
            dek, exp = hit
            if exp < time.monotonic():
                self._d.pop(object_key, None)
                return None
            return dek

    def put(self, object_key: str, dek: bytes) -> None:
        with self._lock:
            if len(self._d) >= DEK_CACHE_MAX:
                oldest = min(self._d, key=lambda k: self._d[k][1])
                self._d.pop(oldest, None)
            self._d[object_key] = (dek, time.monotonic() + DEK_CACHE_TTL_SECONDS)

    def drop(self, object_key: str) -> None:
        with self._lock:
            self._d.pop(object_key, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


DEK_CACHE = _DekCache()


# --------------------------------------------------------------------------
# High-level helpers used by service/routes
# --------------------------------------------------------------------------

@dataclass
class SealedObject:
    object_key: str
    ciphertext: bytes
    enc_kid: str
    enc_wrapped_dek: bytes
    enc_recovery_kid: str
    enc_wrapped_dek_recovery: bytes
    enc_filename: str
    plain_size: int


def seal(data: bytes, original_filename: str, *, tenant: str, config: Optional[EncryptionConfig] = None,
         object_key: Optional[str] = None) -> SealedObject:
    """Encrypt a new object (always a fresh DEK)."""
    cfg = config or load_config()
    if not cfg.ready:
        raise EncryptionUnavailable("encryption not configured (KMS and recovery key required)")
    key = object_key or new_object_key()
    dek = secrets.token_bytes(DEK_SIZE)
    wrapped = cfg.wrapper.wrap(dek, wrap_aad(key, tenant))
    rkid, rwrapped = recovery_wrap(dek, cfg.recovery_public, object_key=key, tenant=tenant)
    ct = encrypt_bytes(data, dek, object_key=key, tenant=tenant, kid=cfg.wrapper.kid)
    fn = encrypt_filename(original_filename or "", dek, object_key=key, tenant=tenant)
    return SealedObject(key, ct, cfg.wrapper.kid, wrapped, rkid, rwrapped, fn, len(data))


def unwrap_dek(obj, config: Optional[EncryptionConfig] = None) -> Tuple[bytes, Optional[bytes]]:
    """DEK of a stored object, via the RAM cache. Returns (dek, rewrapped_or_None)."""
    cached = DEK_CACHE.get(obj.object_key)
    if cached is not None:
        return cached, None
    cfg = config or load_config()
    if cfg.wrapper is None:
        raise EncryptionUnavailable("KMS not configured")
    dek, rewrapped = cfg.wrapper.unwrap(obj.enc_kid, obj.enc_wrapped_dek, wrap_aad(obj.object_key, obj.tenant_id or "arkturian"))
    if len(dek) != DEK_SIZE:
        raise IntegrityError("unwrapped DEK has wrong size")
    DEK_CACHE.put(obj.object_key, dek)
    return dek, rewrapped


def is_encrypted(obj) -> bool:
    return bool(getattr(obj, "encrypted", False))
