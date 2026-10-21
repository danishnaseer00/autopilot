"""Global package denylist for SPEC-10 R-9.

There is no denylist table in the ratified schema. The production shape is
therefore an explicitly supplied, globally shared source: a JSON file mounted
into every registry-proxy replica, or an equivalent config service behind the
``Denylist`` protocol. A file update is observed on the next request, so adding
an advisory does not wait for a process restart.

The persistence gap is recorded as a DECISIONS proposal; this module does not
invent a table or column.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Protocol

from buildplane.supplychain.models import Advisory


class Denylist(Protocol):
    async def advisory_for(self, package: str, version: str) -> Advisory | None: ...


class InMemoryDenylist:
    def __init__(self, entries: Iterable[Advisory] = ()) -> None:
        self._entries: dict[tuple[str, str], Advisory] = {}
        for entry in entries:
            self.deny(entry)

    def deny(self, advisory: Advisory) -> None:
        self._entries[(advisory.package, advisory.version)] = advisory

    def remove(self, package: str, version: str) -> None:
        self._entries.pop((package, version), None)

    async def advisory_for(self, package: str, version: str) -> Advisory | None:
        return self._entries.get((package, version))


class JsonFileDenylist:
    """A shared file, reloaded when its mtime changes.

    The file format is intentionally small and auditable::

        {"denylist": [{"advisory_id": "...", "package": "...",
                       "version": "...", "summary": "...",
                       "incident_id": "..."}]}
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime_ns: int | None = None
        self._entries: dict[tuple[str, str], Advisory] = {}

    async def advisory_for(self, package: str, version: str) -> Advisory | None:
        with self._lock:
            self._load_if_changed()
            return self._entries.get((package, version))

    def deny(self, advisory: Advisory) -> None:
        with self._lock:
            self._load_if_changed()
            self._entries[(advisory.package, advisory.version)] = advisory
            self._write()

    def replace(self, entries: Iterable[Advisory]) -> None:
        with self._lock:
            self._entries = {
                (entry.package, entry.version): entry for entry in entries
            }
            self._write()

    def _load_if_changed(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._entries = {}
            self._mtime_ns = None
            return
        if self._mtime_ns == stat.st_mtime_ns:
            return
        document = json.loads(self.path.read_text(encoding="utf-8"))
        raw = document.get("denylist", []) if isinstance(document, dict) else None
        if not isinstance(raw, list):
            raise ValueError("denylist file must contain a 'denylist' list")
        entries: dict[tuple[str, str], Advisory] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("denylist entries must be objects")
            advisory = Advisory(
                advisory_id=str(item["advisory_id"]),
                package=str(item["package"]),
                version=str(item["version"]),
                summary=str(item["summary"]),
                incident_id=(
                    str(item["incident_id"])
                    if item.get("incident_id") is not None
                    else None
                ),
            )
            entries[(advisory.package, advisory.version)] = advisory
        self._entries = entries
        self._mtime_ns = stat.st_mtime_ns

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "denylist": [
                {
                    "advisory_id": entry.advisory_id,
                    "package": entry.package,
                    "version": entry.version,
                    "summary": entry.summary,
                    "incident_id": entry.incident_id,
                }
                for entry in sorted(
                    self._entries.values(),
                    key=lambda item: (item.package, item.version, item.advisory_id),
                )
            ]
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        self._mtime_ns = self.path.stat().st_mtime_ns
