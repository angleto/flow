"""ORM models. Import all so Alembic sees the metadata."""

from __future__ import annotations

from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.adjudication import (
    Adjudication,
    AdjudicationStatus,
    AdjudicationStep,
    AdjudicationStepKind,
)
from mycelium_core.models.agent_run import AgentRun, AgentRunStatus
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.annotation import (
    ANNOTATION_DOC_KINDS,
    ANNOTATION_KINDS,
    ANNOTATION_STATUSES,
    Annotation,
    AnnotationUIState,
)
from mycelium_core.models.api_idempotency import ApiIdempotency
from mycelium_core.models.attachment import Attachment
from mycelium_core.models.auth_tokens import (
    EmailVerificationToken,
    PasswordResetToken,
    RefreshToken,
    RevokedToken,
)
from mycelium_core.models.base import Base
from mycelium_core.models.billing import (
    BillingConfig,
    CostBasis,
    CreditLedger,
    DefaultRateCard,
    LedgerEntryKind,
    RateCard,
    RateUnit,
    StorageKind,
    StorageRate,
    UsageRecord,
    Wallet,
)
from mycelium_core.models.budget import Budget, BudgetPeriod
from mycelium_core.models.calendar import (
    CalendarHoliday,
    UserCalendar,
    WorkingCalendar,
)
from mycelium_core.models.capability_token import CapabilityToken
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.classification_job import ClassificationJob
from mycelium_core.models.classification_personal_prior import ClassificationPersonalPrior
from mycelium_core.models.classification_personal_prior_snapshot import (
    ClassificationPersonalPriorSnapshot,
)
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.device_authorization import DeviceAuthorization
from mycelium_core.models.dispatch_request import (
    ACTIVE_DISPATCH_STATUSES,
    DEFAULT_AUTONOMOUS_DISPATCH,
    AutonomousDispatch,
    DispatchRequest,
    DispatchStatus,
)
from mycelium_core.models.email import (
    EmailAccount,
    EmailAccountDefaultTag,
    EmailAccountStatus,
    EmailMessage,
    EmailProvider,
    EmailResponderJob,
)
from mycelium_core.models.entity_revision import EntityRevision
from mycelium_core.models.event_outbox import EventOutbox
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.garden_graph_snapshot import GardenGraphSnapshot
from mycelium_core.models.garden_health import GardenHealthDaily
from mycelium_core.models.google_calendar import (
    CalendarSubscription,
    GoogleCalendarStatus,
)
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.invoice import (
    BuyerVerdict,
    ConservationAdhesion,
    ConservationStatus,
    DocumentType,
    Invoice,
    InvoiceCounter,
    InvoiceKind,
    InvoiceLine,
    InvoiceState,
    IssuerProfile,
    PaymentStatus,
    SdiStatus,
    SdiTransmissionCounter,
)
from mycelium_core.models.issuer_api_key import IssuerApiKey
from mycelium_core.models.issuer_key_rate_limit import IssuerKeyRateLimit
from mycelium_core.models.kg import KG_ENTITY_TYPES, KgEdge, KgEntity
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.memory_blob import BlobSource, MemoryBlob, Tier
from mycelium_core.models.note import (
    Note,
    NoteKind,
    NoteMaturity,
    NoteStatus,
    NoteTurn,
    TurnRole,
)
from mycelium_core.models.note_coactivity import NoteCoactivity
from mycelium_core.models.note_edge_usage import NoteEdgeUsage
from mycelium_core.models.note_link import (
    NOTE_NOTE_LINK_KINDS,
    NOTE_TASK_LINK_KINDS,
    NoteNoteLink,
    NoteTaskLink,
)
from mycelium_core.models.note_part import NotePart, NotePartTrash, NotePartUIState
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.notification import (
    Notification,
    NotificationChannelKind,
    NotificationPref,
    NotificationStatus,
    RecurrenceFreq,
    TaskRecurrence,
)
from mycelium_core.models.org_embedder_provider import EmbedderProviderKind, OrgEmbedderProvider
from mycelium_core.models.org_llm_provider import LLMProviderKind, OrgLLMProvider
from mycelium_core.models.organization import Organization
from mycelium_core.models.payment_connector import (
    PaymentConnector,
    PaymentConnectorEvent,
    PaymentCustomerLink,
    PaymentObjectLink,
    PaymentWebhookDelivery,
)
from mycelium_core.models.payment_connector_refusal import PaymentConnectorRefusalBucket
from mycelium_core.models.precomputed_suggestion import PrecomputedSuggestion
from mycelium_core.models.project_profile import ProjectProfile
from mycelium_core.models.push_subscription import PushSubscription
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.sdi_mandate import SdiMandate, SdiMandateStatus
from mycelium_core.models.sdi_notification import InvoiceNotification, ReceivedInvoiceNotification
from mycelium_core.models.sdi_received import ReceivedInvoice
from mycelium_core.models.search_click import (
    SEARCH_CLICK_KINDS,
    SEARCH_CLICK_QUERY_MAX,
    SearchClick,
)
from mycelium_core.models.system_settings import SystemSettings
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.tag_scope import TagScope
from mycelium_core.models.task import (
    ConstraintKind,
    ExecKind,
    Necessity,
    ScheduleMode,
    Task,
)
from mycelium_core.models.task_checklist_item import TaskChecklistItem
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_handoff import HandoffStatus, TaskHandoff
from mycelium_core.models.task_index_pointer import TaskIndexPointer
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.models.task_relation import TaskRelation
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.models.telegram import TelegramLink, TelegramLinkCode, TelegramUpdate
from mycelium_core.models.time_entry import TimeEntry, TimeSource
from mycelium_core.models.user import User
from mycelium_core.models.webhook import WebhookDelivery, WebhookEndpoint
from mycelium_core.models.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
)

__all__ = [
    "ACTIVE_DISPATCH_STATUSES",
    "ANNOTATION_DOC_KINDS",
    "ANNOTATION_KINDS",
    "ANNOTATION_STATUSES",
    "DEFAULT_AUTONOMOUS_DISPATCH",
    "KG_ENTITY_TYPES",
    "NOTE_NOTE_LINK_KINDS",
    "NOTE_TASK_LINK_KINDS",
    "SEARCH_CLICK_KINDS",
    "SEARCH_CLICK_QUERY_MAX",
    "ActivityLog",
    "Adjudication",
    "AdjudicationStatus",
    "AdjudicationStep",
    "AdjudicationStepKind",
    "AgentRun",
    "AgentRunStatus",
    "AgentToken",
    "Annotation",
    "AnnotationUIState",
    "ApiIdempotency",
    "Attachment",
    "AutonomousDispatch",
    "Base",
    "BillingConfig",
    "BlobSource",
    "Budget",
    "BudgetPeriod",
    "BuyerVerdict",
    "CalendarHoliday",
    "CalendarSubscription",
    "CapabilityToken",
    "ClassificationFeedback",
    "ClassificationJob",
    "ClassificationPersonalPrior",
    "ClassificationPersonalPriorSnapshot",
    "ClientProfile",
    "ConservationAdhesion",
    "ConservationStatus",
    "ConstraintKind",
    "CostBasis",
    "CreditLedger",
    "DefaultRateCard",
    "DependencyType",
    "DeviceAuthorization",
    "DispatchRequest",
    "DispatchStatus",
    "DocumentType",
    "EmailAccount",
    "EmailAccountDefaultTag",
    "EmailAccountStatus",
    "EmailMessage",
    "EmailProvider",
    "EmailResponderJob",
    "EmailVerificationToken",
    "EmbedderProviderKind",
    "EntityRevision",
    "EventOutbox",
    "ExecKind",
    "Executor",
    "ExecutorKind",
    "GardenGraphSnapshot",
    "GardenHealthDaily",
    "GoogleCalendarStatus",
    "HandoffStatus",
    "Identity",
    "IdentityKind",
    "IndexScope",
    "Invoice",
    "InvoiceCounter",
    "InvoiceKind",
    "InvoiceLine",
    "InvoiceNotification",
    "InvoiceState",
    "IssuerApiKey",
    "IssuerKeyRateLimit",
    "IssuerProfile",
    "KgEdge",
    "KgEntity",
    "LLMProviderKind",
    "LedgerEntryKind",
    "Membership",
    "MemoryBlob",
    "Necessity",
    "Note",
    "NoteCoactivity",
    "NoteEdgeUsage",
    "NoteKind",
    "NoteMaturity",
    "NoteNoteLink",
    "NotePart",
    "NotePartIndexPointer",
    "NotePartTrash",
    "NotePartUIState",
    "NoteStatus",
    "NoteTag",
    "NoteTaskLink",
    "NoteTurn",
    "Notification",
    "NotificationChannelKind",
    "NotificationPref",
    "NotificationStatus",
    "OrgEmbedderProvider",
    "OrgLLMProvider",
    "Organization",
    "PasswordResetToken",
    "PaymentConnector",
    "PaymentConnectorEvent",
    "PaymentConnectorRefusalBucket",
    "PaymentCustomerLink",
    "PaymentObjectLink",
    "PaymentStatus",
    "PaymentWebhookDelivery",
    "PrecomputedSuggestion",
    "ProjectProfile",
    "PushSubscription",
    "RateCard",
    "RateUnit",
    "ReceivedInvoice",
    "ReceivedInvoiceNotification",
    "RecurrenceFreq",
    "RefreshToken",
    "RevokedToken",
    "Role",
    "Schedule",
    "ScheduleMode",
    "SdiMandate",
    "SdiMandateStatus",
    "SdiStatus",
    "SdiTransmissionCounter",
    "SearchClick",
    "StorageKind",
    "StorageRate",
    "SystemSettings",
    "Tag",
    "TagKind",
    "TagScope",
    "Task",
    "TaskChecklistItem",
    "TaskCollaborator",
    "TaskDependency",
    "TaskHandoff",
    "TaskIndexPointer",
    "TaskParticipant",
    "TaskRecurrence",
    "TaskRelation",
    "TaskTag",
    "TelegramLink",
    "TelegramLinkCode",
    "TelegramUpdate",
    "Tier",
    "TimeEntry",
    "TimeSource",
    "TurnRole",
    "UsageRecord",
    "User",
    "UserCalendar",
    "Wallet",
    "WebhookDelivery",
    "WebhookEndpoint",
    "WorkflowDefinition",
    "WorkflowState",
    "WorkflowTransition",
    "WorkingCalendar",
]
