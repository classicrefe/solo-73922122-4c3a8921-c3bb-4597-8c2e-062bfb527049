"""SQLite helpers for the citation statistics service.

The service deliberately keeps statistics *derived* from the citation
registry instead of storing a cached aggregate.  Every write uses
``BEGIN IMMEDIATE`` and every read that needs more than one statement uses a
read transaction, so readers in WAL mode see a single, consistent snapshot of
the retraction flags and the citation registrations.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    doi           TEXT PRIMARY KEY,
    is_group_work INTEGER NOT NULL DEFAULT 0 CHECK (is_group_work IN (0, 1)),
    retracted     INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0, 1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    retracted_at  TEXT
);

CREATE TABLE IF NOT EXISTS citations (
    source_id  TEXT NOT NULL,
    citing_doi TEXT NOT NULL REFERENCES papers(doi) ON DELETE CASCADE,
    cited_doi  TEXT NOT NULL REFERENCES papers(doi) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, citing_doi, cited_doi)
);

CREATE INDEX IF NOT EXISTS idx_citations_pair
    ON citations (citing_doi, cited_doi);
CREATE INDEX IF NOT EXISTS idx_citations_cited
    ON citations (cited_doi);
CREATE INDEX IF NOT EXISTS idx_citations_citing
    ON citations (citing_doi);

-- First-author knowledge per registered DOI.  Exactly one shape is stored:
-- a confirmed ``author_id``, or two distinct candidates
-- (``candidate_a``/``candidate_b``) while verification is pending.  A DOI
-- with no row at all has no author data yet.
CREATE TABLE IF NOT EXISTS first_authors (
    doi         TEXT PRIMARY KEY REFERENCES papers(doi) ON DELETE CASCADE,
    author_id   TEXT,
    candidate_a TEXT,
    candidate_b TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (
        (author_id IS NOT NULL AND candidate_a IS NULL AND candidate_b IS NULL)
        OR (author_id IS NULL AND candidate_a IS NOT NULL AND candidate_b IS NOT NULL)
    )
);

-- Append-only provenance log.  Statistics never read this table; it exists so
-- that submitted corrections remain auditable even after a registration is
-- withdrawn.
CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    source_id  TEXT,
    doi        TEXT,
    citing_doi TEXT,
    cited_doi  TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_doi ON events (doi);
CREATE INDEX IF NOT EXISTS idx_events_pair ON events (citing_doi, cited_doi);
CREATE INDEX IF NOT EXISTS idx_events_source ON events (source_id);
"""


def utcnow() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a connection configured for concurrent FastAPI requests."""

    conn = sqlite3.connect(
        str(database_path),
        timeout=30.0,
        isolation_level=None,  # we issue BEGIN/COMMIT ourselves
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_database(database_path: str | Path) -> None:
    """Create the database file/directory and the schema if needed."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextlib.contextmanager
def write_transaction(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Run a write transaction, acquiring the database write lock up front.

    ``BEGIN IMMEDIATE`` serialises writers.  Either every statement in the
    block commits or none of them do; in particular a status change and the
    provenance event associated with it are committed together.
    """

    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def read_transaction(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Run a read transaction so multi-statement reads share one snapshot."""

    conn = connect(database_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        conn.close()


def record_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    source_id: str | None = None,
    doi: str | None = None,
    citing_doi: str | None = None,
    cited_doi: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events
            (event_type, source_id, doi, citing_doi, cited_doi, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_type, source_id, doi, citing_doi, cited_doi, detail, utcnow()),
    )
