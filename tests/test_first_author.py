"""Tests for first-author records and the self-citation h-index analysis."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# app.main creates the default application at import time; point it at a
# throwaway database so importing the module never touches the repo.
_BOOTSTRAP_DIR = tempfile.mkdtemp(prefix="citation-tests-bootstrap-")
os.environ.setdefault("CITATION_DB_PATH", str(Path(_BOOTSTRAP_DIR) / "bootstrap.db"))
os.environ.setdefault("CITATION_SEED_FILE", str(Path(_BOOTSTRAP_DIR) / "none.json"))

from fastapi.testclient import TestClient

from app import service
from app.database import init_database, read_transaction
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(
        database_path=db,
        seed_file=tmp_path / "seed.json",
    )
    with TestClient(app) as test_client:
        yield test_client


def add_paper(client, doi: str, is_group_work: bool = False, *, expect: int = 201):
    response = client.post(
        "/papers", json={"doi": doi, "is_group_work": is_group_work}
    )
    assert response.status_code == expect, response.text
    return response.json()


def cite(client, source: str, citing: str, cited: str, *, expect: int = 201):
    response = client.post(
        "/citations",
        json={"source_id": source, "citing_doi": citing, "cited_doi": cited},
    )
    assert response.status_code == expect, response.text
    return response.json()


def set_author(client, doi: str, payload: dict, *, expect: int = 200):
    response = client.put(f"/papers/{doi}/first-author", json=payload)
    assert response.status_code == expect, response.text
    return response.json()


def calc(client, *, expect: int = 200):
    response = client.get("/stats/first-author-h-index")
    assert response.status_code == expect, response.text
    return response.json()


# ------------------------------------------------------------- entry / CRUD


def test_enter_fetch_and_correct_first_author_record(client):
    doi = "10.5555/gw.fa1"
    add_paper(client, doi, True)

    # Confirmed id.
    body = set_author(client, doi, {"author_id": "auth.1"})
    assert body["doi"] == doi
    assert body["author_id"] == "auth.1"
    assert body["candidate_ids"] is None

    fetched = client.get(f"/papers/{doi}/first-author")
    assert fetched.status_code == 200
    assert fetched.json()["author_id"] == "auth.1"

    # Correct to two pending candidates, then back to a confirmed id.
    corrected = set_author(client, doi, {"candidate_ids": ["auth.1", "auth.2"]})
    assert corrected["author_id"] is None
    assert corrected["candidate_ids"] == ["auth.1", "auth.2"]
    assert client.get(f"/papers/{doi}/first-author").json()[
        "candidate_ids"
    ] == ["auth.1", "auth.2"]

    final = set_author(client, doi, {"author_id": "auth.9"})
    assert final["author_id"] == "auth.9"
    assert final["candidate_ids"] is None

    # The ordinary paper route still works alongside the sub-resource.
    assert client.get(f"/papers/{doi}").json()["doi"] == doi


def test_first_author_requires_registered_doi(client):
    response = client.put(
        "/papers/10.5555/unknown/first-author", json={"author_id": "auth.1"}
    )
    assert response.status_code == 404
    assert client.get("/papers/10.5555/unknown/first-author").status_code == 404
    assert (
        client.delete("/papers/10.5555/unknown/first-author").status_code == 404
    )


def test_first_author_record_absent_is_404(client):
    add_paper(client, "10.5555/gw.fa2", True)
    response = client.get("/papers/10.5555/gw.fa2/first-author")
    assert response.status_code == 404
    assert "first-author" in response.json()["detail"]


def test_first_author_entry_validation(client):
    add_paper(client, "10.5555/gw.fa3", True)
    url = "/papers/10.5555/gw.fa3/first-author"

    assert client.put(url, json={}).status_code == 422
    assert (
        client.put(
            url,
            json={"author_id": "auth.1", "candidate_ids": ["auth.1", "auth.2"]},
        ).status_code
        == 422
    )
    # Candidates must be exactly two, and different.
    assert client.put(url, json={"candidate_ids": ["auth.1"]}).status_code == 422
    assert (
        client.put(
            url, json={"candidate_ids": ["auth.1", "auth.2", "auth.3"]}
        ).status_code
        == 422
    )
    assert (
        client.put(url, json={"candidate_ids": ["auth.1", " auth.1 "]}).status_code
        == 422
    )
    # Blank ids are rejected.
    assert client.put(url, json={"author_id": "   "}).status_code == 422
    assert (
        client.put(url, json={"candidate_ids": ["auth.1", "  "]}).status_code == 422
    )


def test_clear_first_author_record(client):
    doi = "10.5555/gw.fa4"
    add_paper(client, doi, True)
    set_author(client, doi, {"author_id": "auth.1"})

    cleared = client.delete(f"/papers/{doi}/first-author")
    assert cleared.status_code == 200
    assert cleared.json() == {"doi": doi, "cleared": True}
    assert client.get(f"/papers/{doi}/first-author").status_code == 404

    again = client.delete(f"/papers/{doi}/first-author")
    assert again.json() == {"doi": doi, "cleared": False}


# ------------------------------------------------------- h-index calculation


def _seeded_group_scenario(client):
    """Three group works cited by externals plus two intra-group citations.

    Counts before self-citation filtering: A=3, B=3, C=3 (h = 3).
    B's first author is pending: auth.a makes A->B a self-citation (h = 2),
    auth.b keeps it (h = 3).
    """

    for doi in ("10.5555/gw.a", "10.5555/gw.b", "10.5555/gw.c"):
        add_paper(client, doi, True)
    for doi in ("10.5555/ext.x", "10.5555/ext.y", "10.5555/ext.z"):
        add_paper(client, doi)
    for citing, cited in (
        ("10.5555/ext.x", "10.5555/gw.a"),
        ("10.5555/ext.y", "10.5555/gw.a"),
        ("10.5555/ext.z", "10.5555/gw.a"),
        ("10.5555/ext.x", "10.5555/gw.b"),
        ("10.5555/ext.y", "10.5555/gw.b"),
        ("10.5555/gw.a", "10.5555/gw.b"),
        ("10.5555/ext.y", "10.5555/gw.c"),
        ("10.5555/ext.z", "10.5555/gw.c"),
        ("10.5555/gw.b", "10.5555/gw.c"),
    ):
        cite(client, "crossref", citing, cited)
    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    set_author(client, "10.5555/gw.b", {"candidate_ids": ["auth.a", "auth.b"]})
    set_author(client, "10.5555/gw.c", {"author_id": "auth.c"})
    set_author(client, "10.5555/ext.x", {"author_id": "auth.x"})
    set_author(client, "10.5555/ext.y", {"author_id": "auth.y"})
    set_author(client, "10.5555/ext.z", {"author_id": "auth.z"})


def test_pending_candidates_yield_min_and_max(client):
    _seeded_group_scenario(client)

    body = calc(client)
    assert body["status"] == "ok"
    assert body["reasons"] == []
    assert body["missing_author_dois"] == []
    assert body["pending_dois"] == ["10.5555/gw.b"]
    # auth.a turns gw.a -> gw.b into a first-author self-citation: h drops.
    assert body["minimum"] == {
        "h_index": 2,
        "assignments": {"10.5555/gw.b": "auth.a"},
    }
    assert body["maximum"] == {
        "h_index": 3,
        "assignments": {"10.5555/gw.b": "auth.b"},
    }


def test_all_confirmed_gives_single_value_and_excludes_self_citations(client):
    add_paper(client, "10.5555/gw.a", True)
    add_paper(client, "10.5555/gw.b", True)
    add_paper(client, "10.5555/ext.x")
    add_paper(client, "10.5555/ext.y")
    cite(client, "crossref", "10.5555/ext.x", "10.5555/gw.a")
    cite(client, "crossref", "10.5555/ext.y", "10.5555/gw.a")
    cite(client, "crossref", "10.5555/ext.x", "10.5555/gw.b")
    cite(client, "crossref", "10.5555/ext.y", "10.5555/gw.b")
    # ext.x shares its first author with gw.a: that citation is excluded.
    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    set_author(client, "10.5555/gw.b", {"author_id": "auth.b"})
    set_author(client, "10.5555/ext.x", {"author_id": "auth.a"})
    set_author(client, "10.5555/ext.y", {"author_id": "auth.y"})

    # /stats still counts everything: A=2, B=2 -> h=2.
    assert client.get("/stats").json()["h_index"] == 2

    body = calc(client)
    assert body["status"] == "ok"
    assert body["pending_dois"] == []
    # Counts after filtering: A=1 (ext.y), B=2 -> h=1, no hypotheses to vary.
    assert body["minimum"] == {"h_index": 1, "assignments": {}}
    assert body["maximum"] == {"h_index": 1, "assignments": {}}


def test_missing_author_data_blocks_and_lists_dois(client):
    add_paper(client, "10.5555/gw.a", True)
    cite(client, "crossref", "10.5555/ext.x", "10.5555/gw.a")

    body = calc(client)
    assert body["status"] == "blocked"
    assert body["reasons"] == ["missing_author_data"]
    assert body["missing_author_dois"] == ["10.5555/ext.x", "10.5555/gw.a"]
    assert body["minimum"] is None and body["maximum"] is None

    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    body = calc(client)
    assert body["status"] == "blocked"
    assert body["missing_author_dois"] == ["10.5555/ext.x"]

    set_author(client, "10.5555/ext.x", {"author_id": "auth.x"})
    body = calc(client)
    assert body["status"] == "ok"
    assert body["minimum"]["h_index"] == 1


def test_more_than_ten_pending_dois_blocks(client):
    add_paper(client, "10.5555/gw.a", True)
    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    for index in range(11):
        doi = f"10.5555/ext.{index}"
        cite(client, "crossref", doi, "10.5555/gw.a")
        set_author(client, doi, {"candidate_ids": [f"c{index}.0", f"c{index}.1"]})

    body = calc(client)
    assert body["status"] == "blocked"
    assert body["reasons"] == ["too_many_pending_dois"]
    assert len(body["pending_dois"]) == 11
    assert body["minimum"] is None and body["maximum"] is None

    # Exactly ten pending DOIs is still computable (2**10 hypotheses).
    set_author(client, "10.5555/ext.10", {"author_id": "auth.z"})
    body = calc(client)
    assert body["status"] == "ok"
    assert len(body["pending_dois"]) == 10
    assert body["minimum"]["h_index"] == 1
    assert body["maximum"]["h_index"] == 1


def test_calculation_follows_stats_scope_rules(client):
    add_paper(client, "10.5555/gw.a", True)
    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    # One pair registered by three sources counts once.
    for source in ("crossref", "openalex", "scopus"):
        cite(client, source, "10.5555/ext.x", "10.5555/gw.a")
    set_author(client, "10.5555/ext.x", {"author_id": "auth.x"})
    body = calc(client)
    assert body["status"] == "ok"
    assert body["minimum"]["h_index"] == 1

    # A retracted citing paper no longer participates; its (now absent)
    # author data must not block the analysis either.
    service.set_retraction(
        client.app.state.database_path, doi="10.5555/ext.x", retracted=True
    )
    cleared = client.delete("/papers/10.5555/ext.x/first-author")
    assert cleared.json()["cleared"] is True
    body = calc(client)
    assert body["status"] == "ok"
    assert body["minimum"] == {"h_index": 0, "assignments": {}}

    # Restoring brings the pair back, and the missing record blocks again.
    service.set_retraction(
        client.app.state.database_path, doi="10.5555/ext.x", retracted=False
    )
    body = calc(client)
    assert body["status"] == "blocked"
    assert body["missing_author_dois"] == ["10.5555/ext.x"]

    # A retracted group work drops out of the analysis entirely.
    set_author(client, "10.5555/ext.x", {"author_id": "auth.x"})
    service.set_retraction(
        client.app.state.database_path, doi="10.5555/gw.a", retracted=True
    )
    body = calc(client)
    assert body["status"] == "ok"
    assert body["minimum"] == {"h_index": 0, "assignments": {}}


def test_confirmed_pairs_are_judged_once_for_all_hypotheses(client, monkeypatch):
    add_paper(client, "10.5555/gw.a", True)
    add_paper(client, "10.5555/gw.b", True)
    cite(client, "crossref", "10.5555/ext.x", "10.5555/gw.a")  # both confirmed
    cite(client, "crossref", "10.5555/ext.x", "10.5555/gw.b")  # one end pending
    cite(client, "crossref", "10.5555/gw.a", "10.5555/gw.b")  # one end pending
    set_author(client, "10.5555/gw.a", {"author_id": "auth.a"})
    set_author(client, "10.5555/gw.b", {"candidate_ids": ["auth.p", "auth.q"]})
    set_author(client, "10.5555/ext.x", {"author_id": "auth.x"})

    calls: list[tuple[str, str]] = []
    real = service._is_first_author_self_citation

    def spy(citing_id: str, cited_id: str) -> bool:
        calls.append((citing_id, cited_id))
        return real(citing_id, cited_id)

    monkeypatch.setattr(service, "_is_first_author_self_citation", spy)
    body = calc(client)
    assert body["status"] == "ok"

    # 1 confirmed pair judged exactly once + 2 open pairs x 2 hypotheses.
    assert len(calls) == 1 + 2 * 2
    assert calls.count(("auth.x", "auth.a")) == 1


def test_corrections_between_calculations_are_picked_up(client):
    _seeded_group_scenario(client)
    assert calc(client)["minimum"]["h_index"] == 2

    # The librarian finishes verifying gw.b: it is auth.b after all.
    set_author(client, "10.5555/gw.b", {"author_id": "auth.b"})
    body = calc(client)
    assert body["status"] == "ok"
    assert body["pending_dois"] == []
    assert body["minimum"] == {"h_index": 3, "assignments": {}}
    assert body["maximum"] == {"h_index": 3, "assignments": {}}


def test_analysis_uses_one_committed_snapshot_while_writes_continue(tmp_path):
    db = tmp_path / "snapshot.db"
    init_database(db)
    service.register_paper(db, doi="10.5555/gw.a", is_group_work=True)
    service.register_citation(
        db, source_id="crossref", citing_doi="10.5555/ext.x", cited_doi="10.5555/gw.a"
    )
    service.set_first_author(db, doi="10.5555/gw.a", author_id="auth.a")
    service.set_first_author(db, doi="10.5555/ext.x", author_id="auth.x")

    # Load the inputs inside a read transaction, then commit a correction
    # from another connection while that snapshot is still open.
    with read_transaction(db) as conn:
        inputs = service._load_first_author_inputs(conn)
        service.set_first_author(db, doi="10.5555/ext.x", author_id="auth.a")

    # The snapshot still reflects the pre-correction state...
    result = service._compute_first_author_h_index(inputs)
    assert result.status == "ok"
    assert result.minimum.h_index == 1

    # ...while a fresh analysis sees the correction (now a self-citation).
    fresh = service.get_first_author_h_index(db)
    assert fresh.minimum.h_index == 0
    assert fresh.maximum.h_index == 0


def test_openapi_lists_first_author_endpoints(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/papers/{doi}/first-author" in paths
    assert "/stats/first-author-h-index" in paths


def test_seed_does_not_overwrite_first_author_corrections(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(
        '{"papers": [{"doi": "10.5555/gw.s", "is_group_work": true}],'
        ' "first_authors": ['
        '  {"doi": "10.5555/gw.s", "candidate_ids": ["auth.a", "auth.b"]}'
        "]}",
        encoding="utf-8",
    )
    db = tmp_path / "seeded.db"
    app = create_app(database_path=db, seed_file=seed)
    with TestClient(app) as first:
        body = first.get("/papers/10.5555/gw.s/first-author").json()
        assert body["candidate_ids"] == ["auth.a", "auth.b"]
        # The librarian finishes verification after seeding.
        first.put(
            "/papers/10.5555/gw.s/first-author", json={"author_id": "auth.final"}
        )

    # Restarting against the same database must keep the correction.
    app2 = create_app(database_path=db, seed_file=seed)
    with TestClient(app2) as second:
        body = second.get("/papers/10.5555/gw.s/first-author").json()
        assert body["author_id"] == "auth.final"
        assert body["candidate_ids"] is None
