"""Small HMAC-signed, expiring tokens (stdlib only).

Used for upload tokens: the API signs "user U may finish uploading object K with hash
H" and the browser hands it back later, so no server-side state is needed in between.
The signature makes the payload tamper-proof; it is *not* encrypted, so never put
secrets in it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import cast

from core.domain.errors import ValidationFailed

_INVALID = "The upload link is invalid or has expired. Start the upload again."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict[str, object], *, secret: bytes, purpose: str, expires_at: datetime) -> str:
    body = dict(payload, _p=purpose, _exp=int(expires_at.timestamp()))
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(mac)}"


def verify(token: str, *, secret: bytes, purpose: str, now: datetime) -> dict[str, object]:
    """Return the payload, or raise ValidationFailed if forged, expired or misused."""
    try:
        raw_b64, mac_b64 = token.split(".", 1)
        raw, mac = _unb64(raw_b64), _unb64(mac_b64)
    except (ValueError, TypeError) as e:
        raise ValidationFailed(_INVALID) from e
    if not hmac.compare_digest(mac, hmac.new(secret, raw, hashlib.sha256).digest()):
        raise ValidationFailed(_INVALID)
    try:
        parsed: object = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationFailed(_INVALID) from e
    if not isinstance(parsed, dict):
        raise ValidationFailed(_INVALID)
    body = cast(dict[str, object], parsed)
    exp = body.get("_exp")
    if body.get("_p") != purpose or not isinstance(exp, int) or exp < int(now.timestamp()):
        raise ValidationFailed(_INVALID)
    return {k: v for k, v in body.items() if k not in {"_p", "_exp"}}
