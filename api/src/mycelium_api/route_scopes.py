"""Per-route scope map for the REST surface (task c19f2f63, enabler B).

The MCP gateway gates every tool against the calling assistant's scope. The
SAME ``mycelium_at_`` tokens also authenticate here, so without an equivalent
map a scoped assistant closes the MCP client and regains full access over
HTTP -- the boundary would be a preference, not a permission. This module is
the REST analogue of ``mcp.tool_scopes.TOOL_SCOPES``, and the two are kept
deliberately consistent: the route and the tool that perform the same
operation require the same key.

Each ``(METHOD, path-template)`` maps to one of:

- a scope key from ``core.mcp_scopes.SCOPE_CATALOG`` -- callable by an
  assistant that was granted it;
- ``PUBLIC`` -- no bearer required at all (health, docs, login, OAuth
  callbacks), so there is nothing to gate;
- ``HUMAN_ONLY`` -- authenticated, but never callable by a scoped assistant
  no matter which scopes it holds. This is the privilege-escalation fence:
  account/session/MFA, workspace and member administration, and above all
  the credential and assistant routes, since an assistant that can PATCH its
  own row could simply widen its own scope and undo the whole boundary;
- ``META`` -- authenticated, callable under any scope including the empty one:
  ``GET /agent/self`` (tenant-scoped) and ``GET /agent/workspaces``
  (pre-tenant, the workspaces the credential may act in).

FAIL-CLOSED: a route absent from the map is denied to a scoped assistant. A
drift-guard test asserts the map covers the live route table exactly, so a
newly added route fails CI instead of silently becoming unreachable (or, if
the default were open, silently becoming a hole).

Only a BOUND assistant carrying a scope list is ever restricted: bare agent
tokens and human session JWTs are unaffected, exactly as on the MCP side.

Cross-surface classification decisions (the workflow that built this map
flagged these as "do not leave undecided"; recorded here so they are not
re-litigated per route):

- ``GET/PATCH /auth/me`` are HUMAN_ONLY even though MCP ``whoami`` is a META
  tool callable under any scope. They are NOT the same operation: ``whoami``
  is a purpose-built agent bootstrap returning a curated identity subset,
  while ``/auth/me`` reads and mutates the raw user-account row (email,
  avatar, admin flag). A scoped assistant bootstraps over ``whoami``; it has
  no reason to touch the account row, and that half of the decision stands.

  The other half -- "no META sentinel is added to this module, the only
  always-callable identity path is the MCP one" -- is SUPERSEDED. It was
  true while every scoped client spoke MCP and could therefore bootstrap on
  ``whoami``. A scoped client that speaks only HTTP cannot, and hardcoding
  the scope list it was minted with drifts silently the moment somebody
  edits the assistant. ``GET /agent/self`` is the REST twin, mapped META,
  and it answers what the CREDENTIAL is rather than who the person is.

- ``POST /capability/text-block`` is HUMAN_ONLY, not a stopgap. It mints a
  ``mycelium_cap_`` token, and a capability token authenticates on a branch
  that carries no ``assistant_scope`` (``current_claims_optional`` returns
  ``{}`` for cap tokens). Letting a scoped assistant mint one is a laundering
  step from a scoped identity to an unscoped credential, so the mint itself is
  fenced off. (Redeeming a cap token minted by a human is unaffected: the
  gate below no-ops for cap tokens.)

- ``POST /notes/{note_id}/task-links`` and ``POST /tasks/{task_id}/note-links``
  multiplex ``kind=subject`` (a notes:write operation on MCP) and
  ``kind=artifact`` (tasks:write) through one endpoint; a single key cannot
  express both. Resolved (review #5): they map to the any-of frozenset
  ``LINK_WRITE_ANY`` (holding either write key passes the coarse gate) and the
  handler calls ``deps.require_agent_scope`` to enforce the exact per-kind key,
  matching the two separate MCP tools. No route split / API change needed.

- ``GET|PATCH|POST /annotations/{id}/body/*`` are mapped to the
  ``annotations:*`` family, which is correct. MCP models the same bytes under
  ``tasks:*`` via ``*_text_block_capability(kind='annotation')`` -- that is the
  MCP side being wrong (an annotations-only assistant is denied its own
  comment body while a tasks-only one is granted it). The REST map does not
  inherit the MCP bug; the MCP-side split is tracked separately.

- The annotation body/lifecycle WRITES (PATCH, DELETE, ``body/patch``,
  ``body/replace``, ``body/stream``, ``restore``) map to the any-of
  ``ANNOTATION_WRITE_ANY``. The MCP twins are split across two families
  addressing the same ``comments`` rows -- ``update_comment`` /
  ``delete_comment`` / ``replace_in_comment`` / ``restore_comment`` are
  ``comments:write``, ``edit_annotation`` / ``delete_annotation`` are
  ``annotations:write`` -- so pinning one key here would make whichever
  surface is cheaper the bypass. Unlike LINK_WRITE_ANY, no handler-side
  narrowing follows: either key genuinely suffices.

- Read-only notification routes fall onto ``notifications:write`` because the
  catalog has no ``notifications:read`` key (neither surface does). Requiring
  MORE scope to read is fail-safe, not a hole; adding the read key is a
  taxonomy change awaiting sign-off.
"""

from __future__ import annotations

# Sentinels. Distinct objects rather than strings so they can never collide
# with a real scope key.
PUBLIC: object = object()
HUMAN_ONLY: object = object()

# META: authenticated and callable under ANY scope -- including the empty
# one. Two routes hold it, ``GET /agent/self`` and ``GET
# /agent/workspaces`` (the second is pre-tenant, because a credential
# that may act in several workspaces has to be able to ask which before
# it can name one),
# and the reason it must not be a scope key is circular: a client cannot
# ask what it may do if asking is itself something it may not do. It then
# either hardcodes the list it was minted with, which drifts silently the
# moment someone edits the assistant, or it probes an unrelated endpoint
# and reads the failure, which is guessing.
#
# This supersedes the decision recorded below that no META sentinel would
# exist here. That was written when every scoped client spoke MCP and
# could bootstrap on the ``whoami`` tool; a scoped client that speaks only
# HTTP is a new fact, not a re-litigation. The fence itself is unchanged:
# ``/auth/me`` reads the raw account row and stays HUMAN_ONLY, and META
# answers the narrower question -- what is this CREDENTIAL -- rather than
# who is the person holding it.
META: object = object()
UNMAPPED: object = object()

# Any-of value: a route whose exact required key depends on the REQUEST BODY (a
# kind multiplexer) cannot be a single key, and the app-level gate runs before
# the body is parsed. Such a route maps to a frozenset -- the coarse gate lets a
# caller holding ANY of the keys through, and the handler then enforces the
# precise per-kind key (see deps.require_agent_scope). ``notes:write`` OR
# ``tasks:write`` covers the note<->task link routes (task c19f2f63 review, #5).
LINK_WRITE_ANY: frozenset[str] = frozenset({"notes:write", "tasks:write"})

# Any-of value, second family: the annotation body/lifecycle writes. ``comments:*``
# denotes the comment/suggestion COLLABORATION family, not "task work-diary rows
# only" -- add_comment, propose/accept/reject_suggestion and resolve all sit on
# comments:write and all already reach inline note annotations, which are the
# same table and the same ``_get``. Editing, replacing and restoring a comment
# therefore cannot cost a different key than creating and deleting it: the
# quintet create/edit/replace/delete/restore either belongs to the family or it
# does not. Unlike LINK_WRITE_ANY there is no per-body discriminator to narrow
# in the handler -- either key genuinely suffices, so no ``require_agent_scope``
# call follows.
ANNOTATION_WRITE_ANY: frozenset[str] = frozenset({"annotations:write", "comments:write"})

# Any-of value, third family, and this one is TRANSITIONAL (2026-09-03).
# REMOVE BY 2026-12-01.
#
# Two keys were each doing two jobs, so a caller that needed the small power
# had to be granted the large one:
#
#   workflows:write  advance ONE task  +  create / edit / DELETE the state
#                                         machine every task in the workspace
#                                         runs on, and repoint a project at
#                                         another one
#   tags:write       file a task into  +  invent, rename and rescope the
#                    an existing         vocabulary every entity carries
#                    client / project
#
# A browser extension whose job is "mark this done" must not hold the power
# to delete a workflow, and one that files a page into a project the user
# already made must not be able to rename that user's clients. Those are
# different resources, so they are different keys: ``tasks:state`` and
# ``tags:assign``.
#
# These sets exist ONLY so an assistant granted the wide key before the split
# keeps working: holding either passes. To remove them, re-grant the narrow
# key where it is relied on, then collapse each set to its narrow member.
# Left in place the split is decorative, because nothing ever moves off the
# wide key. The same two sets exist in ``mcp.tool_scopes``; the pair must be
# collapsed on both surfaces in the same change.
TASK_STATE_ANY: frozenset[str] = frozenset({"tasks:state", "workflows:write"})
TAG_ASSIGN_ANY: frozenset[str] = frozenset({"tags:assign", "tags:write"})

# (METHOD, path template) -> scope key | PUBLIC | HUMAN_ONLY | any-of frozenset
ROUTE_SCOPES: dict[tuple[str, str], object] = {
    # --- actors ---
    ("GET", "/actors"): "executors:read",
    # --- admin_sdi ---
    ("GET", "/admin/sdi-environment"): HUMAN_ONLY,
    ("PUT", "/admin/sdi-environment"): HUMAN_ONLY,
    ("PUT", "/admin/sdi-environment/intermediary"): HUMAN_ONLY,
    # --- admin_users ---
    ("GET", "/admin/users"): HUMAN_ONLY,
    ("PATCH", "/admin/users/{user_id}"): HUMAN_ONLY,
    # --- advisory ---
    ("GET", "/advisory/budget/{budget_id}/plan"): "budgets:read",
    ("POST", "/advisory/errands"): "tasks:read",
    ("POST", "/advisory/what-now"): "tasks:read",
    # --- agent_runs ---
    ("GET", "/agent-runs"): "agent_runs:read",
    ("GET", "/agent-runs/{run_id}"): "agent_runs:read",
    ("POST", "/agent-runs/{run_id}/cancel"): "agent_runs:write",
    ("POST", "/tasks/{task_id}/run"): "agent_runs:start",
    # --- agent_tokens ---
    ("GET", "/agent-tokens"): HUMAN_ONLY,
    ("POST", "/agent-tokens"): HUMAN_ONLY,
    ("DELETE", "/agent-tokens/{token_id}"): HUMAN_ONLY,
    # --- ai_assistants ---
    ("GET", "/ai-assistants"): HUMAN_ONLY,
    ("POST", "/ai-assistants"): HUMAN_ONLY,
    ("GET", "/ai-assistants/connector-info"): HUMAN_ONLY,
    ("GET", "/ai-assistants/scope-catalog"): HUMAN_ONLY,
    ("GET", "/ai-assistants/{assistant_id}"): HUMAN_ONLY,
    ("PATCH", "/ai-assistants/{assistant_id}"): HUMAN_ONLY,
    ("DELETE", "/ai-assistants/{assistant_id}"): HUMAN_ONLY,
    ("POST", "/ai-assistants/{assistant_id}/rotate"): HUMAN_ONLY,
    # --- annotations ---
    ("GET", "/annotations"): "annotations:read",
    ("GET", "/annotations/assigned"): "annotations:read",
    ("POST", "/annotations/comment"): "comments:write",
    ("POST", "/annotations/comment/stream"): "comments:write",
    ("POST", "/annotations/suggestion"): "comments:write",
    ("POST", "/annotations/suggestion/stream"): "comments:write",
    ("PUT", "/annotations/ui-state"): "annotations:write",
    ("GET", "/annotations/{annotation_id}"): "annotations:read",
    ("PATCH", "/annotations/{annotation_id}"): ANNOTATION_WRITE_ANY,
    ("DELETE", "/annotations/{annotation_id}"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/accept"): "comments:write",
    ("POST", "/annotations/{annotation_id}/assign"): "annotations:write",
    ("POST", "/annotations/{annotation_id}/body/append"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/body/patch"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/body/prepend"): ANNOTATION_WRITE_ANY,
    ("GET", "/annotations/{annotation_id}/body/raw"): "annotations:read",
    ("POST", "/annotations/{annotation_id}/body/replace"): ANNOTATION_WRITE_ANY,
    ("PATCH", "/annotations/{annotation_id}/body/stream"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/reject"): "comments:write",
    ("POST", "/annotations/{annotation_id}/reopen"): "annotations:write",
    ("POST", "/annotations/{annotation_id}/purge"): "delete:comments",
    ("POST", "/annotations/{annotation_id}/resolve"): "comments:write",
    ("GET", "/annotations/{annotation_id}/revisions"): "comments:read",
    ("GET", "/annotations/{annotation_id}/revisions/{rev_id}"): "comments:read",
    ("PATCH", "/annotations/{annotation_id}/revisions/{rev_id}"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/revisions/{rev_id}/restore"): ANNOTATION_WRITE_ANY,
    ("POST", "/annotations/{annotation_id}/restore"): ANNOTATION_WRITE_ANY,
    ("PUT", "/annotations/{annotation_id}/ui-state"): "annotations:write",
    # --- app ---
    ("GET", "/apidocs"): PUBLIC,
    ("GET", "/healthz"): PUBLIC,
    # Probe endpoints. Unauthenticated because the kubelet has no
    # credential to present, and safe to be: /healthz is a constant and
    # /readyz answers only which dependency class is down, never why.
    ("GET", "/readyz"): PUBLIC,
    # --- applications ---
    ("GET", "/docs"): PUBLIC,
    ("GET", "/docs/oauth2-redirect"): PUBLIC,
    ("GET", "/openapi.json"): PUBLIC,
    ("GET", "/redoc"): PUBLIC,
    # --- attachments ---
    ("POST", "/attachments/capability"): "attachments:write",
    ("POST", "/attachments/capability/write"): "attachments:write",
    ("POST", "/attachments/stream"): "attachments:write",
    ("DELETE", "/attachments/{attachment_id}"): "attachments:write",
    ("GET", "/attachments/{attachment_id}/download"): "attachments:write",
    # --- auth ---
    ("POST", "/auth/forgot-password"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/login-mfa"): PUBLIC,
    ("POST", "/auth/logout"): HUMAN_ONLY,
    # --- the device authorization grant ---
    # The two halves of the flow sit on opposite sides of the fence, and
    # that is the design rather than an accident of who calls what.
    #
    # PUBLIC, because the caller is a device that holds nothing yet.
    # Neither route yields anything on its own: ``authorize`` opens a
    # request that grants nothing until a person answers it, and ``token``
    # says "still waiting" until one has. Both are rate-limited on network
    # origin and neither confirms the existence of anything to a caller
    # without the right code.
    ("POST", "/auth/device/authorize"): PUBLIC,
    ("POST", "/auth/device/token"): PUBLIC,
    # HUMAN_ONLY, because answering one of those requests MINTS A
    # CREDENTIAL. A scoped credential that could approve a device could
    # ask for a device credential of its own and collect it, which is a
    # laundering step out of its own scope -- the same reason the
    # assistant and agent-token routes are fenced off above.
    ("GET", "/auth/device/pending"): HUMAN_ONLY,
    ("POST", "/auth/device/approve"): HUMAN_ONLY,
    ("POST", "/auth/device/deny"): HUMAN_ONLY,
    ("GET", "/agent/self"): META,
    # Same reason as /agent/self, one question earlier: a credential that
    # may act in several workspaces cannot name one until it knows which
    # ones it may name. Pre-tenant, so it takes no X-Workspace-Id, and it
    # answers only about the caller's own workspaces.
    ("GET", "/agent/workspaces"): META,
    ("GET", "/auth/me"): HUMAN_ONLY,
    ("PATCH", "/auth/me"): HUMAN_ONLY,
    ("GET", "/auth/me/avatar"): HUMAN_ONLY,
    ("POST", "/auth/me/avatar"): HUMAN_ONLY,
    ("POST", "/auth/refresh"): PUBLIC,
    ("POST", "/auth/resend-verification"): PUBLIC,
    ("POST", "/auth/reset-password"): PUBLIC,
    ("POST", "/auth/signup"): PUBLIC,
    ("POST", "/auth/verify-email"): PUBLIC,
    # --- billing ---
    ("GET", "/billing/balance"): "billing:read",
    ("PUT", "/billing/byok-factor"): "billing:write",
    ("POST", "/billing/grant"): "billing:write",
    ("GET", "/billing/ledger"): "billing:read",
    ("POST", "/billing/meter"): "billing:write",
    ("GET", "/billing/rate-cards"): "billing:read",
    ("POST", "/billing/rate-cards"): "billing:write",
    ("PUT", "/billing/storage-rate"): "billing:write",
    ("GET", "/billing/usage"): "billing:read",
    # --- budgets ---
    ("GET", "/budgets"): "budgets:read",
    ("POST", "/budgets"): "budgets:write",
    ("GET", "/budgets/{budget_id}"): "budgets:read",
    ("PATCH", "/budgets/{budget_id}"): "budgets:write",
    ("DELETE", "/budgets/{budget_id}"): "budgets:write",
    ("GET", "/budgets/{budget_id}/consumption"): "budgets:read",
    # --- buildinfo ---
    ("GET", "/buildinfo"): PUBLIC,
    # --- calendars ---
    ("GET", "/calendars"): "calendar:read",
    ("POST", "/calendars"): "calendar:write",
    ("GET", "/calendars/{calendar_id}/holidays"): "calendar:read",
    ("POST", "/calendars/{calendar_id}/holidays"): "calendar:write",
    ("DELETE", "/calendars/{calendar_id}/holidays/{day}"): "calendar:write",
    ("PUT", "/users/{user_id}/calendar"): "calendar:write",
    # --- capabilities ---
    ("POST", "/capability/text-block"): HUMAN_ONLY,
    # --- dependencies ---
    ("GET", "/dependencies"): "dependencies:read",
    ("POST", "/dependencies"): "dependencies:write",
    ("DELETE", "/dependencies/{dependency_id}"): "dependencies:write",
    ("GET", "/graph"): "dependencies:read",
    # --- dispatch ---
    ("GET", "/dispatch/requests"): "dispatch:read",
    ("POST", "/dispatch/requests/{request_id}/approve"): "dispatch:approve",
    ("POST", "/dispatch/requests/{request_id}/deny"): "dispatch:write",
    ("POST", "/dispatch/tick"): "dispatch:approve",
    # --- email ---
    ("GET", "/email/accounts"): "email:read",
    ("POST", "/email/accounts"): "email:write",
    ("GET", "/email/accounts/{account_id}"): "email:read",
    ("PATCH", "/email/accounts/{account_id}"): "email:write",
    ("DELETE", "/email/accounts/{account_id}"): HUMAN_ONLY,
    ("PUT", "/email/accounts/{account_id}/default-tags"): "email:write",
    ("PUT", "/email/accounts/{account_id}/secret"): HUMAN_ONLY,
    ("POST", "/email/accounts/{account_id}/send"): "email:send",
    ("POST", "/email/accounts/{account_id}/sync"): "email:write",
    ("GET", "/email/drafts"): "email:read",
    ("POST", "/email/drafts/{job_id}/approve"): "email:send",
    ("POST", "/email/drafts/{job_id}/reject"): "email:write",
    ("GET", "/email/messages"): "email:read",
    ("GET", "/email/messages/{message_id}"): "email:read",
    ("POST", "/email/messages/{message_id}/draft"): "email:write",
    ("POST", "/email/messages/{message_id}/reply"): "email:send",
    ("POST", "/email/messages/{message_id}/to-note"): "notes:write",
    ("POST", "/email/messages/{message_id}/to-task"): "tasks:write",
    ("GET", "/email/threads/{thread_id}"): "email:read",
    # --- embedder_provider ---
    ("GET", "/embedder-provider"): HUMAN_ONLY,
    ("PUT", "/embedder-provider"): HUMAN_ONLY,
    ("GET", "/embedder-provider/scaleway/models"): HUMAN_ONLY,
    # --- executors ---
    ("GET", "/executors"): "executors:read",
    ("POST", "/executors"): "executors:write",
    ("PATCH", "/executors/{executor_id}"): "executors:write",
    ("DELETE", "/executors/{executor_id}"): "executors:write",
    # --- export ---
    ("POST", "/export/pdf"): HUMAN_ONLY,
    # --- garden ---
    ("POST", "/garden/apply"): "notes:write",
    ("GET", "/garden/audit"): "events:read",
    ("GET", "/garden/candidates"): "notes:read",
    ("GET", "/garden/classify/{node_id}"): "notes:read",
    ("GET", "/garden/clusters"): "notes:read",
    ("GET", "/garden/graph"): "notes:read",
    ("GET", "/garden/health"): "notes:read",
    ("GET", "/garden/health/events"): "notes:read",
    ("GET", "/garden/health/timeseries"): "notes:read",
    ("POST", "/garden/learning/rollback"): "notes:write",
    ("GET", "/garden/learning/telemetry"): "notes:read",
    ("GET", "/garden/link-suggestions/{note_id}"): "notes:read",
    ("GET", "/garden/review/accept-ratio"): "notes:read",
    ("POST", "/garden/review/approve"): "notes:write",
    ("GET", "/garden/review/pending"): "notes:read",
    ("POST", "/garden/review/reject"): "notes:write",
    ("GET", "/garden/review/rejected"): "notes:read",
    ("POST", "/garden/review/restore-source"): "notes:write",
    ("GET", "/garden/walk"): "notes:read",
    # --- invoices ---
    ("GET", "/invoices"): "invoices:read",
    ("POST", "/invoices"): "invoices:write",
    ("POST", "/invoices/credit-note"): "invoices:write",
    ("POST", "/invoices/purge-test"): "invoices:write",
    ("POST", "/invoices/receipt"): "invoices:write",
    ("GET", "/invoices/{invoice_id}"): "invoices:read",
    ("PATCH", "/invoices/{invoice_id}"): "invoices:write",
    ("DELETE", "/invoices/{invoice_id}"): "invoices:write",
    ("POST", "/invoices/{invoice_id}/archive"): "invoices:write",
    ("GET", "/invoices/{invoice_id}/lines"): "invoices:read",
    ("POST", "/invoices/{invoice_id}/lines"): "invoices:write",
    ("PUT", "/invoices/{invoice_id}/lines/{line_id}"): "invoices:write",
    ("DELETE", "/invoices/{invoice_id}/lines/{line_id}"): "invoices:write",
    ("GET", "/invoices/{invoice_id}/lines/{line_id}/altri-dati"): "invoices:read",
    ("PUT", "/invoices/{invoice_id}/lines/{line_id}/altri-dati"): "invoices:write",
    ("GET", "/invoices/{invoice_id}/notifications"): "invoices:read",
    ("GET", "/invoices/{invoice_id}/notifications/{notification_id}/xml"): "invoices:read",
    ("POST", "/invoices/{invoice_id}/paid"): "invoices:write",
    ("GET", "/invoices/{invoice_id}/pdf"): "invoices:read",
    ("GET", "/invoices/{invoice_id}/preview"): "invoices:read",
    ("POST", "/invoices/{invoice_id}/reopen"): "invoices:write",
    ("POST", "/invoices/{invoice_id}/restore"): "invoices:write",
    ("POST", "/invoices/{invoice_id}/transmit"): "invoices:write",
    ("POST", "/invoices/{invoice_id}/trash"): "invoices:write",
    ("POST", "/invoices/{invoice_id}/unarchive"): "invoices:write",
    ("GET", "/invoices/{invoice_id}/xml"): "invoices:read",
    ("GET", "/issuer-profiles"): "invoices:read",
    ("POST", "/issuer-profiles"): "invoices:write",
    ("GET", "/issuer-profiles/{profile_id}"): "invoices:read",
    ("PATCH", "/issuer-profiles/{profile_id}"): "invoices:write",
    ("DELETE", "/issuer-profiles/{profile_id}"): "invoices:write",
    ("PUT", "/issuer-profiles/{profile_id}/conservation"): "invoices:write",
    ("GET", "/issuer-profiles/{profile_id}/counters"): "invoices:read",
    ("PUT", "/issuer-profiles/{profile_id}/counters/{series}/{year}"): "invoices:write",
    ("POST", "/issuer-profiles/{profile_id}/default"): "invoices:write",
    ("GET", "/issuer-profiles/{profile_id}/logo"): "invoices:read",
    ("POST", "/issuer-profiles/{profile_id}/logo"): "invoices:write",
    ("DELETE", "/issuer-profiles/{profile_id}/logo"): "invoices:write",
    ("GET", "/issuer-profiles/{profile_id}/mandate"): "invoices:read",
    ("POST", "/issuer-profiles/{profile_id}/mandate"): HUMAN_ONLY,
    ("DELETE", "/issuer-profiles/{profile_id}/mandate"): HUMAN_ONLY,
    ("GET", "/issuer-profiles/{profile_id}/mandates"): "invoices:read",
    # --- issuer_api_keys ---
    ("GET", "/issuer-profiles/{issuer_profile_id}/api-keys"): HUMAN_ONLY,
    ("POST", "/issuer-profiles/{issuer_profile_id}/api-keys"): HUMAN_ONLY,
    ("DELETE", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}"): HUMAN_ONLY,
    ("PUT", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/allowlist"): HUMAN_ONLY,
    ("POST", "/issuer-profiles/{issuer_profile_id}/api-keys/{key_id}/rotate"): HUMAN_ONLY,
    # --- llm_provider ---
    ("GET", "/llm-provider"): HUMAN_ONLY,
    ("PUT", "/llm-provider"): HUMAN_ONLY,
    ("GET", "/llm-provider/scaleway/models"): HUMAN_ONLY,
    # --- lookup ---
    ("GET", "/lookup/{prefix}"): "tasks:read",
    # --- memory ---
    ("POST", "/memory/blobs"): "memory:write",
    ("GET", "/memory/blobs/{blob_id}"): "memory:read",
    ("DELETE", "/memory/blobs/{blob_id}"): "memory:delete",
    ("POST", "/memory/blobs/{blob_id}/tags"): "memory:write",
    ("DELETE", "/memory/blobs/{blob_id}/tags/{tag_id}"): "memory:write",
    ("POST", "/memory/consolidate"): "memory:write",
    ("POST", "/memory/erase"): "memory:delete",
    ("POST", "/memory/migrate-embeddings"): "memory:write",
    ("GET", "/memory/migration-status"): "memory:read",
    ("POST", "/memory/rechunk"): "memory:admin",
    ("POST", "/memory/recompute-tier"): "memory:write",
    ("POST", "/memory/search"): "search:read",
    ("GET", "/memory/status"): "memory:read",
    # --- memory_channels ---
    ("GET", "/memory/channels"): "memory:read",
    ("POST", "/memory/channels"): "memory:admin",
    ("PATCH", "/memory/channels/{channel_id}"): "memory:admin",
    ("DELETE", "/memory/channels/{channel_id}"): "memory:admin",
    # --- mfa ---
    ("POST", "/mfa/activate"): HUMAN_ONLY,
    ("POST", "/mfa/disable"): HUMAN_ONLY,
    ("POST", "/mfa/setup"): HUMAN_ONLY,
    ("GET", "/mfa/status"): HUMAN_ONLY,
    # --- notes ---
    ("GET", "/notes"): "notes:read",
    ("POST", "/notes"): "notes:write",
    ("POST", "/notes/command"): "notes:write",
    ("POST", "/notes/conversations"): "notes:write",
    ("GET", "/notes/links"): "notes:read",
    ("POST", "/notes/merge"): "notes:write",
    ("POST", "/notes/quick-create"): "notes:write",
    ("POST", "/notes/synthesize"): "ai:generate",
    ("GET", "/notes/{note_id}"): "notes:read",
    ("PATCH", "/notes/{note_id}"): "notes:write",
    ("POST", "/notes/{note_id}/append"): "notes:write",
    ("POST", "/notes/{note_id}/archive"): "notes:write",
    ("GET", "/notes/{note_id}/attachments"): "notes:read",
    ("POST", "/notes/{note_id}/attachments"): "attachments:write",
    ("GET", "/notes/{note_id}/checklist"): "notes:read",
    ("POST", "/notes/{note_id}/checklist"): "notes:write",
    ("PATCH", "/notes/{note_id}/checklist/{item_id}"): "notes:write",
    ("DELETE", "/notes/{note_id}/checklist/{item_id}"): "delete:notes",
    ("POST", "/notes/{note_id}/checklist:clear_done"): "delete:notes",
    ("POST", "/notes/{note_id}/checklist:reorder"): "notes:write",
    ("POST", "/notes/{note_id}/delete"): "notes:write",
    ("POST", "/notes/{note_id}/derive-task"): "tasks:write",
    ("POST", "/notes/{note_id}/distill"): "ai:generate",
    ("POST", "/notes/{note_id}/edit-session/seal"): "notes:write",
    ("POST", "/notes/{note_id}/erase"): "memory:delete",
    ("GET", "/notes/{note_id}/links"): "notes:read",
    ("POST", "/notes/{note_id}/links"): "notes:write",
    ("DELETE", "/notes/{note_id}/links"): "notes:write",
    ("POST", "/notes/{note_id}/maturity"): "notes:write",
    ("POST", "/notes/{note_id}/messages"): "ai:generate",
    ("GET", "/notes/{note_id}/parts"): "notes:read",
    ("POST", "/notes/{note_id}/parts"): "notes:write",
    ("PUT", "/notes/{note_id}/parts/order"): "notes:write",
    ("POST", "/notes/{note_id}/parts/stream"): "notes:write",
    ("PUT", "/notes/{note_id}/parts/ui-state"): "notes:write",
    ("GET", "/notes/{note_id}/parts:trashed"): "notes:read",
    ("PATCH", "/notes/{note_id}/parts/{part_id}"): "notes:write",
    # The PURGE (irreversible) stays on the danger key; the restorable
    # delete is POST .../trash, which is an ordinary note write.
    ("DELETE", "/notes/{note_id}/parts/{part_id}"): "delete:notes",
    ("POST", "/notes/{note_id}/parts/{part_id}/restore"): "notes:write",
    ("POST", "/notes/{note_id}/parts/{part_id}/trash"): "notes:write",
    ("POST", "/notes/{note_id}/parts/{part_id}/append"): "notes:write",
    ("POST", "/notes/{note_id}/parts/{part_id}/body/patch"): "notes:write",
    ("GET", "/notes/{note_id}/parts/{part_id}/body/raw"): "notes:read",
    ("PUT", "/notes/{note_id}/parts/{part_id}/body/stream"): "notes:write",
    ("POST", "/notes/{note_id}/parts/{part_id}/prepend"): "notes:write",
    ("POST", "/notes/{note_id}/parts/{part_id}/replace"): "notes:write",
    ("PUT", "/notes/{note_id}/parts/{part_id}/ui-state"): "notes:write",
    ("POST", "/notes/{note_id}/promote"): "tasks:write",
    ("POST", "/notes/{note_id}/protect"): "notes:write",
    ("POST", "/notes/{note_id}/restore"): "notes:write",
    ("GET", "/notes/{note_id}/revisions"): "notes:read",
    ("GET", "/notes/{note_id}/revisions/{rev_id}"): "notes:read",
    ("PATCH", "/notes/{note_id}/revisions/{rev_id}"): "notes:write",
    ("POST", "/notes/{note_id}/revisions/{rev_id}/restore"): "notes:write",
    ("POST", "/notes/{note_id}/tags"): "notes:write",
    ("DELETE", "/notes/{note_id}/tags/{tag_id}"): "notes:write",
    ("POST", "/notes/{note_id}/task-links"): LINK_WRITE_ANY,
    ("DELETE", "/notes/{note_id}/task-links"): "notes:write",
    ("POST", "/notes/{note_id}/transcribe"): "ai:generate",
    ("GET", "/notes/{note_id}/turns"): "notes:read",
    ("POST", "/notes/{note_id}/unarchive"): "notes:write",
    ("POST", "/notes/{note_id}/unprotect"): "notes:write",
    # --- notifications ---
    ("GET", "/notifications"): "notifications:read",
    ("POST", "/notifications/dispatch"): "notifications:send",
    ("GET", "/notifications/prefs"): "notifications:read",
    ("PUT", "/notifications/prefs"): "notifications:write",
    ("POST", "/notifications/push/subscribe"): "notifications:send",
    ("POST", "/notifications/push/unsubscribe"): "notifications:write",
    ("GET", "/notifications/push/vapid-public-key"): "notifications:read",
    ("POST", "/notifications/recurrences"): "tasks:write",
    ("POST", "/notifications/recurrences/spawn-due"): "tasks:write",
    ("POST", "/notifications/reminders/scan"): "notifications:write",
    ("DELETE", "/notifications/{notification_id}"): "notifications:write",
    # --- oauth ---
    ("GET", "/.well-known/oauth-authorization-server"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource"): PUBLIC,
    ("GET", "/.well-known/oauth-protected-resource/{suffix:path}"): PUBLIC,
    ("GET", "/oauth/authorize"): PUBLIC,
    ("POST", "/oauth/token"): PUBLIC,
    # --- oauth_google ---
    ("GET", "/oauth/google/callback"): PUBLIC,
    ("GET", "/oauth/google/start"): HUMAN_ONLY,
    # --- public_invoices ---
    ("GET", "/api/v1/events"): "invoices:read",
    ("GET", "/api/v1/invoices"): "invoices:read",
    ("POST", "/api/v1/invoices"): "invoices:write",
    ("POST", "/api/v1/invoices/batch"): "invoices:write",
    ("POST", "/api/v1/invoices/credit-note"): "invoices:write",
    ("GET", "/api/v1/invoices/{invoice_id}"): "invoices:read",
    ("GET", "/api/v1/invoices/{invoice_id}/notifications"): "invoices:read",
    ("GET", "/api/v1/invoices/{invoice_id}/notifications/{notification_id}/xml"): "invoices:read",
    ("GET", "/api/v1/invoices/{invoice_id}/pdf"): "invoices:read",
    ("POST", "/api/v1/invoices/{invoice_id}/transmit"): "invoices:write",
    ("GET", "/api/v1/invoices/{invoice_id}/xml"): "invoices:read",
    # --- received_invoices ---
    ("POST", "/received-invoices/{received_invoice_id}/esito-committente"): "invoices:write",
    # --- schedule ---
    ("GET", "/schedule"): "schedule:read",
    ("POST", "/schedule/recompute"): "schedule:write",
    ("GET", "/schedule/{task_id}"): "schedule:read",
    ("PATCH", "/tasks/{task_id}/schedule"): "schedule:write",
    # --- search ---
    ("POST", "/search"): "search:read",
    ("POST", "/search/click"): "search:write",
    ("POST", "/search/reindex"): HUMAN_ONLY,
    # --- tags ---
    ("GET", "/clients"): "tags:read",
    ("POST", "/clients"): "tags:write",
    ("PATCH", "/clients/{tag_id}"): "tags:write",
    ("DELETE", "/clients/{tag_id}"): "delete:taxonomy",
    ("GET", "/projects"): "tags:read",
    ("POST", "/projects"): "tags:write",
    ("PATCH", "/projects/{tag_id}"): "tags:write",
    ("DELETE", "/projects/{tag_id}"): "delete:taxonomy",
    ("GET", "/tags"): "tags:read",
    ("POST", "/tags"): "tags:write",
    ("PATCH", "/tags/{tag_id}"): "tags:write",
    ("PUT", "/tags/{tag_id}/scope"): "tags:write",
    # --- task_relations ---
    ("GET", "/task-relations"): "tasks:read",
    ("POST", "/task-relations"): "tasks:write",
    ("DELETE", "/task-relations/{relation_id}"): "tasks:write",
    # --- tasks ---
    ("GET", "/tasks"): "tasks:read",
    ("POST", "/tasks"): "tasks:write",
    ("GET", "/tasks/{task_id}"): "tasks:read",
    ("PATCH", "/tasks/{task_id}"): "tasks:write",
    ("POST", "/tasks/{task_id}/archive"): "tasks:write",
    ("POST", "/tasks/{task_id}/assignees"): "tasks:write",
    ("DELETE", "/tasks/{task_id}/assignees/{user_id}"): "tasks:write",
    ("GET", "/tasks/{task_id}/attachments"): "tasks:read",
    ("POST", "/tasks/{task_id}/attachments"): "attachments:write",
    ("GET", "/tasks/{task_id}/checklist"): "tasks:read",
    ("POST", "/tasks/{task_id}/checklist"): "tasks:write",
    ("PATCH", "/tasks/{task_id}/checklist/{item_id}"): "tasks:write",
    ("DELETE", "/tasks/{task_id}/checklist/{item_id}"): "delete:tasks",
    ("POST", "/tasks/{task_id}/checklist:clear_done"): "delete:tasks",
    ("POST", "/tasks/{task_id}/checklist:reorder"): "tasks:write",
    ("POST", "/tasks/{task_id}/claim"): "tasks:write",
    ("GET", "/tasks/{task_id}/comments"): "comments:read",
    ("POST", "/tasks/{task_id}/comments"): "comments:write",
    ("POST", "/tasks/{task_id}/decline"): "tasks:write",
    ("POST", "/tasks/{task_id}/delete"): "tasks:write",
    ("POST", "/tasks/{task_id}/description/append"): "tasks:write",
    ("POST", "/tasks/{task_id}/description/patch"): "tasks:write",
    ("POST", "/tasks/{task_id}/description/prepend"): "tasks:write",
    ("POST", "/tasks/{task_id}/description/replace"): "tasks:write",
    ("GET", "/tasks/{task_id}/description/raw"): "tasks:read",
    ("PUT", "/tasks/{task_id}/description/stream"): "tasks:write",
    ("POST", "/tasks/{task_id}/edit-session/seal"): "tasks:write",
    ("GET", "/tasks/{task_id}/handoffs"): "tasks:read",
    ("POST", "/tasks/{task_id}/note"): "notes:write",
    ("GET", "/tasks/{task_id}/note-links"): "notes:read",
    ("POST", "/tasks/{task_id}/note-links"): LINK_WRITE_ANY,
    ("DELETE", "/tasks/{task_id}/note-links"): "notes:write",
    ("POST", "/tasks/{task_id}/notes"): "notes:write",
    ("POST", "/tasks/{task_id}/offer"): "tasks:write",
    ("GET", "/tasks/{task_id}/participants"): "tasks:read",
    ("POST", "/tasks/{task_id}/participants"): "tasks:write",
    ("DELETE", "/tasks/{task_id}/participants/{identity_id}"): "tasks:write",
    ("GET", "/tasks/{task_id}/reminders"): "notifications:read",
    ("POST", "/tasks/{task_id}/reminders"): "notifications:write",
    ("DELETE", "/tasks/{task_id}/reminders/{reminder_id}"): "notifications:write",
    ("POST", "/tasks/{task_id}/restore"): "tasks:write",
    ("GET", "/tasks/{task_id}/revisions"): "tasks:read",
    ("GET", "/tasks/{task_id}/revisions/{rev_id}"): "tasks:read",
    ("PATCH", "/tasks/{task_id}/revisions/{rev_id}"): "tasks:write",
    ("POST", "/tasks/{task_id}/revisions/{rev_id}/restore"): "tasks:write",
    ("POST", "/tasks/{task_id}/state"): TASK_STATE_ANY,
    ("GET", "/tasks/{task_id}/states"): "workflows:read",
    ("POST", "/tasks/{task_id}/tags"): TAG_ASSIGN_ANY,
    ("DELETE", "/tasks/{task_id}/tags/{tag_id}"): TAG_ASSIGN_ANY,
    ("POST", "/tasks/{task_id}/unarchive"): "tasks:write",
    # --- telegram ---
    ("DELETE", "/telegram/link"): HUMAN_ONLY,
    ("POST", "/telegram/link/request"): HUMAN_ONLY,
    ("GET", "/telegram/link/status"): HUMAN_ONLY,
    ("POST", "/telegram/webhook/{secret}"): PUBLIC,
    # --- time_tracking ---
    ("GET", "/time/entries"): "time:read",
    ("POST", "/time/entries"): "time:write",
    ("GET", "/time/entries.csv"): "time:read",
    ("GET", "/time/entries/{entry_id}"): "time:read",
    ("PATCH", "/time/entries/{entry_id}"): "time:write",
    ("DELETE", "/time/entries/{entry_id}"): "time:write",
    ("POST", "/time/pause"): "time:write",
    ("GET", "/time/report"): "time:read",
    ("GET", "/time/report.csv"): "time:read",
    ("GET", "/time/report/by-task"): "time:read",
    ("GET", "/time/report/daily"): "time:read",
    ("GET", "/time/report/daily.csv"): "time:read",
    ("POST", "/time/resume"): "time:write",
    ("GET", "/time/running"): "time:read",
    ("POST", "/time/start"): "time:write",
    ("POST", "/time/stop"): "time:write",
    # --- payment_connectors (ADR-0051) ---
    # Configuring a connector mints the credential that lets an outside system
    # emit fiscal documents in this org's name: human-only, like every other
    # credential surface. The event list/retry are triage over those documents
    # and stay on the same footing.
    ("GET", "/payment-connectors/vocabulary"): HUMAN_ONLY,
    ("GET", "/issuer-profiles/{issuer_profile_id}/payment-connectors"): HUMAN_ONLY,
    ("POST", "/issuer-profiles/{issuer_profile_id}/payment-connectors"): HUMAN_ONLY,
    (
        "PATCH",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}",
    ): HUMAN_ONLY,
    (
        "DELETE",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}",
    ): HUMAN_ONLY,
    (
        "DELETE",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/api-key",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/rotate-signing-secret",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/rotate-api-key",
    ): HUMAN_ONLY,
    (
        "GET",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events",
    ): HUMAN_ONLY,
    (
        "GET",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/deliveries",
    ): HUMAN_ONLY,
    (
        "GET",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/dry-run-xml",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/discard-dry-run",
    ): HUMAN_ONLY,
    (
        "GET",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/payload",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/promote",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/reshoot-dry-run-xml",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/recompose",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/assign-customer",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
        "/events/{event_id}/retry",
    ): HUMAN_ONLY,
    # The inbound provider webhook carries no bearer: its authority is the MAC
    # over the raw body, verified inside the handler.
    ("POST", "/api/v1/connectors/{provider}/{connector_id}"): PUBLIC,
    # --- webhook_endpoints ---
    ("GET", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints"): HUMAN_ONLY,
    ("POST", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints"): HUMAN_ONLY,
    ("PATCH", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}"): HUMAN_ONLY,
    ("DELETE", "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}"): HUMAN_ONLY,
    (
        "GET",
        "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/deliveries",
    ): HUMAN_ONLY,
    (
        "POST",
        "/issuer-profiles/{issuer_profile_id}/webhook-endpoints/{endpoint_id}/rotate-secret",
    ): HUMAN_ONLY,
    # --- workflows ---
    ("PATCH", "/projects/{project_tag_id}/workflow"): "workflows:write",
    ("GET", "/workflows"): "workflows:read",
    ("POST", "/workflows"): "workflows:write",
    ("POST", "/workflows/import"): "workflows:write",
    ("POST", "/workflows/{workflow_id}/import"): "workflows:write",
    ("GET", "/workflows/{workflow_id}/export"): "workflows:read",
    ("PATCH", "/workflows/{workflow_id}"): "workflows:write",
    ("DELETE", "/workflows/{workflow_id}"): "workflows:write",
    ("POST", "/workflows/{workflow_id}/default"): "workflows:write",
    ("GET", "/workflows/{workflow_id}/states"): "workflows:read",
    ("GET", "/workflows/{workflow_id}/transitions"): "workflows:read",
    # --- workspace ---
    ("GET", "/workspaces"): HUMAN_ONLY,
    ("POST", "/workspaces"): HUMAN_ONLY,
    ("GET", "/workspaces/me"): HUMAN_ONLY,
    ("PATCH", "/workspaces/me"): HUMAN_ONLY,
    ("GET", "/workspaces/me/members"): HUMAN_ONLY,
    ("POST", "/workspaces/me/members"): HUMAN_ONLY,
    ("PATCH", "/workspaces/me/members/{user_id}"): HUMAN_ONLY,
    ("DELETE", "/workspaces/me/members/{user_id}"): HUMAN_ONLY,
    ("PATCH", "/workspaces/me/settings"): HUMAN_ONLY,
    ("POST", "/workspaces/me/trash/empty"): HUMAN_ONLY,
    ("DELETE", "/workspaces/{workspace_id}"): HUMAN_ONLY,
    ("POST", "/workspaces/{workspace_id}/archive"): HUMAN_ONLY,
    ("POST", "/workspaces/{workspace_id}/unarchive"): HUMAN_ONLY,
}


def required_scope(method: str, path: str) -> object:
    """The scope key ``(method, path)`` needs, or ``PUBLIC`` / ``HUMAN_ONLY``,
    or the ``UNMAPPED`` sentinel when the route is not in the map (fail-closed:
    a scoped assistant is denied)."""
    return ROUTE_SCOPES.get((method.upper(), path), UNMAPPED)


def scope_permits(method: str, path: str, scope: list[str] | None) -> bool:
    """Whether a caller holding ``scope`` may invoke this route.

    ``scope is None`` means a human session or a bare agent token: no per-route
    restriction, unchanged behaviour. Otherwise the route must be PUBLIC or map
    to a key the assistant holds; HUMAN_ONLY and unmapped routes are denied."""
    if scope is None:
        return True
    req = required_scope(method, path)
    if req is PUBLIC:
        return True
    if req is META:
        return True
    if req is HUMAN_ONLY or req is UNMAPPED:
        return False
    if isinstance(req, frozenset):
        # Any-of: hold at least one of the keys. The handler enforces the exact
        # per-kind key behind this coarse gate (kind multiplexer routes).
        return bool(req & set(scope))
    return req in scope


__all__ = [
    "HUMAN_ONLY",
    "PUBLIC",
    "ROUTE_SCOPES",
    "UNMAPPED",
    "required_scope",
    "scope_permits",
]
