"""#1960 / #3969: recognise the retired public default master key without its value.

Until #1960 step 5, config.py fell back to a 9-character literal when API_KEY
was unset. That literal shipped in browser bundles and is public. The code
still has to recognise it (warn on start, deny it every privilege, count who
still sends it), but it no longer carries the value: only its SHA-256.
"""
import hashlib
import hmac

PUBLIC_DEFAULT_SHA256 = "43ba25cae1dea05aa4ac2b5ca908f4a91960a4f1630bee646421456f5187df29"


def is_public_default(value) -> bool:
    """True if `value` is the retired public default master key."""
    if not value or not isinstance(value, (str, bytes)):
        return False
    raw = value if isinstance(value, bytes) else value.encode()
    return hmac.compare_digest(hashlib.sha256(raw.strip()).hexdigest(), PUBLIC_DEFAULT_SHA256)
