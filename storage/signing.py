"""Short-lived signed read links for non-public media (Post 5235).

A renderer (WeasyPrint in markdown-api) fetches images without any identity.
Instead of giving it a key that reads everything, content-api asks
POST /storage/sign for URLs that open exactly the requested objects for a
few minutes. Who may see which image is decided before signing:

  * content-api decides who may export a post;
  * storage-api decides which objects belong to that post (media_grants,
    created only by the owner's real identity or a real admin) and that the
    signing key is enabled for the tenant.

The secret never leaves this process. Verification is stateless: an
HMAC over "<id>:<exp>:<kid>". Revoking every outstanding link means rotating
the active kid (drop the old one from STORAGE_SIGN_SECRETS).

Configuration (environment, both required, otherwise signing is off):
  STORAGE_SIGN_SECRETS     JSON object {"<kid>": "<secret>", ...}
  STORAGE_SIGN_ACTIVE_KID  kid used for new signatures
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Dict, Optional

MAX_TTL_SECONDS = 900
MAX_IDS_PER_REQUEST = 500
# Clock skew allowance on verification only; never extends the issued ttl.
_SKEW_SECONDS = 60

SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}:[A-Za-z0-9._-]{1,120}$")


def _secrets() -> Dict[str, str]:
    raw = os.getenv("STORAGE_SIGN_SECRETS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    # A short secret is a configuration error, not a weaker mode.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and len(v) >= 32}


def active_kid() -> Optional[str]:
    kid = os.getenv("STORAGE_SIGN_ACTIVE_KID", "").strip()
    return kid if kid and kid in _secrets() else None


def is_configured() -> bool:
    return active_kid() is not None


def _mac(secret: str, object_id: int, exp: int, kid: str) -> str:
    msg = f"{int(object_id)}:{int(exp)}:{kid}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def sign(object_id: int, ttl: int, now: Optional[float] = None) -> Dict[str, object]:
    """Return {exp, kid, sig} for one object. Caller checked ttl bounds."""
    kid = active_kid()
    if kid is None:
        raise RuntimeError("signing not configured")
    issued = int(now if now is not None else time.time())
    exp = issued + int(ttl)
    return {"exp": exp, "kid": kid, "sig": _mac(_secrets()[kid], object_id, exp, kid)}


def verify(object_id: int, exp: Optional[int], kid: Optional[str], sig: Optional[str],
           now: Optional[float] = None) -> bool:
    """True only for an unexpired signature from a known kid for this id."""
    if exp is None or not kid or not sig:
        return False
    secret = _secrets().get(kid)
    if secret is None:
        return False
    t = int(now if now is not None else time.time())
    if int(exp) < t:
        return False
    # Reject links that claim a lifetime longer than we ever issue — a leaked
    # secret must not mint week-long URLs that this check would honour.
    if int(exp) > t + MAX_TTL_SECONDS + _SKEW_SECONDS:
        return False
    return hmac.compare_digest(_mac(secret, object_id, int(exp), kid), str(sig))
