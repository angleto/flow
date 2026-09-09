# Architecture Decision Records

Each ADR records a non-obvious decision: context, decision,
consequences, rejected alternatives. Several decisions here correct the
naive choice that surfaced in critical review: they must not be
regressed.

Format: Status, Context, Decision, Consequences, Alternatives rejected.

## Index

- [0001 Architecture: Python monorepo, single service layer](0001-architecture-monorepo-python-service-layer.md)
- [0002 Multi-tenant: optimistic concurrency, mandatory RLS](0002-multi-tenant-optimistic-concurrency-rls.md)
- [0003 Unified tag with typed satellite profiles](0003-tag-unified-typed-satellite-profiles.md)
- [0004 Deterministic scheduler, not RCPSP](0004-deterministic-scheduler.md)
- [0005 Hierarchical memory on pgvector, hybrid RRF retrieval](0005-hierarchical-memory-pgvector-hybrid-rrf.md)
- [0006 At-rest encryption at the volume level](0006-at-rest-volume-encryption.md)
- [0007 Hard memory isolation per (org, project)](0007-memory-hard-isolation-org-project.md)
- [0008 No-ubiquity: events entity](0008-no-ubiquity-events.md) — superseded by addendum: appointments unified onto `tasks` (mig 0094 / 0095 / 0096 / 0097); the legacy events table is gone
- [0009 Invoice immutability, soft-delete carve-out](0009-invoice-immutability.md)
- [0010 Conservation: free AdE service](0010-conservation-ade-free-service.md)
- [0011 SDI: intermediary/mandate model, v1 B2B/B2C](0011-sdi-intermediary-mandate-v1-b2b.md) — revised by [0053](0053-transmitter-not-emitter.md): the mandate is to TRANSMIT, so `TerzoIntermediarioOSoggettoEmittente` / `SoggettoEmittente=TZ` are no longer emitted; `IdTrasmittente` stands
- [0012 LLM/Embedder abstraction, reuse the bitvision pattern](0012-llm-embedder-abstraction.md)
- [0013 Planning advisory layer, deterministic core](0013-planning-advisory-layer.md)
- [0014 Personal domain and budget envelope](0014-personal-domain-budgets.md)
- [0015 RLS: two Postgres roles and SECURITY DEFINER provisioning](0015-rls-two-role-and-provisioning.md)
- [0016 Memory: human-like tiering and RAG-pattern alignment](0016-memory-human-tiering-and-rag-alignment.md)
- [0017 English-only project language; i18n-ready message catalog](0017-english-only-i18n-message-catalog.md)
- [0018 Archive backup target (dual copy), distinct from legal conservation](0018-archive-backup-target.md)
- [0019 Metering, credits wallet, rate cards, enforcement](0019-metering-credits-billing.md)
- [0020 Voice notes and conversational capture](0020-voice-notes-conversational-capture.md)
- [0021 Command/intent layer (natural language to deterministic actions)](0021-command-intent-layer.md)
- [0022 Hands-free activation (headphone button): native/OS-assistant](0022-handsfree-activation-native.md)
- [0023 Email connector abstraction and idempotent sync](0023-email-connector-abstraction.md)
- [0024 "Workspace": user-facing name of the tenant; personal-first](0024-workspace-user-facing-tenant-name.md)
- [0025 Resource-aware scheduling and human/LLM work orchestration](0025-work-orchestration-resource-scheduling.md)
- [0026 Telegram as an in-process LLM assistant channel](0026-telegram-llm-assistant-channel.md)
- [0027 Adjudication framework for multi-agent convergence](0027-adjudication-framework.md)
- [0028 Identity-first addressing and explicit task ownership](0028-identity-first-addressing.md)
- [0029 Note garden ecosystem and typed note/task relations](0029-note-garden-ecosystem.md)
- [0030 Embedding model migration (dual-column)](0030-embedding-model-migration.md)
- [0031 Mindmap layout and edge weights](0031-mindmap-layout-edge-weights.md)
- [0032 `garden_classify(node_id)`: the proposal engine (incl. automatic maturity promotion)](0032-garden-classify-api.md)
- [0033 Anti-monoculture safeguards in `garden_classify`](0033-anti-monoculture-suggestions.md)
- [0034 Humus policy in the LLM walk](0034-humus-policy-walk-llm.md)
- [0035 Garden health sensors dashboard](0035-garden-sensors-dashboard.md)
- [0036 Event bus for multi-agent coordination on the graph](0036-agent-event-bus.md)
- [0037 Online learning loop on garden suggestions](0037-online-learning-loop.md)
- [0038 UUID-prefix entity resolver (`/lookup/{prefix}`)](0038-uuid-prefix-resolver.md)
- [0039 Fungal decomposition pipeline (humus producer)](0039-fungal-decomposition-pipeline.md)
- [0040 Mycelial 4-verb note-note link model](0040-mycelial-link-model.md) — revises ADR-0029 D3
- [0041 Autonomous retention spares the originals](0041-autonomous-retention-spares-originals.md)
- [0042 Tasks as graph nodes + complete auto-classify-on-ingest](0042-tasks-as-graph-nodes-and-complete-autoclassify.md)
- [0043 Human-gated review state for AI-generated nodes](0043-human-gated-review-state-for-ai-generated-nodes.md)
- [0044 Temporal knowledge graph (Track B)](0044-temporal-knowledge-graph.md)
- [0045 Issuer-scoped API keys, public Invoice API, MCP invoice tools](0045-issuer-scoped-api-keys.md)
- [0046 Two-phase durable invoice transmit (lost-ACK safety)](0046-two-phase-durable-transmit.md)
- [0047 Signed outbound webhooks on invoice state changes](0047-signed-invoice-webhooks.md)
- [0048 Fuel-table retention: pruning is hygiene, not metabolism](0048-fuel-table-retention.md)
- [0049 Working memory is delegated to the calling agent](0049-working-memory-delegated-to-the-caller.md)
- [0050 Structural tag cardinality on tasks and notes](0050-structural-tag-cardinality.md) — revises ADR-0003 (silent on cardinality)
- [0051 Inbound payment connectors (provider webhooks -> FatturaPA)](0051-inbound-payment-connectors.md) — revised by [0054](0054-what-may-become-a-causale.md): a provider's free text never becomes a `Causale`
- [0052 Workflow interchange document (JSON, no database identity)](0052-workflow-interchange-document.md)
- [0053 Mycelium is the soggetto trasmittente, never the soggetto emittente](0053-transmitter-not-emitter.md)
- [0054 What may become a Causale, and where the tracciato's charset is enforced](0054-what-may-become-a-causale.md)
- [0055 Two views of one document, not two documents](0055-two-views-of-one-document.md) — revises the single-surface editor decision in `0228012`
- [0056 An indexing class on the row, not a read boundary](0056-index-scope-opt-out-from-automatic-indexing.md)
- [0057 The browser is a fourth surface, and it holds a scoped credential](0057-the-browser-is-a-fourth-surface.md)
- [0058 Refuse to serve behind the schema, serve ahead of it](0058-refuse-to-serve-behind-the-schema.md)
