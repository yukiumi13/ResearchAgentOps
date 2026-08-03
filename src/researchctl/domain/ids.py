from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def new_id(kind: str, *, now: datetime | None = None) -> str:
    if not _KIND_RE.fullmatch(kind):
        raise ValueError("ID kind must match ^[a-z][a-z0-9_]*$")
    source = now or datetime.now(UTC)
    if source.tzinfo is None or source.utcoffset() is None:
        raise ValueError("ID timestamp must be timezone aware")
    timestamp = source.astimezone(UTC)
    time_part = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}_{time_part}_{secrets.token_hex(12)}"
