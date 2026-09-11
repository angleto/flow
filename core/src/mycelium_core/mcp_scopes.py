"""Scope catalog for AI assistants over MCP.

The user picks a subset of these when creating an assistant in
``/settings/ai-assistants``. The catalog is family-coarse rather than
per-tool: a domain (``tasks``, ``time``, ...) splits into ``:read`` /
``:write`` and a small ``:danger`` bucket for ops that spend credits or
destroy data.

Enforcement (task c19f2f63, enabler B): the HTTP transport reads
``assistant.scope`` (a JSONB list) from the SECURITY DEFINER
``authenticate_agent_token`` lookup (migration 0059) and publishes it into
``server._PRINCIPAL_SCOPE``. Every concrete tool maps to one scope key in
``mcp.tool_scopes.TOOL_SCOPES``; the gateway gate rejects an ``execute_tool``
call outside ``scope`` and hides out-of-scope tools from ``search_tools`` /
``describe_tools`` (``server._scope_permits``). FAIL-CLOSED: a tool with no
mapping is denied to a scoped assistant (a drift-guard test keeps the map in
lockstep with the registry). Legacy bare ``agent_tokens`` rows (assistant_id
IS NULL) and the stdio / human-bearer paths keep their previous all-tools
access — the filter only bites a bound assistant that carries a scope list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["read", "write", "danger"]


@dataclass(frozen=True, slots=True)
class ScopeDef:
    key: str
    category: Category
    label: str
    description: str


SCOPE_CATALOG: tuple[ScopeDef, ...] = (
    # --- read ---
    ScopeDef("tasks:read", "read", "Read tasks", "List, search, and read tasks and their tags."),
    ScopeDef("time:read", "read", "Read time", "Read time entries, running timers and reports."),
    ScopeDef("tags:read", "read", "Read taxonomy", "Read tags, clients and projects."),
    ScopeDef("notes:read", "read", "Read notes", "Read notes and their attachments."),
    ScopeDef("memory:read", "read", "Read memory", "Search the memory store and read blobs."),
    ScopeDef("calendar:read", "read", "Read calendar", "Read calendars, events and holidays."),
    ScopeDef(
        "schedule:read", "read", "Read schedule", "Read the resource-aware schedule projection."
    ),
    ScopeDef("comments:read", "read", "Read comments", "Read task comments."),
    ScopeDef("dependencies:read", "read", "Read dependencies", "Read the task dependency graph."),
    ScopeDef(
        "events:read",
        "read",
        "Read the event bus",
        "Read the workspace coordination event stream (ADR-0036): the "
        "read / propose / commit / reject events other agents and humans emit.",
    ),
    ScopeDef("budgets:read", "read", "Read budgets", "Read budget envelopes and balances."),
    ScopeDef(
        "invoices:read",
        "read",
        "Read invoices",
        "List and read invoices (status, number, totals) and their FatturaPA XML.",
    ),
    # --- write ---
    ScopeDef(
        "tasks:write", "write", "Write tasks", "Create, update and re-state tasks (no deletion)."
    ),
    ScopeDef(
        "tasks:state",
        "write",
        "Advance tasks",
        "Move a task to another state of the workflow it already runs on. "
        "Advancing a task and REDEFINING the state machine every task runs "
        "on are different powers, so they are different keys: this one "
        "cannot create, edit or delete a workflow.",
    ),
    ScopeDef("time:write", "write", "Write time", "Start, stop and edit time entries."),
    ScopeDef(
        "tags:write",
        "write",
        "Write taxonomy",
        "Create or update tags, clients and projects, and change a tag's "
        "scope. This is the vocabulary itself; FILING something into a "
        "client, project or tag that already exists is tags:assign.",
    ),
    ScopeDef(
        "tags:assign",
        "write",
        "File into the taxonomy",
        "Attach and detach existing tags, and move a task into an existing "
        "client or project. Inventing a vocabulary and using one are "
        "different powers: a client that files work does not need to be "
        "able to rename a client or rescope a tag every entity carries.",
    ),
    ScopeDef("notes:write", "write", "Write notes", "Create and edit notes."),
    ScopeDef("memory:write", "write", "Write memory", "Append to the memory store."),
    ScopeDef(
        "comments:write",
        "write",
        "Write comments",
        "Take part in the comment/suggestion conversation: add, edit, replace, "
        "delete and restore a comment, and propose / accept / reject / resolve "
        "a suggestion. Covers both places comments live -- a task's work diary "
        "and an inline note annotation -- since they are one kind of row. "
        "Accepting a suggestion additionally needs write on the document it "
        "splices into (notes:write / tasks:write), so this key is never a way "
        "to edit a document body indirectly.",
    ),
    ScopeDef("calendar:write", "write", "Write calendar", "Create or edit events and holidays."),
    ScopeDef(
        "dependencies:write", "write", "Write dependencies", "Create or remove task dependencies."
    ),
    # --- danger ---
    ScopeDef(
        "attachments:write",
        "danger",
        "Upload attachments",
        "Upload files to tasks or notes. The bytes leave the workspace boundary on read.",
    ),
    ScopeDef(
        "workflows:write",
        "danger",
        "Edit workflows",
        "Create, change and delete the workflow state machines every task "
        "runs on, and choose which one a project uses — invariants matter "
        "here. Moving one task between states is tasks:state.",
    ),
    ScopeDef(
        "delete:taxonomy",
        "danger",
        "Delete clients/projects",
        "Hard-delete archived clients and projects (cascades attachments, notes, time entries).",
    ),
    # Destructive-parity keys (taxonomy review, task c19f2f63): fence
    # IRREVERSIBLE note/task destruction off the ordinary write key, mirroring
    # memory:delete and delete:taxonomy. Only HARD ops move here; soft-delete
    # (trash, restorable) and archive stay on notes:write / tasks:write.
    ScopeDef(
        "delete:notes",
        "danger",
        "Delete note data",
        "Irreversibly destroy note content: PURGE a note part (its row, its "
        "trash entry and its search index) or purge checklist items. The "
        "restorable operations stay under notes:write -- trashing a note, and "
        "trashing a part (trash_note_part / restore_note_part).",
    ),
    ScopeDef(
        "delete:tasks",
        "danger",
        "Delete task data",
        "Irreversibly purge task checklist items (no restore). Trashing a task "
        "(restorable) stays under tasks:write.",
    ),
    ScopeDef(
        "delete:comments",
        "danger",
        "Delete comment data",
        "Irreversibly destroy a comment or suggestion: the entry, its per-user "
        "card state and its whole revision history, with no restore. Deleting a "
        "comment the ordinary (restorable) way stays under comments:write. "
        "Admin-only at the service layer as well: erasing a signed entry from a "
        "shared conversation is not the author's private business.",
    ),
    ScopeDef(
        "dispatch:approve",
        "danger",
        "Approve LLM dispatch",
        "Approve a pending agent dispatch request — every approval can spend credits.",
    ),
    ScopeDef(
        "agent_runs:start",
        "danger",
        "Start LLM runs",
        "Trigger an agent run — costs credits against the workspace budget.",
    ),
    ScopeDef(
        "invoices:write",
        "danger",
        "Edit invoices",
        "Create or edit invoices (fiscal records). Read-only by default.",
    ),
    # Reclassified danger -> read in the taxonomy review (task c19f2f63): it is
    # the only READ that sat in the opt-in tier, and reading a balance neither
    # destroys, spends, sends nor hands out a credential. Now a default read.
    ScopeDef(
        "billing:read",
        "read",
        "Read billing",
        "Read the billing ledger, credit balance and rate cards. Read-only financial visibility.",
    ),
    ScopeDef(
        "budgets:write",
        "danger",
        "Edit budgets",
        "Create or adjust budget envelopes. Loosening a budget is a finance decision.",
    ),
    # --- additions (task c19f2f63, enabler B): cover the MCP domains the coarse
    # v1 catalog missed (email, search, annotations, knowledge graph, workflows
    # read, agent-runs/dispatch/executors read+write) and split off the
    # destructive/credit-spending edges so a writer is not implicitly a destroyer
    # or a spender (least-privilege). Category drives the DEFAULT_SCOPES opt-out:
    # read/write are on by default, danger is opt-in. ---
    # read
    ScopeDef(
        "search:read",
        "read",
        "Search",
        "Run unified and semantic search across tasks, notes and memory, including "
        "graph-based context retrieval. Read-only.",
    ),
    # Added in the taxonomy review (task c19f2f63): the only WRITE in the search
    # family. Recording a click was riding search:read, so a read-only assistant
    # could skew ranking.
    ScopeDef(
        "search:write",
        "write",
        "Tune search",
        "Record which search result was clicked, feeding recall/ranking tuning. "
        "Does not read or modify your data.",
    ),
    ScopeDef(
        "email:read",
        "read",
        "Read email",
        "List and read email accounts, ingested messages, threads and drafted replies.",
    ),
    ScopeDef(
        "annotations:read",
        "read",
        "Read annotations",
        "Read inline comments and suggestions on notes and task descriptions.",
    ),
    ScopeDef(
        "kg:read",
        "read",
        "Read knowledge graph",
        "Query knowledge-graph entities and their relationship facts extracted from notes.",
    ),
    ScopeDef(
        "workflows:read",
        "read",
        "Read workflows",
        "View workflow definitions, their ordered states and allowed transitions.",
    ),
    ScopeDef(
        "agent_runs:read",
        "read",
        "Read agent runs",
        "View agent-run status, history, steps, credits spent and produced artifacts.",
    ),
    ScopeDef(
        "dispatch:read",
        "read",
        "Read dispatch queue",
        "View the agent-dispatch/approval queue, statuses and projected credit costs.",
    ),
    ScopeDef(
        "executors:read",
        "read",
        "Read executors",
        "List workspace executors (human members and LLM agents) and their capacity.",
    ),
    # write
    ScopeDef(
        "email:write",
        "write",
        "Manage email",
        "Register or configure email accounts, set default tags, sync inboxes, and "
        "queue or discard AI draft replies. Does not send mail.",
    ),
    ScopeDef(
        "annotations:write",
        "write",
        "Write annotations",
        "Add, edit, assign, resolve, reopen or withdraw inline comments and "
        "suggestions. Does not rewrite the underlying document body.",
    ),
    ScopeDef(
        "schedule:write",
        "write",
        "Write schedule",
        "Write scheduler pins/constraints on tasks and recompute the derived "
        "schedule. Does not spend credits.",
    ),
    # Added in the taxonomy review (task c19f2f63): four read-only routes (list
    # notifications, preferences, the web-push key, a task's reminders) were
    # riding notifications:write, i.e. reading cost more scope than writing.
    ScopeDef(
        "notifications:read",
        "read",
        "Read notifications",
        "Read in-app notifications, per-channel preferences, and the web-push public key.",
    ),
    ScopeDef(
        "notifications:write",
        "write",
        "Manage notifications",
        "Set per-channel notification preferences and enqueue due-date reminders. "
        "Internal only (see 'Send notifications externally').",
    ),
    ScopeDef(
        "agent_runs:write",
        "write",
        "Control agent runs",
        "Request cooperative cancellation of a running agent. Does not start runs "
        "or spend credits.",
    ),
    ScopeDef(
        "dispatch:write",
        "write",
        "Deny dispatch requests",
        "Deny pending agent-dispatch requests. Never starts a run or spends credits.",
    ),
    ScopeDef(
        "executors:write",
        "write",
        "Manage executors",
        "Create, update and delete executors (capacity, credit budgets, capability "
        "tags). Deletion can make LLM tasks unassignable.",
    ),
    # danger
    ScopeDef(
        "email:send",
        "danger",
        "Send email",
        "Send mail from a connected account (send, reply in-thread, approve-and-send "
        "drafts). Messages leave your boundary and cannot be recalled.",
    ),
    ScopeDef(
        "memory:delete",
        "danger",
        "Delete or erase memories",
        "Hard-delete a memory blob (cascades to tags/sources/vector) or run GDPR "
        "erasure by provenance. Split from memory:write so a writer cannot destroy.",
    ),
    ScopeDef(
        "memory:admin",
        "danger",
        "Manage memory channels",
        "Create, rename, enable/disable or delete the workspace's memory channels "
        "(the channel vocabulary that files and routes every memory).",
    ),
    ScopeDef(
        "ai:generate",
        "danger",
        "Spend LLM credits (synthesis)",
        "Run metered LLM synthesis/extraction (garden distillation, seasonal "
        "summaries, knowledge-graph extraction) that spends the credit budget. "
        "Distinct from agent_runs:start.",
    ),
    ScopeDef(
        "billing:write",
        "danger",
        "Write billing",
        "Grant or meter LLM credits (money movement against the org balance) and "
        "create or update model rate cards. Read-only by default.",
    ),
    ScopeDef(
        "notifications:send",
        "danger",
        "Send notifications externally",
        "Force-send pending notifications to external channels (email/push). An "
        "irreversible external side effect.",
    ),
)


# Default scope set for a freshly-minted assistant. Reads-only, least-privilege
# posture (taxonomy review, task c19f2f63): a new assistant can observe but not
# mutate; writes and danger scopes are both opt-in, granted deliberately at mint
# (a read-only researcher vs a write-scoped editor). The SPA picker mirrors this
# default (web/src/components/AiAssistantsSettings.tsx). Existing assistants are
# unaffected: their scope is stored per-row.
DEFAULT_SCOPES: tuple[str, ...] = tuple(s.key for s in SCOPE_CATALOG if s.category == "read")


VALID_SCOPE_KEYS: frozenset[str] = frozenset(s.key for s in SCOPE_CATALOG)


# The keys a member may delegate to a credential of their OWN, without an
# owner's approval, and the only shape of credential that may reach every
# workspace its holder belongs to.
#
# Minting a long-lived bearer credential is owner-gated because such a
# secret outlives the session that created it and lives wherever the
# holder put it. That threshold is right for a general MCP assistant,
# whose default is close to everything. It is wrong for the browser
# panel, which the product tells every reader to install and which can do
# a fixed, narrow subset of what its holder can already do -- and the two
# statements have contradicted each other in the product for as long as
# the page has existed: the page said "installing this is not an
# administrative act" and the server refused anyone but the owner.
#
# The threshold therefore follows the CAPABILITY, not the label. A
# request for these keys and no others is self-service; a request for one
# key more is an assistant, and owner-gated as before. Nothing here can
# be gamed by a caller: asking for more moves the threshold up, and the
# provider string, which the caller does choose, decides nothing.
#
# This is a ceiling, never a grant. Every operation is still authorized
# against the holder's own role in the workspace named by the request, so
# a credential whose holder is a guest somewhere acts as a guest there.
#
# One key here is catalogued ``danger``: ``attachments:write``, because
# uploaded bytes leave the workspace boundary when they are read back. It
# is also the whole of "file the page you are on", which is the
# capability the panel exists for. The other danger keys -- the ones that
# spend credits or destroy data -- stay owner-granted, and a test pins
# that this list holds exactly the one exception.
SELF_SERVICE_SCOPES: frozenset[str] = frozenset(
    {
        "tasks:read",
        "tasks:write",
        "tasks:state",
        "notes:read",
        "notes:write",
        "tags:read",
        "tags:assign",
        "workflows:read",
        "search:read",
        "search:write",
        "attachments:write",
    }
)


# What the browser extension asks for, and nothing beyond it.
#
# A SUBSET of SELF_SERVICE_SCOPES, never an alias for it, and the two are
# different questions: that one is a policy ceiling ("what may a member
# grant themselves"), this one is a request ("what does this surface
# use"). Aliased, the extension would silently widen every time the
# ceiling did. The subset relation is asserted in
# core/tests/test_extension_scopes.py rather than assumed here.
#
# Two keys are deliberately ABSENT and the absence is the design:
# ``workflows:write`` would let it delete the state machine every task in
# the workspace runs on (advancing one task is ``tasks:state``), and
# ``tags:write`` would let it invent, rename and rescope the taxonomy
# (filing into a client or project that already exists is ``tags:assign``).
EXTENSION_SCOPES: tuple[str, ...] = (
    "tasks:read",
    "tasks:write",
    "tasks:state",
    "notes:read",
    "notes:write",
    "tags:read",
    "tags:assign",
    "workflows:read",
    "search:read",
    "search:write",
    "attachments:write",
)

# What a credential minted for the extension carries, so the app can list
# "connected browsers" without guessing which rows are which.
EXTENSION_PROVIDER = "mycelium-extension"


__all__ = [
    "DEFAULT_SCOPES",
    "EXTENSION_PROVIDER",
    "EXTENSION_SCOPES",
    "SCOPE_CATALOG",
    "SELF_SERVICE_SCOPES",
    "VALID_SCOPE_KEYS",
    "Category",
    "ScopeDef",
]
