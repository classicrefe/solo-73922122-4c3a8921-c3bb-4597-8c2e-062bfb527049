# Citation Statistics API

FastAPI + SQLite service for a research group's citation statistics. The
database is seeded at creation time with the DOIs the group cares about and
marks which of them are the group's own works. Librarians can retract or
restore any paper; counting is retraction-aware.

The service also tracks **first-author ids** per DOI (confirmed, or two
candidates while verification is pending) and reports the **min/max group
h-index after excluding first-author self-citations**.

## Quick start

```bash
docker compose up --build
```

Then open <http://localhost:8000/docs> and try the endpoints interactively.
The SQLite file lives in the `citation-data` named volume, so data survives
container restarts. The demo seed file is `seed/papers.json`; it preloads a
few citations and first-author records so `GET /stats/first-author-h-index`
returns meaningful numbers out of the box (min 2 / max 3 — see
[First-author self-citation analysis](#first-author-self-citation-analysis)).

Local development without Docker:

```bash
python3 -m pip install -r requirements.txt
CITATION_DB_PATH=data/citations.db CITATION_SEED_FILE=seed/papers.json \
  python3 -m uvicorn app.main:app --reload
```

## Counting rules

* A citation is identified by the pair `(citing DOI, cited DOI)`. It may be
  registered by several bibliographic sources (`source_id`), but the pair
  counts **once**.
* A citation counts only if **both** the citing paper and the cited paper are
  currently **not retracted**.
* `/stats` reports, for every group work that is not itself retracted, its
  citation count, plus the group's h-index: the largest `h` such that at least
  `h` group works each have at least `h` citations.
* `/stats` is read-only.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/papers` | Register a DOI; `is_group_work` marks group output. |
| `GET` | `/papers` | List registered papers (filter by `is_group_work`, `retracted`). |
| `GET` | `/papers/{doi}` | Fetch one paper. |
| `PATCH` | `/papers/{doi}` | Change the `is_group_work` flag. |
| `POST` | `/papers/{doi}/retraction` | `{"retracted": true\|false}` — retract or restore. |
| `PUT` | `/papers/{doi}/first-author` | Enter or correct the first-author record. |
| `GET` | `/papers/{doi}/first-author` | Fetch the first-author record. |
| `DELETE` | `/papers/{doi}/first-author` | Remove the first-author record. |
| `POST` | `/citations` | A source registers one citation (idempotent per source). |
| `DELETE` | `/citations` | A source withdraws its own registration only. |
| `GET` | `/citations` | Inspect registrations (filter by source / DOIs). |
| `GET` | `/stats` | Per-work citations and the h-index. |
| `GET` | `/stats/first-author-h-index` | Min/max h-index excluding first-author self-citations. |

DOIs are case-insensitive and are normalised: `https://doi.org/10.5555/X`,
`doi:10.5555/x` and `10.5555/X` all refer to the same paper. The
`/papers/{doi}` routes accept unescaped slashes; percent-encode the DOI if it
contains a `?` or `#`.

## First-author self-citation analysis

Some papers' first authors are not verified yet. Librarians record what is
known per **registered** DOI:

```jsonc
// PUT /papers/10.5555/gw.2022.002/first-author — confirmed:
{"author_id": "auth.a"}
// — or still being verified (exactly two different candidates):
{"candidate_ids": ["auth.a", "auth.b"]}
```

Submitting again overwrites the previous record, so entries can be corrected
at any time; `DELETE` removes a record entirely.

`GET /stats/first-author-h-index` then computes what the group h-index could
be once first-author self-citations (citing and cited paper share a
first-author id) are excluded:

* **Same rules as `/stats`** — only non-retracted group works, both endpoints
  non-retracted, multi-source pairs deduplicated.
* **One value per DOI per hypothesis** — in each hypothesis a pending DOI
  takes one of its two candidates and keeps it for *all* citations it
  appears in.
* **Result** — `minimum` and `maximum` achievable h-index, each with one
  realising `assignments` map (pending DOI → chosen id). Citations whose
  endpoints are both confirmed are judged for self-citation exactly once and
  the verdict is shared by all hypotheses.
* **Blocked, no numbers** — when a participating citation has an endpoint
  without any author record (`missing_author_data`) or more than **10** DOIs
  are pending (`too_many_pending_dois`, i.e. more than 2¹⁰ hypotheses). The
  response lists the offending DOIs in `missing_author_dois` /
  `pending_dois` so the librarian knows what to fill in or confirm first.
* **Consistent snapshot** — author records, paper status and citations are
  read from a single committed snapshot (one read transaction); corrections
  can be submitted while the analysis runs and simply belong to the next
  call.

With the demo seed, `/stats` reports h = 3, while
`/stats/first-author-h-index` reports min **2** (if `10.5555/gw.2022.002`
turns out to be `auth.a`, the intra-group citation `gw.2021.001 →
gw.2022.002` is a self-citation) and max **3** (if it is `auth.b`).

### Retraction / restore semantics

`POST /papers/{doi}/retraction` runs in a single `BEGIN IMMEDIATE`
transaction. It

1. flips the retraction flag (and records an audit event),
2. recomputes the statistics **inside the same transaction**, so the returned
   numbers include every bibliographic correction committed before the call,
3. reports the impact (`newly_counted` / `no_longer_counted`) for counted
   citation pairs involving the paper.

If any step fails the transaction rolls back: the flag keeps its previous
value and every previously submitted correction remains registered and keeps
counting under the original status. Corrections submitted while a paper is
retracted are accepted and become visible to the statistics once it is
restored.

## Seed file

`seed/papers.json` is applied at startup with insert-if-absent semantics, so
restarts never overwrite librarian corrections. Besides the `papers` list it
may carry `citations` and `first_authors` sections:

```json
{
  "papers": [{"doi": "10.5555/gw.1", "is_group_work": true}],
  "citations": [
    {"source_id": "crossref", "citing_doi": "10.5555/ext.1",
     "cited_doi": "10.5555/gw.1"}
  ],
  "first_authors": [
    {"doi": "10.5555/gw.1", "author_id": "auth.a"},
    {"doi": "10.5555/ext.1", "candidate_ids": ["auth.x", "auth.y"]}
  ]
}
```

## Tests

```bash
python3 -m pip install pytest httpx
python3 -m pytest
```

## Notes / known limitations

* Single-process deployment: SQLite in WAL mode handles many readers plus one
  writer at a time, which suits a library back-office. Scaling out to several
  API replicas sharing one volume would need Postgres or a dedicated writer.
* There is no authentication — put the service behind the library's reverse
  proxy or an API gateway before exposing it beyond localhost.
* The `/citations` listing is paginated (`limit`/`offset`, default 500) and the
  full registry is assumed to fit comfortably in memory for `/stats`; the
  aggregate query itself runs in SQLite.
* The first-author analysis enumerates one hypothesis per candidate
  combination, so it caps the number of pending DOIs at 10 (2¹⁰ = 1024
  hypotheses) and declines larger registries until some DOIs are confirmed.
* Author ids are opaque strings compared exactly (after trimming); the
  service does not resolve ORCID/Staff-ID equivalence across schemes — use
  one id scheme consistently.
