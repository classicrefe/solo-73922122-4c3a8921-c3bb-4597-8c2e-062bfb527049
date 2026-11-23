"""Service layer: citation registry, retraction lifecycle, and statistics."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .database import read_transaction, record_event, utcnow, write_transaction
from .models import (
    CitationOut,
    CitationPair,
    CitationRegistrationResult,
    PaperOut,
    RetractionImpact,
    StatsOut,
    StatusChangeResult,
    WithdrawResult,
    WorkSummary,
)

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConflictError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _paper_from_row(row: sqlite3.Row) -> PaperOut:
    return PaperOut(
        doi=row["doi"],
        is_group_work=bool(row["is_group_work"]),
        retracted=bool(row["retracted"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        retracted_at=row["retracted_at"],
    )


def ensure_paper(
    conn: sqlite3.Connection,
    doi: str,
    *,
    is_group_work: bool = False,
    event_type: str = "paper.auto_registered",
) -> bool:
    """Register a DOI if it is not known yet. Returns True when inserted."""

    existing = conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone()
    if existing is not None:
        return False
    now = utcnow()
    conn.execute(
        """
        INSERT INTO papers (doi, is_group_work, retracted, created_at, updated_at)
        VALUES (?, ?, 0, ?, ?)
        """,
        (doi, 1 if is_group_work else 0, now, now),
    )
    record_event(conn, event_type=event_type, doi=doi)
    return True


def register_paper(
    db_path: str | Path, *, doi: str, is_group_work: bool
) -> PaperOut:
    with write_transaction(db_path) as conn:
        if conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone():
            raise ConflictError(f"paper {doi!r} is already registered")
        now = utcnow()
        conn.execute(
            """
            INSERT INTO papers (doi, is_group_work, retracted, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (doi, 1 if is_group_work else 0, now, now),
        )
        record_event(conn, event_type="paper.registered", doi=doi)
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        return _paper_from_row(row)


def update_paper(
    db_path: str | Path, *, doi: str, is_group_work: bool | None
) -> PaperOut:
    with write_transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        if row is None:
            raise NotFoundError(f"paper {doi!r} is not registered")
        if is_group_work is not None and bool(row["is_group_work"]) != is_group_work:
            conn.execute(
                "UPDATE papers SET is_group_work = ?, updated_at = ? WHERE doi = ?",
                (1 if is_group_work else 0, utcnow(), doi),
            )
            record_event(
                conn,
                event_type="paper.group_work_changed",
                doi=doi,
                detail="true" if is_group_work else "false",
            )
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        return _paper_from_row(row)


def get_paper(db_path: str | Path, doi: str) -> PaperOut:
    with read_transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    if row is None:
        raise NotFoundError(f"paper {doi!r} is not registered")
    return _paper_from_row(row)


def list_papers(
    db_path: str | Path,
    *,
    is_group_work: bool | None = None,
    retracted: bool | None = None,
) -> list[PaperOut]:
    clauses: list[str] = []
    params: list[Any] = []
    if is_group_work is not None:
        clauses.append("is_group_work = ?")
        params.append(1 if is_group_work else 0)
    if retracted is not None:
        clauses.append("retracted = ?")
        params.append(1 if retracted else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with read_transaction(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM papers {where} ORDER BY doi", params
        ).fetchall()
    return [_paper_from_row(row) for row in rows]


def register_citation(
    db_path: str | Path, *, source_id: str, citing_doi: str, cited_doi: str
) -> CitationRegistrationResult:
    with write_transaction(db_path) as conn:
        ensure_paper(conn, citing_doi)
        ensure_paper(conn, cited_doi)
        now = utcnow()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO citations
                (source_id, citing_doi, cited_doi, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, citing_doi, cited_doi, now),
        )
        created = cursor.rowcount > 0
        if created:
            record_event(
                conn,
                event_type="citation.registered",
                source_id=source_id,
                citing_doi=citing_doi,
                cited_doi=cited_doi,
            )
        row = conn.execute(
            """
            SELECT * FROM citations
            WHERE source_id = ? AND citing_doi = ? AND cited_doi = ?
            """,
            (source_id, citing_doi, cited_doi),
        ).fetchone()
    return CitationRegistrationResult(
        created=created,
        citation=CitationOut(
            source_id=row["source_id"],
            citing_doi=row["citing_doi"],
            cited_doi=row["cited_doi"],
            created_at=row["created_at"],
        ),
    )


def withdraw_citation(
    db_path: str | Path, *, source_id: str, citing_doi: str, cited_doi: str
) -> WithdrawResult:
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            """
            DELETE FROM citations
            WHERE source_id = ? AND citing_doi = ? AND cited_doi = ?
            """,
            (source_id, citing_doi, cited_doi),
        )
        withdrawn = cursor.rowcount > 0
        if withdrawn:
            record_event(
                conn,
                event_type="citation.withdrawn",
                source_id=source_id,
                citing_doi=citing_doi,
                cited_doi=cited_doi,
            )
    return WithdrawResult(
        withdrawn=withdrawn,
        source_id=source_id,
        citing_doi=citing_doi,
        cited_doi=cited_doi,
    )


def list_citations(
    db_path: str | Path,
    *,
    source_id: str | None = None,
    citing_doi: str | None = None,
    cited_doi: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[CitationOut]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("source_id", source_id),
        ("citing_doi", citing_doi),
        ("cited_doi", cited_doi),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with read_transaction(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM citations {where}
            ORDER BY citing_doi, cited_doi, source_id
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [
        CitationOut(
            source_id=row["source_id"],
            citing_doi=row["citing_doi"],
            cited_doi=row["cited_doi"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def compute_stats(conn: sqlite3.Connection) -> StatsOut:
    """Compute citations per non-retracted group work and the h-index.

    A citation contributes at most once per (citing_doi, cited_doi) pair even
    when several bibliographic sources registered it.  It only contributes
    when both endpoints are currently not retracted.
    """

    rows = conn.execute(
        """
        SELECT p.doi AS doi, COUNT(DISTINCT c.citing_doi) AS citation_count
        FROM papers AS p
        LEFT JOIN citations AS c
               ON c.cited_doi = p.doi
              AND EXISTS (
                    SELECT 1 FROM papers AS citing
                    WHERE citing.doi = c.citing_doi
                      AND citing.retracted = 0
                  )
        WHERE p.is_group_work = 1
          AND p.retracted = 0
        GROUP BY p.doi
        ORDER BY p.doi
        """
    ).fetchall()
    works = [
        WorkSummary(doi=row["doi"], citation_count=row["citation_count"])
        for row in rows
    ]
    counts = sorted((work.citation_count for work in works), reverse=True)
    h_index = 0
    for position, count in enumerate(counts, start=1):
        if count >= position:
            h_index = position
        else:
            break
    return StatsOut(
        h_index=h_index,
        group_work_count=len(works),
        total_citations=sum(work.citation_count for work in works),
        group_works=works,
    )


def get_stats(db_path: str | Path) -> StatsOut:
    """Read-only snapshot of the per-work citation summary."""

    with read_transaction(db_path) as conn:
        return compute_stats(conn)


def _impact_for_change(
    conn: sqlite3.Connection, *, doi: str, was_retracted: bool, now_retracted: bool
) -> RetractionImpact:
    """Diff the counted citation pairs involving ``doi`` before vs after."""

    if was_retracted == now_retracted:
        return RetractionImpact(newly_counted=[], no_longer_counted=[])

    rows = conn.execute(
        """
        SELECT DISTINCT c.citing_doi AS citing_doi, c.cited_doi AS cited_doi
        FROM citations AS c
        JOIN papers AS cited ON cited.doi = c.cited_doi
        WHERE (c.citing_doi = ? OR c.cited_doi = ?)
          AND cited.is_group_work = 1
        """,
        (doi, doi),
    ).fetchall()
    newly_counted: list[CitationPair] = []
    no_longer_counted: list[CitationPair] = []
    for row in rows:
        citing_doi = row["citing_doi"]
        cited_doi = row["cited_doi"]
        other_doi = cited_doi if citing_doi == doi else citing_doi
        other = conn.execute(
            "SELECT retracted FROM papers WHERE doi = ?", (other_doi,)
        ).fetchone()
        other_retracted = bool(other["retracted"]) if other is not None else False
        counted_before = (not was_retracted) and (not other_retracted)
        counted_after = (not now_retracted) and (not other_retracted)
        pair = CitationPair(citing_doi=citing_doi, cited_doi=cited_doi)
        if counted_after and not counted_before:
            newly_counted.append(pair)
        elif counted_before and not counted_after:
            no_longer_counted.append(pair)

    key = lambda pair: (pair.citing_doi, pair.cited_doi)
    newly_counted.sort(key=key)
    no_longer_counted.sort(key=key)
    return RetractionImpact(
        newly_counted=newly_counted,
        no_longer_counted=no_longer_counted,
    )


def set_retraction(
    db_path: str | Path, *, doi: str, retracted: bool
) -> StatusChangeResult:
    """Atomically change the retraction flag and recompute the statistics.

    The ``BEGIN IMMEDIATE`` transaction blocks concurrent corrections while
    the flag flips, so the returned statistics and impact are exactly the
    state produced by this change.  Corrections committed *before* the call
    are part of the snapshot and are therefore included.  If any statement
    fails, the whole transaction is rolled back: the flag keeps its old value
    and previously submitted corrections stay registered and keep counting
    under the original status.
    """

    with write_transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        if row is None:
            raise NotFoundError(f"paper {doi!r} is not registered")

        was_retracted = bool(row["retracted"])
        now = utcnow()
        if was_retracted != retracted:
            conn.execute(
                """
                UPDATE papers
                   SET retracted = ?, retracted_at = ?, updated_at = ?
                 WHERE doi = ?
                """,
                (
                    1 if retracted else 0,
                    now if retracted else None,
                    now,
                    doi,
                ),
            )
            record_event(
                conn,
                event_type="paper.retracted" if retracted else "paper.restored",
                doi=doi,
            )

        impact = _impact_for_change(
            conn, doi=doi, was_retracted=was_retracted, now_retracted=retracted
        )
        paper_row = conn.execute(
            "SELECT * FROM papers WHERE doi = ?", (doi,)
        ).fetchone()
        stats = compute_stats(conn)
        return StatusChangeResult(
            paper=_paper_from_row(paper_row),
            stats=stats,
            impact=impact,
        )


def seed_papers(db_path: str | Path, seed_file: str | Path) -> int:
    """Register papers from a JSON seed file at database creation time.

    The file may hold a list of objects ``{"doi": ..., "is_group_work": ...,
    "retracted": ...}`` or an object with a ``papers`` key.  Existing rows are
    never overwritten, so a librarian's group/retraction changes survive
    restarts.
    """

    path = Path(seed_file)
    if not path.is_file():
        logger.warning("seed file %s does not exist; skipping seeding", path)
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("papers", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise TypeError("seed file must contain a list of papers")

    from .models import normalize_doi

    seeded = 0
    with write_transaction(db_path) as conn:
        now = utcnow()
        for entry in entries:
            if not isinstance(entry, dict) or "doi" not in entry:
                raise ValueError("each seed paper needs a 'doi' field")
            doi = normalize_doi(str(entry["doi"]))
            is_group_work = bool(entry.get("is_group_work", False))
            retracted = bool(entry.get("retracted", False))
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO papers
                    (doi, is_group_work, retracted, created_at, updated_at,
                     retracted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    doi,
                    1 if is_group_work else 0,
                    1 if retracted else 0,
                    now,
                    now,
                    now if retracted else None,
                ),
            )
            if cursor.rowcount > 0:
                seeded += 1
                record_event(
                    conn,
                    event_type="paper.seeded",
                    doi=doi,
                    detail=(
                        "group_work" if is_group_work else "external"
                    )
                    + (",retracted" if retracted else ""),
                )
    return seeded
