"""Worker entry point.

Background jobs (IMAP sync, scheduler, memory promotion/re-embedding,
recurrences, reminders, SdI receipts) land with their phases. The first
registered job is the ADR-0025 P5 closed-loop dispatch tick: on a modest
interval it runs ``dispatch_loop.tick`` per workspace whose autonomous
policy is not ``off`` (recompute -> admit -> governance gate -> dispatch
via the P3 metered path). Per-workspace exception-isolated, logged at
info; the loop logic itself lives in ``mycelium_core.services.dispatch_loop``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mycelium_core.ai_providers import set_llm_override
from mycelium_core.config import get_settings
from mycelium_core.llm_ollama import OllamaLLM
from mycelium_core.notification_channel import set_sender_override
from mycelium_core.schema_revision import verify_schema_revision
from mycelium_core.services.mailer import build_system_mailer, set_mailer
from mycelium_core.services.notification_sender import build_notification_sender
from mycelium_worker import (
    dispatch,
    email_responder,
    email_sync,
    embedding_migration,
    fuel_retention,
    garden,
    google_calendar,
    note_search_backfill,
    payment_connectors,
    reminders,
    revisions,
    revisions_retention,
    revisions_summary,
    task_search_backfill,
    telegram_assistant,
    webhooks,
)


def _enabled_jobs() -> list[Callable[[], Awaitable[None]]]:
    """The run-forever job factories to gather, in order.

    Always-on (each self-gates / no-ops when unconfigured):
     - P5 closed-loop dispatch tick;
     - epic #125 P1 Google Calendar periodic ingest (no-op without OAuth);
     - F5/ADR-0023 email connector periodic sync (no-op without accounts);
     - ADR-0026 P3 Telegram assistant queue drain (no-op when
       MYCELIUM_ASSISTANT_ENABLED is false);
     - WS-4 autonomous email responder queue drain (no-op when
       MYCELIUM_EMAIL_RESPONDER_ENABLED is false);
     - reminders + notification-dispatch tick (FR-12);
     - task-search embedding backfill (timed-out re-embed safety net);
     - note-search pointer backfill (back-catalogue indexing);
     - revisions snapshot/retention/summary sweeps;
     - fuel-path retention (ADR-0048: retrieval_trace / search_clicks
       windows -- hygiene, so NOT gated on the garden loop);
     - embedding backfill (both tiers, per-org hosted via resolver).

    Opt-in:
     - garden seasonal-rule sweep (maturity transitions + garden-health
       ADR-0035 + graph/betweenness d8664631 snapshots). Gated on
       ``garden_loop_enabled`` (default off, tasks 44b4c212 / d8664631):
       it mutates note maturity on real data, so a deployment activates
       it deliberately.
    """
    jobs: list[Callable[[], Awaitable[None]]] = [
        dispatch.run_forever,
        google_calendar.run_forever,
        email_sync.run_forever,
        telegram_assistant.run_forever,
        email_responder.run_forever,
        reminders.run_forever,
        task_search_backfill.run_forever,
        note_search_backfill.run_forever,
        revisions.run_forever,
        revisions_retention.run_forever,
        revisions_summary.run_forever,
        fuel_retention.run_forever,
        embedding_migration.run_forever,
    ]
    if get_settings().garden_loop_enabled:
        jobs.append(garden.run_forever)
    # Signed invoice webhooks (ADR-0047): the delivery drain runs only when
    # enabled, so an unconfigured deploy never touches the fiscal outbox.
    if get_settings().webhooks_enabled:
        jobs.append(webhooks.run_forever)
    # Inbound payment connectors (ADR-0051): the event drain files fiscal
    # documents, so it runs only where the feature was deliberately turned on.
    if get_settings().payment_connectors_enabled:
        jobs.append(payment_connectors.run_forever)
    return jobs


async def _run() -> None:
    # Refuse to run against a schema this build does not expect, before
    # any loop starts. The worker writes on every tick and nothing here
    # is request-shaped, so without this a deploy that skipped the
    # migrate step fails silently in a background loop rather than
    # visibly on a request.
    await verify_schema_revision()
    # Registered jobs run concurrently; see ``_enabled_jobs`` for the
    # list and the opt-in garden loop.
    await asyncio.gather(*(job() for job in _enabled_jobs()))


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    # Process-global mailer wiring for the real worker process (mirrors
    # the API lifespan). Only swap when SMTP is configured; unconfigured
    # the module default LogMailer stands. main() is the process entry,
    # never imported by the test-suite, so no test mailer is clobbered.
    if settings.smtp_configured:
        set_mailer(build_system_mailer(settings))
    # Install the concrete notification sender (telegram over an email
    # fallback that uses the mailer wired just above). Without this the
    # reminders job's dispatch_pending hits DefaultSender and marks every
    # notification failed -- reminders silently never arrive. Mirrors the
    # API lifespan wiring; must run after set_mailer.
    _sender = build_notification_sender()
    set_sender_override(lambda: _sender)
    # Wire the open-model LLM (Ollama) when configured. Without this
    # the ``LocalLLM`` stub stays in place and the revision-summary
    # sweep is a no-op -- CI, dev and unconfigured deploys never hit
    # the network. Build a frozen URL+model snapshot so the override
    # is callable (signature ``() -> LLMProvider``).
    if settings.ollama_url and settings.open_model:
        ollama_url = settings.ollama_url
        open_model = settings.open_model
        set_llm_override(lambda: OllamaLLM(base_url=ollama_url, model=open_model))
    logging.getLogger("mycelium.worker").info(
        "worker started (env=%s); jobs: dispatch-loop, google-calendar-sync, "
        "email-sync, telegram-assistant; garden-loop=%s",
        settings.env,
        "on" if settings.garden_loop_enabled else "off",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
