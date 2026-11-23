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


def stats(client):
    response = client.get("/stats")
    assert response.status_code == 200, response.text
    return response.json()


def test_register_paper_and_reject_duplicate(client):
    paper = add_paper(client, "10.5555/gw.1", True)
    assert paper["is_group_work"] is True
    assert paper["retracted"] is False

    duplicate = client.post("/papers", json={"doi": "10.5555/GW.1"})
    assert duplicate.status_code == 409

    listed = client.get("/papers").json()
    assert [p["doi"] for p in listed] == ["10.5555/gw.1"]


def test_doi_normalisation(client):
    add_paper(client, "https://doi.org/10.5555/GW.Norm", True)
    fetched = client.get("/papers/doi:10.5555/gw.norm")
    assert fetched.status_code == 200
    assert fetched.json()["doi"] == "10.5555/gw.norm"


def test_duplicate_registrations_from_multiple_sources_count_once(client):
    add_paper(client, "10.5555/gw.a", True)
    cite(client, "crossref", "10.5555/c.1", "10.5555/gw.a")
    cite(client, "openalex", "10.5555/c.1", "10.5555/gw.a")
    cite(client, "scopus", "10.5555/c.1", "10.5555/gw.a")
    cite(client, "crossref", "10.5555/c.2", "10.5555/gw.a")

    body = stats(client)
    assert body["group_works"] == [
        {"doi": "10.5555/gw.a", "citation_count": 2}
    ]
    assert body["h_index"] == 1  # a single work can only reach h = 1
    assert body["total_citations"] == 2


def test_idempotent_registration_by_same_source(client):
    add_paper(client, "10.5555/gw.b", True)
    cite(client, "crossref", "10.5555/c.3", "10.5555/gw.b")
    repeat = cite(
        client, "crossref", "10.5555/c.3", "10.5555/gw.b", expect=200
    )
    assert repeat["created"] is False
    assert stats(client)["group_works"][0]["citation_count"] == 1


def test_retracted_citing_paper_is_not_counted(client):
    add_paper(client, "10.5555/gw.c", True)
    add_paper(client, "10.5555/ext.c1")
    cite(client, "crossref", "10.5555/ext.c1", "10.5555/gw.c")
    cite(client, "crossref", "10.5555/ext.c2", "10.5555/gw.c")
    assert stats(client)["group_works"][0]["citation_count"] == 2

    retract = client.post(
        "/papers/10.5555/ext.c1/retraction", json={"retracted": True}
    )
    assert retract.status_code == 200
    body = retract.json()
    assert body["paper"]["retracted"] is True
    assert body["impact"]["no_longer_counted"] == [
        {"citing_doi": "10.5555/ext.c1", "cited_doi": "10.5555/gw.c"}
    ]
    assert body["stats"]["group_works"][0]["citation_count"] == 1

    restore = client.post(
        "/papers/10.5555/ext.c1/retraction", json={"retracted": False}
    )
    assert restore.status_code == 200
    assert restore.json()["impact"]["newly_counted"] == [
        {"citing_doi": "10.5555/ext.c1", "cited_doi": "10.5555/gw.c"}
    ]
    assert restore.json()["stats"]["group_works"][0]["citation_count"] == 2


def test_retracted_group_work_drops_out_of_stats(client):
    add_paper(client, "10.5555/gw.d", True)
    add_paper(client, "10.5555/gw.e", True)
    cite(client, "crossref", "10.5555/c.d1", "10.5555/gw.d")
    cite(client, "crossref", "10.5555/c.e1", "10.5555/gw.e")

    assert stats(client)["h_index"] == 1

    body = client.post(
        "/papers/10.5555/gw.d/retraction", json={"retracted": True}
    ).json()
    assert [w["doi"] for w in body["stats"]["group_works"]] == ["10.5555/gw.e"]
    assert body["impact"]["no_longer_counted"] == [
        {"citing_doi": "10.5555/c.d1", "cited_doi": "10.5555/gw.d"}
    ]
    assert stats(client)["group_work_count"] == 1


def test_retracting_citing_paper_reports_only_affected_counted_pairs(client):
    add_paper(client, "10.5555/gw.f", True)
    add_paper(client, "10.5555/ext.f1")
    cite(client, "crossref", "10.5555/ext.f1", "10.5555/gw.f")
    # A group work citing an external paper is not part of the citation
    # statistics, so it must not appear in the retraction impact report.
    cite(client, "crossref", "10.5555/gw.f", "10.5555/ext.f1")

    body = client.post(
        "/papers/10.5555/ext.f1/retraction", json={"retracted": True}
    ).json()
    assert body["impact"]["no_longer_counted"] == [
        {"citing_doi": "10.5555/ext.f1", "cited_doi": "10.5555/gw.f"},
    ]


def test_withdraw_only_removes_own_registration(client):
    add_paper(client, "10.5555/gw.g", True)
    cite(client, "crossref", "10.5555/c.g1", "10.5555/gw.g")
    cite(client, "openalex", "10.5555/c.g1", "10.5555/gw.g")

    withdrawn = client.delete(
        "/citations",
        params={
            "source_id": "crossref",
            "citing_doi": "10.5555/c.g1",
            "cited_doi": "10.5555/gw.g",
        },
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["withdrawn"] is True
    assert stats(client)["group_works"][0]["citation_count"] == 1

    remaining = client.get("/citations").json()
    assert [c["source_id"] for c in remaining] == ["openalex"]

    again = client.delete(
        "/citations",
        params={
            "source_id": "crossref",
            "citing_doi": "10.5555/c.g1",
            "cited_doi": "10.5555/gw.g",
        },
    )
    assert again.json()["withdrawn"] is False
    assert stats(client)["group_works"][0]["citation_count"] == 1


def test_h_index_definition(client):
    # Counts per group work: 5, 3, 3, 1  -> h-index 3
    expected = {
        "10.5555/gw.h1": 5,
        "10.5555/gw.h2": 3,
        "10.5555/gw.h3": 3,
        "10.5555/gw.h4": 1,
    }
    for doi in expected:
        add_paper(client, doi, True)
    for doi, count in expected.items():
        for index in range(count):
            cite(client, "crossref", f"10.5555/cite.{doi}.{index}", doi)

    body = stats(client)
    assert body["h_index"] == 3
    assert {w["doi"]: w["citation_count"] for w in body["group_works"]} == expected
    assert body["total_citations"] == 12


def test_h_index_ignores_retracted_group_works(client):
    for doi in ("10.5555/gw.i1", "10.5555/gw.i2"):
        add_paper(client, doi, True)
    for index in range(4):
        cite(client, "crossref", f"10.5555/c.i1.{index}", "10.5555/gw.i1")
    for index in range(2):
        cite(client, "crossref", f"10.5555/c.i2.{index}", "10.5555/gw.i2")
    assert stats(client)["h_index"] == 2

    client.post("/papers/10.5555/gw.i1/retraction", json={"retracted": True})
    body = stats(client)
    assert body["h_index"] == 1
    assert body["group_works"] == [
        {"doi": "10.5555/gw.i2", "citation_count": 2}
    ]


def test_status_failure_keeps_corrections_and_previous_state(client, monkeypatch):
    add_paper(client, "10.5555/gw.j", True)
    cite(client, "crossref", "10.5555/c.j1", "10.5555/gw.j")

    from app import service

    real_record_event = service.record_event

    def boom(conn, **kwargs):
        if kwargs.get("event_type") in {"paper.retracted", "paper.restored"}:
            raise RuntimeError("simulated failure after status update")
        return real_record_event(conn, **kwargs)

    monkeypatch.setattr(service, "record_event", boom)

    failing = TestClient(client.app, raise_server_exceptions=False)
    response = failing.post(
        "/papers/10.5555/gw.j/retraction", json={"retracted": True}
    )
    assert response.status_code == 500

    # Flag rolled back; the earlier citation correction is intact and counted.
    paper = client.get("/papers/10.5555/gw.j").json()
    assert paper["retracted"] is False
    body = stats(client)
    assert body["group_works"] == [{"doi": "10.5555/gw.j", "citation_count": 1}]
    assert body["h_index"] == 1


def test_stats_failure_rolls_back_status_and_keeps_counts(client, monkeypatch):
    add_paper(client, "10.5555/gw.n", True)
    cite(client, "crossref", "10.5555/c.n1", "10.5555/gw.n")

    from app import service

    def broken_stats(conn):
        raise RuntimeError("simulated statistics failure")

    monkeypatch.setattr(service, "compute_stats", broken_stats)

    failing = TestClient(client.app, raise_server_exceptions=False)
    response = failing.post(
        "/papers/10.5555/gw.n/retraction", json={"retracted": True}
    )
    assert response.status_code == 500

    monkeypatch.undo()
    paper = client.get("/papers/10.5555/gw.n").json()
    assert paper["retracted"] is False
    assert paper["retracted_at"] is None
    body = stats(client)
    assert body["group_works"] == [{"doi": "10.5555/gw.n", "citation_count": 1}]


def test_restoring_works_after_retraction_keeps_earlier_corrections(client):
    add_paper(client, "10.5555/gw.k", True)
    cite(client, "crossref", "10.5555/c.k1", "10.5555/gw.k")
    cite(client, "openalex", "10.5555/c.k2", "10.5555/gw.k")

    client.post("/papers/10.5555/gw.k/retraction", json={"retracted": True})
    assert stats(client)["group_works"] == []

    # A correction submitted while retracted is recorded and, once the paper is
    # restored, counts together with everything submitted before.
    cite(client, "scopus", "10.5555/c.k3", "10.5555/gw.k")
    restored = client.post(
        "/papers/10.5555/gw.k/retraction", json={"retracted": False}
    ).json()
    assert restored["stats"]["group_works"] == [
        {"doi": "10.5555/gw.k", "citation_count": 3}
    ]


def test_group_flag_can_be_changed_and_drives_stats(client):
    add_paper(client, "10.5555/gw.l1")
    cite(client, "crossref", "10.5555/c.l1", "10.5555/gw.l1")
    assert stats(client)["group_works"] == []

    promoted = client.patch(
        "/papers/10.5555/gw.l1", json={"is_group_work": True}
    )
    assert promoted.status_code == 200
    assert promoted.json()["is_group_work"] is True
    assert stats(client)["group_works"] == [
        {"doi": "10.5555/gw.l1", "citation_count": 1}
    ]

    demoted = client.patch(
        "/papers/10.5555/gw.l1", json={"is_group_work": False}
    )
    assert demoted.json()["is_group_work"] is False
    assert stats(client)["group_works"] == []


def test_stats_endpoint_does_not_modify_registry(client):
    add_paper(client, "10.5555/gw.m", True)
    cite(client, "crossref", "10.5555/c.m", "10.5555/gw.m")
    before = client.get("/citations").json()
    first = stats(client)
    second = stats(client)
    after = client.get("/citations").json()
    assert before == after
    assert first == second


def test_self_citation_rejected(client):
    response = client.post(
        "/citations",
        json={
            "source_id": "crossref",
            "citing_doi": "10.5555/same",
            "cited_doi": "10.5555/SAME",
        },
    )
    assert response.status_code == 422


def test_unknown_paper_retraction_returns_404(client):
    response = client.post(
        "/papers/10.5555/missing/retraction", json={"retracted": True}
    )
    assert response.status_code == 404


def test_seed_file_registers_papers_once(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(
        '{"papers": [{"doi": "10.5555/gw.seed", "is_group_work": true},'
        ' {"doi": "10.5555/ext.seed"}]}',
        encoding="utf-8",
    )
    db = tmp_path / "seeded.db"
    app = create_app(database_path=db, seed_file=seed)
    with TestClient(app) as first:
        assert len(first.get("/papers").json()) == 2
        first.post("/papers/10.5555/gw.seed/retraction", json={"retracted": True})

    # Restarting the app must not overwrite the librarian's retraction flag.
    app2 = create_app(database_path=db, seed_file=seed)
    with TestClient(app2) as second:
        paper = second.get("/papers/10.5555/gw.seed").json()
        assert paper["retracted"] is True
        assert len(second.get("/papers").json()) == 2


def test_docs_available(client):
    response = client.get("/docs")
    assert response.status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/stats" in schema["paths"]


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"
