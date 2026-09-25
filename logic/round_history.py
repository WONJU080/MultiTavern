"""Durable player-safe round history: an append-only JSONL archive.

The engine keeps a bounded in-memory tail for reconnect snapshots; the full
history lives here, one JSON record per line, so long-running rooms never
lose earlier rounds. Records contain only public data: actions, narratives,
per-player outcomes and public dice. Hidden rolls and private guidance never
reach this archive.
"""

import asyncio
import json
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

HISTORY_DIR = Path(".rooms")
PAGE_SIZE = 30


class RoundHistoryStore:
    """Serialize one room's player-safe round records to a JSONL file."""

    def __init__(self) -> None:
        """Initialize the store with a write-serialization lock."""
        self._io_lock = asyncio.Lock()

    @staticmethod
    def _path(compact_code: str) -> Path:
        return HISTORY_DIR / f"history-{compact_code}.jsonl"

    async def append(self, compact_code: str, record: dict[str, object]) -> None:
        """Append a round record to the archive, tolerating cancellation."""
        if not compact_code:
            return
        line = json.dumps(record, ensure_ascii=False)
        async with self._io_lock:
            work = asyncio.create_task(asyncio.to_thread(self._append_line, compact_code, line))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise

    @staticmethod
    def _append_line(compact_code: str, line: str) -> None:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with RoundHistoryStore._path(compact_code).open("a", encoding="utf-8") as history_file:
            history_file.write(line + "\n")

    async def load_before(
        self, compact_code: str, before_round: int | None, limit: int = PAGE_SIZE
    ) -> tuple[list[dict[str, object]], bool]:
        """Return up to `limit` rounds before `before_round`, newest page first."""
        if not compact_code:
            return [], False
        records = await asyncio.to_thread(self._read_records, compact_code)
        if before_round is not None:
            records = [
                record
                for record in records
                if int(record.get("round_number", 0) or 0) < before_round
            ]
        has_more = len(records) > limit
        page = records[-limit:] if has_more else list(records)
        LOGGER.info(
            "History page room=%s before=%s rounds=%d has_more=%s",
            compact_code,
            before_round,
            len(page),
            has_more,
        )
        return page, has_more

    @staticmethod
    def _read_records(compact_code: str) -> list[dict[str, object]]:
        """Read every well-formed record from the archive, oldest first."""
        records: list[dict[str, object]] = []
        path = RoundHistoryStore._path(compact_code)
        if not path.exists():
            return records
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                LOGGER.warning("Skipping malformed history line in %s", path.name)
                continue
            if isinstance(record, dict) and "round_number" in record:
                records.append(record)
        return records

    @staticmethod
    def delete(compact_code: str) -> None:
        """Remove a room's history archive after the room closes."""
        try:
            RoundHistoryStore._path(compact_code).unlink(missing_ok=True)
        except OSError:
            pass
