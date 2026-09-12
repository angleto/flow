"""Read-only retrieval diagnostic (mycelio semantic-recall tuning).

Run inside the backend pod (has the local embedder + DB + mycelium_core):

    kubectl -n mycelium-production exec -i deploy/mycelium-backend \
        -- python - < scripts/diag_retrieval.py

Pure reads: resolves the `mycelium` project's org + a member, prints the per-org
retrieval floor settings, then for a few queries embeds locally and prints the
top dense neighbours by cosine. NO writes (does not call memory.retrieve, so no
access-counter bump). Goal: see (a) the grader/semantic floor values, (b) bge-m3's
cosine gap between genuine matches and compressed-band noise on the real corpus,
so the floors / semantic weight can be calibrated instead of guessed.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.embedder import EmbedSide, get_embedder
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.models.organization import Organization
from mycelium_core.services import memory as M

MYCELIUM_PROJECT = "b5ec1167-11f0-47bf-ba11-f5c25885928c"

# Notes that ARE genuinely relevant to the conceptual probes (so the output can
# flag where they land in the dense ranking vs the noise).
RELEVANT = {
    "3d76d2e5-090d-40b1-9fec-edb5d7758adf": "manifesto foresta",
    "524f0a69-4271-49a9-b29b-c3702202a9c6": "atomo decomposizione fungina",
    "1dcaf6a3-f371-4c44-940d-e9793217fab0": "design canonico mycelio",
}

QUERIES = [
    ("LEXICAL-RICH (control)", "decomposizione fungina humus distillazione pattern stagione"),
    (
        "CONCEPTUAL-1",
        "in che modo il sistema trasforma ciò che archivio per riusarlo invece di accumularlo",
    ),
    (
        "CONCEPTUAL-2",
        "come ritrovo per significato una nota di mesi fa senza ricordarne le parole esatte",
    ),
]

RETRIEVAL_KEYS = (
    M.SEMANTIC_MIN_SIM_KEY,
    M.GRADER_MIN_RRF_KEY,
)


async def main() -> None:
    # admin_session is fail-closed under RLS for org-scoped tables (memory_blobs),
    # but the enumeration tables (organizations, memberships) stay readable. So
    # list orgs + owners here, then probe each org's blobs under a tenant_session.
    async with admin_session() as s:
        orgs = (
            await s.execute(
                select(Membership.org_id, Membership.user_id).where(Membership.role == Role.owner)
            )
        ).all()
    if not orgs:
        print("no owner memberships found; aborting")
        return

    org_id = user_id = None
    for cand_org, cand_user in orgs:
        async with tenant_session(str(cand_org), str(cand_user), project_id=MYCELIUM_PROJECT) as s:
            n = (
                await s.execute(
                    select(MemoryBlob.id).where(MemoryBlob.project_id == MYCELIUM_PROJECT).limit(1)
                )
            ).first()
        if n is not None:
            org_id, user_id = cand_org, cand_user
            break
    if org_id is None:
        print(
            f"project {MYCELIUM_PROJECT} has no blobs in any of {len(orgs)} owned org(s); aborting"
        )
        return

    async with admin_session() as s:
        raw_settings = (
            await s.execute(select(Organization.settings).where(Organization.id == org_id))
        ).scalar_one_or_none()

    print(f"org_id={org_id}  user_id={user_id}")
    print("--- raw Organization.settings (retrieval keys) ---")
    bag = raw_settings if isinstance(raw_settings, dict) else {}
    for k in RETRIEVAL_KEYS:
        print(f"  {k} = {bag.get(k, '<unset>')!r}")
    print(f"  (full settings keys: {sorted(bag.keys())})")

    async with tenant_session(str(org_id), str(user_id), project_id=MYCELIUM_PROJECT) as s:
        sem_floor = await M.semantic_min_similarity(s, org_id)
        grader_floor = await M.grader_min_rrf_floor(s, org_id)
        print("--- resolved effective floors ---")
        print(f"  semantic_min_similarity = {sem_floor}")
        print(f"  grader_min_rrf_floor    = {grader_floor}")
        print(
            f"  _SEMANTIC_RRF_WEIGHT={M._SEMANTIC_RRF_WEIGHT}  _RRF_K={M._RRF_K}  "
            f"_RELATIVE_FLOOR_RATIO={M._RELATIVE_FLOOR_RATIO}"
        )
        print(
            f"  -> semantic-only top contribution = {M._SEMANTIC_RRF_WEIGHT}/{M._RRF_K + 1} "
            f"= {M._SEMANTIC_RRF_WEIGHT / (M._RRF_K + 1):.5f}"
        )

        emb = get_embedder()
        for label, q in QUERIES:
            print(f"\n=== {label}: {q!r} ===")
            qres = await emb.embed(q, side=EmbedSide.query)
            print(f"  query model_id={qres.model_id} dim={len(qres.vector)}")
            dist = MemoryBlob.embedding.max_inner_product(qres.vector)
            rows = (
                await s.execute(
                    select(MemoryBlob.id, MemoryBlob.text, dist.label("d"))
                    .where(
                        MemoryBlob.project_id == MYCELIUM_PROJECT,
                        MemoryBlob.embedding.is_not(None),
                    )
                    .order_by(dist)
                    .limit(15)
                )
            ).all()
            print("  top-15 dense neighbours (cosine = -max_inner_product):")
            for rank, (bid, txt, d) in enumerate(rows, start=1):
                cos = -float(d)
                snippet = (txt or "").strip().replace("\n", " ")[:55]
                flag = f"  <-- RELEVANT: {RELEVANT[str(bid)]}" if str(bid) in RELEVANT else ""
                print(f"   {rank:>2}. cos={cos:+.4f}  {str(bid)[:8]}  {snippet!r}{flag}")
            # Where do the known-relevant notes actually rank (full scan)?
            allrows = (
                await s.execute(
                    select(MemoryBlob.id, dist.label("d"))
                    .where(
                        MemoryBlob.project_id == MYCELIUM_PROJECT,
                        MemoryBlob.embedding.is_not(None),
                    )
                    .order_by(dist)
                )
            ).all()
            order = [str(bid) for bid, _ in allrows]
            cosmap = {str(bid): -float(d) for bid, d in allrows}
            print(f"  corpus size (embedded, mycelium project) = {len(order)}")
            for rid, name in RELEVANT.items():
                if rid in order:
                    print(
                        f"   relevant '{name}' -> dense rank {order.index(rid) + 1}/{len(order)} "
                        f"cos={cosmap[rid]:+.4f}"
                    )


if __name__ == "__main__":
    asyncio.run(main())
