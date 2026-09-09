"""Message catalog (i18n-ready).

Project rule (docs/adr/0017): no hardcoded user-facing strings. Domain
errors carry a stable machine ``MessageCode`` plus parameters; adapters
render the human text via this catalog for the requested locale.

``en`` is the reference table and must cover every ``MessageCode``:
``render`` falls back to it, and a hole there surfaces to the user as
the bare code. Other locales are additive and may stay partial (``it``
covers only the notification strings the backend actually sends), they
inherit the English template for anything they omit. Both properties
are asserted in core/tests/test_i18n_catalog.py.
"""

from __future__ import annotations

import enum
from typing import Any

DEFAULT_LOCALE = "en"


class MessageCode(enum.StrEnum):
    AUTH_INVALID_CREDENTIALS = "auth.invalid_credentials"
    AUTH_MISSING_BEARER = "auth.missing_bearer"
    AUTH_TOKEN_INVALID = "auth.token_invalid"  # noqa: S105 (message code, not a secret)
    AUTH_TOKEN_NO_SUB = "auth.token_no_sub"  # noqa: S105 (message code, not a secret)
    AUTH_WORKSPACE_REQUIRED = "auth.workspace_required"
    AUTH_EMAIL_ALREADY_REGISTERED = "auth.email_already_registered"
    AUTH_EMAIL_NOT_VERIFIED = "auth.email_not_verified"
    AUTH_ACCOUNT_LOCKED = "auth.account_locked"
    AUTH_MFA_REQUIRED = "auth.mfa_required"
    AUTH_MFA_ENROLL_REQUIRED = "auth.mfa_enroll_required"
    AUTH_MFA_ALREADY_ENABLED = "auth.mfa_already_enabled"
    AUTH_MFA_NOT_ENABLED = "auth.mfa_not_enabled"
    AUTH_MFA_SETUP_REQUIRED = "auth.mfa_setup_required"
    AUTH_INVALID_TOTP = "auth.invalid_totp"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"  # noqa: S105 (message code, not a secret)
    AUTH_RESET_TOKEN_INVALID = "auth.reset_token_invalid"  # noqa: S105 (code, not a secret)
    AUTH_VERIFICATION_TOKEN_INVALID = "auth.verification_token_invalid"  # noqa: S105 (code, not a secret)
    AUTH_SIGNUP_DISABLED = "auth.signup_disabled"
    RBAC_NO_MEMBERSHIP = "rbac.no_membership"
    RBAC_ROLE_INSUFFICIENT = "rbac.role_insufficient"
    MEMBER_NOT_FOUND = "member.not_found"
    MEMBER_LAST_OWNER = "member.last_owner"
    MEMBER_ROLE_INVALID = "member.role_invalid"
    ADMIN_REQUIRED = "admin.required"
    ADMIN_SELF_GUARD = "admin.self_guard"
    USER_NOT_FOUND = "user.not_found"
    ORG_NOT_FOUND = "org.not_found"
    CONFLICT_STALE_VERSION = "concurrency.stale_version"
    TASK_NOT_FOUND = "task.not_found"
    CHECKLIST_ITEM_NOT_FOUND = "task.checklist_item.not_found"
    CHECKLIST_ITEM_TEXT_EMPTY = "task.checklist_item.text_empty"
    CHECKLIST_REORDER_MISMATCH = "task.checklist.reorder_mismatch"
    TAG_NOT_FOUND = "tag.not_found"
    ADJUDICATION_NOT_FOUND = "adjudication.not_found"
    TAG_DUPLICATE = "tag.duplicate"
    TAG_AMBIGUOUS = "tag.ambiguous"
    TAG_KIND_MISMATCH = "tag.kind_mismatch"
    # Structural tag invariant (docs/adr/0003 + 0021), enforced by
    # services/tag_assignment.py: a task carries exactly one client and
    # one project tag, a note exactly one client and at most one
    # project, and a carried project's client wins over any other.
    TAG_MULTIPLE_CLIENTS = "tag.multiple_clients"
    TAG_MULTIPLE_PROJECTS = "tag.multiple_projects"
    TAG_CLIENT_PROJECT_MISMATCH = "tag.client_project_mismatch"
    TAG_STRUCTURAL_REQUIRED = "tag.structural_required"
    PROJECT_CLIENT_REQUIRED = "project.client_required"
    TAG_KIND_NOT_CREATABLE = "tag.kind_not_creatable"
    TAG_NOT_ARCHIVED = "tag.not_archived"
    TAG_DEFAULT_PROTECTED = "tag.default_protected"
    CLIENT_HAS_INVOICES = "tag.client_has_invoices"
    CHANNEL_NOT_TAG_CREATABLE = "channel.not_tag_creatable"
    CHANNEL_ADMIN_ONLY = "channel.admin_only"
    CHANNEL_KEY_IMMUTABLE = "channel.key_immutable"
    CHANNEL_SEEDED_UNDELETABLE = "channel.seeded_undeletable"
    CHANNEL_NOT_FOUND = "channel.not_found"
    WORKFLOW_NOT_FOUND = "workflow.not_found"
    WORKFLOW_INVALID = "workflow.invalid"
    WORKFLOW_IN_USE = "workflow.in_use"
    TRANSITION_NOT_ALLOWED = "workflow.transition_not_allowed"
    # Interchange document (docs/adr/0052). One code per rule, each
    # naming the offending row: an import that fails has to tell the
    # caller WHICH line to fix, whether the caller is the SPA or the CLI.
    WORKFLOW_DOC_KIND = "workflow.doc_kind"
    WORKFLOW_DOC_VERSION = "workflow.doc_version"
    WORKFLOW_DOC_NAME = "workflow.doc_name"
    WORKFLOW_DOC_NO_STATES = "workflow.doc_no_states"
    WORKFLOW_DOC_STATE_NAME = "workflow.doc_state_name"
    WORKFLOW_DOC_DUPLICATE_STATE = "workflow.doc_duplicate_state"
    WORKFLOW_DOC_INITIAL_COUNT = "workflow.doc_initial_count"
    WORKFLOW_DOC_UNKNOWN_STATE = "workflow.doc_unknown_state"
    WORKFLOW_DOC_DUPLICATE_TRANSITION = "workflow.doc_duplicate_transition"
    DEPENDENCY_CYCLE = "dependency.cycle"
    CALENDAR_NOT_FOUND = "calendar.not_found"
    EVENT_NOT_FOUND = "event.not_found"
    EVENT_OVERLAP = "event.overlap"
    TIMER_ALREADY_RUNNING = "time.timer_already_running"
    NO_RUNNING_TIMER = "time.no_running_timer"
    TIME_ENTRY_NOT_FOUND = "time_entry.not_found"
    TIME_ENTRY_INVALID = "time_entry.invalid"
    BUDGET_NOT_FOUND = "budget.not_found"
    EMAIL_ACCOUNT_NOT_FOUND = "email.account_not_found"
    EMAIL_ACCOUNT_DUPLICATE = "email.account_duplicate"
    EMAIL_MESSAGE_NOT_FOUND = "email.message_not_found"
    EMAIL_SYNC_FAILED = "email.sync_failed"
    EMAIL_DRAFT_NOT_FOUND = "email.draft_not_found"
    EMAIL_DRAFT_NOT_READY = "email.draft_not_ready"
    INSUFFICIENT_CREDITS = "billing.insufficient_credits"
    RATE_CARD_NOT_FOUND = "billing.rate_card_not_found"
    MEMORY_NOT_FOUND = "memory.not_found"
    MEMORY_DIM_MISMATCH = "memory.dim_mismatch"
    MEMORY_CROSS_SUBJECT = "memory.cross_subject"
    NOTE_NOT_FOUND = "note.not_found"
    NOTE_NOT_LINKED_TO_TASK = "note.not_linked_to_task"
    REVISION_NOT_FOUND = "revision.not_found"
    ATTACHMENT_NOT_FOUND = "attachment.not_found"
    ATTACHMENT_TOO_LARGE = "attachment.too_large"
    ATTACHMENT_STREAM_UNSUPPORTED = "attachment.stream_unsupported"
    BODY_LIMIT_EXCEEDED = "body.limit_exceeded"
    INTENT_UNRECOGNIZED = "intent.unrecognized"
    INVOICE_NOT_FOUND = "invoice.not_found"
    INVOICE_NOT_DRAFT = "invoice.not_draft"
    INVOICE_NOT_REJECTED = "invoice.not_rejected"
    INVOICE_TRANSMIT_IN_PROGRESS = "invoice.transmit_in_progress"
    INVOICE_TRANSMIT_UNCONFIRMED = "invoice.transmit_unconfirmed"
    INVOICE_TRANSMIT_ENV_CHANGED = "invoice.transmit_env_changed"
    CREDIT_NOTE_PARENT_INVALID = "invoice.credit_note_parent_invalid"
    INVOICE_INVALID = "invoice.invalid"
    # AltriDatiGestionali (FatturaPA 2.2.1.16) outside its XSD facets:
    # {detail} names the offending field + the limit it broke.
    INVOICE_ALTRI_DATI_INVALID = "invoice.altri_dati_invalid"
    FISCAL_PROFILE_REQUIRED = "invoice.fiscal_profile_required"
    ISSUER_PROFILE_IN_USE = "invoice.issuer_profile_in_use"
    ISSUER_PROFILE_SOLE_DEFAULT = "invoice.issuer_profile_sole_default"
    ISSUER_PROFILE_NOT_FOUND = "invoice.issuer_profile_not_found"
    ISSUER_API_KEY_NOT_FOUND = "issuer_api_key.not_found"
    ISSUER_API_KEY_NOT_REVOKED = "issuer_api_key.not_revoked"
    ISSUER_API_KEY_PERMISSION_INVALID = "issuer_api_key.permission_invalid"
    ISSUER_API_KEY_IP_ALLOWLIST_INVALID = "issuer_api_key.ip_allowlist_invalid"
    ISSUER_API_KEY_PERMISSION_DENIED = "issuer_api_key.permission_denied"
    INVOICE_BATCH_TOO_LARGE = "invoice.batch_too_large"
    WEBHOOK_ENDPOINT_NOT_FOUND = "webhook_endpoint.not_found"
    WEBHOOK_ENDPOINT_NOT_REVOKED = "webhook_endpoint.not_revoked"
    WEBHOOK_URL_INVALID = "webhook_endpoint.url_invalid"
    WEBHOOK_EVENT_TYPE_INVALID = "webhook_endpoint.event_type_invalid"
    PAYMENT_CONNECTOR_NOT_FOUND = "payment_connector.not_found"
    PAYMENT_CONNECTOR_NOT_REVOKED = "payment_connector.not_revoked"
    PAYMENT_CONNECTOR_NOT_DRY_RUN = "payment_connector.not_dry_run"
    INVOICE_DRY_RUN_NOT_SENDABLE = "invoice.dry_run_not_sendable"
    PAYMENT_CONNECTOR_SECRET_MISSING = "payment_connector.signing_secret_missing"  # noqa: S105 (message code, not a secret)
    PAYMENT_CONNECTOR_KEY_UNSUPPORTED = "payment_connector.ingress_key_unsupported"
    PAYMENT_CONNECTOR_ALREADY_EMITTED = "payment_connector.already_emitted"
    PAYMENT_CONNECTOR_PROVIDER_INVALID = "payment_connector.provider_invalid"
    PAYMENT_CONNECTOR_MODE_INVALID = "payment_connector.mode_invalid"
    PAYMENT_CONNECTOR_EVENT_INVALID = "payment_connector.emission_event_invalid"
    PAYMENT_CONNECTOR_REFUND_EVENT_INVALID = "payment_connector.refund_event_invalid"
    PAYMENT_CONNECTOR_VAT_PRICING_INVALID = "payment_connector.vat_pricing_invalid"
    PAYMENT_CONNECTOR_PAYMENT_PAIR_INVALID = "payment_connector.payment_pair_invalid"
    PAYMENT_CONNECTOR_NUMBERING_INVALID = "payment_connector.numbering_invalid"
    PAYMENT_CONNECTOR_SIGNATURE_INVALID = "payment_connector.signature_invalid"
    PAYMENT_CONNECTOR_SECRET_REQUIRED = "payment_connector.signing_secret_required"  # noqa: S105 (message code, not a secret)
    PAYMENT_CONNECTOR_DISABLED = "payment_connector.disabled"
    PAYMENT_CONNECTOR_PAYLOAD_INVALID = "payment_connector.payload_invalid"
    PAYMENT_CONNECTOR_CLIENT_INCOMPLETE = "payment_connector.client_incomplete"
    PAYMENT_CONNECTOR_EVENT_NOT_FOUND = "payment_connector.event_not_found"
    PAYMENT_CONNECTOR_EVENT_NOT_RETRYABLE = "payment_connector.event_not_retryable"
    PAYMENT_CONNECTOR_EVENT_NOT_RECOMPOSABLE = "payment_connector.event_not_recomposable"
    INVOICE_NUMBER_LABEL_INVALID = "invoice.number_label_invalid"
    SDI_INTERMEDIARY_CODE_INVALID = "sdi.intermediary_code_invalid"
    SDI_INTERMEDIARY_CODE_MISSING = "sdi.intermediary_code_missing"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency.key_required"
    IDEMPOTENCY_BODY_MISMATCH = "idempotency.body_mismatch"
    IDEMPOTENCY_IN_PROGRESS = "idempotency.in_progress"
    COMPOSE_RECIPIENT_INVALID = "invoice.compose_recipient_invalid"
    RATE_LIMITED = "rate.limited"
    MCP_SCOPE_DENIED = "mcp.scope_denied"
    AGENT_SCOPE_DENIED = "agent.scope_denied"
    AGENT_WORKSPACE_MISMATCH = "agent.workspace_mismatch"
    MANDATE_REQUIRED = "invoice.mandate_required"
    MANDATE_NOT_FOUND = "invoice.mandate_not_found"
    NOTIFICATION_NOT_FOUND = "notification.not_found"
    USER_TIMEZONE_INVALID = "user.timezone_invalid"
    USER_DAY_START_INVALID = "user.day_start_invalid"
    EXECUTOR_NOT_FOUND = "executor.not_found"
    EXECUTOR_INVALID = "executor.invalid"
    AGENT_RUN_NOT_FOUND = "agent_run.not_found"
    AGENT_RUN_NOT_DISPATCHABLE = "agent_run.not_dispatchable"
    AGENT_RUN_ASSIGNEE_INACTIVE = "agent_run.assignee_inactive"
    AGENT_RUN_ALREADY_ACTIVE = "agent_run.already_active"
    AGENT_RUN_TERMINAL = "agent_run.terminal"
    AGENT_RUN_NO_PROVIDER = "agent_run.no_provider"
    HANDOFF_NOT_FOUND = "handoff.not_found"
    DISPATCH_NOT_FOUND = "dispatch.not_found"
    DISPATCH_NOT_PENDING = "dispatch.not_pending"
    DISPATCH_ALREADY_DECIDED = "dispatch.already_decided"
    AUTONOMOUS_DISABLED = "dispatch.autonomous_disabled"
    AUTONOMOUS_POLICY_INVALID = "dispatch.autonomous_policy_invalid"
    TASK_NOT_OFFERED = "task.not_offered"
    TASK_ALREADY_CLAIMED = "task.already_claimed"
    TASK_OFFER_NO_RECIPIENTS = "task.offer_no_recipients"
    TASK_OWNER_NOT_MEMBER = "task.owner_not_member"
    RECURRENCE_WITH_DEPS = "recurrence.with_dependencies"
    WORKSPACE_NOT_OWNER = "workspace.not_owner"
    WORKSPACE_SOLE = "workspace.sole"
    OAUTH_NOT_CONFIGURED = "oauth.not_configured"
    OAUTH_STATE_INVALID = "oauth.state_invalid"
    OAUTH_EXCHANGE_FAILED = "oauth.exchange_failed"
    OAUTH_SCOPE_INVALID = "oauth.scope_invalid"
    OAUTH_REFRESH_FAILED = "oauth.refresh_failed"
    GOOGLE_CALENDAR_SUBSCRIPTION_NOT_FOUND = "google_calendar.subscription_not_found"
    GOOGLE_CALENDAR_API_ERROR = "google_calendar.api_error"
    TELEGRAM_NOT_CONFIGURED = "telegram.not_configured"
    TELEGRAM_WEBHOOK_FORBIDDEN = "telegram.webhook_forbidden"
    # Bot webhook replies (ADR-0026 P4 / ADR-0017): Mycelium-generated bot
    # feedback goes through the catalog. The assistant's own generated
    # answer is passthrough (not catalogable).
    TELEGRAM_HELP = "telegram.help"
    TELEGRAM_FREETEXT_HINT = "telegram.freetext_hint"
    TELEGRAM_START_WELCOME = "telegram.start_welcome"
    TELEGRAM_CODE_INVALID = "telegram.code_invalid"
    TELEGRAM_LINKED = "telegram.linked"
    TELEGRAM_NOT_LINKED = "telegram.not_linked"
    TELEGRAM_NO_WORKSPACE = "telegram.no_workspace"
    TELEGRAM_VOICE_FAILED = "telegram.voice_failed"
    TELEGRAM_VOICE_SAVED = "telegram.voice_saved"
    TELEGRAM_VOICE_SAVED_NO_TRANSCRIPT = "telegram.voice_saved_no_transcript"
    TELEGRAM_EMPTY_IGNORED = "telegram.empty_ignored"
    TELEGRAM_TASK_CREATED = "telegram.task_created"
    TELEGRAM_NOTE_SAVED = "telegram.note_saved"
    TELEGRAM_NOTE_USAGE = "telegram.note_usage"
    TELEGRAM_UNKNOWN_COMMAND = "telegram.unknown_command"
    TELEGRAM_ASSISTANT_UNAVAILABLE = "telegram.assistant_unavailable"
    TELEGRAM_ASSISTANT_TIMEOUT = "telegram.assistant_timeout"
    TELEGRAM_ASSISTANT_ERROR = "telegram.assistant_error"
    TELEGRAM_ASSISTANT_BUDGET = "telegram.assistant_budget"
    AGENT_TOKEN_NOT_FOUND = "agent_token.not_found"  # noqa: S105 (message code, not a secret)
    AGENT_TOKEN_INVALID = "agent_token.invalid"  # noqa: S105 (message code, not a secret)
    CAPABILITY_TOKEN_INVALID = "capability_token.invalid"  # noqa: S105 (message code, not a secret)
    CAPABILITY_TOKEN_SCOPE = "capability_token.scope"  # noqa: S105 (message code, not a secret)
    AI_ASSISTANT_NOT_FOUND = "ai_assistant.not_found"
    AI_ASSISTANT_INVALID_SCOPE = "ai_assistant.invalid_scope"
    AI_ASSISTANT_BINDING_TOO_WIDE = "ai_assistant.binding_too_wide"
    NOTE_MATURITY_INVALID = "note.maturity_invalid"
    NOTE_PROMOTED_READONLY = "note.promoted_readonly"
    NOTE_PROTECTED = "note.protected"
    NOTE_LINK_KIND_INVALID = "note.link.kind_invalid"
    NOTE_LINK_SELF = "note.link.self"
    NOTE_TASK_LINK_KIND_INVALID = "note.task_link.kind_invalid"
    NOTE_TASK_LINK_ANCHOR_REQUIRED = "note.task_link.anchor_required"
    NOTE_TASK_LINK_PROMOTED_IMMUTABLE = "note.task_link.promoted_immutable"
    NOTE_PART_ANCHOR_REQUIRED = "note.part.anchor_required"
    NOTE_PART_NOT_TRASHED = "note.part.not_trashed"
    NOTE_BODY_MULTIPART = "note.body.multipart"
    GARDEN_SUGGESTION_TYPE_INVALID = "garden.suggestion_type_invalid"
    GARDEN_ACTION_INVALID = "garden.action_invalid"
    EVENT_QUOTA_EXCEEDED = "event.quota_exceeded"
    EVENT_NODE_NOT_INERT = "event.node_not_inert"
    IDENTITY_HANDLE_REQUIRED = "identity.handle_required"
    IDENTITY_NOT_FOUND = "identity.not_found"
    IDENTITY_INACTIVE = "identity.inactive"
    IDENTITY_NOT_MEMBER = "identity.not_member"
    ANNOTATION_NOT_FOUND = "annotation.not_found"
    ANNOTATION_FORBIDDEN = "annotation.forbidden"
    ANNOTATION_DELETED = "annotation.deleted"
    ANNOTATION_DOC_KIND_INVALID = "annotation.doc_kind_invalid"
    ANNOTATION_NOT_SUGGESTION = "annotation.not_suggestion"
    SUGGESTION_NOT_PENDING = "annotation.suggestion_not_pending"
    SUGGESTION_STALE = "annotation.suggestion_stale"
    SUGGESTION_TEXT_REQUIRED = "annotation.suggestion_text_required"
    ANNOTATION_BODY_REQUIRED = "annotation.body_required"
    BODY_INVALID_ENCODING = "body.invalid_encoding"
    # Unified-diff patch apply (services/text_patch.py). STALE = the base
    # drifted (409, re-download body/raw and rebuild the diff);
    # DOES_NOT_APPLY / MALFORMED = the diff itself is wrong against an
    # agreed base (422, re-downloading will not help).
    PATCH_STALE = "patch.stale"
    PATCH_DOES_NOT_APPLY = "patch.does_not_apply"
    PATCH_MALFORMED = "patch.malformed"
    DOMAIN_ERROR = "domain.error"
    FIELD_NOT_NULLABLE = "field.not_nullable"
    PROVIDER_KEY_INVALID = "provider.key_invalid"
    # Worker-generated reminder text (localised per recipient, see
    # services.notifications.scan_reminders). ``{when}`` is the due
    # date/datetime, ``{offset}`` the humanised lead time.
    REMINDER_TITLE = "reminder.title"
    REMINDER_DUE = "reminder.due"
    REMINDER_DUE_BEFORE = "reminder.due_before"
    # Humanised durations for the reminder lead time (``{n}`` count).
    DURATION_MIN = "duration.min"
    DURATION_HOUR = "duration.hour"
    DURATION_HOURS = "duration.hours"
    DURATION_DAY = "duration.day"
    DURATION_DAYS = "duration.days"


# locale -> code -> template. Templates use str.format named params.
_CATALOG: dict[str, dict[MessageCode, str]] = {
    "en": {
        MessageCode.AUTH_INVALID_CREDENTIALS: "Invalid credentials",
        MessageCode.AUTH_MISSING_BEARER: "Missing Authorization: Bearer header",
        MessageCode.AUTH_TOKEN_INVALID: "Invalid or expired token",
        MessageCode.AUTH_TOKEN_NO_SUB: "Token without subject",
        MessageCode.AUTH_WORKSPACE_REQUIRED: "X-Workspace-Id header is required",
        MessageCode.AUTH_EMAIL_ALREADY_REGISTERED: "Email already registered",
        MessageCode.AUTH_EMAIL_NOT_VERIFIED: "Email not verified",
        MessageCode.AUTH_ACCOUNT_LOCKED: (
            "Account temporarily locked due to repeated failed logins"
        ),
        MessageCode.AUTH_MFA_REQUIRED: "Multi-factor authentication required",
        MessageCode.AUTH_MFA_ENROLL_REQUIRED: "Multi-factor enrolment required",
        MessageCode.AUTH_MFA_ALREADY_ENABLED: "MFA is already enabled",
        MessageCode.AUTH_MFA_NOT_ENABLED: "MFA is not enabled",
        MessageCode.AUTH_MFA_SETUP_REQUIRED: "MFA setup is required first",
        MessageCode.AUTH_INVALID_TOTP: "Invalid authentication code",
        MessageCode.AUTH_TOKEN_REVOKED: "Token has been revoked",
        MessageCode.AUTH_RESET_TOKEN_INVALID: "Invalid or expired reset token",
        MessageCode.AUTH_VERIFICATION_TOKEN_INVALID: "Invalid or expired token",
        MessageCode.AUTH_SIGNUP_DISABLED: "Public sign-up is disabled on this instance",
        MessageCode.RBAC_NO_MEMBERSHIP: "Not a member of this workspace",
        MessageCode.RBAC_ROLE_INSUFFICIENT: (
            "Role {current} is insufficient, requires >= {minimum}"
        ),
        MessageCode.MEMBER_NOT_FOUND: "No user with that email",
        MessageCode.MEMBER_LAST_OWNER: ("Cannot remove or demote the only owner"),
        MessageCode.MEMBER_ROLE_INVALID: "Invalid role",
        MessageCode.ADMIN_REQUIRED: "Administrator privileges required",
        MessageCode.ADMIN_SELF_GUARD: (
            "You cannot remove your own admin role or deactivate "
            "yourself; ask another administrator"
        ),
        MessageCode.USER_NOT_FOUND: "User not found",
        MessageCode.ORG_NOT_FOUND: "Workspace not found",
        MessageCode.CONFLICT_STALE_VERSION: "Stale version write",
        MessageCode.TASK_NOT_FOUND: "Task not found",
        MessageCode.CHECKLIST_ITEM_NOT_FOUND: "Checklist item not found",
        MessageCode.CHECKLIST_ITEM_TEXT_EMPTY: "Checklist item text must not be empty",
        MessageCode.CHECKLIST_REORDER_MISMATCH: (
            "Reorder payload does not match the task's checklist items"
        ),
        MessageCode.TAG_NOT_FOUND: "Tag not found",
        # Raised by services/adjudication.get_adjudication with no
        # params, so the template stays placeholder-free; the entry
        # sits here to mirror the enum's (historical) ordering.
        MessageCode.ADJUDICATION_NOT_FOUND: "Adjudication not found",
        MessageCode.TAG_DUPLICATE: "A tag with this name already exists",
        MessageCode.TAG_AMBIGUOUS: "Ambiguous tag name: {name}",
        MessageCode.TAG_KIND_MISMATCH: "Tag is not of the expected kind",
        MessageCode.TAG_MULTIPLE_CLIENTS: (
            "Only one client tag is allowed: pass a single client, or none "
            "and let the project decide it"
        ),
        MessageCode.TAG_MULTIPLE_PROJECTS: (
            "Only one project tag is allowed: pass a single project"
        ),
        MessageCode.TAG_CLIENT_PROJECT_MISMATCH: (
            "The client tag does not match the project's client: attach the "
            "project alone (its client follows automatically), or pick a "
            "project that belongs to this client"
        ),
        MessageCode.TAG_STRUCTURAL_REQUIRED: (
            "The client and project tags cannot be detached: attach another "
            "project to move this item instead (only a note may drop its "
            "project, which makes it personal again)"
        ),
        MessageCode.PROJECT_CLIENT_REQUIRED: (
            "Every project belongs to a client: provide client_tag_id"
        ),
        MessageCode.TAG_KIND_NOT_CREATABLE: (
            "Clients and projects are not created as plain tags: use "
            "/clients and /projects, which also create their profile"
        ),
        MessageCode.TAG_NOT_ARCHIVED: ("Archive the {kind} before deleting it permanently"),
        MessageCode.TAG_DEFAULT_PROTECTED: ("The default {kind} cannot be deleted"),
        MessageCode.CLIENT_HAS_INVOICES: (
            "Client has {count} invoice(s); void or delete them first"
        ),
        MessageCode.CHANNEL_NOT_TAG_CREATABLE: (
            "Memory channels are managed in settings, not created as tags"
        ),
        MessageCode.CHANNEL_ADMIN_ONLY: (
            "Only a platform administrator can manage memory channels"
        ),
        MessageCode.CHANNEL_KEY_IMMUTABLE: (
            "The system key of a seeded memory channel cannot be changed"
        ),
        MessageCode.CHANNEL_SEEDED_UNDELETABLE: (
            "A seeded memory channel cannot be deleted; disable it instead"
        ),
        MessageCode.CHANNEL_NOT_FOUND: "Memory channel not found",
        MessageCode.WORKFLOW_NOT_FOUND: "Workflow not found",
        MessageCode.WORKFLOW_INVALID: ("Invalid workflow: exactly one initial state is required"),
        MessageCode.WORKFLOW_IN_USE: (
            "Workflow in use or default: reassign its tasks / pick another default first"
        ),
        MessageCode.TRANSITION_NOT_ALLOWED: ("Transition not allowed by the workflow"),
        MessageCode.WORKFLOW_DOC_KIND: (
            "Not a Mycelium workflow document: expected kind {expected}, got {got}"
        ),
        MessageCode.WORKFLOW_DOC_VERSION: (
            "Workflow document version {version} is newer than this server understands "
            "(supported: {supported})"
        ),
        MessageCode.WORKFLOW_DOC_NAME: (
            "The workflow name is empty or longer than {maximum} characters"
        ),
        MessageCode.WORKFLOW_DOC_NO_STATES: "The workflow document declares no states",
        MessageCode.WORKFLOW_DOC_STATE_NAME: (
            "State {position}: the name is empty or longer than {maximum} characters"
        ),
        MessageCode.WORKFLOW_DOC_DUPLICATE_STATE: (
            "Two states are named {name}: state names are unique within a workflow"
        ),
        MessageCode.WORKFLOW_DOC_INITIAL_COUNT: (
            "Exactly one initial state is required, the document has {found}"
        ),
        MessageCode.WORKFLOW_DOC_UNKNOWN_STATE: (
            "Transition {position} points at {name}, which is not one of the states"
        ),
        MessageCode.WORKFLOW_DOC_DUPLICATE_TRANSITION: (
            "The transition {from_state} -> {to_state} appears twice"
        ),
        MessageCode.DEPENDENCY_CYCLE: ("This dependency would create a cycle"),
        MessageCode.CALENDAR_NOT_FOUND: "Working calendar not found",
        MessageCode.EVENT_NOT_FOUND: "Event not found",
        MessageCode.EVENT_OVERLAP: ("Overlapping appointment for a participant (no ubiquity)"),
        MessageCode.TIMER_ALREADY_RUNNING: ("A timer is already running for this user"),
        MessageCode.NO_RUNNING_TIMER: "No running timer for this user",
        MessageCode.TIME_ENTRY_NOT_FOUND: "Time entry not found",
        MessageCode.TIME_ENTRY_INVALID: ("Invalid time entry: provide a positive interval"),
        MessageCode.BUDGET_NOT_FOUND: "Budget not found",
        MessageCode.EMAIL_ACCOUNT_NOT_FOUND: "Email account not found",
        MessageCode.EMAIL_ACCOUNT_DUPLICATE: ("An email account with this address already exists"),
        MessageCode.EMAIL_MESSAGE_NOT_FOUND: "Email message not found",
        MessageCode.EMAIL_SYNC_FAILED: "Email sync failed: {detail}",
        MessageCode.EMAIL_DRAFT_NOT_FOUND: "Email draft not found",
        MessageCode.EMAIL_DRAFT_NOT_READY: "Email draft is not ready to send",
        MessageCode.INSUFFICIENT_CREDITS: ("Insufficient credits: need {needed}, have {balance}"),
        MessageCode.RATE_CARD_NOT_FOUND: ("No active rate card for model {model_id}"),
        MessageCode.MEMORY_NOT_FOUND: "Memory blob not found",
        MessageCode.MEMORY_DIM_MISMATCH: ("Embedding dimension mismatch: expected {expected}"),
        MessageCode.MEMORY_CROSS_SUBJECT: (
            "Consolidation cannot cross org/project (hard isolation)"
        ),
        MessageCode.NOTE_NOT_FOUND: "Note not found",
        MessageCode.NOTE_NOT_LINKED_TO_TASK: ("Link a task to the note before billing time"),
        MessageCode.REVISION_NOT_FOUND: "Revision not found",
        MessageCode.ATTACHMENT_NOT_FOUND: "Attachment not found",
        MessageCode.ATTACHMENT_TOO_LARGE: "Attachment exceeds the maximum size",
        MessageCode.ATTACHMENT_STREAM_UNSUPPORTED: (
            "Streaming upload requires the s3 attachment backend"
        ),
        MessageCode.BODY_LIMIT_EXCEEDED: (
            "Append would exceed the maximum body size ({max_bytes} bytes)"
        ),
        MessageCode.BODY_INVALID_ENCODING: "Request body is not valid UTF-8 text",
        MessageCode.PATCH_STALE: (
            "The document changed since you downloaded it; "
            "re-download body/raw and rebuild the diff."
        ),
        MessageCode.PATCH_DOES_NOT_APPLY: ("The patch does not apply to the current document."),
        MessageCode.PATCH_MALFORMED: "The patch is not a valid unified diff.",
        MessageCode.ANNOTATION_BODY_REQUIRED: "A comment needs a non-empty body",
        MessageCode.INTENT_UNRECOGNIZED: ("Command not recognized: {raw}"),
        MessageCode.INVOICE_NOT_FOUND: "Invoice not found",
        MessageCode.INVOICE_NOT_DRAFT: (
            "Invoice is emitted and immutable (only draft is editable)"
        ),
        MessageCode.INVOICE_NOT_REJECTED: (
            "Only a rejected (SdI scarto) invoice can be reopened for correction"
        ),
        MessageCode.INVOICE_TRANSMIT_IN_PROGRESS: (
            "A transmission of this invoice is already in progress; retry after it settles"
        ),
        MessageCode.INVOICE_TRANSMIT_UNCONFIRMED: (
            "The SdI dispatch outcome is unknown (lost acknowledgement); the invoice is "
            "held as transmitted and a retry will re-send the SAME file, which SdI "
            "deduplicates by file name"
        ),
        MessageCode.INVOICE_TRANSMIT_ENV_CHANGED: (
            "The SdI environment changed since this invoice's unsettled dispatch "
            "({detail}); the file-name dedupe safety net does not cross environments, "
            "so the retry is refused"
        ),
        MessageCode.CREDIT_NOTE_PARENT_INVALID: (
            "A credit note requires an emitted invoice; a draft is not yet issued "
            "and a scartato one is corrected by resend, not a credit note"
        ),
        MessageCode.INVOICE_INVALID: ("Invalid invoice: {detail}"),
        MessageCode.INVOICE_ALTRI_DATI_INVALID: ("Invalid AltriDatiGestionali block ({detail})"),
        MessageCode.FISCAL_PROFILE_REQUIRED: (
            "The issuer profile is missing or incomplete: {detail}"
        ),
        MessageCode.ISSUER_PROFILE_IN_USE: (
            "This issuer profile is used by one or more invoices and cannot be deleted"
        ),
        MessageCode.ISSUER_PROFILE_SOLE_DEFAULT: (
            "Set another profile as default before deleting this one"
        ),
        MessageCode.ISSUER_PROFILE_NOT_FOUND: "Issuer profile not found",
        MessageCode.ISSUER_API_KEY_NOT_FOUND: "Issuer API key not found",
        MessageCode.ISSUER_API_KEY_NOT_REVOKED: (
            "Revoke the key before deleting it (only a revoked key can be purged)"
        ),
        MessageCode.ISSUER_API_KEY_PERMISSION_INVALID: (
            "One or more requested permissions are not valid for an issuer API key"
        ),
        MessageCode.ISSUER_API_KEY_PERMISSION_DENIED: (
            "The API key does not carry the permission required for this operation"
        ),
        MessageCode.ISSUER_API_KEY_IP_ALLOWLIST_INVALID: ("Invalid IP allowlist entry: {detail}"),
        MessageCode.INVOICE_BATCH_TOO_LARGE: "Too many items in the batch: {detail}",
        MessageCode.WEBHOOK_ENDPOINT_NOT_FOUND: "Webhook endpoint not found",
        MessageCode.WEBHOOK_ENDPOINT_NOT_REVOKED: (
            "Revoke the endpoint before deleting it (only a revoked endpoint can be purged)"
        ),
        MessageCode.WEBHOOK_URL_INVALID: "Invalid webhook URL: {detail}",
        MessageCode.WEBHOOK_EVENT_TYPE_INVALID: "Unknown webhook event type: {detail}",
        MessageCode.PAYMENT_CONNECTOR_NOT_FOUND: "Payment connector not found",
        MessageCode.PAYMENT_CONNECTOR_KEY_UNSUPPORTED: (
            "Provider {detail} cannot send a custom header, so an ingress key would "
            "only make every delivery be refused. The signature is the authority here"
        ),
        MessageCode.PAYMENT_CONNECTOR_SECRET_MISSING: (
            "This connector has no signing secret yet, so it could not verify a single "
            "delivery. Register the webhook URL at {detail} and paste the secret it "
            "shows, then enable it"
        ),
        MessageCode.INVOICE_DRY_RUN_NOT_SENDABLE: (
            "This document was composed in shadow mode and is not sendable. Promote it "
            "first if it really has to be filed"
        ),
        MessageCode.PAYMENT_CONNECTOR_NOT_DRY_RUN: (
            "This document was not composed in shadow mode, so there is nothing to promote"
        ),
        MessageCode.PAYMENT_CONNECTOR_ALREADY_EMITTED: (
            "A real document already covers this payment ({detail}); promoting the shadow "
            "would invoice it twice"
        ),
        MessageCode.PAYMENT_CONNECTOR_NOT_REVOKED: (
            "Revoke the connector before deleting it (only a revoked connector can be purged)"
        ),
        MessageCode.PAYMENT_CONNECTOR_PROVIDER_INVALID: "Unknown payment provider: {detail}",
        MessageCode.PAYMENT_CONNECTOR_MODE_INVALID: "Unknown automation mode: {detail}",
        MessageCode.PAYMENT_CONNECTOR_EVENT_INVALID: "Unknown emission event: {detail}",
        MessageCode.PAYMENT_CONNECTOR_VAT_PRICING_INVALID: "Unknown VAT pricing: {detail}",
        MessageCode.PAYMENT_CONNECTOR_NUMBERING_INVALID: ("Unknown numbering mode: {detail}"),
        MessageCode.PAYMENT_CONNECTOR_PAYMENT_PAIR_INVALID: (
            "Payment terms and payment method must be set together on a connector, "
            "or the document states a payment method nobody chose: {detail}"
        ),
        MessageCode.PAYMENT_CONNECTOR_REFUND_EVENT_INVALID: "Unknown refund event: {detail}",
        MessageCode.PAYMENT_CONNECTOR_SIGNATURE_INVALID: "Invalid or expired webhook signature",
        MessageCode.PAYMENT_CONNECTOR_SECRET_REQUIRED: (
            "Provider {detail} issues its own signing secret; paste it here rather than "
            "having one generated"
        ),
        MessageCode.PAYMENT_CONNECTOR_DISABLED: "This payment connector is disabled",
        MessageCode.PAYMENT_CONNECTOR_PAYLOAD_INVALID: "Malformed webhook payload: {detail}",
        MessageCode.PAYMENT_CONNECTOR_CLIENT_INCOMPLETE: (
            "This client cannot be invoiced yet, it is missing: {detail}"
        ),
        MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_FOUND: "Payment connector event not found",
        MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_RETRYABLE: (
            "Only a parked or dead event can be retried"
        ),
        MessageCode.INVOICE_NUMBER_LABEL_INVALID: (
            "The provider's invoice number cannot be used as it stands: {detail}"
        ),
        MessageCode.SDI_INTERMEDIARY_CODE_INVALID: (
            "Not an Italian fiscal code: FatturaPA wants the trasmittente's codice "
            "fiscale, 11 digits for a company or 16 characters for a person ({detail})"
        ),
        MessageCode.SDI_INTERMEDIARY_CODE_MISSING: (
            "The accredited channel has no fiscal code configured; set it in Settings "
            "before transmitting through it"
        ),
        MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_RECOMPOSABLE: (
            "This event cannot be recomposed: its document must still be an untouched "
            "draft (no number and no file name allocated), the event must not be in "
            "flight, and no other event may claim the same payment"
        ),
        MessageCode.IDEMPOTENCY_KEY_REQUIRED: "The Idempotency-Key header is required",
        MessageCode.IDEMPOTENCY_BODY_MISMATCH: (
            "This Idempotency-Key was already used with a different request"
        ),
        MessageCode.IDEMPOTENCY_IN_PROGRESS: (
            "A request with this Idempotency-Key is still in progress"
        ),
        MessageCode.COMPOSE_RECIPIENT_INVALID: (
            "Provide exactly one of client_tag_id or an inline client"
        ),
        MessageCode.RATE_LIMITED: "Rate limit exceeded for this API key; retry later",
        MessageCode.MCP_SCOPE_DENIED: "This assistant's scope does not permit this tool",
        MessageCode.AGENT_SCOPE_DENIED: "This assistant's scope does not permit this request",
        MessageCode.AGENT_WORKSPACE_MISMATCH: ("This credential belongs to another workspace"),
        MessageCode.MANDATE_REQUIRED: (
            "No active SdI transmission mandate for this issuer profile; grant one "
            "before transmitting through the accredited channel"
        ),
        MessageCode.MANDATE_NOT_FOUND: "No active SdI transmission mandate to revoke",
        MessageCode.NOTIFICATION_NOT_FOUND: "Notification not found",
        MessageCode.USER_TIMEZONE_INVALID: "Invalid timezone: {detail}",
        MessageCode.USER_DAY_START_INVALID: (
            "Invalid day start: must be 0..1439 minutes after midnight (got {detail})"
        ),
        MessageCode.EXECUTOR_NOT_FOUND: "Executor not found",
        MessageCode.EXECUTOR_INVALID: "Invalid executor: {detail}",
        MessageCode.AGENT_RUN_NOT_FOUND: "Agent run not found",
        MessageCode.AGENT_RUN_ASSIGNEE_INACTIVE: (
            "@{handle} can no longer act, so this task cannot be run. "
            "Reassign it, or reactivate the assistant."
        ),
        MessageCode.AGENT_RUN_NOT_DISPATCHABLE: (
            "Task is not dispatchable to an agent: it must be an llm_agent "
            "task with an assigned, dispatchable executor"
        ),
        MessageCode.AGENT_RUN_ALREADY_ACTIVE: ("An agent run for this task is already active"),
        MessageCode.AGENT_RUN_TERMINAL: ("Agent run has already finished and cannot be cancelled"),
        MessageCode.AGENT_RUN_NO_PROVIDER: (
            "No language model provider is configured for this workspace, "
            "so an agent run would fail on its first step. Configure one, "
            "or run this task yourself."
        ),
        MessageCode.HANDOFF_NOT_FOUND: "Handoff not found",
        MessageCode.DISPATCH_NOT_FOUND: "Dispatch request not found",
        MessageCode.DISPATCH_NOT_PENDING: ("Dispatch request is not pending (cannot approve)"),
        MessageCode.DISPATCH_ALREADY_DECIDED: ("Dispatch request has already been decided"),
        MessageCode.AUTONOMOUS_DISABLED: ("Autonomous dispatch is disabled for this workspace"),
        MessageCode.AUTONOMOUS_POLICY_INVALID: ("Invalid autonomous dispatch policy: {detail}"),
        MessageCode.TASK_NOT_OFFERED: ("Task is not offered (nothing to claim or decline)"),
        MessageCode.TASK_OFFER_NO_RECIPIENTS: (
            "Nobody in this workspace can take this task on: every member is "
            "deactivated. The task was not offered."
        ),
        MessageCode.TASK_OWNER_NOT_MEMBER: (
            "The owner of a task must be an active member of this workspace."
        ),
        MessageCode.TASK_ALREADY_CLAIMED: ("Task has already been claimed by a member"),
        MessageCode.RECURRENCE_WITH_DEPS: (
            "A recurring task cannot have dependencies (mutually exclusive in v1)"
        ),
        MessageCode.WORKSPACE_NOT_OWNER: "Only the workspace owner can do this",
        MessageCode.WORKSPACE_SOLE: (
            "Cannot delete your only workspace: create or join another first"
        ),
        MessageCode.OAUTH_NOT_CONFIGURED: ("Google OAuth is not configured on this server"),
        MessageCode.OAUTH_STATE_INVALID: "Invalid or expired OAuth state",
        MessageCode.OAUTH_EXCHANGE_FAILED: (
            "Failed to exchange the authorization code for tokens: {detail}"
        ),
        MessageCode.OAUTH_SCOPE_INVALID: (
            "Invalid OAuth scope: must be one of gmail, calendar, both"
        ),
        MessageCode.OAUTH_REFRESH_FAILED: ("Failed to refresh the Google access token: {detail}"),
        MessageCode.GOOGLE_CALENDAR_SUBSCRIPTION_NOT_FOUND: (
            "Google Calendar subscription not found"
        ),
        MessageCode.GOOGLE_CALENDAR_API_ERROR: ("Google Calendar API error: {detail}"),
        MessageCode.TELEGRAM_NOT_CONFIGURED: ("Telegram bot is not configured on this instance"),
        MessageCode.TELEGRAM_WEBHOOK_FORBIDDEN: "Forbidden webhook caller",
        MessageCode.TELEGRAM_HELP: (
            "Mycelium bot:\n"
            "• /note <text> → save a note\n"
            "• /task <title> → create a task\n"
            "• voice message → save a voice note (auto-transcribed)\n"
            "• voice + caption 'task: <title>' → save AND promote to task\n"
            "• /help → this message\n"
            "Send free text to chat with the Mycelium assistant."
        ),
        MessageCode.TELEGRAM_FREETEXT_HINT: (
            "Send /note <text> to save a note, /task <title> for a task, or a voice"
            " message for a voice note. /help for the list."
        ),
        MessageCode.TELEGRAM_START_WELCOME: (
            "Welcome to Mycelium. To link your account, open the deep-link from the Mycelium"
            " web app's Telegram settings."
        ),
        MessageCode.TELEGRAM_CODE_INVALID: (
            "That link code is invalid or has expired. "
            "Generate a new one from the Mycelium web app."
        ),
        MessageCode.TELEGRAM_LINKED: (
            "Your Telegram account is now linked to Mycelium. Send /help to see what I"
            " can do, or just tell me what you need."
        ),
        MessageCode.TELEGRAM_NOT_LINKED: (
            "Your Telegram chat is not linked to Mycelium. Open the Mycelium web app and use"
            " the Telegram settings to link."
        ),
        MessageCode.TELEGRAM_NO_WORKSPACE: (
            "Your Mycelium account has no workspace yet. Open the web app first."
        ),
        MessageCode.TELEGRAM_VOICE_FAILED: "Could not download voice message. Try again.",
        MessageCode.TELEGRAM_VOICE_SAVED: "Voice note saved.",
        MessageCode.TELEGRAM_VOICE_SAVED_NO_TRANSCRIPT: (
            "Voice note saved, but transcription is unavailable. "
            "You can still play it back from the note."
        ),
        MessageCode.TELEGRAM_EMPTY_IGNORED: (
            "Empty message ignored. Send /note <text> to save a note."
        ),
        MessageCode.TELEGRAM_TASK_CREATED: "Task created: {title}",
        MessageCode.TELEGRAM_NOTE_SAVED: "Note saved.",
        MessageCode.TELEGRAM_NOTE_USAGE: "Usage: /note <text>",
        MessageCode.TELEGRAM_UNKNOWN_COMMAND: "Unknown command {command}. {help}",
        MessageCode.TELEGRAM_ASSISTANT_UNAVAILABLE: (
            "The assistant is unavailable right now. Try again, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_TIMEOUT: (
            "I couldn't finish that in time. Try rephrasing, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_ERROR: (
            "Something went wrong handling that. Try again, or use /note and /task."
        ),
        MessageCode.TELEGRAM_ASSISTANT_BUDGET: (
            "Budget for this turn is exhausted. Try a simpler request."
        ),
        MessageCode.AGENT_TOKEN_NOT_FOUND: "Agent token not found",
        MessageCode.AI_ASSISTANT_NOT_FOUND: "AI assistant not found",
        MessageCode.AI_ASSISTANT_INVALID_SCOPE: ("Unknown scope key: {key}"),
        MessageCode.AI_ASSISTANT_BINDING_TOO_WIDE: (
            "A credential that reaches every workspace is offered only for the "
            "browser panel's scope. Remove {key} or bind it to one workspace."
        ),
        MessageCode.AGENT_TOKEN_INVALID: "Invalid or revoked agent token",
        MessageCode.CAPABILITY_TOKEN_INVALID: (
            "Invalid, expired, or already-used capability token"
        ),
        MessageCode.CAPABILITY_TOKEN_SCOPE: (
            "Capability token is not valid for this resource or action"
        ),
        MessageCode.NOTE_MATURITY_INVALID: "Invalid maturity '{maturity}'. Allowed: {valid}.",
        MessageCode.NOTE_PROMOTED_READONLY: (
            "This note was transplanted to a task and is read-only."
        ),
        MessageCode.NOTE_PROTECTED: (
            "This note is protected prose: the distiller never compacts it."
        ),
        MessageCode.NOTE_PART_NOT_TRASHED: (
            "No trashed note part with this id: it was never trashed, was already "
            "restored, or has been purged."
        ),
        MessageCode.NOTE_BODY_MULTIPART: (
            "This note has {parts} parts, and a flat body cannot say where they "
            "begin and end. Edit the part you mean (note_parts), or send back the "
            "body unchanged."
        ),
        MessageCode.NOTE_LINK_KIND_INVALID: "Invalid note link kind '{kind}'. Allowed: {valid}.",
        MessageCode.NOTE_LINK_SELF: "A note cannot be linked to itself.",
        MessageCode.GARDEN_SUGGESTION_TYPE_INVALID: (
            "Invalid suggestion type '{suggestion_type}'. Allowed: {valid}."
        ),
        MessageCode.GARDEN_ACTION_INVALID: "Invalid apply action '{action}'. Allowed: {valid}.",
        MessageCode.EVENT_QUOTA_EXCEEDED: (
            "Event quota exceeded for this actor: {limit} per {window}. Try again later."
        ),
        MessageCode.EVENT_NODE_NOT_INERT: (
            "Autonomous commit rejected: the target note is live (not inert); "
            "refusing to overwrite active work."
        ),
        MessageCode.NOTE_TASK_LINK_KIND_INVALID: (
            "Invalid note-task link kind '{kind}'. Allowed: {valid}."
        ),
        MessageCode.NOTE_TASK_LINK_ANCHOR_REQUIRED: "Provide one of note_id or task_id.",
        MessageCode.NOTE_TASK_LINK_PROMOTED_IMMUTABLE: (
            "A promoted_from link cannot be removed; promotion has no inverse."
        ),
        MessageCode.NOTE_PART_ANCHOR_REQUIRED: (
            "Provide note_id to create a new part, or part_id to append to an existing one."
        ),
        MessageCode.IDENTITY_HANDLE_REQUIRED: (
            "Cannot create an identity for a {kind} without a handle."
        ),
        MessageCode.IDENTITY_NOT_FOUND: "Identity not found",
        # Every raise site fills BOTH params. A template rendered with a
        # missing key falls back to the raw braces (see ``render``), which
        # is how a user once got literal "{current}" on screen.
        MessageCode.IDENTITY_INACTIVE: (
            "@{handle} is deactivated and cannot be assigned ({kind})."
        ),
        MessageCode.IDENTITY_NOT_MEMBER: (
            "@{handle} is no longer a member of this workspace ({kind})."
        ),
        MessageCode.ANNOTATION_NOT_FOUND: "Annotation not found",
        MessageCode.ANNOTATION_FORBIDDEN: (
            "Only the author or an admin can edit or delete this annotation"
        ),
        MessageCode.ANNOTATION_DELETED: "This annotation has been deleted",
        MessageCode.ANNOTATION_DOC_KIND_INVALID: (
            "Invalid document kind: expected one of note_part, task_description."
        ),
        MessageCode.ANNOTATION_NOT_SUGGESTION: "This action applies only to a suggestion",
        MessageCode.SUGGESTION_NOT_PENDING: "This suggestion is no longer pending",
        MessageCode.SUGGESTION_STALE: (
            "The text this suggestion targets has changed; "
            "it can no longer be applied automatically."
        ),
        MessageCode.SUGGESTION_TEXT_REQUIRED: ("A suggestion needs the original text it replaces."),
        MessageCode.DOMAIN_ERROR: "Domain error",
        MessageCode.FIELD_NOT_NULLABLE: (
            "'{field}' cannot be cleared: omit it to leave it unchanged."
        ),
        MessageCode.PROVIDER_KEY_INVALID: (
            "The provider API key could not be validated; check the key and model."
        ),
        MessageCode.REMINDER_TITLE: "Task due: {title}",
        MessageCode.REMINDER_DUE: "Due {when}",
        MessageCode.REMINDER_DUE_BEFORE: "Due {when} ({offset} before)",
        MessageCode.DURATION_MIN: "{n} min",
        MessageCode.DURATION_HOUR: "{n} hour",
        MessageCode.DURATION_HOURS: "{n} hours",
        MessageCode.DURATION_DAY: "{n} day",
        MessageCode.DURATION_DAYS: "{n} days",
    },
    # Italian. Only the user-facing strings that the backend actually
    # emits to a recipient are translated here (currently the reminder
    # notifications); any code missing from this table falls back to the
    # English template via ``render``.
    "it": {
        MessageCode.REMINDER_TITLE: "Attività in scadenza: {title}",
        MessageCode.REMINDER_DUE: "In scadenza {when}",
        MessageCode.REMINDER_DUE_BEFORE: "In scadenza {when} ({offset} prima)",
        MessageCode.DURATION_MIN: "{n} min",
        MessageCode.DURATION_HOUR: "{n} ora",
        MessageCode.DURATION_HOURS: "{n} ore",
        MessageCode.DURATION_DAY: "{n} giorno",
        MessageCode.DURATION_DAYS: "{n} giorni",
    },
}


def render(code: MessageCode, locale: str = DEFAULT_LOCALE, /, **params: Any) -> str:
    table = _CATALOG.get(locale) or _CATALOG[DEFAULT_LOCALE]
    template = table.get(code) or _CATALOG[DEFAULT_LOCALE].get(code) or code.value
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template
