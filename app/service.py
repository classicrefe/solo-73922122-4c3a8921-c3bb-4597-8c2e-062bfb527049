"""Service layer: citation registry, retraction lifecycle, and statistics."""

from __future__ import annotations

import itertools
import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from .database import read_transaction, record_event, utcnow, write_transaction
from .models import (
    CitationOut,
    CitationPair,
    CitationRegistrationResult,
    FirstAuthorClearResult,
    FirstAuthorHIndexOut,
    FirstAuthorOut,
    HIndexScenario,
    PaperOut,
    RetractionImpact,
    StatsOut,
    StatusChangeResult,
    WithdrawResult,
    WorkSummary,
)

logger = logging.getLogger(__name__)

# Upper bound on the number of to-be-verified DOIs (two candidates each) the
# h-index analysis will enumerate hypotheses for: 2**10 = 1024 assignments.
MAX_PENDING_DOIS = 10


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


def _h_index(counts: Iterable[int]) -> int:
    """Largest h such that at least h works each have at least h citations."""

    h_index = 0
    for position, count in enumerate(sorted(counts, reverse=True), start=1):
        if count >= position:
            h_index = position
        else:
            break
    return h_index


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
    return StatsOut(
        h_index=_h_index(work.citation_count for work in works),
        group_work_count=len(works),
        total_citations=sum(work.citation_count for work in works),
        group_works=works,
    )


def get_stats(db_path: str | Path) -> StatsOut:
    """Read-only snapshot of the per-work citation summary."""

    with read_transaction(db_path) as conn:
        return compute_stats(conn)


# ----------------------------------------------------------- first authors


def _first_author_from_row(row: sqlite3.Row) -> FirstAuthorOut:
    candidate_ids = None
    if row["candidate_a"] is not None:
        candidate_ids = [row["candidate_a"], row["candidate_b"]]
    return FirstAuthorOut(
        doi=row["doi"],
        author_id=row["author_id"],
        candidate_ids=candidate_ids,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def set_first_author(
    db_path: str | Path,
    *,
    doi: str,
    author_id: str | None = None,
    candidate_ids: list[str] | None = None,
) -> FirstAuthorOut:
    """Enter or correct the first-author record of a registered DOI.

    Exactly one shape is stored: a confirmed ``author_id`` or two distinct
    ``candidate_ids``.  Re-submitting overwrites the previous record, so a
    librarian can both enter data for the first time and correct it later.
    """

    with write_transaction(db_path) as conn:
        if (
            conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone()
            is None
        ):
            raise NotFoundError(f"paper {doi!r} is not registered")
        now = utcnow()
        if author_id is not None:
            conn.execute(
                """
                INSERT INTO first_authors
                    (doi, author_id, candidate_a, candidate_b,
                     created_at, updated_at)
                VALUES (?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(doi) DO UPDATE SET
                    author_id = excluded.author_id,
                    candidate_a = NULL,
                    candidate_b = NULL,
                    updated_at = excluded.updated_at
                """,
                (doi, author_id, now, now),
            )
            detail = f"definite:{author_id}"
        else:
            assert candidate_ids is not None and len(candidate_ids) == 2
            candidate_a, candidate_b = candidate_ids
            conn.execute(
                """
                INSERT INTO first_authors
                    (doi, author_id, candidate_a, candidate_b,
                     created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, ?)
                ON CONFLICT(doi) DO UPDATE SET
                    author_id = NULL,
                    candidate_a = excluded.candidate_a,
                    candidate_b = excluded.candidate_b,
                    updated_at = excluded.updated_at
                """,
                (doi, candidate_a, candidate_b, now, now),
            )
            detail = f"candidates:{candidate_a}|{candidate_b}"
        record_event(conn, event_type="first_author.set", doi=doi, detail=detail)
        row = conn.execute(
            "SELECT * FROM first_authors WHERE doi = ?", (doi,)
        ).fetchone()
        return _first_author_from_row(row)


def get_first_author(db_path: str | Path, doi: str) -> FirstAuthorOut:
    with read_transaction(db_path) as conn:
        if (
            conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone()
            is None
        ):
            raise NotFoundError(f"paper {doi!r} is not registered")
        row = conn.execute(
            "SELECT * FROM first_authors WHERE doi = ?", (doi,)
        ).fetchone()
    if row is None:
        raise NotFoundError(f"paper {doi!r} has no first-author record yet")
    return _first_author_from_row(row)


def clear_first_author(db_path: str | Path, doi: str) -> FirstAuthorClearResult:
    """Remove a first-author record, returning the DOI to 'no data'."""

    with write_transaction(db_path) as conn:
        if (
            conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone()
            is None
        ):
            raise NotFoundError(f"paper {doi!r} is not registered")
        cursor = conn.execute("DELETE FROM first_authors WHERE doi = ?", (doi,))
        cleared = cursor.rowcount > 0
        if cleared:
            record_event(conn, event_type="first_author.cleared", doi=doi)
    return FirstAuthorClearResult(doi=doi, cleared=cleared)


# --------------------------------------- first-author h-index (self-citation


def _is_first_author_self_citation(citing_author_id: str, cited_author_id: str) -> bool:
    """A citation is a first-author self-citation when both ends share an id."""

    return citing_author_id == cited_author_id


class FirstAuthorInputs(NamedTuple):
    """Everything the h-index analysis needs, read from one snapshot."""

    pairs: list[tuple[str, str]]  # participating (citing_doi, cited_doi)
    definite: dict[str, str]  # doi -> confirmed first-author id
    pending: dict[str, tuple[str, str]]  # doi -> two candidate ids
    missing: list[str]  # participating DOIs without any author record


def _load_first_author_inputs(conn: sqlite3.Connection) -> FirstAuthorInputs:
    """Load the participating citation pairs and their endpoints' author data.

    Participating pairs follow the same rules as :func:`compute_stats`: the
    cited paper is a non-retracted group work, the citing paper is not
    retracted, and a pair registered by several sources appears once.
    """

    pair_rows = conn.execute(
        """
        SELECT DISTINCT c.citing_doi AS citing_doi, c.cited_doi AS cited_doi
        FROM citations AS c
        JOIN papers AS cited  ON cited.doi  = c.cited_doi
        JOIN papers AS citing ON citing.doi = c.citing_doi
        WHERE cited.is_group_work = 1
          AND cited.retracted = 0
          AND citing.retracted = 0
        ORDER BY c.citing_doi, c.cited_doi
        """
    ).fetchall()
    pairs = [(row["citing_doi"], row["cited_doi"]) for row in pair_rows]

    involved = sorted({doi for pair in pairs for doi in pair})
    definite: dict[str, str] = {}
    pending: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    if involved:
        placeholders = ", ".join("?" for _ in involved)
        rows = conn.execute(
            f"SELECT * FROM first_authors WHERE doi IN ({placeholders})",
            involved,
        ).fetchall()
        recorded = set()
        for row in rows:
            recorded.add(row["doi"])
            if row["author_id"] is not None:
                definite[row["doi"]] = row["author_id"]
            else:
                pending[row["doi"]] = (row["candidate_a"], row["candidate_b"])
        missing = [doi for doi in involved if doi not in recorded]
    return FirstAuthorInputs(
        pairs=pairs, definite=definite, pending=pending, missing=missing
    )


def _compute_first_author_h_index(inputs: FirstAuthorInputs) -> FirstAuthorHIndexOut:
    """Min/max h-index over all pending first-author hypotheses.

    In every hypothesis each pending DOI takes *one* of its two candidates
    and keeps it for all citations it appears in.  Citations whose endpoints
    are both confirmed are judged for self-citation exactly once, up front,
    and every hypothesis reuses that baseline.
    """

    pending_dois = sorted(inputs.pending)
    reasons: list[str] = []
    if inputs.missing:
        reasons.append("missing_author_data")
    if len(pending_dois) > MAX_PENDING_DOIS:
        reasons.append("too_many_pending_dois")
    if reasons:
        return FirstAuthorHIndexOut(
            status="blocked",
            reasons=reasons,  # type: ignore[arg-type]
            missing_author_dois=list(inputs.missing),
            pending_dois=pending_dois,
        )

    # Both ends confirmed: count (or discard) these citations a single time.
    baseline: dict[str, int] = {}
    open_pairs: list[tuple[str, str]] = []
    for citing_doi, cited_doi in inputs.pairs:
        citing_id = inputs.definite.get(citing_doi)
        cited_id = inputs.definite.get(cited_doi)
        if citing_id is not None and cited_id is not None:
            if not _is_first_author_self_citation(citing_id, cited_id):
                baseline[cited_doi] = baseline.get(cited_doi, 0) + 1
        else:
            open_pairs.append((citing_doi, cited_doi))

    choice_lists = [sorted(inputs.pending[doi]) for doi in pending_dois]
    minimum: HIndexScenario | None = None
    maximum: HIndexScenario | None = None
    for combo in itertools.product(*choice_lists):
        assignment = dict(zip(pending_dois, combo))
        resolved = {**inputs.definite, **assignment}
        counts = dict(baseline)
        for citing_doi, cited_doi in open_pairs:
            if not _is_first_author_self_citation(
                resolved[citing_doi], resolved[cited_doi]
            ):
                counts[cited_doi] = counts.get(cited_doi, 0) + 1
        h_index = _h_index(counts.values())
        scenario = HIndexScenario(h_index=h_index, assignments=assignment)
        if minimum is None or h_index < minimum.h_index:
            minimum = scenario
        if maximum is None or h_index > maximum.h_index:
            maximum = scenario

    return FirstAuthorHIndexOut(
        status="ok",
        pending_dois=pending_dois,
        minimum=minimum,
        maximum=maximum,
    )


def get_first_author_h_index(db_path: str | Path) -> FirstAuthorHIndexOut:
    """Min/max group h-index after excluding first-author self-citations.

    The participating pairs and the first-author records are loaded inside a
    single read transaction, so the analysis runs on one committed snapshot
    of author data, paper status and citations.  WAL readers never block
    writers, so librarians can keep entering corrections while the
    calculation runs; those corrections simply belong to the next snapshot.
    """

    with read_transaction(db_path) as conn:
        inputs = _load_first_author_inputs(conn)
    return _compute_first_author_h_index(inputs)


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


def seed_database(db_path: str | Path, seed_file: str | Path) -> int:
    """Register seed data at database creation time.

    The file may hold a bare list of papers, or an object with a ``papers``
    key plus optional ``citations`` and ``first_authors`` lists:

    * papers: ``{"doi", "is_group_work"?, "retracted"?}``
    * citations: ``{"source_id", "citing_doi", "cited_doi"}``
    * first_authors: ``{"doi", "author_id"}`` or
      ``{"doi", "candidate_ids": [id_a, id_b]}``

    Existing rows are never overwritten, so a librarian's group/retraction
    changes and first-author corrections survive restarts.  Returns the
    number of newly seeded papers.
    """

    path = Path(seed_file)
    if not path.is_file():
        logger.warning("seed file %s does not exist; skipping seeding", path)
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        paper_entries = payload.get("papers", [])
        citation_entries = payload.get("citations", [])
        first_author_entries = payload.get("first_authors", [])
    else:
        paper_entries, citation_entries, first_author_entries = payload, [], []
    if not isinstance(paper_entries, list):
        raise TypeError("seed file must contain a list of papers")

    from .models import normalize_author_id, normalize_doi, normalize_source_id

    seeded = 0
    with write_transaction(db_path) as conn:
        now = utcnow()
        for entry in paper_entries:
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
        for entry in citation_entries:
            if not isinstance(entry, dict) or not all(
                key in entry for key in ("source_id", "citing_doi", "cited_doi")
            ):
                raise ValueError(
                    "each seed citation needs 'source_id', 'citing_doi' and "
                    "'cited_doi' fields"
                )
            source_id = normalize_source_id(str(entry["source_id"]))
            citing_doi = normalize_doi(str(entry["citing_doi"]))
            cited_doi = normalize_doi(str(entry["cited_doi"]))
            if citing_doi == cited_doi:
                raise ValueError("seed citation must not be a self-citation")
            ensure_paper(conn, citing_doi)
            ensure_paper(conn, cited_doi)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO citations
                    (source_id, citing_doi, cited_doi, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (source_id, citing_doi, cited_doi, now),
            )
            if cursor.rowcount > 0:
                record_event(
                    conn,
                    event_type="citation.seeded",
                    source_id=source_id,
                    citing_doi=citing_doi,
                    cited_doi=cited_doi,
                )
        for entry in first_author_entries:
            if not isinstance(entry, dict) or "doi" not in entry:
                raise ValueError("each seed first-author needs a 'doi' field")
            doi = normalize_doi(str(entry["doi"]))
            author_id = entry.get("author_id")
            candidates = entry.get("candidate_ids")
            if (author_id is None) == (not candidates):
                raise ValueError(
                    "each seed first-author needs exactly one of 'author_id' "
                    "or 'candidate_ids'"
                )
            ensure_paper(conn, doi)
            if author_id is not None:
                author_id = normalize_author_id(str(author_id))
                candidate_a = candidate_b = None
                detail = f"definite:{author_id}"
            else:
                if len(candidates) != 2:
                    raise ValueError(
                        "seed 'candidate_ids' must hold exactly two ids"
                    )
                candidate_a = normalize_author_id(str(candidates[0]))
                candidate_b = normalize_author_id(str(candidates[1]))
                if candidate_a == candidate_b:
                    raise ValueError("seed 'candidate_ids' must differ")
                detail = f"candidates:{candidate_a}|{candidate_b}"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO first_authors
                    (doi, author_id, candidate_a, candidate_b,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doi, author_id, candidate_a, candidate_b, now, now),
            )
            if cursor.rowcount > 0:
                record_event(
                    conn,
                    event_type="first_author.seeded",
                    doi=doi,
                    detail=detail,
                )
    return seeded
