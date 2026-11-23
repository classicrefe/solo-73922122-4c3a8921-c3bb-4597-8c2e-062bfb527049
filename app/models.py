"""Request/response models and DOI normalisation."""

from __future__ import annotations

import re

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
