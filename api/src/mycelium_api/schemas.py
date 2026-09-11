"""API I/O schemas (pydantic v2). No business logic."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mycelium_core.models.agent_run import AgentRunStatus
from mycelium_core.models.agent_token import WorkspaceBinding
from mycelium_core.models.ai_assistant import AssistantRuntime
from mycelium_core.models.billing import CostBasis, RateUnit, StorageKind
from mycelium_core.models.budget import BudgetPeriod
from mycelium_core.models.dependency import DependencyType
from mycelium_core.models.dispatch_request import AutonomousDispatch, DispatchStatus
from mycelium_core.models.email import EmailAccountStatus, EmailProvider
from mycelium_core.models.executor import ExecutorKind
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.invoice import (
    ConservationStatus,
    DocumentType,
    InvoiceKind,
    InvoiceState,
    PaymentStatus,
    SdiStatus,
)
from mycelium_core.models.note import NoteKind, NoteStatus, TurnRole
from mycelium_core.models.notification import (
    NotificationChannelKind,
    NotificationStatus,
    RecurrenceFreq,
)
from mycelium_core.models.tag import TagKind
from mycelium_core.models.task import (
    ConstraintKind,
    ExecKind,
    Necessity,
    ScheduleMode,
    SchedulePolicy,
)
from mycelium_core.models.task_handoff import HandoffStatus
from mycelium_core.models.time_entry import TimeSource


class SignupIn(BaseModel):
    # Personal-first: a personal workspace is auto-provisioned. Naming it
    # is optional (defaults to a personal workspace); the user never has
    # to "create an organization" to sign up.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    workspace_name: str | None = Field(default=None, max_length=200)


class SignupOut(BaseModel):
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    # None when email verification is required: the SPA shows a
    # "check your email" state instead of logging the user in.
    token: str | None = None
    refresh_token: str | None = None
    email_verification_required: bool = False


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    """Access JWT plus a long-lived rotating refresh token. The SPA
    persists both; ``/auth/refresh`` mints a new pair before the
    access token expires, so an actively-used session never logs the
    user out."""

    token: str
    refresh_token: str


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class LogoutIn(BaseModel):
    """Optional refresh token: posting it lets the server revoke the
    entire refresh family on logout (not just the current access
    JWT). The SPA always sends it; legacy clients that don't are
    still logged out from the access JWT."""

    refresh_token: str | None = Field(default=None, max_length=512)


class MeOut(BaseModel):
    """Canonical identity for the SPA (hydrated on load). is_admin is
    server-checked here; the JWT claim is only a render hint."""

    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    # IANA timezone (NULL = UTC). Drives local-time reminder labels.
    timezone: str | None = None
    # Minutes after local midnight that a date-only task's reminders fire
    # (0 = start of day; 360 = 06:00). See ``users.day_start_minute``.
    day_start_minute: int = 0
    # UI / notification locale ("it" | "en"); NULL = default ("en").
    language: str | None = None
    is_admin: bool
    # Whether the user has a generated mycelium avatar stored; the bytes are
    # served separately by GET /auth/me/avatar.
    has_avatar: bool = False
    # The avatar's fingerprint + colours, so a "avatar + QR" issuer logo can
    # reuse the SAME mycelium (keeping the logo aligned with the avatar instead
    # of drifting to a fresh random one).
    avatar_seed: str | None = None
    avatar_bg: str | None = None
    avatar_net: str | None = None


class MePatchIn(BaseModel):
    """Profile update for the caller: the IANA timezone (reminder labels;
    an explicit empty/null clears it -> UTC) and ``day_start_minute`` (the
    minute after local midnight a date-only task's reminders fire; null
    resets it to 0). Only the fields actually sent are applied. Validated
    server-side."""

    timezone: str | None = Field(default=None, max_length=64)
    day_start_minute: int | None = Field(default=None, ge=0, le=1439)
    language: str | None = Field(default=None, max_length=8)


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None
    is_admin: bool
    is_active: bool
    email_verified: bool
    mfa_enabled: bool
    created_at: datetime.datetime


class AdminUserPatchIn(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


class SdiEnvironmentOut(BaseModel):
    """Global SdI environment switch (admin). ``environment`` selects which
    endpoint the live RiceviFile send targets; the two URLs come from config."""

    environment: str  # 'test' | 'production'
    sdicoop_active: bool
    test_url: str
    prod_url: str
    active_endpoint: str
    # The accredited channel's own identity, reflected read-only from the
    # deployment. It is NOT editable here: it is welded to the client
    # certificate it was accredited with, and nothing in the system can detect
    # a mismatch between the two (a wrong-but-well-formed code surfaces only as
    # SdI scarto 00300, after the fiscal identifiers are already burnt). Shown
    # because an operator who cannot see what the running process holds cannot
    # tell a value that matters from one nothing reads.
    intermediary_id_paese: str
    intermediary_id_codice: str
    #: The one thing a shape check cannot decide: an 11-digit value is right for
    #: a company and wrong for a physical person, whose codice fiscale is the
    #: 16-character form. Surfaced next to the field because the failure it
    #: predicts is otherwise invisible until SdI scarta a real invoice (00300).
    intermediary_id_codice_warning: str | None
    # Presence, never the value: these name files mounted from k8s secrets.
    client_cert_configured: bool
    client_key_configured: bool
    ca_bundle_configured: bool


class SdiEnvironmentIn(BaseModel):
    environment: Literal["test", "production"]


class SdiIntermediaryIn(BaseModel):
    """The accredited channel's fiscal code. Empty clears the override and
    returns the deployment to its env value."""

    id_codice: str = Field(default="", max_length=28)


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class EmailIn(BaseModel):
    """Used by resend-verification and forgot-password (enumeration-safe;
    the response never reveals whether the address exists)."""

    email: str = Field(min_length=3, max_length=320)


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=8, max_length=200)


class LoginMfaIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    totp_code: str = Field(min_length=6, max_length=12)


class MfaSetupOut(BaseModel):
    provisioning_uri: str
    qr_png_base64: str
    secret: str = Field(description="Base32 TOTP secret; shown once at setup.")


class MfaActivateIn(BaseModel):
    totp_code: str = Field(min_length=6, max_length=12)


class MfaActivateOut(BaseModel):
    backup_codes: list[str] = Field(description="Shown once; store securely.")
    enabled_at: datetime.datetime


class MfaDisableIn(BaseModel):
    code: str = Field(min_length=6, max_length=12, description="TOTP or a backup code.")


class MfaStatusOut(BaseModel):
    enabled: bool
    pending: bool
    enabled_at: datetime.datetime | None
    backup_codes_remaining: int


DEFAULT_ESTIMATE_PRESETS: list[Decimal] = [
    Decimal("0.5"),
    Decimal("1"),
    Decimal("4"),
    Decimal("8"),
]


class WorkspaceSettings(BaseModel):
    # Task-estimate dropdown values, in hours. Configurable per
    # workspace; the task form adds a "custom value" beyond these.
    estimate_presets: list[Decimal] = Field(default_factory=lambda: list(DEFAULT_ESTIMATE_PRESETS))
    default_client_tag_id: uuid.UUID | None = None
    # The autonomous-dispatch policy (docs/adr/0025 P5). Governance
    # default is human-in-the-loop: an unset value is ``approval_required``
    # (the loop creates pending requests a human must approve), never
    # ``auto`` (no silent auto-spend).
    autonomous_dispatch: AutonomousDispatch = AutonomousDispatch.approval_required
    # Semantic-similarity floor for memory retrieval (cosine, 0..1).
    # 0.0 disables the gate (every kNN neighbour is kept). A positive
    # value drops far semantic neighbours so a keyword/name query is not
    # flooded by noise that ties with the real lexical hits under
    # rank-only RRF. Tuned live here; lexical matches are never gated.
    retrieval_semantic_min_similarity: float = 0.0
    # Grader/abstain floor on the fused RRF score (WS-B1). 0.0 disables it
    # (the first weak hit is always returned). A positive value makes a
    # query with no real match abstain ([] / low-confidence) instead of
    # surfacing the top lexical noise. Tuned live here.
    retrieval_grader_min_rrf: float = 0.0
    # Autonomous metabolism budget (WS-F5). The kill-switch (default on) and
    # the daily system-spend cap (0 = unlimited) that pause the autonomous
    # garden sweep + embedding backfill without touching user actions.
    autonomous_jobs_enabled: bool = True
    autonomous_daily_credit_cap: float = 0.0
    # Per-workspace buffered-attachment size cap (bytes), admin-tunable.
    # The /me handler fills these with the EFFECTIVE value (the override
    # or, absent one, the config default -- always clamped to the
    # ceiling) and the hard ceiling, so the settings page can render the
    # current cap and bound its input. Placeholders here; never the bag.
    attachment_max_bytes: int = 0
    attachment_max_bytes_ceiling: int = 0


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    # The caller's *raw* membership role (the entitlement ceiling): the
    # SPA uses it to know which roles its "act as" switch may offer. A
    # freshly created workspace's caller is always its owner; /me sets
    # this explicitly from the membership (or "owner" for a global
    # admin acting without one).
    my_role: str = "owner"
    # Lifecycle of the workspace itself ("active" | "archived"). The
    # summary list has always carried it; /me did not, so the SPA could
    # only learn that the workspace it is CURRENTLY IN is archived by
    # separately fetching the whole list. The switcher badges the active
    # workspace from this field.
    status: str = "active"


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    role: str
    created_at: datetime.datetime


class MemberAddIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(min_length=1, max_length=16)


class MemberRoleIn(BaseModel):
    role: str = Field(min_length=1, max_length=16)


class WorkspacePatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class WorkspaceSettingsIn(BaseModel):
    expected_version: int = Field(ge=1)
    estimate_presets: list[Decimal] = Field(min_length=1, max_length=20)
    # The autonomous-dispatch policy (docs/adr/0025 P5). Optional so an
    # estimate-presets save does not have to restate it; only written
    # when present (owner-gated, like the rest of the namespace).
    autonomous_dispatch: AutonomousDispatch | None = None
    # Memory-retrieval semantic-similarity floor (cosine, 0..1; 0 = off).
    # Optional so an estimate-presets save does not restate it; only
    # written when present. Tuned live from the SPA settings page.
    retrieval_semantic_min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    # Grader/abstain floor on the fused RRF score (WS-B1; 0 = off). Optional
    # so an estimate-presets save does not restate it; only written when
    # present. Tuned live from the SPA settings page. The ceiling is the
    # fused-RRF domain (~0.03 best case with k=60), NOT 1.0: a [0,1] range let
    # a value like 0.5 make every query abstain (see GRADER_MIN_RRF_MAX).
    # Typical useful band: 0.005-0.02.
    retrieval_grader_min_rrf: float | None = Field(default=None, ge=0.0, le=0.05)
    # Autonomous metabolism budget (WS-F5; both optional, written only when
    # present). Kill-switch + daily system-spend cap (>= 0; 0 = unlimited).
    autonomous_jobs_enabled: bool | None = None
    autonomous_daily_credit_cap: float | None = Field(default=None, ge=0.0)
    # Per-workspace buffered-attachment size cap (bytes), admin-tunable.
    # Optional so an estimate-presets save does not restate it; only
    # written when present. ge=1 is the floor; the endpoint clamps to the
    # runtime config ceiling (the SPA also bounds its input to it).
    attachment_max_bytes: int | None = Field(default=None, ge=1)

    @field_validator("estimate_presets")
    @classmethod
    def _positive_sorted_unique(cls, v: list[Decimal]) -> list[Decimal]:
        if any(x <= 0 for x in v):
            raise ValueError("estimate presets must be positive")
        return sorted(set(v))


class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceSummaryOut(BaseModel):
    """A workspace the authenticated user belongs to (pre-tenant
    selection, for the in-app switcher)."""

    id: uuid.UUID
    name: str
    role: str
    status: str


class VersionOut(BaseModel):
    id: uuid.UUID
    version: int


# Alias used by the workspace router PATCH response.
WorkspaceVersionOut = VersionOut


class RevisionOut(BaseModel):
    """One entry of the recovery-history timeline for a task or note.

    ``sealed_at = None`` flags the open-window revision the SPA can
    label "editing in progress"; everything else is immutable.
    """

    id: uuid.UUID
    entity_kind: str
    entity_id: uuid.UUID
    snapshot: dict[str, Any]
    changed_fields: list[str]
    channel: str
    actor_id: uuid.UUID | None
    actor_kind: str
    actor_subject_id: uuid.UUID | None
    edit_session_id: str | None
    version_from: int
    version_to: int
    # 1-based chronological position of this revision among ALL of the
    # entity's revisions (1 = first ever). The SPA timeline shows it as
    # ``v{n}``. Unlike ``version_to`` (the entity ROW version, which a
    # part-level edit does NOT bump — so a parts-based note's rows would
    # all read v1), this increments once per revision. Only the list
    # endpoints populate it; NULL on the single-revision GET.
    seq: int | None = None
    edit_count: int
    started_at: datetime.datetime
    last_edit_at: datetime.datetime
    sealed_at: datetime.datetime | None
    restored_from: uuid.UUID | None
    # Free-text "speaking name" the user (or the LLM sweep, when wired)
    # can attach to a revision. NULL = no label; SPA falls back to the
    # ``changed_fields`` list. See migration 0010.
    summary: str | None = None


class RevisionSummaryIn(BaseModel):
    """Body of ``PATCH /{tasks|notes}/{id}/revisions/{rev_id}``.

    ``summary=None`` clears the label back to the changed_fields
    fallback. No ``expected_version``: the summary is metadata
    decoupled from the snapshot, and a stale write merely overwrites
    the previous label.
    """

    summary: str | None = Field(default=None, max_length=200)


class RevisionRestoreIn(BaseModel):
    """Body of ``POST /{tasks|notes}/{id}/revisions/{rev_id}/restore``.

    ``fields`` narrows the restore to a subset of the snapshot's
    restorable fields; ``None`` restores everything the policy
    allows. ``expected_version`` is the standard optimistic-lock
    guard against a stale UI.
    """

    expected_version: int = Field(ge=1)
    fields: list[str] | None = None


class EditSessionSealIn(BaseModel):
    """Body of ``POST /{tasks|notes}/{id}/edit-session/seal``.

    Idempotent close of the open web revision matching this
    ``edit_session_id``. Returns ``{sealed: <int>}`` (the count of
    rows actually transitioned to sealed)."""

    edit_session_id: str = Field(min_length=1, max_length=128)


class EditSessionSealOut(BaseModel):
    sealed: int


class TrashEmptyOut(BaseModel):
    """Counts of rows purged by ``POST /workspaces/me/trash/empty``.
    ``note_parts`` counts trashed note BLOCKS (migration 0089), which
    the bin holds independently of their notes -- a live note can have
    trashed parts, and emptying the bin purges those too."""

    tasks: int
    notes: int
    note_parts: int = 0


class TagCreateIn(BaseModel):
    kind: TagKind
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)


class TagPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16)


class TagOut(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None
    status: str
    # Scope: project/client tag ids this tag is restricted to. Empty =
    # global (available everywhere).
    scope_target_ids: list[uuid.UUID] = []
    version: int


class TagScopeIn(BaseModel):
    target_ids: list[uuid.UUID] = []


class TagBrief(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None


class ClientCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    legal_name: str = Field(min_length=1, max_length=200)
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    country_code: str | None = Field(default=None, max_length=2)
    vat_number: str | None = Field(default=None, max_length=30)
    tax_code: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=200)
    civic_number: str | None = Field(default=None, max_length=8)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=4)
    country: str | None = Field(default=None, max_length=2)
    sdi_code: str | None = Field(default=None, max_length=7)
    pec: str | None = Field(default=None, max_length=320)
    # Per-client invoice sezionale (series prefix). None -> auto-derived from
    # the name on the first invoice. Optional override here.
    invoice_series: str | None = Field(default=None, max_length=20)
    # Client-specific payment IBAN (precedence: invoice > client >
    # issuer). Optional.
    payment_iban: str | None = Field(default=None, max_length=34)
    description: str | None = None
    default_billable: bool = True
    hourly_rate: Decimal | None = None
    currency: str = Field(default="EUR", max_length=3)
    # IANA timezone name (e.g. "Europe/Rome"); optional.
    timezone: str | None = Field(default=None, max_length=64)
    # Per-client payment defaults (FatturaPA TPxx / MPxx). NULL = inherit
    # from the issuer (then system default TP02 / MP05).
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    # Locale for the courtesy PDF when the client is foreign (BCP47 tag,
    # e.g. "it", "en", "de"). NULL -> "it". The FatturaPA XML stays
    # untouched (SdI ignores this field; legal purpose/dicitura remain
    # in Italian regardless).
    invoice_language: str | None = Field(default=None, max_length=8)
    # Date format token for the courtesy PDF (closed set validated at the
    # service layer: YYYY-MM-DD | DD-MM-YYYY | DD/MM/YYYY | MM/DD/YYYY |
    # DD.MM.YYYY). NULL -> ISO. Courtesy-PDF only; the XML is unaffected.
    invoice_date_format: str | None = Field(default=None, max_length=16)


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    client_tag_id: uuid.UUID | None = None
    budget: Decimal | None = None
    color: str | None = Field(default=None, max_length=16)
    description: str | None = None


class ClientPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    country_code: str | None = Field(default=None, max_length=2)
    vat_number: str | None = Field(default=None, max_length=30)
    tax_code: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=200)
    civic_number: str | None = Field(default=None, max_length=8)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=4)
    country: str | None = Field(default=None, max_length=2)
    sdi_code: str | None = Field(default=None, max_length=7)
    pec: str | None = Field(default=None, max_length=320)
    invoice_series: str | None = Field(default=None, max_length=20)
    payment_iban: str | None = Field(default=None, max_length=34)
    description: str | None = None
    default_billable: bool | None = None
    hourly_rate: Decimal | None = None
    currency: str | None = Field(default=None, max_length=3)
    timezone: str | None = Field(default=None, max_length=64)
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    invoice_language: str | None = Field(default=None, max_length=8)
    invoice_date_format: str | None = Field(default=None, max_length=16)


class ClientOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    version: int
    legal_name: str
    first_name: str | None
    last_name: str | None
    country_code: str | None
    vat_number: str | None
    tax_code: str | None
    address: str | None
    civic_number: str | None
    postal_code: str | None
    city: str | None
    province: str | None
    country: str | None
    sdi_code: str | None
    pec: str | None
    invoice_series: str | None
    payment_iban: str | None
    description: str | None
    default_billable: bool
    hourly_rate: Decimal | None
    currency: str
    timezone: str | None
    default_payment_conditions_code: str | None
    default_payment_method_code: str | None
    default_payment_terms_days: int | None
    invoice_language: str | None
    invoice_date_format: str | None


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Optional because the field is patchable, NOT because a project may
    # end up without a client: every project has exactly one
    # (docs/adr/0003 invariant d). Omitting the key leaves the client
    # alone; stating it re-points the project AND re-tags every task and
    # note that carries it, in the same transaction
    # (taxonomy.reassign_project_client). An explicit JSON null is a
    # stated request for "no client", which is refused with
    # project.client_required.
    client_tag_id: uuid.UUID | None = None
    budget: Decimal | None = None
    color: str | None = Field(default=None, max_length=16)
    description: str | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    version: int
    # Non-nullable: invariant (d). A project whose
    # ``project_profile.client_tag_id`` is NULL is broken taxonomy, and
    # serialising it as a legal ``null`` is what let the SPA build
    # client-less project pickers; it now fails loudly instead.
    client_tag_id: uuid.UUID
    budget: Decimal | None
    color: str | None
    description: str | None


class TaskCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    # Eisenhower axes drive ``priority``. Low/Low (4/4) is the default
    # since migration 0102. ``priority`` is intentionally not an input:
    # it is exclusively a calculated field (importance x urgency).
    importance: int = Field(default=4, ge=1, le=5)
    urgency: int = Field(default=4, ge=1, le=5)
    start_date: datetime.date | None = None
    # A bare ``YYYY-MM-DD`` is date-only ("due that day, no time"): the
    # service anchors it to end-of-day in the owner's configured timezone
    # (the single source of truth for the time-of-day). A full ISO
    # datetime (with a time component) is an explicit instant, stored
    # as-is. Parsed by ``timewindow.split_due`` in the router.
    due_date: str | None = Field(default=None, max_length=40)
    billable: bool | None = None
    parent_task_id: uuid.UUID | None = None
    # docs/adr/0028: ``executor_kind`` kept as an optional input for
    # API consumers that did not migrate yet; it is ignored by the
    # service (the routing kind comes from the resolved identity).
    # ``executor_user_id`` removed.
    executor_kind: ExecKind = ExecKind.human
    assignee_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    # Handle-based assignee (#21 Stage B). When set, the service
    # resolves the handle to a user / ai_assistant and writes the
    # legacy executor_kind / executor_user_id mirror columns too.
    assignee_handle: str | None = Field(default=None, max_length=40)
    estimate_effort_h: Decimal | None = None
    # Capabilities the task needs from its executor (docs/adr/0025 P2).
    # Empty = any enabled agent. Additive, default [].
    required_capabilities: list[str] = Field(default_factory=list)
    monetary_cost: Decimal | None = None
    location: str | None = Field(default=None, max_length=200)
    necessity: Necessity = Necessity.should
    # Opt-out from automatic search indexing. Not a read boundary and not
    # ``SearchIn.task_scope``, which narrows a query to a project: this one
    # decides whether the row is indexed at all. See models/index_scope.py.
    # Deliberately not gated beyond ``tasks:write``: at ``none`` the row is
    # still readable and still matched by the free-text filter, so there is
    # no privilege to fence off here.
    index_scope: IndexScope = IndexScope.org
    budget_id: uuid.UUID | None = None
    # Free-form facets only (kind generic / memory_channel). Structural
    # ids are still accepted here for the callers that predate the two
    # fields below -- the choke-point classifies the bag by kind either
    # way (docs/adr/0003) -- but naming them is what makes the request
    # single-valued: two projects in one create is TAG_MULTIPLE_PROJECTS,
    # not a silent pick.
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    # The structural pair. ``project_tag_id`` decides: the client is
    # derived from it, and stating a client that disagrees is refused
    # (TAG_CLIENT_PROJECT_MISMATCH) rather than silently dropped.
    # Neither is required: a task without a project falls back to the
    # workspace default project (invariant a -- no orphan tasks).
    client_tag_id: uuid.UUID | None = None
    project_tag_id: uuid.UUID | None = None
    assignee_ids: list[uuid.UUID] = Field(default_factory=list)
    # Appointment unification (migration 0094). When ``start_at`` and
    # ``duration_minutes`` are both set the task is a calendar
    # appointment subject to the no-overlap constraint per assignee.
    # Pairing is enforced by the CHECK constraint (both set or both
    # NULL); a partial input raises 422 at the service layer.
    start_at: datetime.datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    # Recurrence spec consumed by the recurrence engine. Free-form jsonb.
    recurrence: dict[str, Any] | None = None


# The sort keys ``GET /tasks`` accepts, as a type rather than a string.
# The service holds the authoritative name -> column whitelist
# (``services.tasks._TASK_ORDER``, so a sort key can never be
# string-interpolated into SQL) and answers an unrecognised name by
# falling back to its default order. That fallback is right inside the
# service, where the caller is our own code; over HTTP it is wrong,
# because a typo then looks exactly like a sort that was applied. Here an
# unrecognised value is a 422. A drift guard asserts the two stay equal
# in both directions -- a key added to the service and not offered here
# is unreachable, one offered here and removed there silently stops
# sorting.
TaskOrderBy = Literal[
    "priority",
    "due_date",
    "start_date",
    "created_at",
    "updated_at",
    "necessity",
]


class TaskPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    # ``priority`` is intentionally absent (calculated field). Patch
    # importance/urgency to move on the Eisenhower matrix; the service
    # re-derives priority on the next save.
    importance: int | None = Field(default=None, ge=1, le=5)
    urgency: int | None = Field(default=None, ge=1, le=5)
    start_date: datetime.date | None = None
    # Bare ``YYYY-MM-DD`` = date-only (anchored to end-of-day in the
    # owner's timezone); a full ISO datetime is an explicit instant. See
    # ``TaskCreateIn.due_date``.
    due_date: str | None = Field(default=None, max_length=40)
    billable: bool | None = None
    estimate_effort_h: Decimal | None = None
    # docs/adr/0028: ``executor_kind`` is no longer persisted (read
    # via identity), ``executor_user_id`` is removed. Updates set
    # ``assignee_id`` (or ``assignee_handle`` which is resolved
    # service-side) and optionally ``owner_id`` to reassign
    # accountability. Empty assignee_handle clears the assignment.
    assignee_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    assignee_handle: str | None = Field(default=None, max_length=40)
    required_capabilities: list[str] | None = None
    parent_task_id: uuid.UUID | None = None
    monetary_cost: Decimal | None = None
    location: str | None = Field(default=None, max_length=200)
    necessity: Necessity | None = None
    index_scope: IndexScope | None = None
    budget_id: uuid.UUID | None = None
    # Structural re-tagging, single-valued (docs/adr/0003). Before these
    # existed the only way to move a task was attach/detach on
    # /tasks/{id}/tags, which cannot express "this task is now on that
    # project" as one intent. Stating ``project_tag_id`` is a MOVE (the
    # client follows the project); ``client_tag_id`` alone re-points the
    # client and is refused when the attached project belongs to another
    # one. An explicit null on either is refused
    # (TAG_STRUCTURAL_REQUIRED): a task has exactly one of each.
    client_tag_id: uuid.UUID | None = None
    project_tag_id: uuid.UUID | None = None
    # Appointment unification (migration 0094). Patch can set/clear
    # the pair atomically: both fields together. Recurrence is
    # independent and can be patched on its own.
    start_at: datetime.datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1)
    recurrence: dict[str, Any] | None = None


class TaskStateIn(BaseModel):
    expected_version: int = Field(ge=1)
    state_id: uuid.UUID


class StateOut(BaseModel):
    id: uuid.UUID
    name: str
    ord: int
    is_initial: bool
    is_terminal: bool
    is_hidden: bool = False
    description: str | None = None


class TransitionOut(BaseModel):
    from_state_id: uuid.UUID
    to_state_id: uuid.UUID


class ExpectedVersionIn(BaseModel):
    expected_version: int = Field(ge=1)


class ParticipantIn(BaseModel):
    """Body of POST /tasks/{id}/participants. Pass either
    ``identity_id`` (UUID into ``identities``) or ``handle`` (the
    user/assistant handle the picker carries); the service resolves
    the handle through ``identities`` in the workspace's org. At
    least one must be set; ``identity_id`` wins if both are passed."""

    identity_id: uuid.UUID | None = None
    handle: str | None = Field(default=None, max_length=40)


class ParticipantOut(BaseModel):
    """One row of GET /tasks/{id}/participants. The identity ``handle``
    and ``kind`` are denormalised for the SPA so it can render the
    badge without a second /identities lookup. Migration 0095."""

    identity_id: uuid.UUID
    handle: str
    kind: str  # 'user' | 'ai_assistant' (identity_kind)
    start_at: datetime.datetime
    duration_minutes: int


class TaskChecklistItemOut(BaseModel):
    """One row of a task's checklist (the second tab in the SPA task
    view, next to the markdown description). Lightweight: not a
    sub-task. ``version`` enables optimistic concurrency on per-item
    updates; ``done_by``/``done_at`` are stamped on toggle."""

    id: uuid.UUID
    # Polymorphic owner: exactly one of task_id / note_id is set (note
    # checklists, task bae178d2).
    task_id: uuid.UUID | None = None
    note_id: uuid.UUID | None = None
    text: str
    # Optional articulate markdown comment, opened / edited as markdown
    # in the shared checklist widget.
    body: str | None = None
    done: bool
    position: int
    done_at: datetime.datetime | None = None
    done_by: uuid.UUID | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    version: int


class TaskChecklistItemCreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    body: str | None = None
    # When NULL, append at the end. Explicit positions are accepted so
    # an MCP/voice caller can insert at an arbitrary slot.
    position: int | None = None


class TaskChecklistItemPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    # Empty string clears the comment; null leaves it unchanged.
    body: str | None = None
    done: bool | None = None
    position: int | None = None


class TaskChecklistReorderIn(BaseModel):
    """Full-set rewrite of the position column. ``ids`` must list the
    current items of the task in the desired order; mismatch raises
    409-ish (DomainError, code task.checklist.reorder_mismatch)."""

    ids: list[uuid.UUID]


class TaskChecklistClearDoneOut(BaseModel):
    removed: int


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    state_id: uuid.UUID
    state: str
    # ``priority`` is a calculated field, derived server-side from
    # importance x urgency (migration 0102 made the axes mandatory).
    priority: int
    importance: int
    urgency: int
    start_date: datetime.date | None
    # Migration 0005: due_date is a timestamptz (optional time-of-day).
    due_date: datetime.datetime | None
    parent_task_id: uuid.UUID | None
    # docs/adr/0028 Stage C: identity-based addressing.
    # ``assignee_id`` is the FK into ``identities``; ``assignee_handle``
    # is the denormalised display handle for the SPA's task card
    # (resolved at serialisation, NULL when unassigned). ``owner_id``
    # is the accountability axis (always a real user). The legacy
    # ``executor_kind`` is no longer carried; the SPA can derive it
    # via the identity payload when needed.
    assignee_id: uuid.UUID | None = None
    assignee_handle: str | None = None
    # docs/adr/0028 Punto 4: kind of the assignee identity
    # (user / ai_assistant) resolved at serialisation. NULL when the
    # task is unassigned. The SPA renders an IdentityBadge from this
    # without re-querying ``/identities`` per row.
    assignee_kind: str | None = None
    # migration 0091: identity that created the task. The MCP layer
    # sets this to the ai_assistant when the call comes through an
    # agent token, otherwise it defaults to the user identity of
    # ``created_by``. ``created_by_kind`` is the denormalised
    # ``identities.kind`` so the SPA can flag AI-created tasks even
    # when the assignee is empty.
    created_by_identity_id: uuid.UUID | None = None
    created_by_handle: str | None = None
    created_by_kind: str | None = None
    # migration 0093: display label for the creator. For an
    # ai_assistant-bound identity it is ``ai_assistants.label``; for a
    # bare MCP token it is ``agent_tokens.name`` and ``created_by_kind``
    # is set to ``mcp_token`` (vs ``ai_assistant`` when the assistant
    # row exists). The SPA renders the bot icon for both kinds.
    created_by_label: str | None = None
    owner_id: uuid.UUID
    # ``executor_kind`` is re-exposed for SPA backward compat
    # (cards/filters/graph still consume it). The serializer fills it
    # from the resolved identity when ``assignee_id`` is set, else
    # from the task's fallback ``executor_kind`` hint (docs/adr/0029
    # SPA P2 follow-up).
    executor_kind: ExecKind
    estimate_effort_h: Decimal | None
    required_capabilities: list[str] = Field(default_factory=list)
    monetary_cost: Decimal | None
    location: str | None
    necessity: Necessity
    index_scope: IndexScope
    budget_id: uuid.UUID | None
    billable: bool | None = None
    is_archived: bool
    # Contract-net (docs/adr/0025, P4): the task is announced and
    # awaiting a member ``claim`` (cleared on claim).
    offered: bool = False
    deleted_at: datetime.datetime | None = None
    # Recency timestamps (TimestampMixin). ``created_at`` is set on insert;
    # ``updated_at`` is bumped on every mutation (onupdate=now()), so the SPA
    # can sort by ``updated_at`` desc to surface the most recently
    # created/modified tasks first (Recent-tasks widget).
    created_at: datetime.datetime
    updated_at: datetime.datetime
    version: int
    tags: list[TagBrief] = Field(default_factory=list)
    # Appointment unification (migration 0094, ADR-0008 addendum). When
    # both are set the task is a calendar appointment; the calendar
    # view filters on ``duration_minutes IS NOT NULL`` and the SPA
    # renders an event badge. ``due_date`` (date) remains the legacy
    # deadline; reminders use it alone, appointments use ``start_at``.
    start_at: datetime.datetime | None = None
    duration_minutes: int | None = None
    recurrence: dict[str, Any] | None = None
    # Embedded checklist (the second tab in the SPA task view). Populated
    # only by single-task endpoints (``GET /tasks/{id}`` and ``POST
    # /tasks``); list endpoints leave it empty so a hundred-row list
    # doesn't fan out one extra query per task. Mutations go through
    # the dedicated ``/tasks/{id}/checklist/*`` sub-resource so a stale
    # ``PATCH /tasks/{id}`` payload can never overwrite the checklist
    # by accident.
    checklist: list[TaskChecklistItemOut] = Field(default_factory=list)


class CommentCreateIn(BaseModel):
    body: str = Field(min_length=1)


class AnnotationOut(BaseModel):
    """An inline comment or suggestion on a markdown document. ``doc_kind``
    + ``doc_id`` is the generic document handle (note_part | task
    description); the inline rendering is web-only but the data is the
    same on every surface."""

    id: uuid.UUID
    doc_kind: str
    doc_id: uuid.UUID
    kind: str
    body: str
    anchor_quote: str | None = None
    anchor_prefix: str | None = None
    anchor_suffix: str | None = None
    anchor_domain: str = "source"
    original_text: str | None = None
    proposed_text: str | None = None
    status: str
    parent_id: uuid.UUID | None = None
    author_identity_id: uuid.UUID | None = None
    # Resolved author (task 515e13fb): the SPA showed only the raw 8-char
    # ``author_identity_id`` prefix on every card, which collapses across a
    # single author's comments -- it is the AUTHOR id, never the comment id.
    # Surface a human name (like ``TaskOut.created_by_*``): ``author_handle``
    # is the identity handle, ``author_kind`` is 'user' | 'ai_assistant', and
    # ``author_label`` is ``ai_assistants.label`` for an assistant (else None).
    author_handle: str | None = None
    author_kind: str | None = None
    author_label: str | None = None
    resolved_by_identity_id: uuid.UUID | None = None
    assigned_to_identity_id: uuid.UUID | None = None
    resolved_at: datetime.datetime | None = None
    edited_at: datetime.datetime | None = None
    deleted_at: datetime.datetime | None = None
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # Caller-scoped card collapse state (annotation_ui_state, migration
    # 0084; mirrors ``NotePartOut.ui_collapsed``). Default false = expanded.
    ui_collapsed: bool = False


class AnnotationUIStateIn(BaseModel):
    """Per-user collapse state for one annotation card. User-scoped,
    last-write-wins (no version), like ``NotePartUIStateIn``."""

    collapsed: bool


class AnnotationUIStateBulkIn(BaseModel):
    """Collapse/expand every top-level annotation card on one document for
    the caller (the panel's collapse-all / expand-all). Replies keep their
    own per-card state: folding a thread is the root card's job."""

    doc_kind: Literal["note_part", "task_description"]
    doc_id: uuid.UUID
    collapsed: bool


class AnnotationAssignIn(BaseModel):
    """Assign an annotation to a workspace identity, or clear it. Pass an
    ``assignee_identity_id`` OR an ``assignee_handle`` (bare / ``@handle`` /
    login email), or ``clear=true`` to unassign. Optimistic-versioned."""

    expected_version: int = Field(ge=1)
    assignee_identity_id: uuid.UUID | None = None
    assignee_handle: str | None = None
    clear: bool = False


class AnnotationCommentIn(BaseModel):
    doc_kind: Literal["note_part", "task_description"]
    doc_id: uuid.UUID
    body: str = Field(min_length=1)
    anchor_quote: str | None = None
    anchor_prefix: str | None = None
    anchor_suffix: str | None = None
    # Which projection ``anchor_quote`` / ``original_text`` are written in.
    # Omit it: an API, MCP or CLI caller reads the markdown SOURCE and quotes
    # it, which is the default. Only the legacy WYSIWYG surface captures the
    # RENDERED text (markup stripped, links reduced to their label, blocks
    # joined by a space) and has to say so, because a quote read in the wrong
    # domain either fails to locate or, worse, matches the wrong passage.
    anchor_domain: Literal["source", "rendered"] = "source"
    parent_id: uuid.UUID | None = None


class SuggestionIn(BaseModel):
    doc_kind: Literal["note_part", "task_description"]
    doc_id: uuid.UUID
    original_text: str = Field(min_length=1)
    proposed_text: str
    rationale: str = ""
    anchor_prefix: str | None = None
    anchor_suffix: str | None = None
    # Which projection ``anchor_quote`` / ``original_text`` are written in.
    # Omit it: an API, MCP or CLI caller reads the markdown SOURCE and quotes
    # it, which is the default. Only the legacy WYSIWYG surface captures the
    # RENDERED text (markup stripped, links reduced to their label, blocks
    # joined by a space) and has to say so, because a quote read in the wrong
    # domain either fails to locate or, worse, matches the wrong passage.
    anchor_domain: Literal["source", "rendered"] = "source"


class AnnotationEditIn(BaseModel):
    body: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class TagRefIn(BaseModel):
    tag_id: uuid.UUID


class AssigneeIn(BaseModel):
    user_id: uuid.UUID


class WorkflowStateSpecIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ord: int = 0
    is_initial: bool = False
    is_terminal: bool = False
    is_hidden: bool = False
    description: str | None = None


class TransitionIn(BaseModel):
    from_state: str
    to_state: str


class WorkflowCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    states: list[WorkflowStateSpecIn]
    transitions: list[TransitionIn] = Field(default_factory=list)


class WorkflowStateEditIn(BaseModel):
    id: uuid.UUID | None = None  # None = new state
    name: str = Field(min_length=1, max_length=80)
    ord: int = 0
    is_initial: bool = False
    is_terminal: bool = False
    is_hidden: bool = False
    description: str | None = None


class WorkflowPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    states: list[WorkflowStateEditIn]
    transitions: list[TransitionIn] = Field(default_factory=list)


class WorkflowDocStateIn(BaseModel):
    """One state of an imported interchange document (docs/adr/0052).

    ``strict`` is the point of this model: pydantic would otherwise
    read ``"is_terminal": "true"`` as a boolean, and a lifecycle flag
    silently flipped by a string is exactly the import that looks like
    it worked. Lengths and every other rule are checked by
    ``services/workflow_io.normalize`` instead, so the limit applies to
    the TRIMMED name and there is one implementation of the rules.
    """

    model_config = ConfigDict(strict=True)

    name: str
    is_initial: bool = False
    is_terminal: bool = False
    is_hidden: bool = False
    description: str | None = None


class WorkflowDocTransitionIn(BaseModel):
    model_config = ConfigDict(strict=True)

    from_state: str
    to_state: str


class WorkflowDocIn(BaseModel):
    """The body of an import: the file, verbatim. Unknown keys are
    ignored so a document written by a later build still loads
    (docs/adr/0052); ``kind`` and ``version`` are checked in the service,
    which can say WHY it refused instead of emitting a schema mismatch.
    """

    model_config = ConfigDict(strict=True)

    kind: str
    version: int
    name: str
    description: str | None = None
    states: list[WorkflowDocStateIn] = Field(default_factory=list)
    transitions: list[WorkflowDocTransitionIn] = Field(default_factory=list)


class WorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_default: bool
    version: int


class ProjectWorkflowIn(BaseModel):
    expected_version: int = Field(ge=1)
    workflow_id: uuid.UUID | None = None


class DependencyCreateIn(BaseModel):
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    type: DependencyType
    lag_working_minutes: int = 0


class DependencyOut(BaseModel):
    id: uuid.UUID
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    type: DependencyType
    lag_working_minutes: int
    version: int


class TaskRelationCreateIn(BaseModel):
    task_id: uuid.UUID
    other_id: uuid.UUID


class TaskRelationOut(BaseModel):
    id: uuid.UUID
    task_a_id: uuid.UUID
    task_b_id: uuid.UUID
    version: int


class GraphNode(BaseModel):
    id: str
    title: str
    state: str
    blocked: bool


class GraphEdge(BaseModel):
    predecessor: str
    successor: str
    type: str
    lag_working_minutes: int


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --- Garden graph (note↔note weighted + PageRank centrality) -------
# Tasks 4467acb4 (note_edge_strength v1) + 8c0a8f08 (PageRank Phase 1).
# Both surfaces share one round-trip so the SPA mindmap doesn't have
# to coordinate two parallel fetches just to render one frame.


class GardenGraphEdge(BaseModel):
    """One undirected weighted edge between two notes. ``src`` and
    ``dst`` are canonically ordered (sorted by string repr) so two
    rows with the same endpoints never appear in different positions
    across requests. ``weight`` ∈ [0, 1] is the soft-OR of the
    per-kind contributions and the Adamic-Adar tag overlap; the
    third source (co-activity) is documented in ADR-0031 and
    deferred to Phase 2."""

    src: uuid.UUID
    dst: uuid.UUID
    weight: float


class GardenGraphOut(BaseModel):
    """Response of GET /garden/graph: edges + centrality in one
    payload. ``centrality`` is a ``{note_id: pagerank}`` map summing
    to 1 across the workspace; an empty workspace returns ``[]`` and
    ``{}`` respectively.

    Phase 2 (task d8664631): ``betweenness`` is the cluster-bridge
    centrality served from the worker-materialised snapshot (empty
    until the first refresh; ``analytics_computed_at`` is its age).
    ``recency`` is the separate freshness axis (``exp(-age/tau)`` per
    note, computed live) consumers combine with centrality to counter
    the cold start of new, not-yet-linked notes."""

    edges: list[GardenGraphEdge]
    centrality: dict[uuid.UUID, float]
    betweenness: dict[uuid.UUID, float] = Field(default_factory=dict)
    recency: dict[uuid.UUID, float] = Field(default_factory=dict)
    analytics_computed_at: datetime.datetime | None = None


class GardenClustersOut(BaseModel):
    """Response of GET /garden/clusters (task 8c0a8f08): the Leiden
    community index per note plus the partition's global modularity.
    ``clusters`` is ``{note_id: community_index}`` (0-based, dense);
    ``modularity`` is the structure thermometer (ADR-0035), or null when
    the optional clustering extra is not installed (graceful degrade).
    ``count`` is the number of distinct communities."""

    clusters: dict[uuid.UUID, int]
    modularity: float | None
    count: int


# --- Garden walk (task 5bf31b63) ----------------------------------------
# Two regimes packed in one shape: ``focused`` (PPR seeded) returns
# the top-K notes by induced mass and the step is their rank; ``free_
# wander`` (Node2Vec) returns the actual trajectory and the step is
# the hop index. The SPA renders them as the "pollinator trail" with
# the same component.


class GardenWalkStep(BaseModel):
    note_id: uuid.UUID
    step: int
    weight: float
    # Provenance marker (ADR-0034): "humus" when the node is a flagged
    # humus note (the free wander biases toward high-centrality humus),
    # else None. The mindmap renders a leaf marker on humus steps.
    provenance: str | None = None


class GardenWalkOut(BaseModel):
    seed: uuid.UUID
    mode: str  # "focused" | "free_wander"
    steps: list[GardenWalkStep]


# --- Link suggestions (task c7d0bb4c) --------------------------------
# Returned by GET /garden/link-suggestions/{note_id}. ``score`` ∈
# [0, 1], soft-OR of Adamic-Adar (rare-tag overlap) and PPR-induced
# mass, damped by candidate degree to avoid hub bias. ``signals``
# carries the per-feature contribution so the SPA can render a
# tooltip and ADR-0037's audit log can persist the breakdown.


class GardenLinkSuggestion(BaseModel):
    note_id: uuid.UUID
    score: float
    rationale: str
    signals: dict[str, float]


class GardenLinkSuggestionsOut(BaseModel):
    source_note_id: uuid.UUID
    suggestions: list[GardenLinkSuggestion]


# --- garden_classify proposal engine (ADR-0032) ----------------------
# classify is read-only: it proposes {tags, links, maturity, cluster}
# each with a confidence + rationale; ``signals_used`` names the signals
# that fired (transparency). ``apply`` is the mutating, reversible
# counterpart that records a classification_feedback event (ADR-0037).


class GardenTagSuggestionOut(BaseModel):
    tag_id: uuid.UUID
    confidence: float
    rationale: str


class GardenLinkCandidateOut(BaseModel):
    target_id: uuid.UUID
    link_kind: str
    confidence: float
    rationale: str


class GardenMaturitySuggestionOut(BaseModel):
    value: str  # "mature" in v1 (the value axis only proposes upward)
    confidence: float
    rationale: str
    auto_apply: bool


class GardenClusterSuggestionOut(BaseModel):
    leiden_id: int | None
    modularity: float | None
    confidence: float


class GardenClassifyOut(BaseModel):
    """Response of GET /garden/classify/{node_id} (ADR-0032 / ADR-0042). A
    block is null/empty when its signal was not requested or produced nothing;
    ``signals_used`` names the signals that actually fired (and records
    ``leiden_extra_absent`` when clustering degraded gracefully).

    ``source`` (ADR-0042 D4/D6) tells the SPA whether these are the persisted
    on-create suggestions (``precomputed``) or a fresh live recompute
    (``live``); ``generated_at`` dates them so the panel can show freshness and
    offer a refresh."""

    node_id: uuid.UUID
    node_kind: str
    tags: list[GardenTagSuggestionOut]
    links: list[GardenLinkCandidateOut]
    maturity: GardenMaturitySuggestionOut | None
    cluster: GardenClusterSuggestionOut | None
    signals_used: list[str]
    model_version: str
    generated_at: datetime.datetime
    source: Literal["precomputed", "live"] = "live"


class GardenHealthMetricOut(BaseModel):
    """One sensor reading (ADR-0035, "show, never judge"): the value, its
    health floor when it has one, and -- only when ``value`` is null --
    the reason there is no reading yet (data source not built / blocked
    upstream), never a faked number."""

    value: float | None = None
    floor: float | None = None
    reason: str | None = None


class GardenHealthSnapshotOut(BaseModel):
    day: datetime.date
    metrics: dict[str, GardenHealthMetricOut]


class GardenHealthOut(BaseModel):
    """Response of GET /garden/health: current sensor readings plus the
    recent daily snapshots (newest first) for the sparkline."""

    generated_at: datetime.datetime
    metrics: dict[str, GardenHealthMetricOut]
    trend: list[GardenHealthSnapshotOut]


class GardenHealthEventOut(BaseModel):
    """One entry of the "what changed" timeline (ADR-0035 §84): a factual
    record correlating a sensor shift with a cause -- a classifier bump
    or a bulk corpus edit. ``detail`` is a small kind-specific bag:
    ``{"version": ...}`` for ``classifier_version``; ``{"action",
    "count"}`` for ``corpus_edit``. "Show, never judge": it states that
    something happened, never whether it was good."""

    at: datetime.datetime
    kind: Literal["classifier_version", "corpus_edit"]
    detail: dict[str, Any]


class GardenEventOut(BaseModel):
    """One ``event_outbox`` row on the workspace event stream (ADR-0036
    audit panel): the verbatim coordinated read/propose/commit/reject/
    snapshot event. ``applied_state`` is null until the adjudicator decides
    a propose. "Show, never judge": the event, not a verdict."""

    id: uuid.UUID
    actor_id: uuid.UUID
    actor_kind: Literal["human", "agent", "system"]
    kind: Literal["read", "propose", "commit", "reject", "snapshot"]
    node_kind: str | None = None
    node_id: uuid.UUID | None = None
    parent_event_id: uuid.UUID | None = None
    payload: dict[str, Any]
    ts: datetime.datetime
    applied_at: datetime.datetime | None = None
    applied_state: str | None = None


class GardenApplyIn(BaseModel):
    """Apply (or decline) one suggestion. ``accept``/``override`` mutate
    via the existing services; ``reject``/``ignore`` only record the
    decision. ``auto`` is reserved for the worker and is not accepted on
    this surface (a client cannot forge a system promotion)."""

    node_id: uuid.UUID
    suggestion_type: Literal["tag", "link", "maturity", "cluster"]
    suggestion_value: dict[str, Any]
    action: Literal["accept", "reject", "override", "ignore"]
    override_value: dict[str, Any] | None = None
    model_version: str | None = None
    signals_snapshot: dict[str, Any] | None = None


class GardenApplyOut(BaseModel):
    feedback_id: uuid.UUID
    node_id: uuid.UUID
    suggestion_type: str
    action: str
    applied: bool  # True when the action mutated (accept / override)


class GardenReviewPendingItem(BaseModel):
    """One AUTONOMOUSLY-generated humus note awaiting human review (ADR-0043
    review inbox). ``origin_model_id`` is the transparency requirement: the
    reviewer sees the producing model before approving/rejecting."""

    note_id: uuid.UUID
    title: str | None = None
    humus_kind: str | None = None
    origin_model_id: str | None = None
    preview: str
    created_at: datetime.datetime
    # Echo this back as ``expected_version`` on approve/reject: the TOCTOU
    # guard that ensures the reviewer blesses the content they saw.
    version: int


class GardenReviewRejectedItem(BaseModel):
    """One proposal a human DECLINED: the review bin's row. ``rejected_at``
    is when it was declined; ``version`` is the pin to echo back to the
    restore that undoes the rejection."""

    note_id: uuid.UUID
    title: str | None = None
    humus_kind: str | None = None
    origin_model_id: str | None = None
    preview: str
    created_at: datetime.datetime
    # Echo this back as ``expected_version`` on the restore that undoes the
    # rejection.
    version: int
    rejected_at: datetime.datetime


class GardenCandidateNode(BaseModel):
    """A NODE distillation candidate (task 4995a32f): inert material that
    could be compacted into a denser atom. ``kind`` is distill|pattern|
    season; ``note_ids`` are the source(s) to feed to the matching
    decomposition tool."""

    kind: str
    note_ids: list[uuid.UUID]
    title: str
    reason: str
    score: float
    preview: str


class GardenCandidateEdge(BaseModel):
    """An EDGE curation candidate (task 4995a32f): distillation as graph
    maintenance. ``op`` is add (a strong tag/co-activity pair with no
    manual link) or prune (a ``related`` link whose basis has decayed)."""

    op: str
    src_note_id: uuid.UUID
    dst_note_id: uuid.UUID
    link_kind: str
    src_title: str
    dst_title: str
    reason: str
    score: float


class GardenCandidatesOut(BaseModel):
    """Distillation candidates: nodes to compact + edges to add/prune.
    Pure read; nothing is distilled until the caller acts on a candidate."""

    nodes: list[GardenCandidateNode]
    edges: list[GardenCandidateEdge]


class GardenReviewActionIn(BaseModel):
    """Approve, or reject, one proposed node by id (ADR-0043). ``reason`` is
    an optional note recorded on a reject (ignored by approve).
    ``expected_version`` is the version the reviewer READ (served by the
    pending listing): when set, the action fails with ``stale_version`` if
    the node changed in between (TOCTOU guard, task 2e36e732)."""

    note_id: uuid.UUID
    reason: str | None = None
    expected_version: int | None = None


class GardenReviewActionOut(BaseModel):
    note_id: uuid.UUID
    review_state: str | None  # 'approved' after approve; null after a reject
    origin_model_id: str | None = None
    rejected: bool  # True when the action soft-deleted the node
    version: int  # post-action version (callers can chain guarded actions)


class GardenRestoreSourceOut(BaseModel):
    """Fase P (task 561c6aca): outcome of "ripristina originale" on a humus
    atom -- the sources revived and the atom retired (the ``hypha_of`` chain
    is the stack; nothing is ever hard-deleted)."""

    atom_note_id: uuid.UUID
    source_ids: list[uuid.UUID]
    restored_source_ids: list[uuid.UUID]
    atom_retired: bool


class GardenAcceptRatioOut(BaseModel):
    """Per-model reliability of AUTONOMOUSLY-generated proposals (ADR-0043
    D4): how often a human approved vs rejected this model's output. ``ratio``
    is null when there are no decisions yet (no signal, not 0%)."""

    model_id: str
    approved: int
    rejected: int
    ratio: float | None = None


class GardenLearningRollbackIn(BaseModel):
    """Rewind the caller's own learned priors to their state at ``to``
    (ADR-0037 "Snapshots and rollback")."""

    to: datetime.datetime


class GardenFeatureDeltaOut(BaseModel):
    """One feature's prior value before vs after (rollback diff / drift)."""

    feature_key: str
    before: float
    after: float
    delta: float


class GardenLearningRollbackOut(BaseModel):
    """Result of a rollback: the restored window + a one-line diff
    (ADR-0037: "the system was slightly more biased toward tag X")."""

    rolled_back_to: datetime.datetime
    snapshot_at: datetime.datetime | None
    replayed_events: int
    features_changed: int
    top_change: GardenFeatureDeltaOut | None
    summary: str


class GardenRejectHotspotOut(BaseModel):
    """A suggestion feature the caller keeps declining (ADR-0037 reject-
    hotspot view). ``feature_key`` is type-prefixed; the SPA resolves the
    human label from its tag/note context."""

    suggestion_type: str
    feature_key: str
    declines: int
    last_declined_at: datetime.datetime


class GardenLearningTelemetryOut(BaseModel):
    """Read-only learning telemetry for the sensors dashboard (ADR-0037):
    the reject-hotspots and the prior-drift bars, the caller's own history
    only. "Show, never judge"."""

    reject_hotspots: list[GardenRejectHotspotOut]
    drift: list[GardenFeatureDeltaOut]


# --- F3: calendars, events, schedule (FR-4, docs/adr/0004, 0008) ---


class CalendarCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="Europe/Rome", max_length=64)
    # weekday -> ordered [start, end] local "HH:MM" windows
    weekly_hours: dict[str, list[list[str]]]


class CalendarOut(BaseModel):
    id: uuid.UUID
    name: str
    is_default: bool
    timezone: str
    version: int


class HolidayIn(BaseModel):
    day: datetime.date


class HolidayOut(BaseModel):
    day: datetime.date


class UserCalendarIn(BaseModel):
    user_id: uuid.UUID
    calendar_id: uuid.UUID
    daily_capacity_h: Decimal = Field(gt=0, le=24)


class TaskScheduleIn(BaseModel):
    """Drag/write-back of scheduler fields (FR-4). Only provided fields
    are changed; recompute then respects manual/constraint pins."""

    expected_version: int = Field(ge=1)
    schedule_mode: ScheduleMode | None = None
    constraint_kind: ConstraintKind | None = None
    constraint_date: datetime.datetime | None = None
    remaining_effort_h: Decimal | None = None
    actual_start: datetime.datetime | None = None
    is_milestone: bool | None = None


class RecomputeIn(BaseModel):
    project_tag_id: uuid.UUID | None = None
    as_of: datetime.datetime | None = None
    # Resource-leveling objective (docs/adr/0025, P1).
    policy: SchedulePolicy = SchedulePolicy.balanced


class RecomputeOut(BaseModel):
    count: int
    # Projections so policies are comparable (docs/adr/0025).
    makespan_minutes: int
    projected_credit_cost: Decimal
    policy: SchedulePolicy
    # Count of llm tasks with no admissible executor (P2 dispatch gaps).
    unassignable_count: int = 0


class ScheduleOut(BaseModel):
    task_id: uuid.UUID
    es: datetime.datetime | None
    ef: datetime.datetime | None
    ls: datetime.datetime | None
    lf: datetime.datetime | None
    slack_minutes: int | None
    on_logical_critical_path: bool
    # Resource-aware critical chain + projected LLM credit cost
    # (docs/adr/0025, P1).
    on_critical_chain: bool
    projected_cost: Decimal
    scheduled_start: datetime.datetime | None
    scheduled_end: datetime.datetime | None
    # Admission-control dispatch result (docs/adr/0025, P2): the chosen
    # executor, or a flagged dispatch gap with a short stable reason.
    assigned_executor_id: uuid.UUID | None = None
    unassignable: bool = False
    unassignable_reason: str | None = None
    computed_at: datetime.datetime
    input_fingerprint: str | None


# --- Executor registry (docs/adr/0025, P2) ---
# Reads are member-level; mutations are owner-gated in the service
# (mirrors the rate-card / issuer-profile precedent).


class ExecutorOut(BaseModel):
    id: uuid.UUID
    kind: ExecutorKind
    name: str
    user_id: uuid.UUID | None
    context_switch_cost_minutes: int
    provider: str | None
    model_id: str | None
    max_parallel: int
    credit_budget: Decimal | None
    credit_rate_per_hour: Decimal
    enabled: bool
    capability_tags: list[str] = Field(default_factory=list)
    version: int


class ExecutorCreateIn(BaseModel):
    kind: ExecutorKind = ExecutorKind.llm_agent
    name: str = Field(min_length=1, max_length=120)
    # Required + must be a workspace member iff kind == human (the
    # service rejects an unbound human executor).
    user_id: uuid.UUID | None = None
    context_switch_cost_minutes: int = Field(default=0, ge=0)
    provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=120)
    max_parallel: int = Field(default=4, ge=1)
    credit_budget: Decimal | None = None
    credit_rate_per_hour: Decimal = Field(default=Decimal(0), ge=0)
    enabled: bool = True
    capability_tags: list[str] = Field(default_factory=list)


class ExecutorPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    # kind / user_id are immutable identity (not patchable).
    name: str | None = Field(default=None, min_length=1, max_length=120)
    context_switch_cost_minutes: int | None = Field(default=None, ge=0)
    provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=120)
    max_parallel: int | None = Field(default=None, ge=1)
    credit_budget: Decimal | None = None
    credit_rate_per_hour: Decimal | None = Field(default=None, ge=0)
    enabled: bool | None = None
    capability_tags: list[str] | None = None


# --- Agent execution runtime (docs/adr/0025, P3) ---
# Reads are member-level; start/cancel are owner-gated in the service
# (running an agent spends credits -> mirrors the billing-grant gate;
# effective-role sudo enforced).


class AgentRunOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    executor_id: uuid.UUID | None
    status: AgentRunStatus
    steps: int
    credits_spent: Decimal
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    error: str | None
    artifact_note_id: uuid.UUID | None
    cancel_requested: bool
    blocked_reason: str | None
    version: int


# --- P4: coordination handoffs (docs/adr/0025) ---


class HandoffOut(BaseModel):
    id: uuid.UUID
    predecessor_task_id: uuid.UUID
    successor_task_id: uuid.UUID
    from_executor_id: uuid.UUID | None
    to_executor_id: uuid.UUID | None
    message: str
    artifact_note_id: uuid.UUID | None
    status: HandoffStatus
    delivered_at: datetime.datetime | None
    consumed_at: datetime.datetime | None
    version: int


# --- P5: closed-loop dispatch + approval gates (docs/adr/0025) ---
# Reads are member-level (the queue is visible to the team); approve /
# deny / tick / policy-set are owner-gated in the service (a tick can
# spend credits via the P3 metered path -> mirrors the start_run /
# billing-grant gate; effective-role sudo enforced).


class DispatchRequestOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    # Denormalized for the queue UI (no extra round-trip): the task
    # title and the assigned executor's name. Defaults so the generated
    # TS type marks them optional appropriately.
    task_title: str = ""
    executor_id: uuid.UUID | None = None
    executor_name: str | None = None
    status: DispatchStatus
    projected_credit_cost: Decimal
    agent_run_id: uuid.UUID | None = None
    requested_at: datetime.datetime
    decided_at: datetime.datetime | None = None
    decided_by: uuid.UUID | None = None
    reason: str | None = None
    version: int


class DispatchDecisionIn(BaseModel):
    expected_version: int = Field(ge=1)
    # Optional short cause recorded on deny/skip (never free-form prose
    # to the user; trimmed/clamped server-side).
    reason: str | None = Field(default=None, max_length=200)


class DispatchTickIn(BaseModel):
    # Optional resource-leveling policy for the recompute the tick runs
    # (fastest|cheapest|balanced|throughput; default balanced), matching
    # the /schedule/recompute contract.
    policy: SchedulePolicy = SchedulePolicy.balanced


class DispatchTickOut(BaseModel):
    """The "last tick" summary the UI shows (counts of requests touched
    + the scheduler projections so the loop and schedule view agree)."""

    policy: AutonomousDispatch
    enabled: bool
    created: int
    approved: int
    dispatched: int
    skipped: int
    failed: int
    projected_makespan_minutes: int
    projected_credit_cost: Decimal


# --- F4: time tracking (FR-5, docs/adr/0002) ---


class TimeStartIn(BaseModel):
    # Proposal A: with ``note_id`` the billing task is derived from the
    # note's task (the note must be linked to one); ``task_id`` may be
    # omitted, or must agree if both are given.
    task_id: uuid.UUID | None = None
    # None = inherit (task override -> project default -> billable).
    billable: bool | None = None
    # Free-text memo on the entry (renamed from ``note``: it is NOT the
    # Note entity; use ``note_id`` for the work-note link).
    memo: str | None = None
    note_id: uuid.UUID | None = None
    # Double-play: run alongside other timers instead of replacing the
    # serial one (e.g. parallel LLM tasks).
    parallel: bool = False


class TimeStopIn(BaseModel):
    # Stop a specific task's running timer; omit to stop the serial one.
    task_id: uuid.UUID | None = None
    memo: str | None = None


class TimePauseIn(BaseModel):
    # Pause a specific task's running timer; omit to pause the serial one.
    task_id: uuid.UUID | None = None


class TimeResumeIn(BaseModel):
    # Resume a specific task's paused timer; omit to resume the serial one.
    task_id: uuid.UUID | None = None


class TimeManualIn(BaseModel):
    # As TimeStartIn: a ``note_id`` derives the billing task.
    task_id: uuid.UUID | None = None
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = None
    duration_seconds: int | None = Field(default=None, gt=0)
    billable: bool | None = None
    memo: str | None = None
    note_id: uuid.UUID | None = None


class TimeEntryPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    memo: str | None = None
    billable: bool | None = None
    # Reassign the entry to another task (transitively changes its
    # project/client). Adjust the recorded interval if the timer was
    # started late / never stopped; duration is recomputed server-side.
    task_id: uuid.UUID | None = None
    # Set/clear the work note this time was logged in. Sent only when
    # present in the request body (model_fields_set): an explicit null
    # clears the link, omitting it preserves the stored value.
    note_id: uuid.UUID | None = None
    started_at: datetime.datetime | None = None
    ended_at: datetime.datetime | None = None


class TimeEntryOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    started_at: datetime.datetime
    ended_at: datetime.datetime | None
    duration_seconds: int | None
    # Pause/resume. ``accumulated_seconds`` is the active time banked so
    # far; ``resumed_at`` is the start of the current live segment (null
    # while paused, and once stopped). The client derives live elapsed as
    # ``accumulated_seconds + (now - resumed_at)`` while running, frozen at
    # ``accumulated_seconds`` while paused — never accumulated client-side.
    accumulated_seconds: int
    resumed_at: datetime.datetime | None
    source: TimeSource
    executor_kind: ExecKind
    billable: bool
    parallel: bool
    rate_snapshot: Decimal | None
    currency: str
    memo: str | None
    # Provenance: the work note this time was logged in (Proposal A),
    # with its resolved title so the report can show *where* time was
    # logged. Both null when the entry has no linked note.
    note_id: uuid.UUID | None = None
    note_title: str | None = None
    version: int
    # Resolved context (task -> project tag -> client tag -> client
    # profile) so the list / report need no N+1 round-trips.
    task_title: str | None = None
    client_tag_id: uuid.UUID | None = None
    client_name: str | None = None
    project_tag_id: uuid.UUID | None = None
    project_name: str | None = None
    client_timezone: str | None = None


class ReportRowOut(BaseModel):
    key: str | None
    label: str | None
    seconds: int
    billable_seconds: int
    amount: Decimal
    currency: str


class DailyReportRowOut(ReportRowOut):
    """One (day, bucket) cell of the per-day histogram: a ``ReportRowOut``
    plus the calendar day it falls on, in the timezone the caller asked
    for. Only days with tracked time are emitted; the SPA zero-fills the
    rest of the selected range."""

    day: datetime.date


class TaskTimeReportOut(BaseModel):
    task_id: uuid.UUID
    task_title: str | None
    client_tag_id: uuid.UUID | None
    client_name: str | None
    project_tag_id: uuid.UUID | None
    project_name: str | None
    client_timezone: str | None
    total_seconds: int
    billable_seconds: int
    entry_count: int


# --- F4b: budgets + advisory (FR-13/FR-14, docs/adr/0013, 0014) ---


class BudgetCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    period_kind: BudgetPeriod
    period_start: datetime.date
    period_end: datetime.date
    amount: Decimal = Field(ge=0)
    currency: str = Field(default="EUR", max_length=3)


class BudgetPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    period_kind: BudgetPeriod | None = None
    period_start: datetime.date | None = None
    period_end: datetime.date | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)


class BudgetOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None
    period_kind: BudgetPeriod
    period_start: datetime.date
    period_end: datetime.date
    amount: Decimal
    currency: str
    version: int


class ConsumptionOut(BaseModel):
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    consumed: Decimal
    residual: Decimal
    task_count: int


class WhatNowIn(BaseModel):
    # Optional: omit to plan from the server's UTC now() (req #1). The
    # router substitutes now() and coerces a naive value to UTC.
    window_start: datetime.datetime | None = None
    duration_minutes: int = Field(gt=0)
    location: str | None = None
    context_tags: list[str] = Field(default_factory=list)
    # Selection filters. focus_tag_ids is a hard SCOPE (AND); any_tag_ids,
    # min_priority and min_necessity UNION within it. Empty list == inactive.
    # min_priority keeps priority <= the level (1=top..25), an importance
    # FLOOR mirroring min_necessity.
    focus_tag_ids: list[uuid.UUID] = Field(default_factory=list)
    any_tag_ids: list[uuid.UUID] = Field(default_factory=list)
    min_priority: int | None = None
    min_necessity: Necessity | None = None
    # Opt-in narration (req #4b). Accepted now but the deterministic T4
    # edge always returns narrated=false; T3 wires the real narrate call.
    narrate: bool = False


class ErrandsIn(BaseModel):
    location: str | None = None
    context: str | None = None


class FeasibleTaskOut(BaseModel):
    task_id: uuid.UUID
    title: str
    necessity: Necessity
    priority: int
    # Migration 0005: due_date is a timestamptz.
    due_date: datetime.datetime | None
    remaining_minutes: int
    # Deterministic deadline signal (ADR-0013), computed in the core.
    slack_minutes: int | None
    deadline_bucket: str


class NarratedPlanOut(BaseModel):
    """what-now envelope: the deterministic ranked plan plus an optional
    LLM narration. ``narrated`` is false (and narration null) unless the
    metered narrate layer (T3) ran and succeeded; the ranked plan is
    always present and authoritative regardless."""

    ranked: list[FeasibleTaskOut]
    # Tasks that clear every filter except they need MORE time than the
    # window (effort > duration). Surfaced separately so a too-long
    # overdue/at-risk task stays visible instead of being silently dropped;
    # not narrated (it cannot be finished within this window).
    over_window: list[FeasibleTaskOut] = Field(default_factory=list)
    narration: str | None = None
    narration_model: str | None = None
    narrated: bool = False


class ErrandItemOut(BaseModel):
    task_id: uuid.UUID
    title: str
    location: str | None
    necessity: Necessity
    priority: int


class BudgetPickOut(BaseModel):
    task_id: uuid.UUID
    title: str
    cost: Decimal
    necessity: Necessity
    priority: int
    value: int


class BudgetPlanOut(BaseModel):
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    allocated: Decimal
    residual: Decimal
    selected: list[BudgetPickOut]
    excluded: list[dict[str, str]]


# --- F5: email (FR-7, docs/adr/0023) ---


class EmailAccountCreateIn(BaseModel):
    provider: EmailProvider
    email_address: str = Field(min_length=3, max_length=320)
    secret: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=200)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None


class EmailAccountPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=200)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None
    status: EmailAccountStatus | None = None
    ingest_to_memory: bool | None = None
    auto_draft_replies: bool | None = None


class EmailSecretIn(BaseModel):
    expected_version: int = Field(ge=1)
    secret: str = Field(min_length=1)


class EmailAccountOut(BaseModel):
    """No secret is ever returned (ADR-0023)."""

    id: uuid.UUID
    provider: EmailProvider
    email_address: str
    display_name: str | None
    imap_host: str | None
    imap_port: int | None
    smtp_host: str | None
    smtp_port: int | None
    status: EmailAccountStatus
    last_sync_at: datetime.datetime | None
    last_error: str | None
    ingest_to_memory: bool
    # Per-account opt-in for the autonomous responder (WS-4).
    auto_draft_replies: bool
    # Per-account default tags (WS-1) auto-applied to ingested memory +
    # email->task/note (typ. one client + one project tag).
    default_tags: list[TagBrief] = []
    version: int


class EmailDefaultTagsIn(BaseModel):
    """Body of ``PUT /email/accounts/{id}/default-tags`` — replace the
    account's default-tag set (set-replace, like the secret rotation)."""

    expected_version: int = Field(ge=1)
    tag_ids: list[uuid.UUID] = Field(default_factory=list)


class EmailMessageOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    provider_message_id: str
    thread_id: str | None
    message_id: str | None
    in_reply_to: str | None
    from_addr: str
    to_addrs: str
    subject: str | None
    body_text: str | None
    snippet: str | None
    received_at: datetime.datetime
    is_read: bool
    linked_task_id: uuid.UUID | None
    linked_note_id: uuid.UUID | None
    version: int


class SyncResultOut(BaseModel):
    account_id: uuid.UUID
    fetched: int
    created: int
    ok: bool
    error: str | None


class EmailToTaskIn(BaseModel):
    project_tag_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    assignee_ids: list[uuid.UUID] = Field(default_factory=list)


class EmailToNoteIn(BaseModel):
    """Body of ``POST /email/messages/{id}/to-note`` (WS-3). The account's
    default tags are applied automatically; ``tag_ids`` adds more."""

    tag_ids: list[uuid.UUID] = Field(default_factory=list)


class NoteIdOut(BaseModel):
    note_id: uuid.UUID


class EmailSendIn(BaseModel):
    to_addrs: list[str] = Field(min_length=1)
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: str | None = None


class EmailReplyIn(BaseModel):
    body_text: str = Field(min_length=1)


class TaskIdOut(BaseModel):
    task_id: uuid.UUID


class SentOut(BaseModel):
    sent_id: str


# --- WS-4: autonomous responder (draft review) ---


class EmailDraftOut(BaseModel):
    """A queued/drafted reply awaiting human review. ``message_id`` keys
    back to the source message the SPA already has, so subject/sender are
    resolved client-side (no second fetch)."""

    id: uuid.UUID
    message_id: uuid.UUID
    status: str
    draft_reply: str | None
    origin_model_id: str | None
    error: str | None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None


class EmailDraftApproveIn(BaseModel):
    """Optional edited body; ``None`` sends the stored draft as-is."""

    body_text: str | None = Field(default=None, min_length=1)


class DraftIdOut(BaseModel):
    job_id: uuid.UUID


# --- F5b: billing / metering (FR-15, docs/adr/0019) ---


class BalanceOut(BaseModel):
    balance: Decimal


class GrantIn(BaseModel):
    amount: Decimal = Field(gt=0)
    reason: str | None = None


class MeterIn(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    op: str = Field(min_length=1, max_length=80)
    model_id: str | None = None
    units_in: Decimal = Decimal(0)
    units_out: Decimal = Decimal(0)
    basis: CostBasis = CostBasis.local


class UsageOut(BaseModel):
    id: uuid.UUID
    operation_id: str
    model_id: str | None
    op: str
    basis: CostBasis
    units_in: Decimal
    units_out: Decimal
    credits: Decimal


class LedgerOut(BaseModel):
    id: uuid.UUID
    kind: str
    amount: Decimal
    operation_id: str | None
    reason: str | None
    balance_after: Decimal


class RateCardUpsertIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=80)
    unit: RateUnit = RateUnit.token
    credits_per_input: Decimal = Decimal(0)
    credits_per_output: Decimal = Decimal(0)
    provider_cost_per_input: Decimal | None = None
    provider_cost_per_output: Decimal | None = None
    markup: Decimal = Decimal(1)
    is_active: bool = True
    tier: str | None = None


class RateCardOut(BaseModel):
    id: uuid.UUID
    model_id: str
    provider: str
    unit: RateUnit
    credits_per_input: Decimal
    credits_per_output: Decimal
    markup: Decimal
    is_active: bool
    tier: str | None
    version: int


class StorageRateIn(BaseModel):
    kind: StorageKind
    credits_per_gb_month: Decimal = Field(ge=0)


class ByokFactorIn(BaseModel):
    factor: Decimal = Field(ge=0)


class LLMProviderOut(BaseModel):
    """The org's LLM provider selection. The BYOK key is NEVER returned;
    ``has_key`` reports whether one is stored (mirrors EmailAccountOut)."""

    provider: str
    model: str | None
    base_url: str | None
    has_key: bool
    is_active: bool
    version: int


class LLMProviderSetIn(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    model: str | None = Field(default=None, max_length=160)
    base_url: str | None = Field(default=None, max_length=400)
    # api_key semantics: ``None`` leaves the stored key untouched, ``""``
    # clears it (back to our-key/local), a value stores it as the org's BYOK
    # key (fail-closed probed server-side before it is persisted active).
    api_key: str | None = None


class ScalewayModelsOut(BaseModel):
    models: list[str]


class EmbedderProviderOut(BaseModel):
    """The org's hosted-embedder selection. The BYOK key is NEVER returned;
    ``has_key`` reports whether one is stored."""

    provider: str
    model: str | None
    base_url: str | None
    has_key: bool
    is_active: bool
    version: int


class EmbedderProviderSetIn(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    model: str | None = Field(default=None, max_length=160)
    base_url: str | None = Field(default=None, max_length=400)
    # Same semantics as the LLM provider: None=leave, ""=clear, value=store
    # (fail-closed probed server-side: the model must emit the hosted dim).
    api_key: str | None = None


# --- F6: hierarchical memory (FR-8, docs/adr/0005, 0007, 0016) ---


class MemoryWriteIn(BaseModel):
    project_id: uuid.UUID | None = None
    text: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(default="note", max_length=40)
    sources: list[tuple[str, str]] = Field(default_factory=list)
    importance: Decimal = Decimal(0)
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    # Optional memory channel: a tag of kind ``memory_channel`` this
    # blob is filed under. Folded into the attached tag set.
    channel_tag_id: uuid.UUID | None = None
    # Deterministic alternative: the channel's stable ``system_key``
    # (e.g. "email", "telegram"). What integrations use. If both this
    # and ``channel_tag_id`` are given they must resolve to the same
    # channel. Manual writes stay channel-optional (omit both).
    channel_key: str | None = Field(default=None, max_length=64)


class MemorySearchIn(BaseModel):
    project_id: uuid.UUID | None = None
    query: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, gt=0, le=100)
    grader_min_rrf: float | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    # Optional memory channel: narrows to blobs filed under this
    # ``memory_channel`` tag (ANDed into the tag facet).
    channel_tag_id: uuid.UUID | None = None
    # Deterministic alternative: narrow by the channel's stable
    # ``system_key``. If both are given they must resolve to the same
    # channel.
    channel_key: str | None = Field(default=None, max_length=64)
    # Provenance filter (migration 0085): recall only what this author identity
    # (a user or an ai_assistant) wrote. Omit to keep reads shared.
    created_by: uuid.UUID | None = None


class MemoryStatusOut(BaseModel):
    # True when semantic (vector) retrieval is available; False when the
    # optional embedding model is missing and memory is keyword-only.
    semantic: bool


class MemoryBlobOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    namespace: str
    tier: str
    text: str | None
    summary: str | None
    model_id: str | None
    dim: int
    access_count: int
    cluster_id: uuid.UUID | None
    # Provenance (migration 0085): the authoring identity (user or ai_assistant)
    # and the LLM that produced the text (None for a human author).
    created_by: uuid.UUID | None = None
    origin_model_id: str | None = None
    tags: list[TagBrief] = Field(default_factory=list)


class MemoryHitOut(BaseModel):
    blob: MemoryBlobOut
    rrf: float
    # Winning chunk index inside a multi-chunk note (paragraph-split via
    # ParagraphChunker); 0 for whole-doc blobs. Used by the SPA to deep-
    # link to the right paragraph (e.g. ``/notes/:id?chunk=2``).
    chunk_index: int = 0
    # ts_headline over the winning chunk text. Populated only when the
    # source is multi-chunk; ``None`` means the caller should fall back
    # to ``blob.summary`` / a head of ``blob.text``.
    chunk_snippet: str | None = None
    # Provenance marker (ADR-0034): "humus" when surfaced via the parallel
    # humus source (archived material decomposed into atoms), else None.
    # The SPA renders a leaf icon + "from archived material" affordance.
    provenance: str | None = None


class MemoryEraseIn(BaseModel):
    source_kind: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=255)


class MemoryConsolidateIn(BaseModel):
    project_id: uuid.UUID | None = None
    blob_ids: list[uuid.UUID] = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class TierCountsOut(BaseModel):
    hot: int
    warm: int
    cold: int


class ErasedOut(BaseModel):
    deleted: int


# --- Memory channels (controlled, seeded vocabulary; FR-8) ---------


class MemoryChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    # Stable slug; None for a keyless custom channel. Integrations use
    # this, never the name.
    system_key: str | None
    # Enable/disable maps to the tag soft-state; False = disabled (not a
    # valid write/search target).
    enabled: bool
    # True for a canonical seeded channel: renamable and disable-able
    # but its key is immutable and it is not deletable.
    seeded: bool
    # Short read-only English copy keyed by ``system_key`` (manual/
    # agent/note); None for a keyless/custom channel.
    description: str | None = None
    version: int


class MemoryChannelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_key: str | None = Field(default=None, min_length=1, max_length=64)


class MemoryChannelPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Enable/disable the channel (maps to the tag soft-state).
    enabled: bool | None = None
    # Only meaningful for a custom channel; changing a seeded channel's
    # key is rejected (channel.key_immutable).
    system_key: str | None = Field(default=None, min_length=1, max_length=64)


# --- F6b: notes / conversation / intent (FR-16, docs/adr/0020, 0021) ---


class NoteCreateIn(BaseModel):
    kind: NoteKind
    # The structural pair (docs/adr/0003). ``project_tag_id`` is the
    # canonical name, shared with the task schemas; ``project_id`` is
    # what this endpoint shipped with and stays accepted (web, CLI).
    # Both stated with different values is a contradiction, not a
    # precedence question, so it is refused instead of arbitrated.
    project_tag_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    # A note is at-most-one-project: no project is a first-class
    # personal perimeter (docs/adr/0021), and this is the only way to
    # say WHICH client that perimeter belongs to -- omitted, the note
    # lands on the workspace default client. A client that contradicts
    # the project is refused (TAG_CLIENT_PROJECT_MISMATCH).
    client_tag_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    audio_ref: str | None = Field(default=None, max_length=512)
    audio_seconds: int | None = None
    # Opt-out from automatic search indexing, for the whole note. Not a
    # read boundary: see models/index_scope.py.
    index_scope: IndexScope = IndexScope.org

    @model_validator(mode="after")
    def _collapse_project_alias(self) -> NoteCreateIn:
        legacy = "project_id" in self.model_fields_set
        canonical = "project_tag_id" in self.model_fields_set
        if legacy and canonical and self.project_id != self.project_tag_id:
            raise ValueError("project_id and project_tag_id disagree")
        if legacy and not canonical:
            self.project_tag_id = self.project_id
        return self


class NoteTagIn(BaseModel):
    tag_id: uuid.UUID


class NoteAppendIn(BaseModel):
    """Body for POST /notes/{id}/append (task 4ac39ecf). Context-blind
    edit: the caller does NOT need to have read the note body.
    ``target`` picks the field to extend; ``expected_version`` is
    optional (omit to append onto whatever state the row currently has
    -- natural for log-style appenders)."""

    target: str = Field(pattern="^(summary|transcript)$")
    text: str = Field(min_length=1)
    separator: str = Field(default="\n\n", max_length=16)
    expected_version: int | None = None
    dedupe_if_tail_matches: bool = False


class TaskDescriptionAppendIn(BaseModel):
    """Body for POST /tasks/{id}/description/append (task 4ac39ecf).
    Same semantics as NoteAppendIn, scoped to ``task.description``."""

    text: str = Field(min_length=1)
    separator: str = Field(default="\n\n", max_length=16)
    expected_version: int | None = None
    dedupe_if_tail_matches: bool = False


class TaskDescriptionPrependIn(BaseModel):
    """Body for POST /tasks/{id}/description/prepend (task 5662a07f):
    prepend ``text`` to the FRONT of ``task.description``. Mirror of
    TaskDescriptionAppendIn; ``dedupe_if_head_matches`` no-ops when the
    body already starts with ``text``."""

    text: str = Field(min_length=1)
    separator: str = Field(default="\n\n", max_length=16)
    expected_version: int | None = None
    dedupe_if_head_matches: bool = False


class TaskDescriptionReplaceIn(BaseModel):
    """Body for POST /tasks/{id}/description/replace: anchored
    find/replace inside ``task.description`` without resending it. Same
    semantics as the note-part and annotation replaces -- ``count=0``
    (default) swaps every occurrence of the literal ``find``, a positive
    ``count`` only the first N, and a no-op returns ``replacements=0``
    without bumping the version. ``expected_version`` omitted writes onto
    the current version, matching the append/prepend twins."""

    find: str = Field(min_length=1)
    replace: str
    expected_version: int | None = None
    count: int = Field(default=0, ge=0)


class AppendOut(BaseModel):
    """Response for the append endpoints. ``appended_chars`` is 0 when
    ``dedupe_if_tail_matches=True`` triggered a no-op."""

    id: uuid.UUID
    version: int
    appended_chars: int


class ReplaceOut(BaseModel):
    """Response for the note-part replace endpoint. ``replacements`` is
    the number of occurrences swapped (0 on a no-op, in which case the
    version is unchanged)."""

    id: uuid.UUID
    version: int
    replacements: int


class NotePartAppendIn(BaseModel):
    """Body for POST /notes/{id}/parts/{pid}/append (task 27f4d6c9).
    Chunked append: stream a large markdown body in N ordered chunks,
    each asserting ``expected_version`` (the cursor returned by the
    previous chunk). Chunks concatenate **raw** (no separator) for
    byte-exact reassembly. Recommended client chunk size ~32k chars to
    stay under any transport payload postal_code. ``chunk_index`` is advisory
    (client-side ordering / progress); idempotency is version-based.
    Set ``is_last=True`` on the final chunk so the recovery-history
    revision is sealed once for the whole upload."""

    chunk: str = Field(min_length=1)
    expected_version: int
    chunk_index: int = Field(default=0, ge=0)
    is_last: bool = True
    operation_id: str | None = None


class NotePartPrependIn(BaseModel):
    """Body for POST /notes/{id}/parts/{pid}/prepend (task 5662a07f):
    prepend ``text`` to the FRONT of a part without resending the body.
    Single-shot (the natural shape for a header / intro); concatenated
    raw before the current body. ``expected_version`` is the optimistic
    cursor."""

    text: str = Field(min_length=1)
    expected_version: int
    operation_id: str | None = None


class NotePartReplaceIn(BaseModel):
    """Body for POST /notes/{id}/parts/{pid}/replace (task 5662a07f):
    anchored find/replace inside one part without resending the body.
    ``count=0`` (default) replaces every occurrence of the literal
    ``find``; a positive ``count`` only the first N. ``expected_version``
    is the optimistic cursor. A no-op (``find`` absent) returns
    ``replacements=0`` and does not bump the version."""

    find: str = Field(min_length=1)
    replace: str
    expected_version: int
    count: int = Field(default=0, ge=0)
    operation_id: str | None = None


class AnnotationAppendIn(BaseModel):
    """Body for POST /annotations/{id}/body/{append,prepend}: add text to
    one end of a comment/annotation body without resending it. The
    annotation twin of the task-description append, so the same shape --
    ``expected_version`` omitted writes onto the current version, and the
    dedupe flag makes a replay a no-op (it tests the tail on append, the
    head on prepend)."""

    text: str = Field(min_length=1)
    separator: str = "\n\n"
    expected_version: int | None = None
    dedupe_if_tail_matches: bool = False


class AnnotationReplaceIn(BaseModel):
    """Body for POST /annotations/{id}/body/replace: anchored
    find/replace inside one comment/annotation body without resending
    it. The annotation twin of ``NotePartReplaceIn``, same semantics --
    ``count=0`` (default) replaces every occurrence of the literal
    ``find``, a positive ``count`` only the first N, and a no-op
    (``find`` absent) returns ``replacements=0`` without bumping the
    version."""

    find: str = Field(min_length=1)
    replace: str
    expected_version: int
    count: int = Field(default=0, ge=0)


class NotePartOut(BaseModel):
    """One ordered markdown block of a note (task 71c9d670 Phase 2a).
    ``ui_collapsed`` is the caller's current collapse state for this
    part; missing/no row → ``false`` (default expanded). Populated
    on GET /notes/{id} only; bulk listings omit it to stay light."""

    id: uuid.UUID
    note_id: uuid.UUID
    ord: int
    title: str | None = None
    body: str
    lang: str | None = None
    merged_from_note_id: uuid.UUID | None = None
    version: int
    ui_collapsed: bool = False


class NotePartTrashOut(BaseModel):
    """A trashed note part, restorable by id (migration 0089). Same
    content shape as ``NotePartOut`` minus the live-only fields, plus
    when and by whom it was trashed. ``ord`` is the position it will
    aim for on restore."""

    id: uuid.UUID
    note_id: uuid.UUID
    ord: int
    title: str | None = None
    body: str
    lang: str | None = None
    trashed_at: datetime.datetime
    trashed_by: uuid.UUID | None = None


class NotePartCreateIn(BaseModel):
    """Body for POST /notes/{id}/parts. ``ord`` is optional; when
    omitted the new part lands at the end. When supplied every part
    with ord ≥ value is shifted forward by one."""

    body: str = Field(default="")
    title: str | None = Field(default=None, max_length=300)
    lang: str | None = Field(default=None, max_length=16)
    ord: int | None = Field(default=None, ge=0)


class NotePartPatchIn(BaseModel):
    """Body for PATCH /notes/{id}/parts/{pid}. Each field may be
    omitted to leave it unchanged. Passing ``lang=null`` (or
    ``title=null``) explicitly clears the value."""

    expected_version: int
    body: str | None = None
    title: str | None = None
    lang: str | None = None
    # Bit of Pydantic awkwardness: we want to distinguish "field
    # absent from the JSON" from "field explicitly null". The router
    # peeks at ``model_fields_set`` to make the distinction (same
    # pattern for both ``lang`` and ``title``).


class NotePartReorderIn(BaseModel):
    """Body for PUT /notes/{id}/parts/order. ``part_ids`` must be the
    complete set of the note's parts in the desired order; a missing
    or extra id raises a domain error so the SPA can't accidentally
    drop a row via reorder."""

    part_ids: list[uuid.UUID] = Field(min_length=1)


class NotePartUIStateIn(BaseModel):
    """Body for PUT /notes/{id}/parts/{pid}/ui-state. User-scoped,
    last-write-wins (no version)."""

    collapsed: bool


class NoteMergeIn(BaseModel):
    """Body for POST /notes/merge (task 71c9d670 Phase 2b). Folds the
    source note's parts into the target with a fresh ord. ``strategy``
    is reserved for future ``interleave`` variants; v1 ships
    ``append`` only."""

    source_note_id: uuid.UUID
    target_note_id: uuid.UUID
    strategy: str = Field(default="append", pattern="^(append)$")


class _NoteCommon(BaseModel):
    """Fields shared by the list and the single-note projections.

    Not a response model itself. It exists so ``NoteListOut`` is a
    strict subset of ``NoteOut`` by construction, rather than by two
    field lists that drift apart on the next added column.
    """

    id: uuid.UUID
    project_id: uuid.UUID | None
    # Set when the note is a task's "work note" (the SPA detects it to
    # open it from the task and bill its timer to the task).
    task_id: uuid.UUID | None = None
    # Title of the linked task (``task_id``), denormalized so the note's
    # "work note" banner can show *which* task time is billed to without
    # a second round-trip. Resolved regardless of the task's lifecycle
    # state (archived / soft-deleted), so the banner never blanks out for
    # a note linked to a closed task. None when the note has no task.
    task_title: str | None = None
    kind: NoteKind
    status: NoteStatus
    title: str | None
    summary: str | None
    audio_ref: str | None
    audio_seconds: int | None = None
    is_archived: bool = False
    # Fase P (task 561c6aca): finished prose the distiller never compacts.
    protected: bool = False
    deleted_at: datetime.datetime | None = None
    tags: list[TagBrief] = []
    version: int
    # docs/adr/0029 P1: garden lifecycle. ``maturity`` defaults to
    # ``seed`` (the migration backfilled every existing note). When
    # ``promoted_at`` is set the note is read-only at the service
    # layer (transplanted to a task).
    maturity: str = "seed"
    # Typed, unlike ``maturity`` above: the column is a native enum and
    # the SPA gets a union instead of a bare string, matching how
    # ``TaskOut.necessity`` is declared.
    index_scope: IndexScope = IndexScope.org
    promoted_at: datetime.datetime | None = None
    # Humus atom subtype (distillation/pattern/season); None for ordinary
    # notes. Lets the SPA show the "sorgente/ripristina" affordance
    # (Fase P) only on atoms.
    humus_kind: str | None = None
    # docs/adr/0029 P1: every task generated from this note
    # (kind ∈ {derived_from, promoted_from}), in emission order. The
    # SPA renders an "N tasks" chip on the note row from this list;
    # ``task_id`` above stays the SPA-canonical "primary".
    derived_task_ids: list[uuid.UUID] = Field(default_factory=list)
    # Task 1e07437e: total count of typed links into this note across
    # every kind (subject, artifact, derived_from, promoted_from), so
    # the SPA chip reflects all linked tasks -- not just the two
    # "fruit" kinds that ``derived_task_ids`` covers.
    linked_task_count: int = 0


class NoteListOut(_NoteCommon):
    """The list projection: metadata plus a bounded one-line preview.

    Deliberately WITHOUT ``transcript``. A note body is unbounded, so
    serializing it per row makes ``GET /notes`` cost O(total content of
    the org) in bytes instead of O(rows shown) -- in production that
    was a multi-MB response for a screen that renders one line per
    note. Callers that need the body read ``GET /notes/{id}``; callers
    that need to MATCH on it pass ``q``, which filters server-side over
    part bodies (``services.notes.list_notes``) instead of shipping
    every body so the client can filter locally.
    """

    # First non-empty line of the body, capped server-side
    # (``services.notes._previews_by_note``). Enough for a row label or
    # a card subtitle, which is all the list surfaces ever did with the
    # full body. None when the note has no text yet.
    preview: str | None = None


class NoteOut(_NoteCommon):
    """The single-note projection: everything, body included."""

    transcript: str | None
    # Task 71c9d670 Phase 2a: the note's ordered markdown blocks
    # (note_part). Populated on GET /notes/{id} (single-note path);
    # the other single-note paths leave it empty to stay light. Empty
    # when no parts exist yet (a freshly created note, or one whose
    # transcript was never split).
    parts: list[NotePartOut] = Field(default_factory=list)


class NoteSetMaturityIn(BaseModel):
    maturity: str = Field(pattern="^(seed|growing|mature|dormant)$")


class NotePromoteIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)


class NoteDeriveTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    estimate_effort_h: Decimal | None = None
    # Optional tag inheritance: the SPA passes the note's tags (client
    # + project) so the derived task lands under the same project /
    # client as its parent note instead of the workspace default.
    extra_tag_ids: list[uuid.UUID] = Field(default_factory=list)


class NoteLinkIn(BaseModel):
    parent_note_id: uuid.UUID
    child_note_id: uuid.UUID
    # The mycelial 4-verb model (ADR-0040). ``related`` is undirected:
    # the service canonicalises (parent, child) regardless of the order
    # the client sends.
    kind: str = Field(pattern="^(hypha_of|related|supersedes|contradicts)$")


class NoteLinkOut(BaseModel):
    id: uuid.UUID
    parent_note_id: uuid.UUID
    child_note_id: uuid.UUID
    kind: str
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime


class NoteTaskLinkOut(BaseModel):
    id: uuid.UUID
    note_id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime


class NoteTaskLinkIn(BaseModel):
    """Body for POST /notes/{id}/task-links and POST /tasks/{id}/note-links.
    Only ``subject`` and ``artifact`` are accepted here; ``derived_from``
    and ``promoted_from`` are creation-with-link operations exposed via
    their dedicated endpoints (derive-task / promote)."""

    task_id: uuid.UUID
    kind: str = Field(pattern="^(subject|artifact)$")


class TaskNoteLinkIn(BaseModel):
    """Body for POST /tasks/{id}/note-links. Mirror of NoteTaskLinkIn
    from the task-side (the note id lives in the body)."""

    note_id: uuid.UUID
    kind: str = Field(pattern="^(subject|artifact)$")


class NoteWithLinksOut(BaseModel):
    note: NoteOut
    outgoing: list[NoteLinkOut] = []
    incoming: list[NoteLinkOut] = []
    task_links: list[NoteTaskLinkOut] = []


class TaskNoteLinksOut(BaseModel):
    """Lookup payload for the task-side LinkedNotesPanel. Returns the
    full set of typed note↔task relations touching ``task_id`` (all four
    kinds), independent of NoteOut so list endpoints stay slim."""

    task_id: uuid.UUID
    note_links: list[NoteTaskLinkOut] = []


class DerivedTaskOut(BaseModel):
    task_id: uuid.UUID
    link: NoteTaskLinkOut


class NotePatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    # Bidirectional Proposal A link (NOTE side): set OR clear
    # notes.task_id. Honoured only when the key is present in the
    # request body (model_fields_set): an explicit null unlinks,
    # omitting it preserves the existing link.
    task_id: uuid.UUID | None = None
    # Voice-note audio binding (#46): set after the attachment upload
    # completes ("attachment:<id>"). Same model_fields_set semantics
    # as task_id (omit = no change, ``None`` = clear).
    audio_ref: str | None = Field(default=None, max_length=512)
    # Structural re-tagging, single-valued (docs/adr/0003). Same
    # model_fields_set semantics as ``task_id``: omitting the key
    # changes nothing. Stating ``project_tag_id`` is a MOVE (the client
    # follows the project); an explicit null CLEARS the project -- the
    # un-share path, which sends the note's blobs back to the personal
    # perimeter (docs/adr/0021) and is legal only because a note is
    # at-most-one-project. ``client_tag_id`` re-points the client and is
    # refused when it contradicts the attached project; an explicit null
    # there is refused too (a note always has exactly one client).
    project_tag_id: uuid.UUID | None = None
    client_tag_id: uuid.UUID | None = None
    # Omit = no change. Unlike the two above there is no "clear": the
    # column is NOT NULL, so the way back is ``org``, stated.
    index_scope: IndexScope | None = None


class TaskNoteCreateIn(BaseModel):
    # POST /tasks/{task_id}/notes: title optional (defaults to the task
    # title); a fresh work note pre-linked to the task.
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    index_scope: IndexScope = IndexScope.org


class NoteTranscribeIn(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    embed: bool = True


class ConversationStartIn(BaseModel):
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)


class AppendMessageIn(BaseModel):
    content: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class NoteTurnOut(BaseModel):
    id: uuid.UUID
    role: TurnRole
    content: str
    ord: int


class SynthesizeIn(BaseModel):
    text: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class SynthOut(BaseModel):
    audio_ref: str
    model_id: str


class DistillationOut(BaseModel):
    # Result of the fungal-decomposition pass (ADR-0034, task 4a718dc4):
    # the source note's distillation note. ``created`` is False when an
    # earlier distillation already existed (idempotent no-op), in which
    # case ``model_id`` is "cached".
    source_note_id: uuid.UUID
    distilled_note_id: uuid.UUID
    model_id: str
    created: bool


class CommandIn(BaseModel):
    text: str = Field(min_length=1)


class NoteEraseOut(BaseModel):
    audio_ref: str | None
    memory_blobs_deleted: int


class AttachmentOut(BaseModel):
    # Metadata only: the binary ``data`` is NEVER serialised here (it is
    # streamed by GET /attachments/{id}/download). Exactly one of
    # note_id / task_id is set (the file's single parent).
    id: uuid.UUID
    note_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime.datetime


class AttachmentCapabilityIn(BaseModel):
    # Mint a parent-scoped ``attachment:read`` capability token. ``parent_kind``
    # selects the note vs task scope; ``ttl_seconds`` caps the lifetime (the
    # grant is multi-use until then, never consumed).
    parent_kind: Literal["note", "task"]
    parent_id: uuid.UUID
    ttl_seconds: int = Field(default=300, ge=1, le=3600)


class AttachmentCapabilityOut(BaseModel):
    # ``token`` is the raw ``mycelium_cap_`` value, returned exactly once. The
    # caller builds a ``GET /attachments/{id}/download`` per listed attachment
    # with ``Authorization: Bearer <token>`` (no PAT, no X-Workspace-Id).
    # ``attachments`` is empty for a write grant (nothing to enumerate; the
    # caller POSTs the upload), populated for a read grant.
    token: str
    expires_at: datetime.datetime
    parent_kind: str
    parent_id: uuid.UUID
    attachments: list[AttachmentOut] = Field(default_factory=list)


class TextBlockCapabilityIn(BaseModel):
    # Mint a mycelium_cap_ token for a text block (note part body / task
    # description / comment body). ``verb`` picks read (multi-use) vs write
    # / patch (single-use); ``kind`` + ``resource_id`` pin the exact target.
    kind: Literal["note_part", "task_description", "annotation"]
    resource_id: uuid.UUID
    verb: Literal["read", "write", "patch"]
    ttl_seconds: int = Field(default=300, ge=1, le=3600)


class TextBlockCapabilityOut(BaseModel):
    # ``token`` is the raw ``mycelium_cap_`` value, returned exactly once. The
    # caller hits the matching raw / stream / patch route with
    # ``Authorization: Bearer <token>`` (no PAT, no X-Workspace-Id).
    token: str
    expires_at: datetime.datetime
    kind: str
    resource_id: uuid.UUID
    verb: str


# --- F7: electronic invoicing (FR-9, docs/adr/0009, 0010, 0011) ---


class IssuerProfileIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    # FatturaPA Anagrafica is a choice: Denominazione (legal_name) OR
    # Nome+Cognome (first_name+last_name). legal_name is optional so a persona
    # fisica can omit it; the service enforces that one mode is complete.
    legal_name: str | None = Field(default=None, max_length=200)
    vat_number: str | None = Field(default=None, max_length=28)
    tax_code: str | None = Field(default=None, max_length=16)
    tax_regime: str = Field(default="RF01", max_length=4)
    country_code: str = Field(default="IT", max_length=2)
    address: str = Field(default="", max_length=200)
    civic_number: str | None = Field(default=None, max_length=8)
    postal_code: str = Field(default="", max_length=10)
    city: str = Field(default="", max_length=120)
    province: str | None = Field(default=None, max_length=4)
    country: str = Field(default="IT", max_length=2)
    # CodiceDestinatario of this issuer's own reception channel (passive
    # SdI cycle): suppliers addressing it route invoices to our inbound.
    sdi_code: str | None = Field(default=None, max_length=7)
    rea: str | None = Field(default=None, max_length=40)
    # Fallback payment IBAN (precedence: invoice > client > issuer).
    default_iban: str | None = Field(default=None, max_length=34)
    legal_reference: str | None = Field(default=None, max_length=100)
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    # Optional contact channels. PEC prints on the PDF; the rest go in
    # CedentePrestatore/Contatti (Telefono/Fax/Email).
    pec: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=20)
    fax: str | None = Field(default=None, max_length=20)
    # Per-contact "show on invoice" toggles (gate XML Contatti + PDF for
    # phone/email; PDF-only for pec). Default true.
    show_phone: bool = True
    show_email: bool = True
    show_pec: bool = True
    # Issuer-level fallbacks for payment metadata (used only when the
    # client carries no own default). Closed-enum codes (TPxx / MPxx);
    # validated server-side.
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    # Free-text header block (the "intestazione") printed at the top of
    # the courtesy PDF; the logo image is uploaded separately.
    letterhead: str | None = Field(default=None, max_length=2000)
    is_default: bool = False


class IssuerProfilePatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    vat_number: str | None = Field(default=None, max_length=28)
    tax_code: str | None = Field(default=None, max_length=16)
    tax_regime: str | None = Field(default=None, max_length=4)
    country_code: str | None = Field(default=None, max_length=2)
    address: str | None = Field(default=None, max_length=200)
    civic_number: str | None = Field(default=None, max_length=8)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=4)
    country: str | None = Field(default=None, max_length=2)
    sdi_code: str | None = Field(default=None, max_length=7)
    rea: str | None = Field(default=None, max_length=40)
    default_iban: str | None = Field(default=None, max_length=34)
    legal_reference: str | None = Field(default=None, max_length=100)
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    pec: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=20)
    fax: str | None = Field(default=None, max_length=20)
    show_phone: bool | None = None
    show_email: bool | None = None
    show_pec: bool | None = None
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    letterhead: str | None = Field(default=None, max_length=2000)
    logo_kind: str | None = Field(default=None, max_length=16)
    logo_position: str | None = Field(default=None, max_length=8)
    is_default: bool | None = None


class IssuerProfileOut(BaseModel):
    id: uuid.UUID
    label: str
    legal_name: str | None
    vat_number: str | None
    tax_code: str | None
    tax_regime: str
    country_code: str
    address: str
    civic_number: str | None
    postal_code: str
    city: str
    province: str | None
    country: str
    sdi_code: str | None
    rea: str | None
    default_iban: str | None
    legal_reference: str | None
    first_name: str | None
    last_name: str | None
    pec: str | None
    email: str | None
    phone: str | None
    fax: str | None
    show_phone: bool
    show_email: bool
    show_pec: bool
    default_payment_conditions_code: str | None
    default_payment_method_code: str | None
    default_payment_terms_days: int | None
    letterhead: str | None
    logo_mime: str | None
    has_logo: bool
    logo_kind: str
    logo_position: str
    logo_qr_fields: str
    logo_qr_ecc: str
    is_default: bool
    conservation_adhesion: str
    version: int


class InvoiceCounterOut(BaseModel):
    """A counter row for the admin override UI. ``max_emitted`` is the
    floor (the highest number already on an invoice under the same key);
    the new ``last_number`` must be >= ``max_emitted`` or the override is
    rejected."""

    issuer_profile_id: uuid.UUID
    series: str
    year: int
    last_number: int
    max_emitted: int


class InvoiceCounterPatchIn(BaseModel):
    last_number: int = Field(ge=0)


class ConservationAdhesionIn(BaseModel):
    adhesion: str = Field(pattern="^(none|requested|active)$")


class SdiMandateIn(BaseModel):
    reference: str | None = Field(default=None, max_length=200)


class SdiMandateOut(BaseModel):
    id: uuid.UUID
    issuer_profile_id: uuid.UUID
    status: str
    scope: str
    reference: str | None
    granted_at: datetime.datetime
    revoked_at: datetime.datetime | None
    version: int


class InvoiceCreateIn(BaseModel):
    client_tag_id: uuid.UUID
    issuer_profile_id: uuid.UUID | None = None
    year: int | None = None
    # None -> the service defaults to the client's own sezionale (per-client
    # numbering). An explicit value pins a custom series.
    series: str | None = Field(default=None, max_length=20)
    purpose: str | None = Field(default=None, max_length=200)


class InvoicePatchIn(BaseModel):
    client_tag_id: uuid.UUID | None = None
    issuer_profile_id: uuid.UUID | None = None
    series: str | None = Field(default=None, max_length=20)
    currency: str | None = Field(default=None, max_length=3)
    purpose: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    payment_iban: str | None = Field(default=None, max_length=34)
    payment_due_date: datetime.date | None = None
    # Per-document overrides of the client/issuer payment defaults
    # (FatturaPA TPxx / MPxx + net days). NULL = inherit (and the XML
    # falls through to the client, then issuer, then TP02 / MP05).
    payment_conditions_code: str | None = Field(default=None, max_length=4)
    payment_method_code: str | None = Field(default=None, max_length=4)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class InvoiceLineAltriDatiIn(BaseModel):
    """One AltriDatiGestionali block of a line (FatturaPA 2.2.1.16),
    0..N per line and ORDERED (the array order is the emission order).

    ``tipo_dato`` is a LABEL naming the kind of data, not a description:
    the free text belongs in ``riferimento_testo``. The spec fixes no
    enum; the binding conventions worth offering as UI shortcuts are
    INTENTO (dichiarazione d'intento: protocollo + progressivo in the
    text), N.DOC.COMM (documento commerciale: id / progressivo / date
    across the three reference fields) and NB3 (bollo exemption between
    banks and account holders, all three left empty).

    Bounds mirror the XSD (String10Type / String60LatinType /
    Amount8DecimalType); the service re-checks them against the real
    facets (character ranges, decimal places) and raises a coded error."""

    tipo_dato: str = Field(min_length=1, max_length=10)
    riferimento_testo: str | None = Field(default=None, max_length=60)
    riferimento_numero: Decimal | None = None
    riferimento_data: datetime.date | None = None


class InvoiceLineAltriDatiOut(BaseModel):
    id: uuid.UUID
    tipo_dato: str
    riferimento_testo: str | None
    riferimento_numero: Decimal | None
    riferimento_data: datetime.date | None


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=1000)
    unit_price: Decimal
    quantity: Decimal = Decimal(1)
    # None = unset: the service resolves it from the issuer's regime
    # (forfettario RF19 -> 0% + Natura N2.2; ordinary regime -> 22%).
    # An explicit value is always honoured.
    vat_rate: Decimal | None = None
    vat_nature: str | None = Field(default=None, max_length=4)
    # AltriDatiGestionali, empty by default (omitted -> nothing emitted).
    # Tri-state on the PUT: None leaves the line's existing blocks alone
    # (a price fix must not silently drop them), a list REPLACES them,
    # and [] clears them.
    altri_dati: list[InvoiceLineAltriDatiIn] | None = None


class InvoiceLineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    vat_nature: str | None
    # Emission order; empty for the overwhelmingly common line that
    # carries no AltriDatiGestionali.
    altri_dati: list[InvoiceLineAltriDatiOut] = Field(default_factory=list)


class InvoiceOut(BaseModel):
    id: uuid.UUID
    client_tag_id: uuid.UUID
    issuer_profile_id: uuid.UUID | None
    kind: InvoiceKind
    document_type: DocumentType
    parent_invoice_id: uuid.UUID | None
    series: str
    year: int
    number: int | None
    state: InvoiceState
    currency: str
    purpose: str | None
    notes: str | None
    payment_iban: str | None
    payment_due_date: datetime.date | None
    payment_conditions_code: str | None
    payment_method_code: str | None
    payment_terms_days: int | None
    taxable: Decimal
    vat: Decimal
    stamp_duty: Decimal
    total: Decimal
    identificativo_sdi: str | None
    sdi_status: SdiStatus
    # Non-null while a dispatch is unsettled (ADR-0046): together with
    # state=transmitted and a null identificativo_sdi it marks the invoice
    # retryable (the SPA's "Ritrasmetti" affordance keys on this, NOT on the
    # bare null ident, which a successful manual export also has).
    sdi_dispatch_started_at: datetime.datetime | None
    payment_status: PaymentStatus
    conservation_status: ConservationStatus
    deleted_at: datetime.datetime | None
    is_archived: bool
    #: Composed by a payment connector in shadow mode and deliberately not sent
    #: (ADR-0051). Distinct from every other reason a draft is unsent, which is
    #: the whole point during a parallel run with an incumbent provider.
    dry_run: bool = False
    version: int


class TransmitIn(BaseModel):
    progressivo: str | None = None


class CreditNoteIn(BaseModel):
    parent_invoice_id: uuid.UUID
    purpose: str | None = Field(default=None, max_length=200)


class ReceiptIn(BaseModel):
    identificativo_sdi: str = Field(min_length=1, max_length=40)
    outcome: str = Field(pattern="^(RC|MC|NS|AT)$")


class EsitoCommittenteIn(BaseModel):
    """Buyer-side EsitoCommittente (ADR-0011 v1.1): EC01 accepts the
    invoice, EC02 rejects it (max 255-char descrizione, only meaningful on
    rejection). The signed XML is built + persisted server-side; clients
    never craft the signature."""

    esito: str = Field(pattern="^(EC01|EC02)$")
    descrizione: str | None = Field(default=None, max_length=255)


class EsitoCommittenteOut(BaseModel):
    received_invoice_id: uuid.UUID
    esito: str
    message_id: str
    sent_at: datetime.datetime


class InvoiceXmlOut(BaseModel):
    xml: str


class SdiNotificationXmlOut(BaseModel):
    """The raw signed SdI notification XML (RC/MC/NS/...) for view/download,
    plus its SdI file name. The XML is the XAdES-signed document SdI delivered:
    the legal proof of the transmission outcome."""

    xml: str
    file_name: str | None


class InvoiceNotificationError(BaseModel):
    """One ``Errore`` from a NotificaScarto error list."""

    codice: str
    descrizione: str


class InvoiceNotificationOut(BaseModel):
    """One SdI notification in the invoice's transmission timeline. ``esito``
    is the buyer EC verdict (EC01 accepted / EC02 rejected) on an NE;
    ``errors`` is the rejection list on an NS (empty for every other kind).
    ``id`` addresses the notification for the signed-XML view/download."""

    id: uuid.UUID
    kind: str
    received_at: datetime.datetime
    file_name: str | None
    message_id: str | None
    esito: str | None
    errors: list[InvoiceNotificationError]


class PurgeTestInvoicesOut(BaseModel):
    deleted: int


class InvoicePreviewParty(BaseModel):
    """Resolved issuer or client identity for the preview. None when the
    draft has no profile resolved yet."""

    legal_name: str
    vat_number: str | None = None
    tax_code: str | None = None
    tax_regime: str | None = None
    address: str | None = None
    civic_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    # Client only (the SdI recipient address); None on the issuer side.
    sdi_code: str | None = None
    pec: str | None = None
    # Issuer-side contacts (None / default on the client side). ``phone`` and
    # ``email`` ride in FatturaPA <Contatti>, ``pec`` on the courtesy PDF; each
    # is gated by its per-contact visibility toggle so the SPA can render the
    # cedente block exactly as the emitted document carries it. Defaults show
    # (mirrors the emit-side ``is not False`` rule for a profile without toggles).
    phone: str | None = None
    email: str | None = None
    show_phone: bool = True
    show_email: bool = True
    show_pec: bool = True


class InvoicePreviewLine(BaseModel):
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    vat_rate: Decimal
    vat_nature: str | None = None
    # AltriDatiGestionali of this line, in emission order: the courtesy
    # rendering must show what the XML carries (owner requirement).
    # Empty for the ordinary line that declares none.
    altri_dati: list[InvoiceLineAltriDatiOut] = Field(default_factory=list)


class InvoicePreviewTotals(BaseModel):
    taxable: Decimal
    vat: Decimal
    stamp_duty: Decimal
    total: Decimal


class InvoicePreviewOut(BaseModel):
    """Full resolved document for the SPA: it renders this without
    re-deriving anything. Tolerant of an incomplete draft (issuer/client
    may be null)."""

    number: str
    series: str
    year: int
    document_type: DocumentType
    date: datetime.date
    payment_due_date: datetime.date | None
    issuer: InvoicePreviewParty | None
    client: InvoicePreviewParty | None
    lines: list[InvoicePreviewLine]
    totals: InvoicePreviewTotals
    effective_iban: str | None
    # "invoice" | "client" | "issuer" | None.
    iban_source: str | None
    purpose: str | None
    notes: str | None
    is_forfettario: bool
    state: InvoiceState
    # SdI transmission lifecycle, read-only (ADR-0011). Scalar status
    # fields on the invoice row -- there is no per-receipt history table
    # yet; richer notifiche need the SdI cooperative channel (out of
    # scope here, see the F7 report). ``identificativo_sdi`` is the
    # correlation id assigned at transmit (None until then / for manual
    # export); ``sdi_status`` is the latest outcome (none/RC/MC/NS/AT);
    # ``conservation_status`` is the AdE free-conservation coverage.
    identificativo_sdi: str | None
    sdi_status: SdiStatus
    conservation_status: ConservationStatus


# --- F8: notifications, recurrence, reminders (FR-12) ---


class NotificationPrefIn(BaseModel):
    user_id: uuid.UUID
    channel: NotificationChannelKind
    enabled: bool = True
    target: str = Field(default="", max_length=320)


class NotificationPrefOut(BaseModel):
    user_id: uuid.UUID
    channel: NotificationChannelKind
    enabled: bool
    target: str


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(max_length=256)
    auth: str = Field(max_length=256)


class PushSubscriptionIn(BaseModel):
    """A browser PushManager subscription in its ``toJSON()`` shape:
    endpoint + encryption keys. ``expirationTime`` is ignored."""

    endpoint: str = Field(max_length=2048)
    keys: PushSubscriptionKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(max_length=2048)


class VapidPublicKeyOut(BaseModel):
    """Handed to the SPA so it can subscribe the browser's push manager.
    ``configured`` is false when the deploy has no VAPID keypair, in which
    case the SPA hides the browser-notifications affordance."""

    configured: bool
    public_key: str


class NotificationOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    channel: NotificationChannelKind
    kind: str
    title: str
    body: str
    status: NotificationStatus
    created_at: datetime.datetime
    sent_at: datetime.datetime | None = None


class DispatchOut(BaseModel):
    sent: int
    failed: int


class RecurrenceIn(BaseModel):
    task_id: uuid.UUID
    freq: RecurrenceFreq
    next_run: datetime.datetime
    interval: int = Field(default=1, ge=1)
    until: datetime.datetime | None = None


class RecurrenceOut(BaseModel):
    task_id: uuid.UUID
    freq: RecurrenceFreq
    interval: int
    next_run: datetime.datetime
    until: datetime.datetime | None
    active: bool


class CountOut(BaseModel):
    count: int


class ReminderIn(BaseModel):
    # Minutes before the task due date (0 = at due).
    offset_minutes: int = Field(ge=0, le=525600)
    # Channels for THIS reminder; null/empty = the user's default (all their
    # enabled channels). Lets one reminder go to e.g. email + telegram.
    channels: list[NotificationChannelKind] | None = None


class ReminderOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    offset_minutes: int
    channels: list[str] | None = None


# --- Telegram bot integration (epic #125 P2) ---


class TelegramLinkRequestOut(BaseModel):
    """The single-use deep-link payload the SPA renders as a button /
    QR. ``code`` is the short token embedded in the URL; ``deep_link``
    is the pre-built ``https://t.me/<bot>?start=<code>``."""

    code: str
    expires_at: datetime.datetime
    bot_username: str
    deep_link: str


class TelegramLinkStatusOut(BaseModel):
    linked: bool
    chat_username: str | None = None
    linked_at: datetime.datetime | None = None


class TelegramWebhookOut(BaseModel):
    ok: bool


class AgentTokenCreateIn(BaseModel):
    """Mint a long-lived bearer credential for MCP / external automation."""

    name: str = Field(min_length=1, max_length=120)
    scope: str = Field(default="mcp", min_length=1, max_length=32)
    # 1-year TTL by default (matches the service constant). ``None`` =
    # never expires; pass ``0`` to be explicit about "never". Capped at
    # 5 years so a forgotten secret has a bounded lifetime even when
    # the operator picks a long-lived rotation.
    ttl_days: int | None = Field(default=365, ge=0, le=365 * 5)


class AgentTokenOut(BaseModel):
    """Persisted token metadata. The raw value is never on this shape;
    it appears only on :class:`AgentTokenCreateOut`."""

    id: uuid.UUID
    name: str
    scope: str
    prefix: str
    expires_at: datetime.datetime | None
    last_used_at: datetime.datetime | None
    revoked_at: datetime.datetime | None
    created_at: datetime.datetime


class AgentTokenCreateOut(BaseModel):
    """The mint response. ``raw`` is the only place the plaintext token
    ever leaves the server; the operator is told to copy it now or
    rotate."""

    id: uuid.UUID
    name: str
    scope: str
    prefix: str
    expires_at: datetime.datetime | None
    created_at: datetime.datetime
    raw: str


class IssuerApiKeyCreateIn(BaseModel):
    """Mint a per-issuer-profile API key for the public Invoice REST API."""

    name: str = Field(min_length=1, max_length=120)
    # Least-privilege default: read-only. The service validates each value
    # against the whitelist and rejects unknowns.
    permissions: list[str] = Field(default_factory=lambda: ["invoice:read"])
    # Mandatory expiry: ``None`` maps to the service's default (and cap) of
    # 365 days -- there is no never-expiring key.
    ttl_days: int | None = Field(default=None, ge=1, le=365)
    # Optional CIDR allowlist (single IPs or networks, v4/v6); None/empty =
    # no restriction. Validated + canonicalized by the service.
    ip_allowlist: list[str] | None = Field(default=None, max_length=32)


class IssuerApiKeyAllowlistIn(BaseModel):
    """Replace a key's CIDR allowlist without re-minting (owner-gated)."""

    ip_allowlist: list[str] | None = Field(default=None, max_length=32)


class IssuerApiKeyOut(BaseModel):
    """Persisted key metadata; the secret never appears here (only on
    :class:`IssuerApiKeyCreateOut`). ``prefix`` is the non-secret display handle
    ``mycelium_ik_<key_public_id>``; ``days_to_expiry`` is derived for the UI."""

    id: uuid.UUID
    issuer_profile_id: uuid.UUID
    name: str
    prefix: str
    permissions: list[str]
    created_at: datetime.datetime
    expires_at: datetime.datetime
    last_used_at: datetime.datetime | None
    previous_secret_last_used_at: datetime.datetime | None
    rotated_at: datetime.datetime | None
    revoked_at: datetime.datetime | None
    days_to_expiry: int
    ip_allowlist: list[str] | None


class IssuerApiKeyCreateOut(IssuerApiKeyOut):
    """Mint / rotate response. ``raw`` is the ONLY place the plaintext secret
    ever leaves the server; copy it now (or rotate)."""

    raw: str


# ---------- Signed invoice webhooks (task 2c23e955, ADR-0047)
class WebhookEndpointIn(BaseModel):
    """Create a signed-webhook endpoint bound to an issuer profile."""

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    # Empty = subscribe to ALL invoice events; else a whitelist. Validated by
    # the service against the event vocabulary.
    event_types: list[str] = Field(default_factory=list)


class WebhookEndpointUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_types: list[str] | None = None
    active: bool | None = None


class WebhookEndpointOut(BaseModel):
    """Endpoint metadata; the signing secret never appears here (only once on
    :class:`WebhookEndpointCreateOut`)."""

    id: uuid.UUID
    issuer_profile_id: uuid.UUID
    name: str
    url: str
    event_types: list[str]
    active: bool
    created_at: datetime.datetime
    revoked_at: datetime.datetime | None


class WebhookEndpointCreateOut(WebhookEndpointOut):
    """Create / rotate response. ``secret`` is the ONLY place the plaintext
    signing secret leaves the server; store it now."""

    secret: str


class WebhookDeliveryOut(BaseModel):
    """One delivery attempt row for the endpoint's recent-activity view."""

    id: uuid.UUID
    event_type: str
    invoice_id: uuid.UUID | None
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime.datetime
    last_attempt_at: datetime.datetime | None
    delivered_at: datetime.datetime | None
    response_code: int | None
    last_error: str | None
    created_at: datetime.datetime


# ---------- AI assistants (ADR-0XX, replaces /settings/mcp manual setup)
class ScopeCatalogEntry(BaseModel):
    """One row of the scope catalog returned by ``GET /ai-assistants/
    scope-catalog`` — drives the SPA's permission picker."""

    key: str
    category: str  # 'read' | 'write' | 'danger'
    label: str
    description: str


class ConnectorInfoOut(BaseModel):
    """Where to point an MCP client. The SPA shows ``mcp_url`` in the
    connector card and the operator pastes it into Claude / Cursor."""

    mcp_url: str
    instructions_md: str


class AiAssistantCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    scope: list[str] | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    # Omitted means ``external``: a credential pasted into a client that
    # runs elsewhere, which is what this endpoint hands out. Declaring
    # ``internal`` says the dispatch loop may drive this assistant.
    runtime: AssistantRuntime = Field(default=AssistantRuntime.external)
    # Omitted means ``workspace``: the credential acts only in the
    # workspace it is created in, which is what every client but the
    # browser panel wants and what the CLI already tells its users.
    # ``account`` is refused for any scope wider than the self-service
    # set, so asking for it is not a way around the mint threshold.
    workspace_binding: WorkspaceBinding = Field(default=WorkspaceBinding.workspace)


class AiAssistantPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=255)
    scope: list[str] | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = Field(default=None)
    runtime: AssistantRuntime | None = Field(default=None)


class AiAssistantOut(BaseModel):
    """Metadata; no secret ever ships in this shape."""

    id: uuid.UUID
    label: str
    provider: str | None
    model_id: str | None
    notes: str | None
    scope: list[str]
    is_active: bool
    runtime: AssistantRuntime
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # First chars of the latest secret (for UI disambiguation across
    # rotations). NULL when no token has been minted yet (shouldn't
    # happen via this flow but defensive).
    token_prefix: str | None = None
    # Read from that same live token, so Settings can say what the
    # secret reaches rather than inferring it from the provider label.
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace


class AiAssistantCreatedOut(BaseModel):
    """Returned exactly once at create / rotate time. ``raw_secret`` is
    plaintext — the SPA copies it to the clipboard and shows a
    "credentials" card; after the user acknowledges, it cannot be
    recovered."""

    assistant: AiAssistantOut
    raw_secret: str


# --- Unified search (task_search) ---------------------------------------


class SearchIn(BaseModel):
    """Unified free-text search across the org. ``kinds`` defaults to
    ['task', 'blob', 'note']; ``kinds=['task']`` is the SPA's task-search
    path, ``kinds=['blob']`` mirrors /memory/search, ``kinds=['note']``
    returns titled note hits (a note part blob resolved to its note).
    ``include_archived`` and ``include_deleted`` apply to ``task`` and
    ``note`` hits. ``rerank`` opts into the cross-encoder reranker for
    this call regardless of the env default (task 27579d6a)."""

    q: str = Field(min_length=1, max_length=2000)
    kinds: list[str] = Field(default_factory=lambda: ["task", "blob", "note"])
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    channel_keys: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, gt=0, le=100)
    include_archived: bool = False
    include_deleted: bool = False
    rerank: bool = False
    operation_id: str = Field(min_length=1, max_length=128, default="search")
    # The four below were accepted by the service long before they were
    # reachable over HTTP. A caller that could not express them had to
    # apply them AFTER the fact, to a list the server had already cut to
    # ``limit`` -- which means "of the twenty best text matches, the ones
    # due today" instead of "the best matches among the ones due today".
    # Same words, different answer, and nothing in the response says
    # which one you got.
    #
    # 'org' (the default) searches every task; 'project' ANDs the
    # caller's ``X-Project-Id`` project tag into the task branch, so task
    # hits are scoped the way note and blob hits already are.
    task_scope: Literal["org", "project"] = "org"
    due_before: datetime.datetime | None = None
    assignee_handles: list[str] | None = None
    task_state_id: uuid.UUID | None = None


class SearchHit(BaseModel):
    """One row in the unified search response. The entity ref depends on
    ``kind``: ``task_id`` for ``kind='task'`` (via ``task_index_pointer``),
    ``note_id`` + ``part_id`` for ``kind='note'`` (via
    ``note_part_index_pointer``), neither for an opaque ``kind='blob'``.
    The ``blob_id`` is always the underlying memory row. ``snippet`` is
    the server-side ``ts_headline`` extract; ``title`` is the task/note
    title when applicable, otherwise None.

    ``tags`` carries the entity's own tags for a task or note hit,
    batched server-side, and is empty for an opaque blob hit, which has
    no entity to carry them. The field was declared here and never
    filled, so a caller wanting to badge a result with its project had
    exactly one option: a fetch per row, turning a twenty-row page into
    twenty-one requests.

    ``scores_by_stage`` says WHY the row ranked where it did. Its
    per-branch entries are 1-based RANKS (lexical_exact / lexical_stem /
    semantic / semantic_hosted / humus), not scores; only ``rrf`` is a
    score, and it is the same number as ``score``. The fused value alone
    cannot separate a hit the lexical branch found from one only the
    dense branch reached, because RRF fuses by rank: a dense-only hit
    scores exactly ``0.2/(60+rank)`` whatever its cosine was. Empty on
    the entity-code path, where nothing was ranked.

    ``scope`` says which boundary the row was retrieved under, ``org`` or ``project``, and
    ``model_id`` is the backing blob's embedding model, where ``none`` means the row is
    keyword-only and carries no dense vector. Both had been on the MCP surface for some time
    and on this one not at all, although the two run the same function: a REST caller could
    not tell a project-scoped hit from an org-wide one, nor a row that rode FTS alone from one
    the dense branch found."""

    kind: str  # 'task' | 'note' | 'blob'
    task_id: uuid.UUID | None = None
    note_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    blob_id: uuid.UUID
    title: str | None = None
    snippet: str | None = None
    score: float
    tags: list[TagBrief] = Field(default_factory=list)
    scores_by_stage: dict[str, float] = Field(default_factory=dict)
    scope: str = "org"
    model_id: str | None = None


class SearchClickIn(BaseModel):
    """One search-result click event (ADR-0035 ``recall_at_k``,
    task 89508ca9). ``rank`` is the clicked hit's 1-based position in
    the ranked list the user saw; ``result_count`` is how many ranked
    hits were shown. ``is_probe`` marks synthetic golden-fixture
    queries so the recall sensor reads real queries only."""

    q: str = Field(min_length=1, max_length=500)
    hit_kind: str  # 'task' | 'note' | 'blob'
    hit_id: uuid.UUID
    rank: int = Field(ge=1)
    result_count: int = Field(ge=1)
    is_probe: bool = False


# --- Device authorization grant (browser extension, and any later
# --- surface that cannot be handed a secret through a web page).


class DeviceAuthorizeIn(BaseModel):
    """Opened by a device with no session. ``client`` names which of our
    own surfaces is asking, so the approval screen can say what it is
    approving rather than "a device"."""

    client: str = Field(min_length=1, max_length=32)


class DeviceAuthorizeOut(BaseModel):
    """Two codes doing two jobs. ``device_code`` is the only one that
    collects anything and is returned exactly once; ``user_code`` is the
    one a person compares against the screen in front of them, and it is
    not a secret.

    ``verification_path`` is a PATH, not a URL: the device knows which
    deployment it is talking to, and returning an absolute address would
    let this response redirect it somewhere else."""

    device_code: str
    user_code: str
    verification_path: str
    expires_at: datetime.datetime
    interval: int


class DeviceTokenIn(BaseModel):
    device_code: str = Field(min_length=16, max_length=512)


class DeviceTokenOut(BaseModel):
    """What the device collects. ``secret`` exists from this moment and
    not before: it is minted in the transaction that answers this call,
    so a request nobody collects leaves no credential behind."""

    secret: str
    assistant_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    scope: list[str]
    expires_at: datetime.datetime | None


class DevicePendingOut(BaseModel):
    """What the person is being asked to approve. Everything here is
    read by a human deciding, which is why it carries the code to
    COMPARE, when the request was opened and from where, and the exact
    grant in the server's own words rather than the client's."""

    user_code: str
    client: str
    opened_at: datetime.datetime
    expires_at: datetime.datetime
    origin_ip: str | None
    scope: list[str]


class DeviceAnswerIn(BaseModel):
    user_code: str = Field(min_length=1, max_length=32)
