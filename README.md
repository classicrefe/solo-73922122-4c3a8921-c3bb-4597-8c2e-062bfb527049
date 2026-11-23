# Citation Statistics API

FastAPI + SQLite service for a research group's citation statistics. The
database is seeded at creation time with the DOIs the group cares about and
marks which of them are the group's own works. Librarians can retract or
restore any paper; counting is retraction-aware.

## Quick start

```bash
docker compose up --build
```

Then open <http://localhost:8000/docs> and try the endpoints interactively.
The SQLite file lives in the `citation-data` named volume, so data survives
container restarts. The demo seed file is `seed/papers.json`.

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
| `POST` | `/citations` | A source registers one citation (idempotent per source). |
| `DELETE` | `/citations` | A source withdraws its own registration only. |
| `GET` | `/citations` | Inspect registrations (filter by source / DOIs). |
| `GET` | `/stats` | Per-work citations and the h-index. |

DOIs are case-insensitive and are normalised: `https://doi.org/10.5555/X`,
`doi:10.5555/x` and `10.5555/X` all refer to the same paper. The
`/papers/{doi}` routes accept unescaped slashes; percent-encode the DOI if it
contains a `?` or `#`.

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
