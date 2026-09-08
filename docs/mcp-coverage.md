# MCP coverage

The MCP surface is the agent-facing control plane, **co-equal to the REST
API** (docs/adr/0001): `mcp/` is a thin adapter over `core/`, so every
capability here has a REST sibling and vice-versa. A GUI- or REST-only
capability is a genuine gap, not an asymmetry by design.

Over HTTP the surface is the three-meta-tool **dynamic-toolset gateway**
(`search_tools` → `describe_tools` → `execute_tool`, see
`mcp/src/mycelium_mcp/gateway.py`): the LLM discovers tools semantically
instead of carrying every input schema per turn. The legacy stdio
entrypoint (`mcp/src/mycelium_mcp/main.py`) still exposes the concrete tools
directly, and the test suite imports the registry the same way.

**This document is half generated, half curated.** The tool inventory
between the markers is emitted from the live registry by
`scripts/gen_mcp_coverage.py` (run `make mcp-coverage` to refresh); CI fails
if it drifts (`make mcp-coverage-check`). The decision table, scope model
and gap notes around it are hand-written — they encode design intent the
registry cannot.

## Discovery decision table

Pick the tool by the **shape of the question**, not by enumerating a list
and filtering client-side. Each row routes one discovery need to its
canonical tool; the notes give the load-bearing parameter.

| I need… | Tool | Parameter |
|---|---|---|
| tasks in a given workflow state | `list_tasks` | `state_id=` |
| tasks by tag / project | `list_tasks` | `tag_id=` (org-wide unless project-scoped) |
| tasks by due / start / updated window | `list_tasks` | `due_before` / `due_after` / `start_after` / `updated_since` |
| tasks assigned to / owned by someone | `list_tasks` | `assignee_handles=` / `owner_handles=` |
| ranked free-text across tasks + notes + memory | `search` | `kinds=` to narrow |
| notes by free-text or title | `list_notes` | server-side `q=` over the whole corpus |
| “what should I do now”, time-boxed plan | `what_can_i_do_now` | deterministic advisory planner |
| errands near a place | `errands` | location-aware task slice |
| an entity from a short id prefix | `resolve_prefix` | ADR-0038 uuid-prefix → entity |
| recall from long-term memory (humus) | `memory_search` | hybrid retrieval over `memory_blobs` |
| a note’s graph neighbourhood | `graph_focus_context` | personalized-PageRank reading set |
| comments / suggestions on a doc | `list_annotations` | `doc_kind` + `doc_id`, `include_resolved=` |

The old “`list_tasks` filters by `state_id` only — use the SPA for
tag/date/text” gap is **closed**: `list_tasks` now takes `tag_id`,
free-text `q`, the due/start/updated date windows and assignee/owner
handles (see the generated `tasks` section below). An agent never needs to
over-fetch the org table and filter in its head.

<!-- BEGIN GENERATED: mcp tool inventory (scripts/gen_mcp_coverage.py) -->
**275 tools across 14 domains.** This inventory is generated from the live registry by `scripts/gen_mcp_coverage.py` — do not edit by hand; run `make mcp-coverage` to refresh. The one-line summary is each tool's first docstring line, so it cannot drift from the code.

### search (3)

| Tool | Summary |
|---|---|
| `graph_focus_context` | PPR-seeded reading set around a seed note: the relevant subgraph and |
| `memory_search` | Hybrid RRF retrieval within the (org, project) boundary. |
| `search` | Unified search across tasks/notes/blobs; the TASK branch is org-wide |

### identity (1)

| Tool | Summary |
|---|---|
| `whoami` | Session bootstrap: who am I, what may I do, and my durable memory here. |

### navigation (19)

| Tool | Summary |
|---|---|
| `accept_suggestion` | Accept a suggestion: splice the proposed text into the document |
| `add_dependency` | Add a typed task dependency (FS/SS/FF/SF). Cycles are rejected. |
| `add_task_relation` | Link two tasks as "related" (symmetric, bidirectional; NOT a |
| `graph` | Return the dependency DAG (nodes + edges) for a scope. |
| `graph_walk` | Traverse the note graph ("micelio") rooted at ``seed`` and return the |
| `link_notes` | Link two notes with a typed relation. The mycelial 4-verb model |
| `list_note_links` | Traverse a note's typed note↔note links. Returns |
| `list_note_task_links` | Traverse typed note↔task links. Pass ``task_id`` to get every note |
| `list_task_relations` | List symmetric "related task" links (a pure navigation aid, |
| `propose_suggestion` | Propose an edit to a markdown document: replace ``original_text`` |
| `propose_suggestion_instructions` | Recipe for a TOKEN-FREE suggestion: stream the PROPOSED replacement |
| `reject_suggestion` | Reject a pending suggestion; the document body is untouched. |
| `remove_dependency` | Remove a task dependency edge. |
| `remove_task_relation` | Remove a symmetric task relation by its id (from |
| `resolve_annotation` | Mark a comment thread resolved. |
| `resolve_prefix` | Resolve a short UUID prefix (the 8-char id in a roadmap note or a |
| `suggest_note_links` | Suggest the top-``k`` candidate notes to link from this note |
| `unlink_note_task` | Remove a typed note↔task link (``subject``, ``artifact``, |
| `unlink_notes` | Remove a typed note-to-note link. Returns ``removed`` true/false |

### time (11)

| Tool | Summary |
|---|---|
| `add_time_entry` | Add a manual time entry (provide ended_at or duration_seconds). |
| `delete_time_entry` | Delete a time entry. |
| `get_time_entry` | Read one time entry. |
| `list_running_timers` | Live timers (the serial one plus any parallel). Defaults to the |
| `pause_timer` | Pause a running timer without finalizing it: the one for |
| `resume_timer` | Resume a paused timer: the one for ``task_id`` if given, else the |
| `start_timer` | Start the live timer. Serial (default) replaces the running |
| `stop_timer` | Stop a running timer: the one for ``task_id`` if given, else the |
| `time_report` | Aggregated time report grouped by project\|client\|generic\|user\|task. |
| `time_report_by_task` | Per-task time aggregate for the caller: total/billable seconds |
| `update_time_entry` | Correct a time entry. Reassign with ``task_id`` (transitively |

### calendar (11)

| Tool | Summary |
|---|---|
| `add_holiday` | Add a holiday (ISO date) to a calendar; idempotent. |
| `create_calendar` | Create a working calendar (weekday -> [start, end] HH:MM windows). |
| `get_schedule` | Read one task's derived schedule row. |
| `list_calendars` | List the org working calendars. |
| `list_events` | Read the workspace coordination event bus (ADR-0036): the |
| `list_holidays` | List a calendar's holidays (ascending). |
| `list_schedule` | List derived schedule rows for a scope. |
| `recompute_schedule` | Deterministically recompute the schedule for a scope under a |
| `remove_holiday` | Remove a holiday (ISO date) from a calendar. |
| `set_task_schedule` | Write-back scheduler pins/constraints; survives recompute (FR-4). |
| `set_user_calendar` | Assign a calendar + daily capacity (hours) to a user. |

### orchestration (16)

| Tool | Summary |
|---|---|
| `agent_run_cancel` | Owner: request cancellation (cooperative kill switch the loop |
| `agent_run_get` | Read one agent run (member-level, RLS-scoped). |
| `agent_run_start` | Owner: run the agent on an already-dispatched ``llm_agent`` task |
| `agent_runs_list` | List agent runs (member-level), newest first, optionally filtered |
| `dispatch_approve` | Owner: approve a pending dispatch request, then immediately |
| `dispatch_deny` | Owner: deny an active dispatch request (never starts a run), with |
| `dispatch_notifications` | Send pending notifications (per-item fault isolation). |
| `dispatch_requests_list` | List the closed-loop dispatch queue (member-level, RLS-scoped), |
| `dispatch_tick` | Owner: run one closed-loop tick now (recompute -> admit -> gate |
| `executor_create` | Owner: create an executor (docs/adr/0025 P2). An ``llm_agent`` |
| `executor_delete` | Owner: delete an executor. Always allowed (including the seeded |
| `executor_update` | Owner: patch an executor (optimistic concurrency). ``kind`` and |
| `executors_list` | List the workspace executors (humans + llm agents). Member-level |
| `task_claim` | Member: claim an offered task (contract-net award) -> the caller |
| `task_handoffs_list` | List the coordination handoffs touching a task (incoming + |
| `task_offer` | Owner: announce a task to eligible members (contract-net call- |

### workflow (10)

| Tool | Summary |
|---|---|
| `create_workflow` | Create a workflow. ``states`` items: {name, ord?, is_initial?, |
| `delete_workflow` | Delete a workflow (refused for the default or if its states |
| `list_workflows` | List the org workflow definitions, each with its description. |
| `set_default_workflow` | Promote a workflow to the org default (keeps exactly one). |
| `set_project_workflow` | Set (or clear, with ``workflow_id=None``) a project's workflow |
| `set_task_state` | Transition a task to a workflow state (validated). |
| `task_workflow` | Which workflow governs THIS task, what its states mean, and where the |
| `update_workflow` | Rename + reconcile a workflow's states (match by ``id``; new |
| `workflow_states` | List a workflow's states (ordered). |
| `workflow_transitions` | List a workflow's allowed (from -> to) transitions. |

### memory (16)

| Tool | Summary |
|---|---|
| `memory_attach_tag` | Curate memory by hand: attach an existing tag to a memory blob |
| `memory_channel_create` | Create a custom memory channel. PLATFORM-ADMIN only (see the |
| `memory_channel_delete` | Delete a custom memory channel. PLATFORM-ADMIN only. A seeded |
| `memory_channel_update` | Rename and/or enable/disable a memory channel. PLATFORM-ADMIN |
| `memory_channels_list` | List the tenant's configured memory channels (seeds the |
| `memory_consolidate` | Merge same-(org, project) blobs into one concept, provenance |
| `memory_delete_blob` | Delete a single memory entry (hard delete; cascades to its |
| `memory_detach_tag` | Remove a tag from a memory blob (idempotent). Member-level. |
| `memory_erase` | GDPR erasure by provenance; cascades to embedding/sources. |
| `memory_get_blob` | Read one memory blob by id (with its tags), when you already hold |
| `memory_migrate` | Backfill missing dense embeddings for this workspace's memory blobs |
| `memory_migration_status` | Embedding-backfill coverage for the workspace: ``{total, migrated, |
| `memory_recompute_tiers` | Recompute the hot/warm/cold tier of EVERY memory blob in the |
| `memory_status` | Whether semantic (vector) retrieval is available, or memory is |
| `memory_write` | Write a memory blob (embedding metered when produced; degrades |
| `set_email_ingest_to_memory` | Toggle whether this account's synced (non-bulk) messages are |

### notes (60)

| Tool | Summary |
|---|---|
| `add_note_part` | Append a markdown block to a note (task 7070a456 Phase 3). |
| `add_note_part_instructions` | Recipe for a TOKEN-FREE note-part create: stream a local markdown |
| `add_note_tag` | Attach a free-form tag (generic / memory channel) to a note, |
| `add_task_participant` | Pin an identity to an appointment-task (the task must carry |
| `append_message` | Append a user message; returns the metered LLM reply turn. |
| `append_note_part` | Stream a LARGE markdown body into a note part in chunks, past the |
| `append_to_note` | Append ``text`` to ``note.summary`` (default) or ``note.transcript`` |
| `archive_note` | Archive (or unarchive with ``archived=False``) a note. |
| `clear_note_project` | Un-share a note: drop its project, KEEP its client. A projectless |
| `create_note` | Capture a note (voice\|text\|conversation). Unmetered. |
| `create_task_note` | TASK-side of the bidirectional Proposal A link: create a *fresh* |
| `delete_note` | Soft-delete a note (recoverable via restore_note). |
| `delete_note_part` | PURGE a part: irreversible, nothing to restore from afterwards. |
| `derive_task_from_note` | Create a task as a fruit of this note. The note stays alive |
| `distill_note` | Fungal decomposition (ADR-0034): distil a note's body into a |
| `email_to_note` | Create a note from a message (WS-3), with a back-link. The account's |
| `gdpr_erase_note` | GDPR hard-erasure of a note: cascades to its memory blobs (by note |
| `get_note` | Read one note. Includes the ordered ``parts[]`` (markdown blocks) |
| `get_note_part` | Read a single note part by id: random access into a long note's |
| `get_note_part_body_capability` | Mint a multi-use ``note_part_body:read`` capability and return a |
| `get_note_revision` | Single note-revision lookup; 404 if the id doesn't belong to |
| `get_or_create_task_note` | Open a task's "work note" (creating it on first call). Idempotent: |
| `get_text_block_capability` | Mint a multi-use ``<kind>:read`` capability for a task description |
| `invoice_credit_note` | Create a TD04 credit note linked to a transmitted invoice. |
| `list_distillation_candidates` | Are there distillations to do? (task 4995a32f). Distillation is graph |
| `list_email_messages` | List ingested messages, optionally filtered by account. |
| `list_note_parts` | Outline (table of contents) of a note's markdown parts in ``ord`` |
| `list_note_revisions` | Recovery-history timeline for a note, most recent first. |
| `list_notes` | List notes (newest first); for the @note picker. Returns the paginated |
| `list_task_participants` | List the additional identities pinned to an appointment-task |
| `list_trashed_note_parts` | Parts of a note that were trashed and can still be restored, most |
| `list_turns` | List the turns of a conversation note, in order. Returns the paginated |
| `merge_notes` | Fold the source note's parts into the target (task 7070a456 |
| `move_note_to_project` | Move a note into a project (share it). A note has at most one |
| `note_restore_source` | Fase P (task 561c6aca), "ripristina originale": given a humus ATOM, |
| `patch_note_part_body_capability` | Mint a single-use ``note_part_body:patch`` capability and return a |
| `patch_text_block_capability` | Mint a single-use ``<kind>:patch`` capability for a task description |
| `prepend_note_part` | Prepend markdown ``text`` to the FRONT of a note part without |
| `promote_note_to_task` | Transplant the note to a task. The note becomes read-only |
| `protect_note` | Mark a note as finished prose the distiller must never compact |
| `remove_note_tag` | Detach a free-form tag from a note. The client cannot be detached |
| `remove_task_participant` | Unpin an identity from an appointment-task. No-op if the |
| `reorder_note_parts` | Rewrite the entire ordering of a note's parts. ``part_ids`` |
| `replace_in_note_part` | Anchored edit inside ONE note part: replace occurrences of the |
| `restore_note` | Restore a soft-deleted note. |
| `restore_note_part` | Put a trashed part back into its note -- the inverse of |
| `restore_note_revision` | Revert a note's ``title`` / ``transcript`` to a past revision. |
| `run_command` | Deterministic canonical NL command (offline, unmetered). |
| `set_note_client` | Re-point a note at a client. A note has exactly one client, so |
| `set_note_maturity` | Manual override of a note's garden lifecycle (seed \| growing \| |
| `set_note_part_body_capability` | Like ``set_note_part_body_instructions`` but needs NO long-lived |
| `set_note_part_body_instructions` | Recipe for a TOKEN-FREE full-body REPLACE of an existing note part: |
| `set_text_block_capability` | Mint a single-use ``<kind>:write`` capability for a task description |
| `start_conversation_session` | Start a new conversation session (a conversation Note). |
| `start_task_on_note` | Watering: this task is the work of growing the note. Records a |
| `synthesize_speech` | TTS voice-out (metered per character). |
| `transcribe_note` | Run STT on a voice note (metered per audio-minute). |
| `trash_note_part` | DELETE one part of a note, restorably: the block leaves the note |
| `update_note` | Edit a note's title/body. A blank title is re-derived from the |
| `update_note_part` | Edit a part's body / lang. ``expected_version`` enforces |

### billing (19)

| Tool | Summary |
|---|---|
| `add_invoice_line` | Add a line to a draft invoice. |
| `budget_consumption` | Deterministic consumption vs residual for a budget. |
| `create_budget` | Create a budget envelope (period_kind: month\|quarter\|year\|custom). |
| `create_invoice` | Create a draft invoice. |
| `delete_budget` | Delete a budget envelope. |
| `get_invoice` | Read one invoice's status + data (state, SdI status, number, total, |
| `get_invoice_xml` | Return an invoice's FatturaPA XML inline (the frozen transmitted document, |
| `grant_credits` | Admin: top up credits (manual grant; v1 has no payment gateway). |
| `list_budgets` | List budget envelopes. |
| `list_invoice_lines` | List an invoice's lines with their AltriDatiGestionali blocks. |
| `list_invoices` | List invoices, newest first. Filter by ``client_tag_id`` (the recipient) |
| `list_rate_cards` | List the org rate cards. |
| `list_usage` | List recent metered usage records. |
| `meter_usage` | Idempotent metered debit (re-running the same operation_id does |
| `prioritize_within_budget` | Deterministic priority/value-density selection within a budget. |
| `set_invoice_line_altri_dati` | Set one draft line's AltriDatiGestionali (FatturaPA 2.2.1.16). |
| `transmit_invoice` | Validate, allocate the progressive number and transmit (channel |
| `update_budget` | Edit a budget envelope (only the given fields). |
| `upsert_rate_card` | Admin: create or update a model rate card. |

### email (13)

| Tool | Summary |
|---|---|
| `approve_email_draft` | Approve a drafted reply and SEND it in-thread (WS-4). ``body_text`` |
| `create_email_account` | Register an email account. The secret is stored encrypted and |
| `draft_email_reply` | On-demand (WS-4): queue a draft reply for one message (idempotent). |
| `email_thread` | Fetch a whole email thread (oldest first) by its provider thread id |
| `email_to_task` | Create a task from a message, with a source link. |
| `list_email_accounts` | List email accounts (no secrets), each with its default tags. |
| `list_email_drafts` | List drafted replies awaiting human review (WS-4). |
| `reject_email_draft` | Discard a drafted reply without sending (WS-4). |
| `reply_email` | Reply in-thread to an ingested message. |
| `send_email` | Send a message from an account. |
| `set_email_auto_draft_replies` | Toggle the autonomous responder for this account (WS-4). When enabled |
| `set_email_default_tags` | Replace this account's default tags (WS-1): a flat set |
| `sync_email_account` | Idempotently sync one account (known messages are skipped). |

### taxonomy (15)

| Tool | Summary |
|---|---|
| `add_task_tag` | Attach a free-form tag (generic / memory channel) to a task, |
| `create_client` | Create a client tag with its typed profile. |
| `create_project` | Create a project under a client. EVERY project has exactly one |
| `create_tag` | Create a free-form tag (kind: generic). A client or a project |
| `get_tag` | Read one tag (generic/client/project). |
| `list_clients` | List clients with their invoicing profile. |
| `list_projects` | List projects with their profile (client link, budget, color). |
| `list_tags` | List tags, optionally filtered by kind. |
| `move_task_to_project` | Move a task to another project. A task has exactly one project |
| `remove_task_tag` | Detach a free-form tag from a task. The client and the project |
| `set_tag_scope` | Replace a tag's scope with the given project/client tag ids |
| `set_task_client` | Re-point a task at a client. A task has exactly one client, so |
| `update_client` | Edit a client's name and invoicing card. Only the given fields |
| `update_project` | Edit a project. Only the given fields are changed; a project can |
| `update_tag` | Rename / recolor / set status of a tag (status: active\|archived). |

### tasks (46)

| Tool | Summary |
|---|---|
| `add_checklist_item` | Append a checklist item to a task ("alexa, add bread to the |
| `add_comment` | Add a comment to a task (a chronological work-diary entry on the |
| `add_comment_instructions` | Recipe for a TOKEN-FREE inline comment: stream the comment text |
| `append_to_comment` | Append ``text`` to the END of a comment without reading or |
| `append_to_task_description` | Append ``text`` to ``task.description`` without first reading the |
| `archive_task` | Archive (or unarchive with ``archived=False``) a task. |
| `assign_task` | Assign a user to a task (idempotent). |
| `check_item` | Mark a checklist item as done. Stamps ``done_at`` / ``done_by``. |
| `count_tasks` | Count tasks matching the SAME filters as ``list_tasks`` with one |
| `create_task` | Create a task. ``importance``/``urgency`` 1..5 Eisenhower |
| `delete_attachment` | Hard-delete an attachment (the stored blob goes with the row). |
| `delete_comment` | Soft-delete a comment -- the inverse of ``add_comment`` (author or |
| `delete_task` | Soft-delete a task (recoverable via restore_task). |
| `download_attachment_capability` | Mint ONE short-TTL capability token that downloads EVERY attachment |
| `get_comment` | Read ONE comment by id: random access into a long work diary |
| `get_comment_revision` | Single comment-revision lookup; 404 if the id does not belong to |
| `get_task` | Read one task with its full attribute set (for editing). Includes |
| `get_task_revision` | Single revision lookup; 404 if the id doesn't belong to this |
| `list_attachments` | List a note's OR a task's attachments (metadata only; the binary |
| `list_checklist` | List a task's checklist items, ordered by position. |
| `list_comment_revisions` | Recovery-history timeline for ONE comment, most recent first |
| `list_comments` | List a task's work-diary comments (doc_kind='task_description'), oldest |
| `list_task_revisions` | Recovery-history timeline for a task, most recent first. |
| `list_tasks` | List tasks: filter by state, tag, parent, assignee, owner, free-text |
| `list_trashed_comments` | Comments of a task that were deleted and can still be restored, |
| `prepend_to_comment` | Prepend ``text`` to the FRONT of a comment without reading or |
| `prepend_to_task_description` | Prepend ``text`` to the FRONT of ``task.description`` without |
| `purge_comment` | PURGE a comment: destroy the entry, its card state and its whole |
| `record_task_artifact` | The task produced (or updated) this note. Records an |
| `remove_item` | Remove an item from the task's checklist. |
| `replace_in_comment` | Anchored edit inside ONE comment: replace occurrences of the |
| `replace_in_task_description` | Anchored edit inside ``task.description``: replace occurrences of |
| `restore_comment` | Undo a ``delete_comment``: the comment (or withdrawn suggestion) |
| `restore_comment_revision` | Revert a comment's BODY to a past revision. Only the body is |
| `restore_task` | Restore a soft-deleted task. |
| `restore_task_revision` | Revert a task's restorable content fields to a past revision. |
| `set_task_assignee` | Set or clear who should work on the task (docs/adr/0028 D2). |
| `set_task_owner` | Reassign accountability for a task (docs/adr/0028 D2). The owner |
| `task_decline` | Member: decline an offered task (lightweight: notify the offerer |
| `unassign_task` | Unassign a user from a task. |
| `uncheck_item` | Un-tick a checklist item (clears ``done_at`` / ``done_by``). |
| `update_comment` | EDIT a comment: replace its whole body. Completes the |
| `update_task` | Edit task fields (only the given ones). ``priority`` is a |
| `upload_attachment` | Attach a file (base64-encoded bytes) to a note OR a task. |
| `upload_attachment_capability` | Mint ONE single-use capability token that UPLOADS a file to a note or |
| `upload_attachment_instructions` | Recipe for a TOKEN-FREE large-file upload (MRI, DICOM, PDF, ...). |

### misc (35)

| Tool | Summary |
|---|---|
| `add_annotation` | Add an inline comment to a markdown document. ``doc_kind`` is |
| `assign_annotation` | Assign an annotation to a workspace identity (``assignee_handle``: a |
| `billing_balance` | Current credit balance for the org. |
| `clear_done` | Remove every item already ticked done. Returns the count for |
| `count_annotations` | Count the annotations on a markdown document with ``COUNT`` queries |
| `create_recurrence` | Make a task recurring (mutually exclusive with dependencies). |
| `delete_annotation` | Soft-delete an annotation / withdraw a pending suggestion (author |
| `edit_annotation` | Edit an annotation's body (author or admin only). ``expected_version`` |
| `edit_annotation_body_instructions` | Recipe for a TOKEN-FREE replace of an annotation's body (a |
| `errands` | Place/context matcher: tasks for an errand run at a location and/or |
| `extract_cluster_pattern` | Phase-2 decomposition (ADR-0039): synthesise a ``pattern`` humus note |
| `garden_apply` | Apply or decline a ``garden_classify`` suggestion (ADR-0032 / |
| `garden_classify` | Proposal engine (ADR-0032): for a note, propose {tags, links, |
| `garden_review_approve` | Approve a proposed humus note (ADR-0043): it becomes effective and |
| `garden_review_pending` | Review inbox (ADR-0043): the workspace's AUTONOMOUSLY-generated humus |
| `garden_review_reject` | Reject a proposed humus note (ADR-0043): soft-delete it so a weak |
| `garden_review_rejected` | Review BIN (ADR-0043): the proposals a human DECLINED, most recently |
| `help` | Answer questions about Mycelium ITSELF -- its features, configuration and |
| `ingest_sdi_receipt` | Correlate an SdI receipt (RC/MC/NS/AT) by IdentificativoSdI. |
| `kg_entities` | Look up knowledge-graph entities whose name matches ``query`` (ADR-0044). |
| `kg_extract` | Extract a TEMPORAL KNOWLEDGE GRAPH (typed entities + relation facts) |
| `kg_neighbors` | Effective knowledge-graph facts around an entity id (ADR-0044). |
| `list_annotations` | List the annotations (comments + suggestions) on a markdown document, |
| `list_assigned_annotations` | The "assigned to me" inbox: annotations assigned to ``assignee_handle`` |
| `list_dependencies` | List task dependencies, newest first, optionally only those touching a |
| `list_issuer_profiles` | List the workspace's issuer profiles (the cedente VAT subjects) so an |
| `list_time_entries` | List time entries, optionally filtered by task or user. |
| `ping` | Liveness probe; returns the mycelium-core version. |
| `reopen_annotation` | Reopen a resolved comment thread. |
| `scan_reminders` | Enqueue idempotent due-date reminders for assignees. |
| `set_issuer_profile` | Create-or-update the default issuer profile, the invoice |
| `set_notification_pref` | Set a user's per-channel notification preference. |
| `spawn_due_recurrences` | Materialize due recurrences as independent task rows. |
| `synthesize_season` | Phase-2 decomposition (ADR-0039): synthesise a ``season`` humus note |
| `what_can_i_do_now` | Deterministic plan over the CALLER's OWN actionable tasks for a free |
<!-- END GENERATED -->

## Scope model

Every tool maps to a scope key in `SCOPE_CATALOG`
(`core/src/mycelium_core/mcp_scopes.py`) — the single source of truth, in
three buckets: **read**, **write**, **danger**. Enforcement is **live and
fail-closed**: a bound assistant is denied any tool whose key it does not
hold, and a tool absent from `TOOL_SCOPES` is denied outright rather than
allowed (`mcp/src/mycelium_mcp/server.py`, `_scope_permits`). A denied tool
is not merely un-callable — it is filtered out of `search_tools` /
`describe_tools`, so to the caller it reads as absent from the surface. The
default grant is reads-only; writes and danger keys are opt-in, granted by
a human at mint. The binding follows a **most-specific** rule: a tool that
mutates a task while touching a tag is `tasks:write`, not `tags:write`,
because the mutation lives on the task row.

Danger keys carry exactly the operations that **cannot be undone**. The
restorable counterpart of a destructive operation belongs on the ordinary
write key, and every destructive operation should have one: `delete_note`
(soft) vs the trash purge, `trash_note_part` / `restore_note_part` vs
`delete_note_part`, `delete_comment` / `restore_comment`. A destructive
verb with no inverse is the thing to fix, not the thing to fence.

## Service capabilities not (yet) on MCP

Whole subsystems that live in `core/src/mycelium_core/services/` with zero
MCP surface — most by design, a few real gaps. (Per-tool gaps are not
tracked here any more: the generated inventory above is the source of truth
for what exists, and chasing line numbers was the drift this doc kept
accumulating.)

- **Auth / MFA** (`auth.py`, `mfa.py`): signup, login, password reset, TOTP
  setup. Out of scope by definition — the agent arrives *with* a token.
- **Memberships** (`memberships.py`): list / add / remove members, role
  changes. A multi-user workspace would want a `members:*` family; the
  single-operator build does not need it yet.
- **Agent-token / AI-assistant lifecycle** (`agent_tokens.py`,
  `ai_assistants.py`): minting tokens and binding scopes configures *the MCP
  transport itself* — chicken-and-egg, kept REST-only.
- **Binary attachment upload / download**: multipart bytes do not survive
  JSON-RPC; the streaming gateway (REST + capability tokens) owns the bytes.
  MCP keeps metadata reads and the capability-token mint only.
- **SdI / passive-email side channels** (`sdi_*`, `email.access_token_for`):
  internal to the invoice and mail pipelines, not user-facing.
- **Admin-mode elevation**: `X-Admin-Mode` / sudo is an HTTP-header pattern;
  MCP gates the equivalent inline on the `users.is_admin` capability.

A capability that has a REST route *and* a clear agent use-case but no MCP
tool is a real gap — file it as a task rather than letting it rot in prose
here.
