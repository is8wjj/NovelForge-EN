
from sqlmodel import SQLModel, Field, Relationship, Column, JSON
from sqlalchemy import UniqueConstraint
import sqlalchemy as sa
from typing import Optional, List, Any
from datetime import datetime


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: Optional[str] = None

    cards: List["Card"] = Relationship(back_populates="project", sa_relationship_kwargs={"cascade": "all, delete-orphan"})



class LLMConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    display_name: Optional[str] = None
    model_name: str
    api_base: Optional[str] = None
    api_key: str
    # Must include server_default here, otherwise the startup auto-add-column logic
    # will not consider it a "safe appended column". A Python default alone is not
    # enough; legacy databases need a database-side default during ALTER TABLE to
    # backfill historical rows.
    api_protocol: str = Field(
        default="chat_completions",
        sa_column=Column(sa.String, nullable=False, server_default="chat_completions"),
    )
    custom_request_path: Optional[str] = None
    models_path: Optional[str] = None
    user_agent: Optional[str] = None
    base_url: Optional[str] = None  # Legacy compatibility field; new implementations converge on api_base
    # Statistics and quota (-1 means unlimited) — also set server_default at the DB layer so Alembic auto-includes it
    token_limit: int = Field(
        default=-1,
        sa_column=Column(sa.Integer, nullable=False, server_default='-1')
    )
    call_limit: int = Field(
        default=-1,
        sa_column=Column(sa.Integer, nullable=False, server_default='-1')
    )
    used_tokens_input: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default='0')
    )
    used_tokens_output: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default='0')
    )
    used_calls: int = Field(
        default=0,
        sa_column=Column(sa.Integer, nullable=False, server_default='0')
    )
    # RPM/TPM are placeholders only, not implemented yet
    rpm_limit: int = Field(
        default=-1,
        sa_column=Column(sa.Integer, nullable=False, server_default='-1')
    )
    tpm_limit: int = Field(
        default=-1,
        sa_column=Column(sa.Integer, nullable=False, server_default='-1')
    )
    capability_summary: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    recommended_assistant_mode: str = Field(
        default="auto",
        sa_column=Column(sa.String, nullable=False, server_default="auto"),
    )
    disable_stream: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
    )
    capability_last_checked_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(sa.DateTime, nullable=True),
    )


class Prompt(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: Optional[str] = None
    template: str
    version: int = 1
    built_in: bool = Field(default=False)



class CardType(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    # Compatible with legacy model names (e.g. CharacterCard/SceneCard); if empty, defaults to name
    model_name: Optional[str] = Field(default=None, index=True)
    description: Optional[str] = None
    # Type built-in structure (JSON Schema)
    json_schema: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Type-level default AI params (model ID / prompt / sampling, etc.)
    ai_params: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    editor_component: Optional[str] = None  # e.g., 'NovelEditor' for custom UI
    is_ai_enabled: bool = Field(default=True)
    is_singleton: bool = Field(default=False)  # e.g., only one 'Synopsis' card per project
    built_in: bool = Field(default=False)
    # Card-type-level default context injection template
    default_ai_context_template: Optional[str] = Field(default=None)
    default_ai_context_template_review: Optional[str] = Field(default=None)
    # UI layout (optional), used by the frontend SectionedForm
    ui_layout: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    cards: List["Card"] = Relationship(back_populates="card_type")


class Card(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    # Compatible with legacy model names; if empty, follows the type's model_name or type name
    model_name: Optional[str] = Field(default=None, index=True)
    content: Any = Field(default={}, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)

    # Allow instance-level custom structure; if empty, follows the type
    json_schema: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    # Instance-level AI params; if empty, follows the type
    ai_params: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    # Self-referential relationship, used for tree structures
    parent_id: Optional[int] = Field(default=None, foreign_key="card.id")
    parent: Optional["Card"] = Relationship(
        back_populates="children",
        sa_relationship_kwargs={"remote_side": "[Card.id]"}
    )
    children: List["Card"] = Relationship(
        back_populates="parent",
        sa_relationship_kwargs={
            "cascade": "all, delete, delete-orphan",
            "single_parent": True,
        },
    )

    # Project foreign key
    project_id: int = Field(foreign_key="project.id")
    project: "Project" = Relationship(back_populates="cards")

    # Card type foreign key
    card_type_id: int = Field(foreign_key="cardtype.id")
    card_type: "CardType" = Relationship(back_populates="cards")

    # Used for card sorting, for ordering under the same parent
    display_order: int = Field(default=0)
    ai_context_template: Optional[str] = Field(default=None)
    ai_context_template_review: Optional[str] = Field(default=None)
    
    # AI modification status flags
    ai_modified: bool = Field(default=False)  # Whether modified by AI
    needs_confirmation: bool = Field(default=False)  # Whether user confirmation is needed (used to trigger workflow)
    last_modified_by: Optional[str] = Field(default=None)  # Last modifier: 'user' | 'ai' | None

# Foreshadow registry table
class ForeshadowItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    chapter_id: Optional[int] = Field(default=None)  # Chapter card ID or chapter ID
    title: str
    type: str = Field(default='other', index=True)  # goal | item | person | other
    note: Optional[str] = None
    status: str = Field(default='open', index=True)  # open | resolved
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    resolved_at: Optional[datetime] = None


# Living Bible: pending update proposals awaiting user review.
# A first-class table (not a card) because proposals are transient review state
# with per-change decisions, and must never be injected into generation context.
class BibleUpdateReview(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    chapter_card_id: Optional[int] = Field(default=None, index=True)
    volume_number: Optional[int] = None
    chapter_number: Optional[int] = None
    # pending | partially_applied | applied | dismissed
    status: str = Field(default="pending", index=True)
    proposal_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    decisions_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


# Knowledge base model
class Knowledge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: Optional[str] = None
    content: str
    built_in: bool = Field(default=False)


# Workflow system
class Workflow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    dsl_version: int = Field(default=2)  # DSL version: 2=code-style workflow
    is_built_in: bool = Field(default=False)
    is_active: bool = Field(default=True)
    
    # Workflow definition (code-style)
    definition_code: str = Field(default="")  # Workflow code
    
    # Workflow template
    is_template: bool = Field(default=False)
    template_category: Optional[str] = None  # e.g.: "Content Generation", "Data Processing"
    
    # Run data retention policy
    # True: long-term retention (subject to the global validity period)
    # False: short-term retention (only for the frontend to view results, auto-cleaned afterwards)
    keep_run_history: bool = Field(default=False)
    
    # Trigger cache (optimizes performance, avoids querying the WorkflowTrigger table separately)
    triggers_cache: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    """Trigger cache (auto-extracted from code)
    
    Structure:
    [
        {
            "trigger_on": "onsave",           # Trigger event type
            "card_type_name": "Chapter",      # Card type (optional)
            "filter_json": {                  # Filter config (optional)
                "events": ["create", "update"],
                "conditions": [...]
            }
        },
        ...
    ]
    
    Advantages:
    - 100x startup performance improvement (5ms vs 500ms)
    - Avoids data redundancy and sync issues
    - Code is the single source of truth
    """
    
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)

    # Relations
    runs: List["WorkflowRun"] = Relationship(back_populates="workflow", sa_relationship_kwargs={
        "cascade": "all, delete-orphan"
    })


class WorkflowRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_id: int = Field(foreign_key="workflow.id")
    workflow: Workflow = Relationship(back_populates="runs")

    definition_version: int = Field(default=1)
    # queued | running | succeeded | failed | cancelled | paused | timeout
    status: str = Field(default="queued", index=True)
    scope_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    params_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    idempotency_key: Optional[str] = Field(default=None, index=True)
    
    # Execution status
    state_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))  # Runtime state (variables, node outputs, etc.)
    error_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))  # Error info
    
    # Time control
    max_execution_time: Optional[int] = None  # Seconds; None means unlimited
    
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    
    # Relations
    node_states: List["NodeExecutionState"] = Relationship(back_populates="run", sa_relationship_kwargs={
        "cascade": "all, delete-orphan"
    })


class NodeExecutionState(SQLModel, table=True):
    """Node execution state table - used to track each node's execution in detail"""
    __tablename__ = "nodeexecutionstate"
    __table_args__ = (
        # Add a unique constraint: the same node in the same run can only have one record
        UniqueConstraint('run_id', 'node_id', name='uq_run_node'),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflowrun.id", index=True)
    run: WorkflowRun = Relationship(back_populates="node_states")

    node_id: str = Field(index=True)  # Node ID (from DSL)
    node_type: str  # Node type

    # Execution status: idle | pending | running | success | error | skipped
    status: str = Field(default="idle", index=True)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    progress: int = Field(default=0)  # 0-100
    
    # Node output (used for checkpoint resume)
    outputs_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    """Node output data (used for execution recovery)
    
    When a workflow is resumed after being paused, the outputs of completed nodes
    need to be read from here so that subsequent nodes can access the results
    of their predecessors.
    
    Example:
    {
        "project_id": 123,
        "card_id": 456,
        "result": {...}
    }
    """
    
    # Error info (simplified)
    error_message: Optional[str] = None
    
    # Checkpoint data (new)
    checkpoint_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    """Checkpoint data (lightweight metadata)
    
    Structure:
    {
        "percent": 50.0,                    # Progress percentage
        "message": "Processed 30/60",       # Progress message
        "data": {                           # Node custom data (optional)
            "processed_count": 30,          # ✅ Lightweight: counter
            "last_item_id": "item_30",      # ✅ Lightweight: identifier
            "current_batch": 3              # ✅ Lightweight: batch number
        },
        "timestamp": "2026-02-04T10:30:00"  # Save time
    }
    
    Notes:
    - The data field only stores positional info, not business data
    - Size limit: < 10KB
    - Used for checkpoint resume; nodes access it via context.checkpoint
    """
    
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)

class KGRelation(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("project_id", "source", "target", "kind_en", name="uq_kg_relation_key"),
        sa.Index("ix_kg_relation_project_source", "project_id", "source"),
        sa.Index("ix_kg_relation_project_target", "project_id", "target"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    source: str = Field(index=True)
    target: str = Field(index=True)
    kind_en: str = Field(index=True)
    kind_cn: str = Field(default="Other")
    fact: Optional[str] = None
    a_to_b_addressing: Optional[str] = None
    b_to_a_addressing: Optional[str] = None
    recent_dialogues: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    recent_event_summaries: List[dict] = Field(default_factory=list, sa_column=Column(JSON))
    stance: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


# ---------------------------------------------------------------------------
# Forge: reverse-engineering / generation-control tables
# ---------------------------------------------------------------------------

class ReferenceExample(SQLModel, table=True):
    """One short, function-tagged excerpt of the imported source manuscript.

    Excerpts never leave the source project except as technique demonstrations
    inside a compiled generation context; ``evidence_hash`` ties the excerpt to
    the exact imported chapter text.
    """

    __table_args__ = (
        UniqueConstraint("project_id", "manuscript_id", "example_id", name="uq_reference_example_key"),
        sa.Index("ix_reference_example_project_function", "project_id", "beat_function"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    manuscript_id: str = Field(index=True)
    example_id: str
    chapter_card_id: Optional[int] = Field(default=None, index=True)
    chapter_number: int = Field(default=0, index=True)
    span_start: int = Field(default=0)
    span_end: int = Field(default=0)
    excerpt: str
    language: str = Field(default="")
    pov_type: str = Field(default="")
    scene_type: str = Field(default="")
    dominant_emotion: str = Field(default="")
    beat_function: str = Field(default="", index=True)
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    position: str = Field(default="middle")
    dialogue_ratio: float = Field(default=0.0)
    pacing: str = Field(default="")
    entity_roles: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSON))
    retrieval_terms: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    evidence_hash: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)


class ArtifactProvenance(SQLModel, table=True):
    """Dependency-graph record for one derived artifact (card or manifest entry)."""

    __table_args__ = (
        UniqueConstraint("project_id", "artifact_kind", "artifact_key", name="uq_artifact_provenance_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    artifact_kind: str = Field(index=True)
    artifact_key: str = Field(index=True)
    card_id: Optional[int] = Field(default=None, index=True)
    content_hash: str = Field(default="")
    upstream: List[dict] = Field(default_factory=list, sa_column=Column(JSON))
    dependency_hash: str = Field(default="")
    producer: str = Field(default="")
    model_role: str = Field(default="")
    model_name: str = Field(default="")
    prompt_version: str = Field(default="")
    schema_version: str = Field(default="")
    stale: bool = Field(default=False, index=True)
    stale_reason: Optional[str] = None
    version: int = Field(default=1)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


class ProjectManifest(SQLModel, table=True):
    """Project Narrative Manifest: the single authoritative revision record."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(unique=True, index=True)
    project_role: str = Field(default="original")
    source_project_id: Optional[int] = Field(default=None, index=True)
    source_manuscript_id: Optional[str] = None
    canon_revision: int = Field(default=0)
    outline_revision: int = Field(default=0)
    fingerprint_revision: int = Field(default=0)
    latest_committed_chapter: int = Field(default=0)
    next_allowed_chapter: int = Field(default=1)
    context_compiler_version: str = Field(default="")
    unresolved_errors: List[dict] = Field(default_factory=list, sa_column=Column(JSON))
    stale_dependency_count: int = Field(default=0)
    last_sync_status: str = Field(default="never")
    last_sync_chapter: Optional[int] = None
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


class CanonFact(SQLModel, table=True):
    """Temporal, append-only canonical state.

    A fact is valid from ``valid_from_chapter`` until another row for the same
    (subject, attribute) supersedes it. Rows are never mutated after commit, so
    the state as-of any chapter can be reconstructed.
    """

    __table_args__ = (
        sa.Index("ix_canon_fact_lookup", "project_id", "subject", "attribute", "valid_from_chapter"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    fact_id: str = Field(index=True)
    subject: str = Field(index=True)
    subject_kind: str = Field(default="character")
    attribute: str = Field(index=True)
    value: Any = Field(default=None, sa_column=Column(JSON))
    valid_from_chapter: int = Field(default=0, index=True)
    superseded_by_id: Optional[int] = Field(default=None, index=True)
    canon_revision: int = Field(default=0, index=True)
    support: str = Field(default="explicit")
    evidence: List[dict] = Field(default_factory=list, sa_column=Column(JSON))
    source: str = Field(default="sync")
    chapter_card_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)


class ChapterPipelineRun(SQLModel, table=True):
    """One compile -> draft -> validate -> repair -> commit -> sync execution."""

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    chapter_number: int = Field(index=True)
    chapter_card_id: Optional[int] = Field(default=None, index=True)
    outline_card_id: Optional[int] = Field(default=None)
    status: str = Field(default="pending", index=True)
    stage: str = Field(default="compile")
    canon_revision_before: int = Field(default=0)
    canon_revision_after: Optional[int] = None
    context_hash: str = Field(default="")
    context_manifest: dict = Field(default_factory=dict, sa_column=Column(JSON))
    validation_report: dict = Field(default_factory=dict, sa_column=Column(JSON))
    style_report: dict = Field(default_factory=dict, sa_column=Column(JSON))
    sync_report: dict = Field(default_factory=dict, sa_column=Column(JSON))
    repair_attempts: int = Field(default=0)
    model_calls: int = Field(default=0)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)


# ---------------------------------------------------------------------------
# Autonomous novel production (upload -> select -> finished novel)
# ---------------------------------------------------------------------------

class AutonomousNovelJob(SQLModel, table=True):
    """Durable state of one 'Create Novel from EPUB' run.

    ``stage`` is the next stage to execute (or the one executing while
    ``status == 'running'``). Every transition is committed before the next
    stage starts, so a restarted process resumes from ``stage``.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    idempotency_key: str = Field(index=True, unique=True)
    status: str = Field(default="queued", index=True)  # queued|running|waiting_for_user|paused|failed|cancelled|completed
    stage: str = Field(default="INGEST", index=True)
    mode: str = Field(default="fully_automatic")  # fully_automatic|approval_gates|manual
    source_project_id: Optional[int] = Field(default=None, index=True)
    original_project_id: Optional[int] = Field(default=None, index=True)
    llm_config_id: int = Field(default=0)
    role_llm_config_ids: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_filename: str = Field(default="")
    source_file_hash: str = Field(default="")
    source_bytes: Optional[bytes] = Field(default=None, sa_column=Column(sa.LargeBinary))
    options: dict = Field(default_factory=dict, sa_column=Column(JSON))
    selected_storyline_id: Optional[int] = Field(default=None)
    chapter_count: int = Field(default=0)
    chapters_committed: int = Field(default=0)
    progress_percent: float = Field(default=0.0)
    progress_message: str = Field(default="")
    stage_results: dict = Field(default_factory=dict, sa_column=Column(JSON))
    warnings: List[dict] = Field(default_factory=list, sa_column=Column(JSON))
    error: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    model_calls: int = Field(default=0)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    lease_owner: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    # Fencing token: every successful acquisition advances it; publications carry the
    # generation they were acquired under and are rejected when it no longer matches.
    lease_generation: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    heartbeat_at: Optional[datetime] = Field(default=None)
    # Budget ceilings (0 = unlimited) and reserved-but-unreconciled usage.
    budget: dict = Field(default_factory=dict, sa_column=Column(JSON))
    reserved_calls: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    reserved_tokens: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    # Split reservations so input/output/cost caps are enforced before every provider attempt.
    reserved_input_tokens: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    reserved_output_tokens: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    reserved_cost_usd: float = Field(default=0.0, sa_column=Column(sa.Float, nullable=False, server_default="0"))
    # Known accumulated cost; ``cost_unknown_calls`` > 0 means the total is unknown (never reported as zero).
    cost_usd: float = Field(default=0.0, sa_column=Column(sa.Float, nullable=False, server_default="0"))
    cost_unknown_calls: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    usage_estimated_calls: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    repair_calls: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    reserved_repair_calls: int = Field(default=0, sa_column=Column(sa.Integer, nullable=False, server_default="0"))
    # Terminal quality verdict: completed | completed_with_warnings | quality_gate_failed | manual_review_required
    quality_status: Optional[str] = Field(default=None, index=True)
    quality_summary: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.now, nullable=False)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobStageAttempt(SQLModel, table=True):
    """One attempt at one stage of an autonomous job (audit trail + retry accounting)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    stage: str = Field(index=True)
    attempt: int = Field(default=1)
    status: str = Field(default="running", index=True)  # running|succeeded|failed|paused
    failure_category: Optional[str] = Field(default=None)
    recovery_action: Optional[str] = Field(default=None)
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    model_calls: int = Field(default=0)
    started_at: datetime = Field(default_factory=datetime.now, nullable=False)
    finished_at: Optional[datetime] = None


class ModelInvocation(SQLModel, table=True):
    """Every model call made on behalf of an autonomous job: role, prompt version, usage, outcome."""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = Field(default=None, index=True)
    project_id: Optional[int] = Field(default=None, index=True)
    stage: str = Field(default="", index=True)
    role: str = Field(default="", index=True)
    llm_config_id: Optional[int] = Field(default=None)
    model_name: str = Field(default="")
    prompt_version: str = Field(default="")
    schema_name: str = Field(default="")
    temperature: Optional[float] = None
    input_tokens_estimate: int = Field(default=0)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    latency_ms: int = Field(default=0)
    retries: int = Field(default=0)
    validation_status: str = Field(default="ok")  # ok|invalid|error
    error: Optional[str] = None
    prompt_hash: str = Field(default="", sa_column=Column(sa.String, nullable=False, server_default=""))
    total_attempts: int = Field(default=1, sa_column=Column(sa.Integer, nullable=False, server_default="1"))
    selected_attempt: Optional[int] = Field(default=None)
    fallback_used: bool = Field(default=False, sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()))
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)


class ModelInvocationAttempt(SQLModel, table=True):
    """One provider attempt of a logical ``ModelInvocation`` (retries, fallbacks, verifiers).

    Rows are written in their own short transaction so a failed attempt survives the
    surrounding stage rollback. No prompt or response text is stored: hashes plus a
    bounded, redacted diagnostic excerpt only.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    invocation_id: Optional[int] = Field(default=None, index=True)
    job_id: Optional[int] = Field(default=None, index=True)
    attempt: int = Field(default=1)
    provider: str = Field(default="")
    model_name: str = Field(default="")
    llm_config_id: Optional[int] = Field(default=None)
    fallback: bool = Field(default=False)
    role: str = Field(default="", index=True)
    stage: str = Field(default="")
    started_at: datetime = Field(default_factory=datetime.now, nullable=False)
    completed_at: Optional[datetime] = None
    latency_ms: int = Field(default=0)
    status: str = Field(default="ok", index=True)  # ok|invalid|error|timeout|budget_refused
    error_category: Optional[str] = None
    provider_status: Optional[str] = None
    provider_request_id: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    timeout_seconds: Optional[float] = None
    response_hash: str = Field(default="")
    diagnostic: Optional[str] = Field(default=None)
    # Whether the provider reported usage (False -> tokens above are estimates) and the clamped max_tokens sent.
    usage_reported: Optional[bool] = Field(default=None)
    max_tokens: Optional[int] = Field(default=None)


class BudgetReservation(SQLModel, table=True):
    """Ledger of one provider attempt's budget reservation (open -> closed | abandoned).

    The job row holds the aggregate reserved counters; this ledger makes every
    reservation individually recoverable so a crashed worker cannot leave phantom
    reservations behind and a late reconcile cannot release twice.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    role: str = Field(default="")
    stage: str = Field(default="", index=True)
    stage_key: str = Field(default="", index=True)
    llm_config_id: Optional[int] = Field(default=None)
    status: str = Field(default="open", index=True)  # open|dispatched|closed|released|uncertain_charged
    reserved_input_tokens: int = Field(default=0)
    reserved_output_tokens: int = Field(default=0)
    reserved_cost_usd: float = Field(default=0.0)
    charged_input_tokens: int = Field(default=0)
    charged_output_tokens: int = Field(default=0)
    charged_cost_usd: Optional[float] = Field(default=None)
    succeeded: Optional[bool] = Field(default=None)
    usage_reported: Optional[bool] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
    dispatched_at: Optional[datetime] = Field(default=None)
    closed_at: Optional[datetime] = Field(default=None)


class RecoveryAction(SQLModel, table=True):
    """One executed recovery-ladder action with its inputs, outputs and outcome."""

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    stage: str = Field(index=True)
    stage_attempt: int = Field(default=0)
    failure_category: str = Field(default="")
    action: str = Field(index=True)
    reason: str = Field(default="")
    parameters_before: dict = Field(default_factory=dict, sa_column=Column(JSON))
    parameters_after: dict = Field(default_factory=dict, sa_column=Column(JSON))
    input_artifact: Optional[str] = None
    output_artifact: Optional[str] = None
    original_model: str = Field(default="")
    selected_model: str = Field(default="")
    downstream_invalidations: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    validation: dict = Field(default_factory=dict, sa_column=Column(JSON))
    success: bool = Field(default=False)
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=datetime.now, nullable=False)
    finished_at: Optional[datetime] = None


class StorylineCandidate(SQLModel, table=True):
    """One generated original storyline option for a job."""

    __table_args__ = (UniqueConstraint("job_id", "option_index", name="uq_storyline_job_option"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    source_project_id: int = Field(index=True)
    option_index: int = Field(default=0)
    title: str = Field(default="")
    content: dict = Field(default_factory=dict, sa_column=Column(JSON))
    originality_score: float = Field(default=0.0)
    originality_report: dict = Field(default_factory=dict, sa_column=Column(JSON))
    similarity_to_others: dict = Field(default_factory=dict, sa_column=Column(JSON))
    recommended_chapters_min: int = Field(default=0)
    recommended_chapters_max: int = Field(default=0)
    rejected: bool = Field(default=False, index=True)
    rejection_reason: Optional[str] = None
    selected: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)


class ExportArtifact(SQLModel, table=True):
    """A produced deliverable (EPUB, DOCX, Markdown, text, report) stored for download."""

    __table_args__ = (UniqueConstraint("job_id", "kind", name="uq_export_job_kind"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    project_id: int = Field(index=True)
    kind: str = Field(index=True)  # epub|docx|markdown|text|report
    filename: str = Field(default="")
    media_type: str = Field(default="application/octet-stream")
    size_bytes: int = Field(default=0)
    content_hash: str = Field(default="")
    data: bytes = Field(default=b"", sa_column=Column(sa.LargeBinary))
    created_at: datetime = Field(default_factory=datetime.now, nullable=False)
