"""Report citation and evidence metadata validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from schemas import Evidence


CITATION_RE = re.compile(r"\[证据\s*(\d+)\]|\[Evidence\s*(\d+)\]", re.IGNORECASE)


@dataclass
class CitationValidationResult:
    ok: bool
    cited_ids: list[int] = field(default_factory=list)
    missing_ids: list[int] = field(default_factory=list)
    incomplete_evidence_ids: list[int] = field(default_factory=list)
    uncited_evidence_ids: list[int] = field(default_factory=list)

    @property
    def issues(self) -> list[str]:
        issues: list[str] = []
        if self.missing_ids:
            issues.append(f"missing evidence ids: {self.missing_ids}")
        if self.incomplete_evidence_ids:
            issues.append(f"incomplete evidence metadata: {self.incomplete_evidence_ids}")
        return issues


def extract_citation_ids(markdown: str) -> list[int]:
    ids: list[int] = []
    for match in CITATION_RE.finditer(markdown or ""):
        raw = match.group(1) or match.group(2)
        if raw:
            ids.append(int(raw))
    return sorted(set(ids))


def validate_report_citations(markdown: str, evidence_pool: Iterable[Evidence]) -> CitationValidationResult:
    evidence_by_id = {idx: ev for idx, ev in enumerate(evidence_pool, 1)}
    cited_ids = extract_citation_ids(markdown)
    missing_ids = [idx for idx in cited_ids if idx not in evidence_by_id]
    incomplete_ids = [
        idx
        for idx in cited_ids
        if idx in evidence_by_id and not _has_required_metadata(evidence_by_id[idx])
    ]
    uncited_ids = [idx for idx in evidence_by_id if idx not in cited_ids]
    ok = not missing_ids and not incomplete_ids
    return CitationValidationResult(
        ok=ok,
        cited_ids=cited_ids,
        missing_ids=missing_ids,
        incomplete_evidence_ids=incomplete_ids,
        uncited_evidence_ids=uncited_ids,
    )


def _has_required_metadata(evidence: Evidence) -> bool:
    has_source = bool((evidence.source_url or "").strip())
    has_title = bool((evidence.source_title or "").strip())
    has_date = bool((evidence.published_at or evidence.as_of or evidence.fetched_at or "").strip())
    return has_source and has_title and has_date
