"""Application configuration.

All variables are prefixed ``MYCELIUM_`` (12-factor, see
docs/non-functional-requirements.md). No hardcoded secrets.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYCELIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://mycelium_app:mycelium_app@localhost:5432/mycelium",
        description="Async URL (asyncpg). Runtime role mycelium_app (RLS).",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://mycelium:mycelium@localhost:5432/mycelium",
        description="Sync URL (psycopg) for Alembic. Owner role mycelium.",
    )

    # Async engine pooling. Explicit, NOT SQLAlchemy's implicit defaults
    # (pool_size 5 + max_overflow 10), so the footprint on a managed Postgres
    # instance shared with another project is a KNOWN number. See
    # ``mycelium_core.db.get_engine`` for the per-deployment arithmetic.
    #
    # The split matters more than the total. 15 concurrent connections per
    # process is what the implicit defaults already gave us, and it has held
    # in production; what was wrong was the SHAPE: only 5 were persistent, so
    # every burst above 5 paid brand-new TCP+TLS handshakes on the critical
    # path and then threw those connections away. Moving the same ceiling to
    # 10 + 5 makes ten of them persistent, which is the whole point.
    #
    # Do not shrink the total below the structural concurrency of the WORKER
    # process: it runs ~12 sweep loops (dispatch, reminders, webhooks, the
    # search/embedding backfills, ...) whose ticks overlap, and each opens its
    # own session. A ceiling under that turns normal operation into checkout
    # timeouts. The pools grow ON DEMAND, so the theoretical worst case
    # (every process at its ceiling simultaneously) is not the steady state.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    # How long a checkout waits for a free pooled connection before giving up.
    # Short on purpose: piling requests up behind an exhausted pool converts a
    # capacity problem into a latency problem that outlives the burst.
    db_pool_timeout_seconds: float = 10.0
    # Age-based recycling, in seconds. -1 is SQLAlchemy's "never recycle" and
    # is the shipped default ON PURPOSE.
    #
    # A recycle window is evaluated per connection AT CHECKOUT, and a pool's
    # connections tend to be opened together (one burst fills it in one go),
    # so their ages are correlated. After an idle gap longer than the window,
    # the next burst of N concurrent checkouts finds N expired connections and
    # re-opens all N AT ONCE: synchronized, not staggered. Under sparse
    # traffic that is a connect storm on a schedule -- and it throws away
    # connections that were perfectly alive.
    #
    # What actually covers a socket killed while idle (a NAT/conntrack idle
    # timeout, a server-side disconnect) is ``pool_pre_ping``, which is always
    # on: it tests each connection as it is handed out and replaces only the
    # ones that are really dead, one checkout at a time. ``pool_use_lifo``
    # then concentrates reuse on a small hot set, so the cold tail is rarely
    # touched at all.
    #
    # Set this only against a server or pooler that enforces a hard connection
    # lifetime, and mind the semantics: SQLAlchemy gates on ``_recycle > -1``,
    # so 0 recycles on essentially every checkout -- it does not mean "off".
    db_pool_recycle_seconds: int = -1
    # Per-attempt cap on the connection handshake itself (asyncpg's own
    # ``timeout``, whose default is 60 s). Without it an unreachable DB would
    # let one request hang for attempts x 60 s. A handshake to a managed
    # Postgres in the same region is milliseconds, so 5 s leaves two to three
    # orders of magnitude of headroom; the product
    # ``db_connect_max_attempts * db_connect_timeout_seconds`` (15 s) is the
    # ceiling on how long a request can sit at the connect boundary before it
    # fails, plus the negligible backoff sum below.
    db_connect_timeout_seconds: float = 5.0
    # Bounded retry around connection CREATION (see ``db._connect_with_retry``).
    # SQLAlchemy retries nothing there: a socket reset during the handshake
    # propagates straight out as a 500 on a request that never ran a query.
    # This absorbs a SINGLE-EVENT failure (one dropped handshake, a server a
    # moment away from accepting again); it is not a circuit breaker and does
    # not survive a server that refuses connections, which exhausts every
    # attempt (worst case the 15 s above) and surfaces the driver error.
    # Attempts are total (1 = no retry) and the backoff is full-jitter
    # exponential off this base, so N coroutines that failed together do not
    # re-storm together.
    db_connect_max_attempts: int = 3
    db_connect_retry_base_seconds: float = 0.1

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth / JWT. No default for the secret: it must be provided via
    # MYCELIUM_JWT_SECRET (fail-closed). In dev it comes from .env.
    # >=32 bytes: RFC 7518 requirement for HMAC-SHA256.
    jwt_secret: str = Field(min_length=32, description="JWT signing secret.")
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 3600
    # Refresh-token TTL (default 90d). Rotated on every /auth/refresh,
    # so a session that's actively used renews indefinitely; only an
    # idle gap beyond this kicks the user back to /login.
    refresh_token_ttl_seconds: int = 90 * 24 * 3600

    # App-level envelope for opaque secrets (OAuth tokens, IMAP
    # passwords; ADR-0006). Fernet key = urlsafe-b64 of 32 bytes (44
    # chars). No default: fail-closed, provided via MYCELIUM_SECRET_KEY.
    secret_key: str = Field(
        min_length=44,
        description="Fernet key for the opaque-secret envelope.",
    )

    # Dedicated pepper for the issuer-API-key keyed hash
    # (HMAC-SHA256(pepper, raw)); key-separated from ``secret_key`` so the two
    # blast radii are independent. No default: fail-closed, provided via
    # MYCELIUM_ISSUER_KEY_PEPPER. A DB-only dump is inert without it. Rotation
    # requires the dual-pepper window below plus a re-mint of every key (no raw
    # is stored); see docs/runbooks/issuer-key-pepper.md. Treat it as
    # long-lived secret-manager material.
    issuer_key_pepper: str = Field(
        min_length=32,
        description="Dedicated pepper for the issuer-API-key keyed hash.",
    )
    # Pepper-rotation window (task d3dd69c3): when set, ``authenticate`` also
    # probes the hash computed with the PREVIOUS pepper (second probe, only on
    # a current-pepper miss), so existing keys keep working while each one is
    # re-minted under the new pepper. Unset it once every key is re-minted.
    # A previous-pepper match emits a security event (rotation progress /
    # compromise telemetry). See docs/runbooks/issuer-key-pepper.md.
    issuer_key_pepper_previous: str | None = Field(
        default=None,
        min_length=32,
        description="Previous issuer-key pepper, valid during a rotation window.",
    )
    # A key silent for this many days that suddenly authenticates emits the
    # ``issuer_key.dormant_key_used`` security event (stolen-credential signal).
    issuer_key_dormant_days: int = 30
    # Trusted reverse-proxy hops (CIDR) in front of the backend, for the
    # issuer-key IP-allowlist SOURCE resolution (task d3dd69c3). The real
    # client is the rightmost X-Forwarded-For entry that is NOT one of these.
    # EMPTY (default) means the forwarding chain is not trusted, so a key that
    # HAS an allowlist fails CLOSED behind a proxy (the source is
    # unattributable) -- the allowlist is a security control only once this is
    # configured to the actual infra proxies AND the pod is reachable only via
    # them (NetworkPolicy). See docs/runbooks/issuer-key-pepper.md. Set as
    # JSON, e.g. MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES='["10.0.0.0/8"]'.
    issuer_key_trusted_proxies: list[str] = Field(default_factory=list)
    # Rotation grace: how long the PREVIOUS issuer-key secret keeps
    # authenticating after a rotate(). Default 0 = hard rotation; a per-call
    # value is clamped to the ceiling (compromise -> grace 0 / revoke).
    issuer_key_rotation_grace_seconds: int = 0
    issuer_key_rotation_grace_max_seconds: int = 3600
    # Max (and default) issuer-key lifetime. No never-expiring key.
    issuer_key_max_lifetime_seconds: int = 365 * 24 * 3600
    # Per-key rate limiter (shared Postgres fixed-window counter): the window and
    # the per-window budget per endpoint class. Irreversible/expensive verbs
    # (transmit, credit-note) get a strict budget; reads a looser one.
    issuer_key_rate_window_seconds: int = 60
    issuer_key_rate_limit_read: int = 300
    issuer_key_rate_limit_write: int = 60
    issuer_key_rate_limit_transmit: int = 30
    # Max drafts per POST /api/v1/invoices/batch (compose-only bulk). Bounds the
    # per-request work; the rate limit charges one "write" per element.
    issuer_batch_max_items: int = 200

    # Two-phase transmit (ADR-0046). The dispatch wall time is bounded
    # explicitly (httpx's 30 s timeout is PER PHASE -- connect/write/read --
    # so it does not bound the total); the lease must comfortably exceed
    # that bound so an expired lease provably implies no in-flight dispatch.
    # After expiry a crashed/unsettled dispatch becomes retryable (the retry
    # re-sends the SAME NomeFile; SdI dedupes by file name).
    sdi_dispatch_timeout_seconds: int = 120
    sdi_dispatch_lease_seconds: int = 300

    # Signed outbound webhooks on invoice state changes (task 2c23e955,
    # ADR-0047). Fail-closed: OFF by default, so an unconfigured deploy never
    # emits or delivers. Enabling requires a worker restart (the delivery loop
    # is registered at startup); events during a disabled window are dropped,
    # not back-filled. The signing secret is Fernet-enveloped by
    # ``secret_key`` (no new secret). See docs/runbooks + ADR-0047.
    webhooks_enabled: bool = False
    # Per-POST httpx timeout (scalar, all phases); the delivery is also wrapped
    # in an outer asyncio.timeout so an unresponsive receiver cannot hold the
    # worker. The lease MUST exceed both, so a crashed in-flight POST is
    # reclaimed only once it provably cannot still be running.
    webhook_delivery_timeout_seconds: int = 10
    webhook_delivery_lease_seconds: int = 120
    webhook_max_attempts: int = 12
    webhook_backoff_base_seconds: int = 30
    webhook_backoff_cap_seconds: int = 3600
    webhook_poll_interval_seconds: int = 15
    # Retention: a delivered/dead delivery row is purged after this (its
    # payload_snapshot carries cessionario data; the invoice itself is the
    # durable fiscal record).
    webhook_delivery_retention_days: int = 90

    # Inbound payment connectors: provider events -> FatturaPA (ADR-0051).
    # Fail-closed like the outbound side: OFF by default, so an unconfigured
    # deploy neither accepts a provider webhook nor runs the processing loop.
    # Enabling requires a worker restart (the loop is registered at startup).
    # No new secret: the signing-secret envelope is keyed by ``secret_key`` and
    # the optional ingress API key is peppered with ``issuer_key_pepper``.
    # ON by default, unlike its outbound sibling ``webhooks_enabled``. The
    # fail-closed guarantee this subsystem needs does not live here: a connector
    # is created ``enabled = false``, it cannot exist without the provider's own
    # signing secret, and what it may do is decided per issuer profile by
    # ``invoice_mode`` / ``credit_note_mode``. A fleet-wide switch on top of
    # those adds no safety -- nothing can be emitted without a connector row
    # somebody deliberately created and armed -- while costing a worker restart
    # to flip and making a correctly-configured connector silently inert.
    # Turning it OFF remains the kill switch: the ingress 404s and the worker
    # loop is not registered.
    payment_connectors_enabled: bool = True
    # Origin the connector's public webhook URL is advertised under, so the
    # operator can copy it straight into the provider's dashboard. Falls back to
    # frontend_base_url (SPA and API share a host in the OSS deploy); production
    # overrides it with the API origin.
    payment_connector_base_url: str = ""
    # Replay window on the signed timestamp bound into the MAC. 300 s is
    # Stripe's own default and is applied symmetrically (a far-future timestamp
    # is refused too, or a captured request would replay forever).
    payment_connector_tolerance_seconds: int = 300
    # Hard cap on an accepted body. The MAC is computed over the raw bytes, so
    # this bounds the work an unauthenticated caller can force before the
    # signature is even checked.
    payment_connector_max_body_bytes: int = 1_048_576
    payment_connector_poll_interval_seconds: int = 10
    # MUST exceed sdi_dispatch_timeout_seconds + sdi_dispatch_lease_seconds:
    # an event whose lease expires is re-claimed, and re-claiming one whose SdI
    # dispatch could still be in flight is exactly the double-filing this
    # subsystem exists to avoid.
    payment_connector_lease_seconds: int = 600
    payment_connector_max_attempts: int = 10
    payment_connector_backoff_base_seconds: int = 15
    payment_connector_backoff_cap_seconds: int = 3600
    payment_connector_batch: int = 20
    # How long a rotated signing secret / ingress key keeps working, so a
    # rotation never drops a redelivery of an event signed with the old one.
    #: Refusals per connector per window before the ingress stops appending to
    #: the delivery ledger. The refusal itself never changes shape -- the caller
    #: still gets a 401 and learns nothing about a limit. Counts REFUSALS only, so a
    #: correctly signed burst is never throttled -- whoever can sign is the
    #: provider. Generous against real traffic (a busy merchant's webhook feed
    #: is a few events per second at most) and still a hard bound on what an
    #: unauthenticated caller who learned the URL can make us write.
    payment_connector_refusal_budget: int = 120
    payment_connector_refusal_window_seconds: int = 60
    payment_connector_secret_grace_hours: int = 24
    # Retention for terminal event rows. Long by default: these are the
    # provenance of fiscal documents, and the invoice outlives any sweep.
    # ``needs_attention`` and ``dead`` rows are never swept.
    payment_connector_event_retention_days: int = 730

    # Max size of a single note/task attachment. Stored as BYTEA in the
    # DB (no object store; co-tenant deploy), so the cap is deliberately
    # conservative. This is the DEFAULT when a workspace has not set its
    # own (admin-tunable) override in the settings bag; enforced
    # server-side in the attachments service before the bytes are
    # persisted. Override via MYCELIUM_ATTACHMENT_MAX_BYTES.
    attachment_max_bytes: int = 10 * 1024 * 1024
    # Hard ceiling for the per-workspace, admin-tunable buffered cap
    # above. The buffered path reads the WHOLE file into memory before it
    # is persisted, so the admin knob must not be unbounded (an oversize
    # value would OOM the worker). Ops sets the absolute max here; an
    # admin then tunes the live cap within [1, this] from the settings
    # page. Override via MYCELIUM_ATTACHMENT_MAX_BYTES_CEILING.
    attachment_max_bytes_ceiling: int = 100 * 1024 * 1024
    # Larger cap for the STREAMING upload path (the backend pipes the body
    # to S3 in chunks, never buffering the whole file nor exposing S3):
    # medical files (DICOM/MRI) can be hundreds of MB. Enforced
    # incrementally so an oversize body is aborted mid-stream. Override via
    # MYCELIUM_ATTACHMENT_STREAM_MAX_BYTES.
    attachment_stream_max_bytes: int = 512 * 1024 * 1024

    # Cap on the post-append byte size of note.transcript / note.summary /
    # task.description (task 4ac39ecf). The append helpers refuse a
    # write whose resulting body would exceed this. Override via
    # MYCELIUM_NOTE_BODY_MAX_BYTES (legacy update_note / SPA editors are
    # NOT gated by this -- they replace, not extend).
    note_body_max_bytes: int = 1 * 1024 * 1024

    # Cap on a POSTed unified-diff patch body (services/text_patch.py).
    # A full-replace diff carries both the old and the new body, so it can
    # legitimately exceed note_body_max_bytes; ~2x + headroom. The applier
    # ALSO caps the resulting body at note_body_max_bytes, so this only
    # bounds the transport size of the diff itself. Override via
    # MYCELIUM_NOTE_PATCH_MAX_BYTES.
    note_patch_max_bytes: int = 2 * 1024 * 1024 + 64 * 1024

    # Pluggable attachment storage backend (mirrors the LLM/embedder
    # seam). "pg" (default) keeps today's behaviour exactly: bytes live
    # in the attachments.data BYTEA column, atomic with the row, no
    # external dependency. "s3" offloads the bytes to an S3-compatible
    # object store (Scaleway Object Storage) and keeps only metadata +
    # a storage_key in the DB. Selected via MYCELIUM_ATTACHMENT_STORE.
    attachment_store: Literal["pg", "s3"] = "pg"
    # S3 (Scaleway is S3-compatible) target. Only consulted/required when
    # attachment_store == "s3" (a model validator fails closed below so a
    # half-configured prod is rejected at startup; "pg" needs none).
    attachment_s3_endpoint_url: str = ""
    attachment_s3_region: str = ""
    attachment_s3_bucket: str = ""
    attachment_s3_access_key_id: str = ""
    attachment_s3_secret_access_key: str = ""
    # Optional key namespace inside the bucket (e.g. "mycelium/attachments").
    attachment_s3_prefix: str = ""

    # Public self-service signup. Default True so OSS self-hosters and
    # the existing test-suite are unaffected; set MYCELIUM_ALLOW_SIGNUP=false
    # for a single-user prod (the admin is provisioned out-of-band by
    # `python -m mycelium_core.bootstrap_admin`, which calls the service
    # layer, not the gated HTTP endpoint).
    allow_signup: bool = True

    # System (transactional) mailer transport (W1b; ADR-0024). Pluggable
    # like attachment_store: empty host (default) = the LogMailer, so
    # OSS self-host + dev + the whole test-suite are unaffected (the
    # verification/reset link is in the body and is logged). Setting
    # MYCELIUM_SMTP_HOST (+ MYCELIUM_SMTP_FROM) switches to the real SmtpMailer
    # (stdlib smtplib over STARTTLS for Scaleway TEM, port 587). The
    # username/password are secret (k8s mycelium-smtp Secret) and may be
    # legitimately empty for an unauthenticated relay, so they are NOT
    # required; only host+from gate the SMTP transport (validated
    # fail-closed below, same spirit as attachment_store='s3').
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Auth hardening (W1b, ported from bitvision_phoenix; ADR-0024).
    require_email_verification: bool = False
    email_verification_ttl_seconds: int = 86400
    password_reset_ttl_minutes: int = 30
    # TOTP MFA.
    mfa_issuer: str = "Mycelium"
    require_mfa_for_admin: bool = False
    # Login lockout (failed attempts within the window lock the account).
    login_max_failures: int = 5
    login_lockout_seconds: int = 900
    # Base URL the verification/reset links point at (the SPA).
    frontend_base_url: str = "http://localhost:5173"

    # CORS. Comma-separated allowed origins for the browser SPA when it
    # is served from a different origin than the API (production splits
    # mycelium.xeno.garden → SPA and api.mycelium.xeno.garden → API). Empty disables
    # the middleware (same-origin dev/proxy needs no CORS).
    cors_origins: str = "http://localhost:5173"

    # Google OAuth 2.0 (Gmail + Google Calendar; epic #125 P1). All
    # empty by default so OSS self-hosters / dev / the test-suite are
    # unaffected; ``google_configured`` is True iff all three are set
    # and the router refuses to start an OAuth flow otherwise (same
    # fail-closed spirit as smtp_configured). The redirect URI must
    # match the one registered in the Google Cloud console exactly; it
    # points at our public callback (``/oauth/google/callback``).
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    # Tick interval for the periodic Google Calendar sync worker (epic
    # #125 P1). Modest by default (do not hammer Google's API);
    # per-subscription and exception-isolated, like the dispatch loop.
    google_calendar_sync_interval_seconds: int = 300

    # Tick interval + per-account fetch cap for the periodic email
    # connector sync worker (docs/adr/0023, FR-7). NOT gated on
    # ``google_configured``: a workspace may hold only IMAP/Proton
    # accounts; Gmail accounts fail per-account when OAuth is absent.
    # Modest by default (do not hammer the IMAP/Gmail endpoints);
    # per-account exception-isolated. MYCELIUM_EMAIL_SYNC_INTERVAL_SECONDS /
    # MYCELIUM_EMAIL_SYNC_FETCH_LIMIT.
    email_sync_interval_seconds: int = 300
    email_sync_fetch_limit: int = 50

    # Autonomous email responder (WS-4). When enabled, the periodic worker
    # drafts a reply for each new non-bulk message on an account that has
    # opted in (per-account ``auto_draft_replies``). The draft is WITHHELD:
    # it is stored on an ``email_responder_jobs`` row in state ``drafted``
    # and only SENT when a human approves it (the responder NEVER auto-sends
    # -- email is outward-facing/irreversible). Each draft is one metered LLM
    # call through the per-org provider seam (``resolve_llm``), so a free
    # local model costs nothing and a premium model debits credits exactly
    # like the rest of the fleet. OFF by default: with no caller enabled no
    # job is ever enqueued and behaviour is unchanged.
    # MYCELIUM_EMAIL_RESPONDER_ENABLED / MYCELIUM_EMAIL_RESPONDER_LOOP_INTERVAL_SECONDS.
    email_responder_enabled: bool = False
    email_responder_loop_interval_seconds: int = 60

    # Closed-loop dispatch worker (docs/adr/0025 P5). The periodic tick
    # interval, in seconds. Modest by default (do not hammer the
    # scheduler); per-workspace and exception-isolated. Configurable via
    # MYCELIUM_DISPATCH_LOOP_INTERVAL_SECONDS like the other worker knobs.
    dispatch_loop_interval_seconds: int = 60

    # Garden auto-maturity (docs/adr/0032): the worker auto-promotes
    # growing notes to ``mature`` when they clear the high-confidence tier
    # (central AND curated). Reversible (label-only, audited + a feedback
    # event). Per-workspace opt-out is a later knob; this global flag
    # disables it outright. MYCELIUM_GARDEN_AUTO_MATURE_ENABLED=false to turn off.
    garden_auto_mature_enabled: bool = True

    # Garden seasonal-rule worker loop (docs/adr/0029 P1 + tasks 44b4c212 /
    # d8664631). The single periodic sweep that runs the maturity
    # transitions (seed->growing->dormant) AND materialises the
    # garden-health (ADR-0035) + graph/betweenness (d8664631) snapshots.
    # OFF by default: the maturity sweep mutates note maturity on real
    # data, so a deployment activates it deliberately. Set
    # MYCELIUM_GARDEN_LOOP_ENABLED=true to schedule it. Auto-promotion
    # growing->mature stays independently gated by
    # ``garden_auto_mature_enabled`` above.
    garden_loop_enabled: bool = False

    # Memory tier recompute inside the garden sweep (task 09007016 / WS-D4;
    # ADR-0016: tier = latency, NOT retention). When set, each garden tick
    # also runs the per-workspace access-decay tier recompute, so cold blobs
    # are demoted (never deleted, always queryable) in autonomy instead of
    # only on an on-demand API/MCP call. OFF by default and gated by
    # ``garden_loop_enabled`` above (it rides the same sweep), so a
    # deployment opts in deliberately. MYCELIUM_GARDEN_TIER_RECOMPUTE_ENABLED=true.
    garden_tier_recompute_enabled: bool = False

    # Autonomous classify-on-ingest inside the garden sweep (task b8c60940 /
    # WS-D2, ADR-0032 P4). When set, each garden tick stamps not-yet-seen
    # notes with the structural Leiden community the graph snapshot already
    # computed for them + an auto_classified_at marker, so new nodes are
    # classified proactively instead of waiting for a human to open the
    # classify panel. Read-only (no tag/link/maturity auto-apply). OFF by
    # default and gated by ``garden_loop_enabled`` (rides the same sweep).
    # MYCELIUM_GARDEN_AUTOCLASSIFY_ENABLED=true.
    garden_autoclassify_enabled: bool = False

    # On-CREATE classification queue (task b8c60940 / WS-D2, ADR-0042 D5).
    # When set, create_note / create_task enqueue a classification_job in the
    # create's own transaction; the garden sweep drains it (classify_node +
    # cache the suggestions in precomputed_suggestions). This is the proactive
    # "classify new nodes at birth" path, distinct from the periodic
    # autoclassify sweep above. Read-only proposals; NEVER gates the node. OFF
    # by default; the drain rides the garden loop. For TASKS the enqueue also
    # requires ``garden_unified_task_graph_enabled`` (classify_node accepts a
    # task only then). MYCELIUM_GARDEN_AUTOCLASSIFY_ON_CREATION_ENABLED=true.
    garden_autoclassify_on_creation_enabled: bool = False

    # Online-learning prior decay inside the garden sweep (task 49d24048,
    # ADR-0037 "Time decay"). When set, each garden tick applies the nightly
    # geometric decay + consolidation to the per-user classification priors,
    # so stale preferences fade and neutral rows are pruned. ON by default
    # (it is part of the loop's correctness, like ``garden_auto_mature``) but
    # still gated by ``garden_loop_enabled`` -- it only runs when the sweep
    # runs. MYCELIUM_GARDEN_LEARNING_DECAY_ENABLED=false to disable.
    garden_learning_decay_enabled: bool = True

    # Tasks as first-class graph nodes + complete auto-classify (ADR-0042,
    # task b8c60940 / WS-D2). When set, ``garden_classify`` accepts TASK ids
    # (the engine is notes-only today) and the tag co-occurrence corpus spans
    # notes AND tasks; the unified graph (task cluster/link) is layered on
    # behind this same flag as ADR-0042 D1 lands. OFF by default so the
    # notes-only behaviour and the existing mindmap / clusters stay
    # byte-identical until a workspace opts in.
    # MYCELIUM_GARDEN_UNIFIED_TASK_GRAPH_ENABLED=true.
    garden_unified_task_graph_enabled: bool = False

    # Daily prior snapshot inside the garden sweep (task ea2156df, ADR-0037
    # "Snapshots and rollback"). When set, each garden tick checkpoints each
    # user's learned priors into classification_personal_prior_snapshot
    # (daily-idempotent), so POST /garden/learning/rollback is decay-aware
    # point-in-time and the drift bar chart has a baseline. Runs after the
    # decay step. ON by default (part of the loop's reversibility story) but
    # still gated by ``garden_loop_enabled``.
    # MYCELIUM_GARDEN_LEARNING_SNAPSHOT_ENABLED=false to disable.
    garden_learning_snapshot_enabled: bool = True

    # Co-activity edge materialisation inside the garden sweep (task
    # f0a15247, ADR-0031 w_coact). When set, each garden tick aggregates
    # the activity log into pairwise co-activity session counts
    # (note_coactivity), the third soft-OR source of the unified note edge
    # weight. Runs before the graph snapshot so the materialised centrality
    # reflects the fresh co-activity edges. ON by default (it is part of
    # the edge-weight's correctness) but still gated by
    # ``garden_loop_enabled`` -- it only runs when the sweep runs.
    # MYCELIUM_GARDEN_COACTIVITY_ENABLED=false to disable.
    garden_coactivity_enabled: bool = True

    # Search-informed edge-usage aggregation (Fase 2, task 561c6aca).
    # When set, each garden tick folds the windowed retrieval traces
    # into note_edge_usage pair counters, the FOURTH soft-OR source of
    # the unified note edge weight (and the link_direct candidate's
    # substrate). Same posture as co-activity above: runs before the
    # graph snapshot, ON by default, still gated by
    # ``garden_loop_enabled``.
    # MYCELIUM_GARDEN_EDGE_USAGE_ENABLED=false to disable.
    garden_edge_usage_enabled: bool = True

    # Distillation fidelity verify pass (task a44e72a4). When set,
    # ``decomposition.distill_note`` runs a SECOND metered LLM pass that keeps
    # only the claims of the draft distillation explicitly supported by the
    # source, dropping anything inferred/generalised/invented, before
    # persisting the humus. The grounding instruction in the distil prompt is
    # always on (free); this is the stronger, cost-doubling second opinion, so
    # it is opt-in (OFF by default, like the other risky/costly garden flags)
    # and is the recommended companion to autonomous distillation (4b786034).
    # MYCELIUM_DISTILL_VERIFY_PASS_ENABLED=true.
    distill_verify_pass_enabled: bool = False

    # Human-gated review state for AUTONOMOUSLY-generated nodes (ADR-0043,
    # task e87daff4). When set, a humus note generated by the autonomous,
    # unsolicited garden sweep (``autonomous=True``) is born
    # ``review_state='proposed'`` -- withheld from every retrieval/listing
    # surface until a human approves it via the review inbox; a reject
    # soft-deletes it so a weak summary never pollutes the corpus.
    # USER-initiated generations (MCP / SPA / on-demand, the default
    # ``autonomous=False``) are always effective, exactly as today.
    # ``origin_model_id`` is stamped on every synthesised note regardless of
    # this flag (pure transparency). OFF by default: with no autonomous
    # caller wired, no ``proposed`` note is ever created and behaviour is
    # byte-identical. MYCELIUM_GARDEN_REVIEW_GATE_ENABLED=true.
    garden_review_gate_enabled: bool = False

    # Reminders + notification-dispatch worker. One periodic tick scans
    # due reminders into pending Notifications (idempotent by
    # dedupe_key) and then dispatches all pending notifications through
    # the configured sender per channel pref. Per-workspace and
    # exception-isolated like the dispatch loop. Configurable via
    # MYCELIUM_REMINDERS_LOOP_INTERVAL_SECONDS. 60s is the floor for
    # minute-precision reminders on appointment tasks.
    reminders_loop_interval_seconds: int = 60

    # Task-search embedding backfill worker. Re-embeds task blobs whose
    # initial write timed out (model_id='none'); race-protected via the
    # pointer ``content_hash``. Per-workspace + exception-isolated like
    # the other loops. 60s is a low-cost default: the population is
    # bounded (only timed-out writes) and the worker quickly drains.
    task_search_backfill_interval_seconds: int = 60

    # Note-search pointer backfill worker. Indexes note parts that
    # pre-date the per-part index deploy (migration 0040) and never went
    # through the listener path, so old notes become searchable without a
    # manual edit. Keyword-only re-embedding is handled generically by the
    # embedding-migration worker, so this loop only needs the pointer
    # sweep. Same low-cost cadence as the task-search backfill.
    note_search_backfill_interval_seconds: int = 60

    # Default LOCAL embedder model (the rank-0 fallback, ``embedding``
    # column). bge-m3 emits ``embed_dims.EMBED_DIM`` natively.
    embed_model: str = "BAAI/bge-m3"
    # Does ``embed_model`` document Matryoshka (MRL) training? Only an MRL
    # checkpoint may be truncated to the fleet dim: its leading dimensions
    # are trained to stand alone, so the cut is the procedure the model was
    # built for. Truncating any other model still produces a vector of the
    # right shape and silently worse retrieval, which is the failure this
    # flag exists to refuse. Default false: the default model is natively
    # at the fleet dim and never truncated, so nothing is claimed on its
    # behalf, and a WIDER model refuses to load until someone who checked
    # the model card says so. Not inferable from the checkpoint: MRL is a
    # property of how it was trained, not of what it emits.
    embed_model_is_mrl: bool = False
    # Sequence window the LOCAL embedder is allowed to use, in tokens.
    # bge-m3 ships 8192; attention memory is quadratic in this number, so a
    # single long note part could allocate multiple GB and OOMKill the whole
    # worker process (it did, 2026-07-24, taking reminders/dispatch down with
    # it). 2048 keeps a whole note part in the window in the common case
    # while bounding the worst case; longer texts are truncated, exactly as
    # they already were past 8192. 0 = leave the model's own default.
    embedder_max_seq_tokens: int = 2048
    # Per-encode token budget = (longest text in the group) x (group size).
    # Bounds PEAK memory regardless of input shape: a fixed batch count does
    # not, because 32 titles and 32 note parts are two orders of magnitude
    # apart in activations. 16384 = 8 texts at the full 2048-token window,
    # or many more short ones.
    embedder_batch_token_budget: int = 16384
    # Embedding backfill worker (sweep-rate per workspace). Re-embeds
    # blobs whose vector is missing/stale (e.g. after a dim rebuild or a
    # per-org model swap). Per-workspace + exception-isolated, modest
    # default so a large workspace drains without saturating the API.
    embedding_migration_interval_seconds: int = 60

    # In-cluster open-model LLM for non-interactive labelling
    # (revision summaries, future tag/title suggestions). Wired via
    # ``ai_providers.set_llm_override`` at worker startup; the API
    # process can opt in the same way. Empty URL = no override, the
    # ``LocalLLM`` stub stays in place and the sweep is a no-op so
    # CI / dev / unconfigured deploys never hit the network.
    ollama_url: str = ""
    open_model: str = "llama3.2:3b"
    # Hosted LLM providers behind the same ``LLMProvider`` seam (task
    # 8afda4e7). These keys are the "on our key" credentials: an org with
    # a hosted provider but NO own key bills on ``CostBasis.our_key``
    # (provider_cost x RateCard.markup); an org that stores its OWN key
    # (Fernet-encrypted, per-org, migration 0026) bills on ``byok``. Empty
    # keys are fine -- resolve_provider falls back to the local seam.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"
    # Scaleway Generative APIs (EU/fr-par, OpenAI-compatible) on OUR key
    # (basis our_key). The resolver reuses ``OpenAILLM`` with this base_url
    # (or a per-org override stored on ``org_llm_provider.base_url``). An
    # org with its own Scaleway IAM secret key bills on ``byok`` (task
    # d2c60a83). Empty key => resolve_provider falls back to local.
    scaleway_api_key: str = ""
    scaleway_base_url: str = "https://api.scaleway.ai/v1"
    # Revision-summary worker (LLM-generated labels for the
    # recovery-history timeline). Cadence is slow because each
    # generation is a multi-second LLM call; the sweep is also bounded
    # by ``revisions_summary_batch`` per tick.
    revisions_summary_interval_seconds: int = 30
    revisions_summary_batch: int = 5

    # Cross-encoder reranker (task 27579d6a). Opt-in second-stage
    # scorer that re-orders the top-K from RRF using a cross-encoder
    # model (sees query+doc joined). Off by default because the local
    # model is ~1GB resident and adds latency to interactive search;
    # production toggles it on selectively. The per-call ``rerank=true``
    # body flag also enables it for a single search regardless of the
    # env default. The model id is the sentence-transformers
    # ``CrossEncoder`` identifier; the default targets
    # ``bge-reranker-v2-m3`` which is multilingual and pairs naturally
    # with bge-m3 dense embeddings.
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # Gate thresholds: skip the reranker on queries too short to
    # discriminate (one-word queries leave the cross-encoder little to
    # work with) or on candidate sets too small to be worth the cost.
    reranker_min_query_tokens: int = 3
    reranker_min_candidates: int = 5

    # Fase 0 of the search-informed graph (task 561c6aca): append-only
    # trace of the returned top-m per search (``retrieval_trace``
    # table), the raw signal the offline aggregation turns into
    # ``note_edge_usage`` counters. Cost is one INSERT per non-probe
    # search; flip off to shed even that on read-heavy deployments.
    # Probe traffic (the eval harness) is never traced regardless.
    retrieval_trace_enabled: bool = True

    # Fuel-path retention (ADR-0048, task 68052297). Historically the ONLY
    # ``retrieval_trace`` pruning lived inside the edge-usage fold, which
    # rides the default-off garden sweep -- so a stock deployment wrote
    # traces forever and never deleted one. Pruning is HYGIENE, not
    # metabolism: the dedicated ``fuel_retention`` worker job runs
    # unconditionally (like ``revisions_retention``), and it deletes only
    # rows the fold could never use again (the service floors the trace
    # window at ``EDGE_USAGE_WINDOW_DAYS``, so retention can be raised but
    # never undercut the aggregation window). ``search_clicks`` rides the
    # same job on its own, longer window (it feeds the recall sensor and a
    # future active-learning loop). ``activity_log`` is deliberately NOT
    # retained-away: it is the append-only audit spine (decision recorded
    # in ADR-0048).
    retrieval_trace_retention_days: int = 90
    search_click_retention_days: int = 365
    fuel_retention_interval_seconds: int = 86400

    # Humus retrieval branch (ADR-0034) master switch. Humus is the PARALLEL
    # source that late-fuses distilled/consolidated atoms into the focused walk
    # with a small boost + a 30% hard cap. Default ON = historical behaviour;
    # this is an operator kill-switch AND the lever the humus empirical gate
    # (task 4836a6cc / note 9a2adb4a) uses to A/B the branch's marginal value.
    # An explicit ``humus=`` arg to ``memory.retrieve`` overrides it per-call.
    humus_enabled: bool = True

    # Recovery-history retention worker (entity_revision). The sweep
    # has two parts: a coarsening pass that keeps 1 revision/day past
    # ``revisions_retain_full_days`` and 1 revision/week past
    # ``revisions_coarse_to_weekly_days``, and a hard-delete of
    # soft-deleted task/note rows older than
    # ``revisions_hard_delete_after_days`` (the cascade trigger then
    # purges their revisions). Cadence is daily by default because
    # the time-bucket thresholds are coarse; can be sped up in tests.
    revisions_retention_interval_seconds: int = 86_400
    revisions_retain_full_days: int = 30
    revisions_coarse_to_weekly_days: int = 365
    revisions_hard_delete_after_days: int = 90

    # Telegram bot integration (epic #125 P2). Empty defaults so OSS
    # self-host + tests are unaffected: the link router refuses to mint
    # codes when the bot is not configured, the webhook 404s on an empty
    # secret, and the Telegram NotificationSender records a failure
    # instead of trying to reach api.telegram.org. The HTTP client
    # Protocol allows a fake-in-test injection (same seam pattern as the
    # email connector / LLM provider). The bot token + webhook secret
    # are deploy secrets (env / k8s Secret); the bot username is public
    # (built into the t.me deep-link). The webhook base URL falls back
    # to frontend_base_url when not set, since the SPA + API typically
    # share a host in the OSS deploy; production override points at the
    # API origin.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_base_url: str = ""
    # Network timeout for Telegram Bot API calls (seconds). Short on
    # purpose: the webhook handler must reply quickly or Telegram retries.
    telegram_http_timeout_seconds: float = 10.0

    # Web Push (VAPID, RFC 8292). The public key is handed to the SPA (it
    # subscribes the browser's push manager with it); the private key signs
    # each push and is a secret; subject is a mailto:/https: contact the
    # push service may use. All three set => webpush dispatch is active;
    # unset => the channel fails closed per item (like an unconfigured bot)
    # and the SPA hides the subscribe affordance.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    # Conversational assistant (ADR-0026). When enabled, a free-text
    # Telegram message is handled by an in-process LLM agent that uses
    # Mycelium's tools (read/scoped-write on notes/tasks) and replies. Off by
    # default: OSS / dev / CI keep the safe "free-text -> hint" behavior
    # unless a model is configured. Spend follows the configured model
    # (free local Ollama costs nothing; a premium model debits credits via
    # the existing metering); ``assistant_credit_budget`` caps per-turn
    # spend (<= 0 means no cap) and ``assistant_max_steps`` bounds the
    # tool/think loop regardless of the script.
    assistant_enabled: bool = False
    assistant_max_steps: int = 8
    assistant_credit_budget: float = 0.0
    # Worker poll interval for the assistant job queue (ADR-0026 P3).
    assistant_loop_interval_seconds: int = 5

    # SdI electronic-invoice transmission (ADR-0011, FR-9). Default
    # "manual_export": Mycelium builds downloadable XML and the tenant submits
    # it (no SdI transit; AdE conservation not covered). "sdicoop":
    # transmit through Mycelium's single accredited channel -- the tenant's
    # identity is the whole FatturaPA payload, Mycelium's appears only in
    # IdTrasmittente and in the file name (requires a per-issuer SdiMandate,
    # whose scope is transmission; see ADR-0053 for why nothing of Mycelium's
    # goes in the document body). The intermediary identity + the mutual-TLS SOAP
    # transport below are required only when "sdicoop" is selected
    # (validated fail-closed, same spirit as smtp/s3). vat_number is the
    # accredited channel holder's P.IVA; cert/key/ca are PEM file paths
    # (deploy secrets).
    sdi_channel: Literal["manual_export", "sdicoop"] = "manual_export"
    sdi_intermediary_id_paese: str = "IT"
    # The SdICoop RiceviFile endpoint. ``sdi_endpoint_url`` is the legacy single
    # value (still honoured as a fallback). The two env-specific URLs below let
    # the active one be picked AT RUNTIME from the DB (system_settings.
    # sdi_environment) instead of an env-var redeploy: only the test<->prod URL
    # differs (the channel cert/key + trust bundle + intermediary are shared).
    sdi_endpoint_url: str = ""
    sdi_endpoint_url_test: str = ""
    sdi_endpoint_url_prod: str = ""
    sdi_client_cert: str = ""
    sdi_client_key: str = ""
    sdi_ca_bundle: str = ""
    # EsitoCommittente outbound (ADR-0011 v1.1): the NotificaEsitoCommittente
    # we generate as buyer carries an XMLDSig enveloped signature. The
    # signing key / cert are PEM bytes loaded from the deploy secret store;
    # tests inject ephemeral self-signed material at runtime. Empty (the
    # default) means "no EC outbound is wired"; the service raises on call.
    sdi_ec_signing_key_pem: str = ""
    sdi_ec_signing_cert_pem: str = ""

    # App
    env: str = "dev"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_attachment_store(self) -> Settings:
        """Fail closed: if the S3 backend is selected, every credential /
        target must be present. A half-configured prod is rejected at
        startup rather than failing on the first upload (same spirit as
        the no-default jwt_secret / secret_key fields)."""
        if self.attachment_store == "s3":
            missing = [
                name
                for name, value in (
                    ("MYCELIUM_ATTACHMENT_S3_ENDPOINT_URL", self.attachment_s3_endpoint_url),
                    ("MYCELIUM_ATTACHMENT_S3_REGION", self.attachment_s3_region),
                    ("MYCELIUM_ATTACHMENT_S3_BUCKET", self.attachment_s3_bucket),
                    ("MYCELIUM_ATTACHMENT_S3_ACCESS_KEY_ID", self.attachment_s3_access_key_id),
                    (
                        "MYCELIUM_ATTACHMENT_S3_SECRET_ACCESS_KEY",
                        self.attachment_s3_secret_access_key,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError("attachment_store='s3' requires: " + ", ".join(sorted(missing)))
        return self

    @model_validator(mode="after")
    def _validate_smtp(self) -> Settings:
        """Fail closed: configuring SMTP means configuring it fully. An
        empty MYCELIUM_SMTP_HOST is the safe dev/OSS default (LogMailer, no
        requirements). A non-empty host means the real SmtpMailer, which
        needs an envelope/From identity, so MYCELIUM_SMTP_FROM is required;
        a half-configured prod is rejected at startup rather than
        emitting mail with no From. Username/password may be empty (an
        unauthenticated relay is valid), so they are not required here."""
        if self.smtp_host and not self.smtp_from:
            raise ValueError(
                "MYCELIUM_SMTP_HOST is set but MYCELIUM_SMTP_FROM is required to enable SMTP"
            )
        return self

    @model_validator(mode="after")
    def _validate_sdi(self) -> Settings:
        """Fail closed: selecting the SdICoop channel means configuring the
        mutual-TLS transport. The CA bundle is optional (the system trust store
        is acceptable for SdI's cert).

        The trasmittente's fiscal code is NOT checked here any more: it lives in
        ``system_settings`` so an operator can correct it without a redeploy,
        which means a boot-time presence check would refuse to start a
        deployment that is configured perfectly well in the database. Presence
        is enforced where the value is used instead -- in ``invoice.transmit``,
        upstream of the number, the NomeFile and the frozen XML, so a missing
        one costs nothing durable."""
        if self.sdi_channel == "sdicoop":
            missing = [
                name
                for name, value in (
                    ("MYCELIUM_SDI_CLIENT_CERT", self.sdi_client_cert),
                    ("MYCELIUM_SDI_CLIENT_KEY", self.sdi_client_key),
                )
                if not value
            ]
            # At least one endpoint must be configured; the active one is chosen
            # at runtime (system_settings.sdi_environment) between the legacy
            # single URL and the env-specific test/prod URLs.
            if not (
                self.sdi_endpoint_url or self.sdi_endpoint_url_test or self.sdi_endpoint_url_prod
            ):
                missing.append("MYCELIUM_SDI_ENDPOINT_URL (or _TEST/_PROD)")
            if missing:
                raise ValueError("sdi_channel='sdicoop' requires: " + ", ".join(sorted(missing)))
        return self

    @property
    def google_configured(self) -> bool:
        """Google OAuth is active iff client_id, client_secret and
        redirect_uri are all set. Same fail-closed spirit as
        smtp_configured: the OAuth router rejects flows when False."""
        return bool(
            self.google_client_id and self.google_client_secret and self.google_redirect_uri
        )

    @property
    def telegram_configured(self) -> bool:
        """Telegram bot is active iff bot token, bot username and the
        webhook secret are all set. Same fail-closed spirit as
        smtp_configured / google_configured: the router refuses to mint
        link codes and the webhook 404s when False. The username is
        required because the deep-link URL (``https://t.me/<bot>?start=
        <code>``) cannot be built without it."""
        return bool(
            self.telegram_bot_token and self.telegram_bot_username and self.telegram_webhook_secret
        )

    @property
    def telegram_send_configured(self) -> bool:
        """Telegram *outbound send* needs only the bot token: ``sendMessage``
        builds its URL from the token alone (see ``HttpxTelegramApi``). Unlike
        ``telegram_configured`` -- which additionally requires the username
        (for deep links) and the webhook secret (for inbound verification) --
        this gates the notification dispatch path, so a deploy that has the
        token but not the webhook half can still deliver reminders. The worker
        is exactly that case: it sends but never serves the webhook, so wiring
        only ``MYCELIUM_TELEGRAM_BOT_TOKEN`` into it is sufficient and correct."""
        return bool(self.telegram_bot_token)

    @property
    def telegram_webhook_url_base(self) -> str:
        """Resolved public base URL the bot webhook is exposed at: the
        explicit override when set, otherwise the SPA's base URL (the
        co-host default in OSS deploys). Empty when neither is set."""
        return self.telegram_webhook_base_url or self.frontend_base_url

    @property
    def smtp_configured(self) -> bool:
        """SMTP transport is active iff host and From are both set
        (the validator above guarantees from is present when host is)."""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def vapid_configured(self) -> bool:
        """Web Push dispatch is active iff the VAPID keypair and subject are
        all set. Same fail-closed spirit as smtp/telegram: unconfigured, the
        webpush sender records a per-item failure and the SPA does not offer
        the browser-subscribe button."""
        return bool(self.vapid_public_key and self.vapid_private_key and self.vapid_subject)

    @property
    def sdicoop_active(self) -> bool:
        """True iff invoices are transmitted through Mycelium's accredited
        SdICoop channel (Mycelium as intermediary). False = manual export."""
        return self.sdi_channel == "sdicoop"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings as a cached singleton."""
    return Settings()
