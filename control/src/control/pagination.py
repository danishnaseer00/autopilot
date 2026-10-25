"""Opaque, stable cursors (SPEC-36 §2, AC-6).

Offset pagination skips and duplicates rows when deployments arrive between
pages. The cursor stores the last `(created_at, id)` pair, which is a total
order because ids are unique, and the query uses a row comparison.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from control.errors import ApiProblem


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return 25
    return max(1, min(int(limit), 100))


def encode_cursor(created_at: datetime, row_id: str | uuid.UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(row_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw)
        created_at = datetime.fromisoformat(payload["created_at"])
        row_id = uuid.UUID(payload["id"])
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is a 422
        raise ApiProblem(
            422,
            "validation_failed",
            "cursor is not valid",
            fields={"cursor": "invalid cursor"},
        ) from exc
    return created_at, row_id


def next_cursor(rows: list, limit: int) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_cursor(last["created_at"], last["id"])
