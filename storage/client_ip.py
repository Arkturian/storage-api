"""Client-Adresse hinter nginx (Befund 137, #3072).

Das linke Element von X-Forwarded-For schreibt der Client selbst; nginx haengt
mit `$proxy_add_x_forwarded_for` nur an. Kopfzeilen gelten darum nur, wenn die
direkte Gegenstelle ein vertrauter Proxy ist (Loopback oder TRUSTED_PROXY_IPS).
Dann von rechts der erste nicht vertraute Eintrag, sonst X-Real-IP. Nur
gueltige IP-Adressen.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Optional


def _ip(wert: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(wert.strip().strip("[]")))
    except ValueError:
        return None


def _vertraut(ip: str) -> bool:
    if ipaddress.ip_address(ip).is_loopback:
        return True
    return ip in {_ip(x) for x in (os.getenv("TRUSTED_PROXY_IPS") or "").split(",") if x.strip()}


def client_ip(xff: str, real_ip: str, peer: Optional[str]) -> str:
    """Adresse des Aufrufers aus X-Forwarded-For, X-Real-IP und der Gegenstelle."""
    gegen = _ip(peer or "")
    if gegen is None or not _vertraut(gegen):
        return gegen or (peer or "")
    kette = [i for i in (_ip(x) for x in (xff or "").split(",")) if i]
    for ip in reversed(kette):
        if not _vertraut(ip):
            return ip
    real = _ip(real_ip or "")
    if real and not _vertraut(real):
        return real
    return kette[0] if kette else gegen
