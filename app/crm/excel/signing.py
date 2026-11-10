"""Signature for the workbook's hidden baseline (pure).

The signature is an integrity check, not authorisation (that is the admin
key): it detects a corrupted or hand-edited baseline and a file exported by a
different deployment, so a bad file is refused instead of merged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Iterable, Sequence

from app.crm.excel.schema import SCHEMA_VERSION

_DERIVATION_LABEL = b"crm-workbook-signing-v1"


class SigningUnavailable(RuntimeError):
    """Neither CRM_SIGNING_KEY nor ADMIN_API_KEY is configured."""


def derive_key(signing_key: str | None, admin_key: str | None) -> bytes:
    """Explicit CRM_SIGNING_KEY wins; otherwise derive one from ADMIN_API_KEY.

    The derivation keeps the admin key itself out of any file or digest.
    """
    if signing_key:
        return hmac.new(signing_key.encode("utf-8"), _DERIVATION_LABEL, hashlib.sha256).digest()
    if admin_key:
        return hmac.new(admin_key.encode("utf-8"), _DERIVATION_LABEL, hashlib.sha256).digest()
    raise SigningUnavailable("Set CRM_SIGNING_KEY or ADMIN_API_KEY to sign the CRM workbook")


def encode_row(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical(header: dict[str, str], rows: Iterable[Sequence[str]]) -> bytes:
    lines = [f"{key}={header[key]}" for key in sorted(header)]
    lines += ["\x1f".join(row) for row in sorted(rows)]
    return "\n".join(lines).encode("utf-8")


def sign(key: bytes, header: dict[str, str], rows: Iterable[Sequence[str]]) -> str:
    return hmac.new(key, _canonical(header, rows), hashlib.sha256).hexdigest()


def verify(key: bytes, header: dict[str, str], rows: Iterable[Sequence[str]], signature: str) -> bool:
    return hmac.compare_digest(sign(key, header, rows), str(signature or ""))


def make_header(export_id: str, generated_at: str, timezone: str) -> dict[str, str]:
    return {
        "schema_version": str(SCHEMA_VERSION),
        "export_id": export_id,
        "generated_at": generated_at,
        "timezone": timezone,
    }
