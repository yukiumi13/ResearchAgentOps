"""The one shape every document check reports in.

Both policy versions, and every service that lints on their behalf, produce the
same finding record. It lives here so a shared checker can build one without
depending on either version's service module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DocumentFinding:
    kind: Literal["warning", "invalid"]
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
