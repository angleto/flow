"""Whether this process can serve right now.

Readiness and liveness answer different questions and must not share a
check. Liveness asks whether the process is alive: a socket answer,
touching nothing, because the remedy for a failing liveness probe is a
restart and a restart only helps a process that is stuck. Readiness asks
whether this process can do its job at this instant, and the remedy is
to stop sending it traffic.

Pointing both at one endpoint is the obvious thing to do and it converts
every dependency outage into a restart loop: the pod is killed for a
fault a restart cannot fix, at exactly the moment the system is already
degraded. Both services here pointed all three probes at ``/healthz``,
which returns ``{"status": "ok"}`` unconditionally -- so readiness was a
liveness check under another name, and a database outage left the pod
advertising itself as able to serve.

**Only the database.** It is the one dependency without which no surface
answers anything true. Object storage, the embedder, Ollama and SMTP are
deliberately NOT checked: each degrades to a partial service, and a
readiness probe that failed on them would take the whole deployment out
of rotation for a fault confined to one feature -- turning a partial
outage into a total one, which is the failure this file exists to avoid
rather than to reproduce one level up.

What it does not cover: it proves the database answered one trivial
query, not that it is healthy, not that the pool has capacity, and not
that any particular table is readable. A process that passes here can
still fail a real query.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from mycelium_core.db import get_engine

_log = logging.getLogger("mycelium.readiness")

#: Stable code for the caller, so the probe response never carries the
#: driver's message. The detail goes to the log, not over the wire.
DATABASE = "database"

#: Bounded so the handler answers rather than letting the probe time
#: out. Kubelet treats a timeout and a 503 alike, but only one of them
#: says which dependency was the problem, and an unbounded handler also
#: piles up connections against a database that is already struggling.
_TIMEOUT_SECONDS = 2.0


async def not_ready_because() -> str | None:
    """The dependency that is not answering, or None when ready.

    The deadline is not a parameter: every caller is a probe handler
    answering the same question under the same kubelet timeout, and a
    caller that wants a different one can wrap this in its own
    ``asyncio.timeout`` rather than reach through an argument.
    """
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:
        # Every failure is the same verdict -- not ready -- so the type
        # is not branched on; it is logged, which is where the detail
        # belongs. Broad on purpose: a readiness probe that raises
        # instead of answering tells the operator nothing.
        _log.warning(
            "readiness: database did not answer within %ss", _TIMEOUT_SECONDS, exc_info=True
        )
        return DATABASE
    return None
