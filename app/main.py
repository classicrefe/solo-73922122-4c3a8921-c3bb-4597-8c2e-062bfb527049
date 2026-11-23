"""FastAPI application exposing the citation statistics API."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from . import service
from .database import init_database
from .models import (
    CitationCreate,
    CitationOut,
    CitationRegistrationResult,
    PaperCreate,
    PaperOut,
    PaperUpdate,
    RetractionRequest,
    StatsOut,
    StatusChangeResult,
    WithdrawResult,
    normalize_doi,
    normalize_source_id,
)

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH = "data/citations.db"
DEFAULT_SEED_FILE = "seed/papers.json"


def _normalise_doi_or_422(value: str) -> str:
    try:
        return normalize_doi(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid DOI: {exc}"
        ) from exc


def create_app(
    *,
    database_path: str | Path | None = None,
    seed_file: str | Path | None = None,
) -> FastAPI:
    db_path = str(
        database_path
        if database_path is not None
        else os.environ.get("CITATION_DB_PATH", DEFAULT_DATABASE_PATH)
    )
    seed = (
        seed_file
        if seed_file is not None
        else os.environ.get("CITATION_SEED_FILE", DEFAULT_SEED_FILE)
    )

    app = FastAPI(
        title="Research Group Citation Statistics",
        version="1.0.0",
        description=(
            "Citation registry for a research group with retraction-aware "
            "counting. A citation counts only when both the citing and the "
            "cited paper are currently not retracted, and a citation "
            "registered by several bibliographic sources counts once."
        ),
    )
    app.state.database_path = db_path

    init_database(db_path)
    seeded = service.seed_papers(db_path, seed)
    if seeded:
        logger.info("seeded %d papers from %s", seeded, seed)

    def get_db() -> str:
        return app.state.database_path

    @app.exception_handler(service.NotFoundError)
    async def _not_found_handler(
        request: Request, exc: service.NotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": exc.message})

    @app.exception_handler(service.ConflictError)
    async def _conflict_handler(
        request: Request, exc: service.ConflictError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": exc.message})

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    # ---------------------------------------------------------------- papers

    @app.post(
        "/papers",
        response_model=PaperOut,
        status_code=status.HTTP_201_CREATED,
        tags=["papers"],
        summary="Register a paper DOI",
    )
    def create_paper(payload: PaperCreate) -> PaperOut:
        return service.register_paper(
            get_db(), doi=payload.doi, is_group_work=payload.is_group_work
        )

    @app.get("/papers", response_model=list[PaperOut], tags=["papers"])
    def list_papers(
        is_group_work: bool | None = Query(default=None),
        retracted: bool | None = Query(default=None),
    ) -> list[PaperOut]:
        return service.list_papers(
            get_db(), is_group_work=is_group_work, retracted=retracted
        )

    @app.get("/papers/{doi:path}", response_model=PaperOut, tags=["papers"])
    def get_paper(doi: str) -> PaperOut:
        return service.get_paper(get_db(), _normalise_doi_or_422(doi))

    @app.patch("/papers/{doi:path}", response_model=PaperOut, tags=["papers"])
    def update_paper(doi: str, payload: PaperUpdate) -> PaperOut:
        return service.update_paper(
            get_db(),
            doi=_normalise_doi_or_422(doi),
            is_group_work=payload.is_group_work,
        )

    @app.post(
        "/papers/{doi:path}/retraction",
        response_model=StatusChangeResult,
        tags=["papers"],
        summary="Mark a paper retracted or restore it; returns fresh statistics",
    )
    def set_retraction(doi: str, payload: RetractionRequest) -> StatusChangeResult:
        return service.set_retraction(
            get_db(), doi=_normalise_doi_or_422(doi), retracted=payload.retracted
        )

    # ------------------------------------------------------------- citations

    @app.post(
        "/citations",
        response_model=CitationRegistrationResult,
        status_code=status.HTTP_201_CREATED,
        tags=["citations"],
        summary="Register a citation from a bibliographic source",
    )
    def register_citation(
        payload: CitationCreate, response: Response
    ) -> CitationRegistrationResult:
        result = service.register_citation(
            get_db(),
            source_id=payload.source_id,
            citing_doi=payload.citing_doi,
            cited_doi=payload.cited_doi,
        )
        if not result.created:
            # Already registered by this source: idempotent acknowledgement.
            response.status_code = status.HTTP_200_OK
        return result

    @app.delete(
        "/citations",
        response_model=WithdrawResult,
        tags=["citations"],
        summary="Withdraw this source's registration of a citation",
    )
    def withdraw_citation(
        source_id: str = Query(..., min_length=1, max_length=128),
        citing_doi: str = Query(..., min_length=1, max_length=512),
        cited_doi: str = Query(..., min_length=1, max_length=512),
    ) -> WithdrawResult:
        try:
            source = normalize_source_id(source_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return service.withdraw_citation(
            get_db(),
            source_id=source,
            citing_doi=_normalise_doi_or_422(citing_doi),
            cited_doi=_normalise_doi_or_422(cited_doi),
        )

    @app.get("/citations", response_model=list[CitationOut], tags=["citations"])
    def list_citations(
        source_id: str | None = Query(default=None),
        citing_doi: str | None = Query(default=None),
        cited_doi: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> list[CitationOut]:
        try:
            source = normalize_source_id(source_id) if source_id else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return service.list_citations(
            get_db(),
            source_id=source,
            citing_doi=_normalise_doi_or_422(citing_doi) if citing_doi else None,
            cited_doi=_normalise_doi_or_422(cited_doi) if cited_doi else None,
            limit=limit,
            offset=offset,
        )

    # ----------------------------------------------------------------- stats

    @app.get(
        "/stats",
        response_model=StatsOut,
        tags=["stats"],
        summary="Read-only per-work citation counts and h-index",
    )
    def stats(response: Response) -> StatsOut:
        response.headers["Cache-Control"] = "no-store"
        return service.get_stats(get_db())

    return app


app = create_app()
