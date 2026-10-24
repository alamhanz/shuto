"""SQLite persistence for batches, photos, rejections, LLM calls, events, jobs and settings.

Every method opens its own short-lived connection (WAL, foreign keys, busy timeout), so a
`Store` is safe to share between threads (web server, background worker) and processes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shuto.core.models import ALLOWED, REASON_CODES, Batch, Job, LLMCall, Photo, Rejection, Status
from shuto.store import schema

BUSY_TIMEOUT_S = 30  # writers wait this long for a lock (slow CI disks, antivirus)

# Photo attribute -> column, for JSON-encoded columns.
_JSON_FIELDS = {
    "cull": "cull_json",
    "metadata": "metadata_json",
    "detected": "detected_json",
    "guardrail": "guardrail_json",
    "filters": "filters_json",
    "draft": "draft_json",
    "release_ids": "release_ids",
}
_PLAIN_FIELDS = {
    "batch_id",
    "dhash",
    "src_path",
    "orig_name",
    "width",
    "height",
    "captured_at",
    "camera",
    "has_gps",
    "near_dup_group",
    "metadata_source",
    "submit_name",
    "prepared_path",
    "uploaded_at",
    "exported_at",
    "export_file",
    "submitted_at",
}
_JOB_FIELDS = {"kind", "batch_id", "state", "done", "total", "error", "started_at", "finished_at"}


class InvalidTransition(ValueError):
    """Raised when a status change is not an allowed edge of the status machine."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _row_to_photo(row: sqlite3.Row) -> Photo:
    d = dict(row)
    for attr, col in _JSON_FIELDS.items():
        d[attr] = _loads(d.pop(col))
    d["release_ids"] = d["release_ids"] or []
    d["has_gps"] = bool(d["has_gps"])
    d["status"] = Status(d["status"])
    return Photo(**d)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    # -- plumbing -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as conn:
            yield conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One immediate (write-locking) transaction; commits on success."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def migrate(self) -> None:
        with closing(self._connect()) as conn:
            self._backup_before_upgrade(conn)
            schema.migrate(conn, self.path)

    def _backup_before_upgrade(self, conn: sqlite3.Connection) -> None:
        """Copy an existing database once to `<name>.v<old>.bak` before it moves to a newer schema.

        An older shuto refuses a newer database, so this copy is the way back. New and
        in-memory databases are skipped, an existing backup is never overwritten, and a failed
        copy stops the migration.
        """
        if str(self.path) == ":memory:" or not self.path.is_file():
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version < schema.SCHEMA_VERSION:
            return
        backup = self.path.with_name(f"{self.path.name}.v{version}.bak")
        if backup.exists():
            return
        partial = backup.with_name(backup.name + ".part")
        try:
            partial.unlink(missing_ok=True)
            with closing(sqlite3.connect(partial)) as target:
                conn.backup(target)
            partial.replace(backup)
        except (OSError, sqlite3.Error) as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                f"Could not save a backup of your database before updating it ({backup}): {exc}. "
                "Nothing was changed. Free some disk space or check the folder, then try again."
            ) from exc

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        kind: str,
        *,
        photo_id: int | None = None,
        batch_id: str | None = None,
        detail: Any = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(photo_id, batch_id, kind, detail_json, at) VALUES (?,?,?,?,?)",
            (photo_id, batch_id, kind, _dumps(detail) if detail is not None else None, _now()),
        )

    # -- batches ------------------------------------------------------------------------

    def upsert_batch(
        self, batch_id: str, *, notes: str | None = None, source: str | None = None
    ) -> Batch:
        """Create the batch, or update notes/source when given. Returns the stored batch."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO batches(id, created_at, notes, source) VALUES (?,?,?,?)",
                (batch_id, _now(), notes or "", source),
            )
            if notes is not None:
                conn.execute("UPDATE batches SET notes=? WHERE id=?", (notes, batch_id))
            if source is not None:
                conn.execute("UPDATE batches SET source=? WHERE id=?", (source, batch_id))
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return Batch(**dict(row))

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return Batch(**dict(row)) if row else None

    def list_batches(self) -> list[tuple[Batch, dict[Status, int]]]:
        with self._read() as conn:
            batches = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, id DESC"
            ).fetchall()
            counts = conn.execute(
                "SELECT batch_id, status, COUNT(*) AS n FROM photos GROUP BY batch_id, status"
            ).fetchall()
        by_batch: dict[str, dict[Status, int]] = {}
        for r in counts:
            by_batch.setdefault(r["batch_id"], {})[Status(r["status"])] = r["n"]
        return [(Batch(**dict(b)), by_batch.get(b["id"], {})) for b in batches]

    # -- photos -------------------------------------------------------------------------

    def add_photo(self, photo: Photo | None = None, /, **fields: Any) -> int:
        """Insert a photo and return its id; on a sha256 conflict return the existing id.

        Pass a `Photo` or keyword fields (`sha256`, `src_path`, `orig_name` are required).
        """
        if photo is not None:
            data = {
                k: getattr(photo, k) for k in (*_PLAIN_FIELDS, *_JSON_FIELDS, "sha256", "status")
            }
            data = {k: v for k, v in data.items() if v is not None}
            data.update(fields)
        else:
            data = dict(fields)
        for req in ("sha256", "src_path", "orig_name"):
            if not data.get(req):
                raise ValueError(f"add_photo requires {req}")
        self._check_fields(data, allow={"sha256", "status"})
        now = _now()
        cols: dict[str, Any] = {
            "created_at": now,
            "updated_at": now,
            "status": Status(data.get("status") or Status.IMPORTED).value,
            "sha256": data["sha256"],
        }
        for k, v in data.items():
            if k in ("sha256", "status"):
                continue
            if k in _JSON_FIELDS:
                cols[_JSON_FIELDS[k]] = None if v is None else _dumps(v)
            elif k == "has_gps":
                cols[k] = int(bool(v))
            else:
                cols[k] = v
        with self._write() as conn:
            existing = conn.execute(
                "SELECT id FROM photos WHERE sha256=?", (cols["sha256"],)
            ).fetchone()
            if existing:
                return existing["id"]
            if cols.get("batch_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO batches(id, created_at, notes) VALUES (?,?,'')",
                    (cols["batch_id"], now),
                )
            names = ",".join(cols)
            marks = ",".join("?" * len(cols))
            cur = conn.execute(
                f"INSERT INTO photos({names}) VALUES ({marks})",
                tuple(cols.values()),
            )
            pid = cur.lastrowid
            self._event(
                conn,
                "imported",
                photo_id=pid,
                batch_id=cols.get("batch_id"),
                detail={"status": cols["status"]},
            )
        return pid

    @staticmethod
    def _check_fields(fields: dict[str, Any], allow: Iterable[str] = ()) -> None:
        known = _PLAIN_FIELDS | set(_JSON_FIELDS) | set(allow)
        unknown = set(fields) - known
        if unknown:
            raise ValueError(f"Unknown photo field(s): {', '.join(sorted(unknown))}")

    def get_photo(self, photo_id: int) -> Photo | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        return _row_to_photo(row) if row else None

    def find_by_sha(self, sha256: str) -> Photo | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM photos WHERE sha256=?", (sha256,)).fetchone()
        return _row_to_photo(row) if row else None

    def find_by_submit_name(self, submit_name: str) -> Photo | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE submit_name=?", (submit_name,)
            ).fetchone()
        return _row_to_photo(row) if row else None

    def list_photos(
        self,
        batch_id: str | None = None,
        statuses: Iterable[Status | str] | None = None,
    ) -> list[Photo]:
        sql = "SELECT * FROM photos"
        where: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            where.append("batch_id=?")
            params.append(batch_id)
        if statuses is not None:
            values = [Status(s).value for s in statuses]
            if not values:
                return []
            where.append(f"status IN ({','.join('?' * len(values))})")
            params.extend(values)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_photo(r) for r in rows]

    def update_photo(self, photo_id: int, **fields: Any) -> None:
        """Update non-status columns. JSON fields (cull, metadata, ...) take Python values."""
        if "status" in fields:
            raise ValueError("Use set_status() to change status")
        self._check_fields(fields)
        if not fields:
            return
        sets: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k in _JSON_FIELDS:
                sets.append(f"{_JSON_FIELDS[k]}=?")
                params.append(None if v is None else _dumps(v))
            elif k == "has_gps":
                sets.append("has_gps=?")
                params.append(int(bool(v)))
            else:
                sets.append(f"{k}=?")
                params.append(v)
        sets.append("updated_at=?")
        params.append(_now())
        params.append(photo_id)
        with self._write() as conn:
            cur = conn.execute(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", params)
            if cur.rowcount == 0:
                raise KeyError(f"No photo with id {photo_id}")

    def set_status(
        self,
        photo_id: int,
        new: Status | str,
        *,
        force: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Change status, validating against `ALLOWED` unless forced; always logs an event."""
        new = Status(new)
        with self._write() as conn:
            row = conn.execute(
                "SELECT status, batch_id FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No photo with id {photo_id}")
            old = Status(row["status"])
            allowed = new in ALLOWED[old]
            if not allowed and not force:
                raise InvalidTransition(f"Photo {photo_id}: {old.value} -> {new.value} not allowed")
            conn.execute(
                "UPDATE photos SET status=?, updated_at=? WHERE id=?",
                (new.value, _now(), photo_id),
            )
            info: dict[str, Any] = {"from": old.value, "to": new.value}
            if not allowed:
                info["forced"] = True
            if detail:
                info["detail"] = detail
            self._event(conn, "status", photo_id=photo_id, batch_id=row["batch_id"], detail=info)

    # -- rejections ---------------------------------------------------------------------

    def add_rejection(self, photo_id: int, reason_code: str, note: str | None = None) -> int:
        if reason_code not in REASON_CODES:
            raise ValueError(f"Unknown reason code: {reason_code}")
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO rejections(photo_id, reason_code, note, at) VALUES (?,?,?,?)",
                (photo_id, reason_code, note, _now()),
            )
            return cur.lastrowid

    def list_rejections(self, photo_id: int) -> list[Rejection]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM rejections WHERE photo_id=? ORDER BY id", (photo_id,)
            ).fetchall()
        return [Rejection(**dict(r)) for r in rows]

    def rejection_stats(self, since_days: int = 90) -> dict[str, int]:
        """Count of rejections per reason code in the last `since_days`, most frequent first."""
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT reason_code, COUNT(*) AS n FROM rejections WHERE at >= ? "
                "GROUP BY reason_code ORDER BY n DESC, reason_code",
                (cutoff,),
            ).fetchall()
        return {r["reason_code"]: r["n"] for r in rows}

    def recent_rejection_examples(self, limit: int = 3) -> list[dict[str, Any]]:
        """Latest rejections that carry a note, with the photo's title when known."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT r.reason_code, r.note, r.at, p.orig_name, p.metadata_json "
                "FROM rejections r JOIN photos p ON p.id = r.photo_id "
                "WHERE r.note IS NOT NULL AND r.note != '' ORDER BY r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            meta = _loads(r["metadata_json"]) or {}
            out.append(
                {
                    "reason_code": r["reason_code"],
                    "note": r["note"],
                    "at": r["at"],
                    "orig_name": r["orig_name"],
                    "title": meta.get("title") if isinstance(meta, dict) else None,
                }
            )
        return out

    # -- LLM usage ----------------------------------------------------------------------

    def log_llm_call(self, call: LLMCall) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO llm_calls(photo_id, provider, model, input_tokens, output_tokens, "
                "seconds, repaired, retries, ok, error, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    call.photo_id,
                    call.provider,
                    call.model,
                    call.input_tokens,
                    call.output_tokens,
                    call.seconds,
                    int(call.repaired),
                    call.retries,
                    int(call.ok),
                    call.error,
                    call.at or _now(),
                ),
            )
            return cur.lastrowid

    def usage_by_batch(self, batch_id: str) -> dict[str, float | int]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(c.input_tokens),0) AS input_tokens, "
                "COALESCE(SUM(c.output_tokens),0) AS output_tokens, "
                "COALESCE(SUM(c.seconds),0.0) AS seconds "
                "FROM llm_calls c JOIN photos p ON p.id = c.photo_id WHERE p.batch_id=?",
                (batch_id,),
            ).fetchone()
        return dict(row)

    def ai_times(self, batch_id: str) -> dict[int, tuple[float, int]]:
        """Per photo: (seconds, tries) of its latest successful describe call. A call's seconds
        already include its retries; tries is retries + 1."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT c.photo_id, c.seconds, c.retries FROM llm_calls c "
                "JOIN photos p ON p.id = c.photo_id WHERE p.batch_id=? AND c.ok=1 "
                "AND c.id = (SELECT MAX(id) FROM llm_calls WHERE photo_id=c.photo_id AND ok=1)",
                (batch_id,),
            ).fetchall()
        return {
            int(r["photo_id"]): (float(r["seconds"] or 0.0), int(r["retries"] or 0) + 1)
            for r in rows
        }

    # -- jobs ---------------------------------------------------------------------------

    def create_job(self, kind: str, batch_id: str | None = None, total: int = 0) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(kind, batch_id, state, done, total, started_at) "
                "VALUES (?,?,?,?,?,?)",
                (kind, batch_id, "queued", 0, total, _now()),
            )
            return cur.lastrowid

    def update_job(self, job_id: int, **fields: Any) -> None:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"Unknown job field(s): {', '.join(sorted(unknown))}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._write() as conn:
            cur = conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
            if cur.rowcount == 0:
                raise KeyError(f"No job with id {job_id}")

    def fail_unfinished_jobs(self, note: str) -> int:
        """Mark jobs left `queued`/`running` (the app was closed under them) as failed."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=? "
                "WHERE state IN ('queued','running')",
                (note, _now()),
            )
            return cur.rowcount

    def get_job(self, job_id: int) -> Job | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    # -- events and settings ------------------------------------------------------------

    def log_event(
        self,
        kind: str,
        *,
        photo_id: int | None = None,
        batch_id: str | None = None,
        detail: Any = None,
    ) -> None:
        with self._write() as conn:
            self._event(conn, kind, photo_id=photo_id, batch_id=batch_id, detail=detail)

    def events_for(self, photo_id: int) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE photo_id=? ORDER BY id", (photo_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "photo_id": r["photo_id"],
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "detail": _loads(r["detail_json"]),
                "at": r["at"],
            }
            for r in rows
        ]

    def batch_events(self, batch_id: str, kinds: Iterable[str]) -> list[dict[str, Any]]:
        """Events of one batch (by `batch_id`) of the given kinds, oldest first."""
        kinds = list(kinds)
        if not kinds:
            return []
        marks = ",".join("?" * len(kinds))
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM events WHERE batch_id=? AND kind IN ({marks}) ORDER BY id",
                (batch_id, *kinds),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "photo_id": r["photo_id"],
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "detail": _loads(r["detail_json"]),
                "at": r["at"],
            }
            for r in rows
        ]

    def status_times(self, batch_id: str) -> dict[int, dict[str, str]]:
        """Per photo: latest time it entered each status, as `{photo_id: {status: at}}`."""
        out: dict[int, dict[str, str]] = {}
        for ev in self.batch_events(batch_id, ["status"]):
            to = (ev["detail"] or {}).get("to")
            if ev["photo_id"] is not None and to:
                out.setdefault(ev["photo_id"], {})[to] = ev["at"]
        return out

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._read() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at",
                (key, _dumps(value), _now()),
            )
