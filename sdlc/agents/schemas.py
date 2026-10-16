"""Structured output contracts for every agent role.

Agents finish by calling `submit(result=...)`; the result is validated against these
models before it is accepted, so downstream stages always receive well-formed artifacts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["must", "should", "could"]
Level = Literal["low", "medium", "high"]
Severity = Literal["low", "medium", "high", "critical"]


# ------------------------------------------------------------------ requirements
class FunctionalRequirement(BaseModel):
    id: str = Field(pattern=r"^FR-\d+$")
    title: str
    description: str
    acceptance_criteria: list[str] = Field(min_length=1)
    priority: Priority = "must"


class NonFunctionalRequirement(BaseModel):
    id: str = Field(pattern=r"^NFR-\d+$")
    category: Literal[
        "performance",
        "reliability",
        "security",
        "privacy",
        "operability",
        "maintainability",
        "scalability",
        "compliance",
    ]
    requirement: str
    measure: str = Field(description="how it will be verified")


class Ambiguity(BaseModel):
    id: str = Field(pattern=r"^Q-\d+$")
    question: str
    why: str = Field(description="what changes in the build depending on the answer")
    options: list[str] = Field(default_factory=list)
    default: str = Field(description="the assumption used if nobody answers")
    blocking: bool = Field(description="true only if building on the default would risk significant rework")
    resolved_by: str | None = Field(default=None, description="answer text when already resolved")


class Risk(BaseModel):
    id: str
    description: str
    likelihood: Level = "medium"
    impact: Level = "medium"
    mitigation: str


class RequirementsSpec(BaseModel):
    title: str
    problem_statement: str
    functional: list[FunctionalRequirement] = Field(min_length=1)
    non_functional: list[NonFunctionalRequirement] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)


# ------------------------------------------------------------------- brownfield
class ModuleInfo(BaseModel):
    path: str
    responsibility: str


class ImpactedFile(BaseModel):
    path: str
    change: Literal["modify", "add", "delete"]
    reason: str


class ExistingDefect(BaseModel):
    description: str
    location: str
    evidence: str


class ImpactAnalysis(BaseModel):
    summary: str
    architecture: list[ModuleInfo]
    impacted_files: list[ImpactedFile]
    apis_affected: list[str] = Field(default_factory=list)
    data_model_changes: list[str] = Field(default_factory=list)
    data_flows: list[str] = Field(
        default_factory=list, description="request/data paths touched by the change"
    )
    regression_areas: list[str] = Field(default_factory=list)
    existing_defects: list[ExistingDefect] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)


# ------------------------------------------------------------------------ design
class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(description="FastAPI-style path, e.g. /api/v1/links/{code}")
    summary: str
    request: str = ""
    responses: dict[str, str] = Field(description="status code -> meaning")
    fr_ids: list[str] = Field(default_factory=list)


class FieldDef(BaseModel):
    name: str
    type: str
    constraints: str = ""


class Entity(BaseModel):
    name: str
    fields: list[FieldDef]
    indexes: list[str] = Field(default_factory=list)


class ADR(BaseModel):
    id: str = Field(pattern=r"^ADR-\d+$")
    title: str
    decision: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)


class WorkPackage(BaseModel):
    id: str = Field(pattern=r"^WP-\d+$")
    title: str
    description: str = Field(description="precise instructions for the implementer")
    files: list[str] = Field(min_length=1, description="exact relative file paths this package owns")
    depends_on: list[str] = Field(default_factory=list)
    fr_ids: list[str] = Field(default_factory=list)
    risk: Level = "low"


class Component(BaseModel):
    name: str
    responsibility: str
    files: list[str] = Field(default_factory=list)


class Design(BaseModel):
    overview: str
    components: list[Component]
    api_contract: list[Endpoint] = Field(min_length=1)
    data_model: list[Entity] = Field(default_factory=list)
    decisions: list[ADR] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    work_packages: list[WorkPackage] = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list, description="pip packages the service needs")
    test_strategy: str
    config: list[str] = Field(default_factory=list, description="environment variables and defaults")


# ---------------------------------------------------------------- build/verify
class ImplementationReport(BaseModel):
    summary: str
    files_changed: list[str]
    notes: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(
        default_factory=list, description="anything left undone or needing another WP"
    )


class Defect(BaseModel):
    description: str
    location: str
    failing_test: str = ""


class TestSuiteReport(BaseModel):
    summary: str
    test_files: list[str]
    fr_coverage: dict[str, list[str]] = Field(description="FR id -> test function names that verify it")
    defects_found: list[Defect] = Field(
        default_factory=list, description="real product defects your tests exposed (not test bugs)"
    )


class SecurityFinding(BaseModel):
    id: str
    severity: Severity
    title: str
    location: str
    recommendation: str


class SecurityReview(BaseModel):
    summary: str
    findings: list[SecurityFinding] = Field(default_factory=list)


class DocsReport(BaseModel):
    summary: str
    files: list[str]
