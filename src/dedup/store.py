"""SQLite-backed incremental cache for exact hashes and fingerprints."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Self

from .models import FileRecord, Fingerprint
from .util import utc_now_iso

SCHEMA_VERSION = 1
CACHE_VERSION = 1


class CacheStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._initialize()

    def _initialize(self) -> None:
        connection = self._connection
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                adapter TEXT NOT NULL,
                exact_hash TEXT NOT NULL,
                fingerprints TEXT NOT NULL DEFAULT '{}',
                fp_status TEXT NOT NULL DEFAULT 'none',
                fp_error TEXT,
                fp_sig TEXT NOT NULL DEFAULT '',
                cache_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_exact_hash ON files(exact_hash);
            """
        )
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        elif version != SCHEMA_VERSION:
            raise RuntimeError(
                f"cache schema version {version} is not supported by this build "
                f"(expected {SCHEMA_VERSION}); use a new database file"
            )

    def get(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        adapter: str,
        *,
        require_fingerprint: bool,
        expected_kinds: tuple[str, ...] = (),
        fp_sig: str = "",
    ) -> FileRecord | None:
        row = self._connection.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return None
        if (
            int(row["size"]) != int(size)
            or int(row["mtime_ns"]) != int(mtime_ns)
            or row["adapter"] != adapter
            or int(row["cache_version"]) != CACHE_VERSION
        ):
            return None
        if require_fingerprint:
            if row["fp_sig"] != fp_sig:
                return None
            status = row["fp_status"]
            if status == "none":
                return None
            if status == "ok":
                fingerprints = self._decode_fingerprints(row["fingerprints"])
                if any(kind not in fingerprints for kind in expected_kinds):
                    return None
            else:
                fingerprints = self._decode_fingerprints(row["fingerprints"])
        else:
            fingerprints = self._decode_fingerprints(row["fingerprints"])
        return FileRecord(
            path=row["path"],
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            adapter=row["adapter"],
            exact_hash=row["exact_hash"],
            fingerprints=fingerprints,
            fp_status=row["fp_status"],
            fp_error=row["fp_error"],
            fp_sig=row["fp_sig"],
            from_cache=True,
        )

    def get_exact_hash(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        adapter: str,
    ) -> str | None:
        """Return a cached exact hash even when the fingerprint cache is stale."""
        row = self._connection.execute(
            "SELECT size, mtime_ns, adapter, exact_hash, cache_version FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        if (
            int(row["size"]) != int(size)
            or int(row["mtime_ns"]) != int(mtime_ns)
            or row["adapter"] != adapter
            or int(row["cache_version"]) != CACHE_VERSION
        ):
            return None
        return str(row["exact_hash"])

    def put(self, record: FileRecord, *, commit: bool = False) -> None:
        payload = json.dumps(
            {kind: fingerprint.to_dict() for kind, fingerprint in record.fingerprints.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._connection.execute(
            """
            INSERT INTO files (
                path, size, mtime_ns, adapter, exact_hash, fingerprints,
                fp_status, fp_error, fp_sig, cache_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                adapter=excluded.adapter,
                exact_hash=excluded.exact_hash,
                fingerprints=excluded.fingerprints,
                fp_status=excluded.fp_status,
                fp_error=excluded.fp_error,
                fp_sig=excluded.fp_sig,
                cache_version=excluded.cache_version,
                updated_at=excluded.updated_at
            """,
            (
                record.path,
                record.size,
                record.mtime_ns,
                record.adapter,
                record.exact_hash,
                payload,
                record.fp_status,
                record.fp_error,
                record.fp_sig,
                CACHE_VERSION,
                utc_now_iso(),
            ),
        )
        if commit:
            self._connection.commit()

    def commit(self) -> None:
        self._connection.commit()

    def stats(self) -> dict[str, Any]:
        total = self._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        rows = self._connection.execute(
            "SELECT fp_status, COUNT(*) AS count FROM files GROUP BY fp_status"
        ).fetchall()
        by_status = {row["fp_status"]: int(row["count"]) for row in rows}
        return {
            "path": self.path,
            "files": int(total),
            "by_fingerprint_status": by_status,
        }

    def prune_missing(self) -> int:
        rows = self._connection.execute("SELECT path FROM files").fetchall()
        missing = [row["path"] for row in rows if not Path(row["path"]).is_file()]
        if missing:
            self._connection.executemany(
                "DELETE FROM files WHERE path = ?", ((path,) for path in missing)
            )
            self._connection.commit()
        return len(missing)

    def clear(self) -> int:
        count = int(self._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        self._connection.execute("DELETE FROM files")
        self._connection.commit()
        return count

    @staticmethod
    def _decode_fingerprints(payload: str) -> dict[str, Fingerprint]:
        try:
            raw = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return {}
        fingerprints: dict[str, Fingerprint] = {}
        for kind, data in raw.items():
            try:
                fingerprints[kind] = Fingerprint.from_dict(data)
            except (KeyError, TypeError, ValueError):
                continue
        return fingerprints

    def close(self) -> None:
        try:
            self._connection.commit()
        finally:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
