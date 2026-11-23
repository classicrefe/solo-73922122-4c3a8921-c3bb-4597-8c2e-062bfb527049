"""Request/response models and DOI normalisation."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)
_WHITESPACE = re.compile(r"\s+")


class InvalidDOI(ValueError):
    pass


def normalize_doi(value: str) -> str:
    """Normalise a DOI to the bare, lower-case form used as the primary key.

    DOIs are case-insensitive and people commonly paste resolver URLs or a
    ``doi:`` prefix.  Normalising on every entry point keeps the citation
    registry free of duplicate spellings of the same paper.
    """

    doi = value.strip()
    lowered = doi.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :].strip()
            lowered = doi.lower()
            break
    if not doi:
        raise InvalidDOI("DOI must not be empty")
    if len(doi) > 512:
        raise InvalidDOI("DOI must be at most 512 characters")
    if _WHITESPACE.search(doi):
        raise InvalidDOI("DOI must not contain whitespace")
    return lowered


def normalize_source_id(value: str) -> str:
    source_id = value.strip()
    if not source_id:
        raise ValueError("source_id must not be empty")
    if len(source_id) > 128:
        raise ValueError("source_id must be at most 128 characters")
    return source_id


AUTHOR_ID_MAX_LENGTH = 128


def normalize_author_id(value: str) -> str:
    """Normalise a first-author identifier (ORCID, staff number, ...)."""

    author_id = value.strip()
    if not author_id:
        raise ValueError("author id must not be empty")
    if len(author_id) > AUTHOR_ID_MAX_LENGTH:
        raise ValueError(
            f"author id must be at most {AUTHOR_ID_MAX_LENGTH} characters"
        )
    return author_id


class PaperCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    doi: str = Field(min_length=1, max_length=512, examples=["10.5555/gw.2024.001"])
    is_group_work: bool = Field(
        default=False,
        description="True when the paper is a work of the research group.",
    )

    @field_validator("doi")
    @classmethod
    def _normalise_doi(cls, value: str) -> str:
        return normalize_doi(value)


class PaperUpdate(BaseModel):
    is_group_work: bool | None = None


class RetractionRequest(BaseModel):
    retracted: bool


class PaperOut(BaseModel):
    doi: str
    is_group_work: bool
    retracted: bool
    created_at: str
    updated_at: str
    retracted_at: str | None = None


class CitationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=128, examples=["crossref"])
    citing_doi: str = Field(min_length=1, max_length=512, examples=["10.5555/citing.1"])
    cited_doi: str = Field(min_length=1, max_length=512, examples=["10.5555/gw.2024.001"])

    @field_validator("source_id")
    @classmethod
    def _normalise_source(cls, value: str) -> str:
        return normalize_source_id(value)

    @field_validator("citing_doi", "cited_doi")
    @classmethod
    def _normalise_doi(cls, value: str) -> str:
        return normalize_doi(value)

    @model_validator(mode="after")
    def _not_self_citing(self) -> CitationCreate:
        if self.citing_doi == self.cited_doi:
            raise ValueError("citing_doi and cited_doi must differ")
        return self


class CitationOut(BaseModel):
    source_id: str
    citing_doi: str
    cited_doi: str
    created_at: str


class CitationRegistrationResult(BaseModel):
    created: bool
    citation: CitationOut


class WithdrawResult(BaseModel):
    withdrawn: bool
    source_id: str
    citing_doi: str
    cited_doi: str


class CitationPair(BaseModel):
    citing_doi: str
    cited_doi: str


class WorkSummary(BaseModel):
    doi: str
    citation_count: int


class StatsOut(BaseModel):
    h_index: int
    group_work_count: int
    total_citations: int
    group_works: list[WorkSummary]


class RetractionImpact(BaseModel):
    newly_counted: list[CitationPair]
    no_longer_counted: list[CitationPair]


class StatusChangeResult(BaseModel):
    paper: PaperOut
    stats: StatsOut
    impact: RetractionImpact


class FirstAuthorEntry(BaseModel):
    """Librarian-supplied first-author knowledge for one registered DOI.

    Exactly one of two shapes is accepted:

    * ``author_id`` — the first author is confirmed;
    * ``candidate_ids`` — exactly two *different* candidates; the first
      author is one of them, but which one is still being verified.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    author_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=AUTHOR_ID_MAX_LENGTH,
        examples=["orcid:0000-0002-1825-0097"],
    )
    candidate_ids: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        examples=[["orcid:0000-0002-1825-0097", "orcid:0000-0001-5109-3700"]],
    )

    @field_validator("author_id")
    @classmethod
    def _normalise_author_id(cls, value: str | None) -> str | None:
        return normalize_author_id(value) if value is not None else None

    @field_validator("candidate_ids")
    @classmethod
    def _normalise_candidates(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [normalize_author_id(item) for item in value]
        if cleaned[0] == cleaned[1]:
            raise ValueError("candidate_ids must be two different author ids")
        return cleaned

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> FirstAuthorEntry:
        if (self.author_id is None) == (self.candidate_ids is None):
            raise ValueError("provide exactly one of author_id or candidate_ids")
        return self


class FirstAuthorOut(BaseModel):
    doi: str
    author_id: str | None = None
    candidate_ids: list[str] | None = None
    created_at: str
    updated_at: str


class FirstAuthorClearResult(BaseModel):
    doi: str
    cleared: bool


class HIndexScenario(BaseModel):
    """One hypothesis: an achievable h-index plus the pending-DOI choices
    (one first-author id per to-be-verified DOI) that realise it."""

    h_index: int
    assignments: dict[str, str]


class FirstAuthorHIndexOut(BaseModel):
    """Result of the first-author self-citation analysis.

    ``status`` is ``"ok"`` when the min/max h-index could be computed and
    ``"blocked"`` when input data is missing or the hypothesis space is too
    large; in the blocked case ``reasons`` says why and the DOI lists name
    the offending records instead of returning numbers.
    """

    status: Literal["ok", "blocked"]
    reasons: list[Literal["missing_author_data", "too_many_pending_dois"]] = []
    missing_author_dois: list[str] = []
    pending_dois: list[str] = []
    minimum: HIndexScenario | None = None
    maximum: HIndexScenario | None = None
