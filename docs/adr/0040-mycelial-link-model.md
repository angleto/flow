# ADR-0040: Mycelial 4-verb note-note link model

Status: Accepted
Date: 2026-05-29
Revises: ADR-0029 (note garden ecosystem) — its D3 note-note link model.
Relates to: ADR-0031 (mindmap layout + edge weights), ADR-0039 (fungal
decomposition pipeline), ADR-0034 (humus policy in the walk), ADR-0028
(identity-first addressing). Implemented by migration `0022`
(`0022_note_link_split_atom_of`).

## Context

ADR-0029's D3 gave the note-note link table four kinds: `atom_of`,
`references`, `replies_to`, `supersedes`. Daily use exposed two
mismatches with how thinking actually evolves in the forest-of-memory:

- **Four kinds, but only one genesis relation, and three flavours of
  "see also".** `references` and `replies_to` are both "these two are
  connected"; the distinction (cite vs continue) never paid for itself
  in retrieval or in the mindmap, and it forced the user to choose
  between near-synonyms at link time. Meanwhile the one relation that
  carries real information, "this grew out of that", was conflated with
  the structural index pattern under `atom_of`.
- **Links were inert.** A `supersedes` link said "the canonical reading
  is the child" (ADR-0029 D3) but did nothing to the obsolete note's
  lifecycle. The garden's seed→growing→mature→dormant→humus arc
  (ADR-0029 D2, ADR-0039) was driven only by time and manual maturity;
  the relations the user drew between ideas never fed the self-pruning
  the forest metaphor promises.

A third pressure came from ADR-0039: the decomposition pipeline linked
its distillation to its source with `atom_of`, overloading the same
kind for "human structural index" and "machine-derived lesson". And the
mindmap's centrality (ADR-0031) needed a single, principled answer to
whether link direction confers authority.

This ADR re-cuts the kinds along the lines the mycelium suggests: a few
verbs that name how one idea relates to another, with the directional
ones wired into the lifecycle.

## Decision

### D1. Four verbs

`note_note_link.kind` is now exactly four mycelial verbs. The legacy
kinds `atom_of` / `references` / `replies_to` are gone.

- **`hypha_of`** — "derived from" / "grew from". **Directional**: stored
  `parent = origin`, `child = derived`. The hypha is the filament a new
  thought sends out from the one it sprouted from. Absorbs the old
  `atom_of` (a human structural/index link is a derivation) and is also
  the link the decomposition pipeline writes (D3).
- **`related`** — "simply connected". **Undirected**: the associative
  weave with no genesis or obsolescence claim. Absorbs `references` and
  `replies_to`. The server canonicalises endpoints to `parent < child`
  (by id string) so `(a, b)` and `(b, a)` are the same edge; direction
  is meaningless and must not be read.
- **`supersedes`** — A makes B obsolete. **Directional**: `parent = A`
  (superseder), `child = B` (superseded).
- **`contradicts`** — A refutes B as false. **Directional**: `parent =
  A`, `child = B`.

The CHECK constraint, the `link_notes` / `unlink_notes` services
(`core/.../services/note_links.py`), the REST/MCP surface, and the
mindmap all read these four. i18n labels and glosses already exist
(`garden.mindmap.linkKind.*`, `linkKindHint.*`, and the `noteLinks.*`
namespace); this ADR does not add or rename keys.

### D2. Directional links nudge the target toward dormant

`supersedes` and `contradicts` are not inert. On link creation the
service decays the **target** (`child`) one step toward `dormant`: a
superseded or refuted idea begins rotting into the deadwood that feeds
the humus layer (ADR-0039 / ADR-0034). This wires the relations the user
draws into the seed→growing→mature→dormant→humus lifecycle (ADR-0029
D2): the forest self-prunes from meaning, not only from elapsed time.

Constraints on the nudge (`link_notes`, `auto_dormant` audit action):

- **One-way.** It only pushes toward dormant; it never resurrects or
  promotes. Already-`dormant` targets are left alone.
- **Manual maturity still overrides.** ADR-0029 D2's rule stands: a user
  who explicitly sets maturity wins over the automatic nudge.
- **Transplanted/promoted notes are read-only and skipped.** A note with
  `promoted_at IS NOT NULL` (ADR-0029 D4) is not decayed; the
  service-layer read-only invariant is preserved.

`hypha_of` and `related` carry no lifecycle side effect.

### D3. `atom_of` is gone; the distillation link is a `hypha_of`

Human structural/index links (the old Zettelkasten `atom_of`) become
`hypha_of`: an index note is, in this model, a note the children grew
out of. The decomposition pipeline (ADR-0039) now writes its
**distillation → source** edge as a `hypha_of` as well — the
distillation literally derived from its source. This keeps the 1:1
thread ADR-0039 relies on for reversibility: a lesson can always be
decompressed back to the source it was distilled from, inspectable from
either end.

Humus is **not** a link kind. It is a **node facet** carried on the note
(`humus_kind` / `humus_flag`, migration `0015`, ADR-0039). A distillation
is an ordinary `text` note flagged as humus, linked to its source by a
`hypha_of` — exactly as ADR-0039's "facet, not a new entity" decision
intends.

**Pooled humus is anonymised — the commons.** Phase-2 producers
(`pattern` per Leiden cluster, `season` per time window; ADR-0039) emit
humus notes with **no per-source link**: a pattern note does not carry a
`hypha_of` back to each distillation it aggregated. Once thinking rots
into the shared substrate it loses its individual genealogy, the same
way you cannot trace which rotting log fed which mushroom. The 1:1
`hypha_of` thread holds for distillation (still personal, still
reversible); pooled patterns/seasons are the commons.

### D4. Keystone principle: direction means genesis, never authority

Importance/centrality is computed **undirected** over the weighted weave.
`compute_pagerank` and the focused-walk `compute_personalized_pagerank`
(`core/.../services/graph.py`) both add every typed edge in **both
directions**: a `hypha_of` from origin to derived contributes
symmetrically to the rank of both endpoints.

The consequence is deliberate: **a derived idea can outrank the idea that
generated it.** Link direction carries meaning (genesis for `hypha_of`,
obsolescence for `supersedes` / `contradicts`) but never authority. The
parent of a `hypha_of` is not "more important" because it came first.

Rationale: the forest-of-memory differentiator is anti-hierarchical.
Genealogy is real but it dissolves into the humus commons (D3); a graph
where the root always wins would re-impose the tree we are trying not to
build. PageRank over the undirected weave lets a late, well-connected
sprout become the keystone of the workspace even though it grew from
something now dormant.

### D5. Migration `0022` (lossy downgrade)

Migration `0022_note_link_split_atom_of` performs the in-place rewrite:

1. Drop the CHECK and the UNIQUE constraint (the rewrite would otherwise
   collide).
2. `atom_of → hypha_of` (includes the decomposition source→distillation
   edges, which were `atom_of` under ADR-0039).
3. `references`, `replies_to → related`.
4. Canonicalise `related` rows to `parent < child` (by id string), so the
   now-undirected pairs have one orientation.
5. De-duplicate rows that collide on `(org, parent, child, kind)` after
   the fold (a reverse-orientation `related`, or a `references` and a
   `replies_to` that folded onto the same pair). Keep the lowest id.
6. Re-add UNIQUE `(parent_note_id, child_note_id, kind)` and a new CHECK
   over the four verbs.

`note_note_link` is ENABLE-only RLS (not FORCE): the owner role bypasses
the policy and no `notes` JOIN is needed, so no NO FORCE / FORCE bracket
around the migration.

The **downgrade is best-effort and lossy** — the fold cannot be inverted
(`related` cannot be re-split into citations vs replies; pooled folds are
gone). It restores `hypha_of → atom_of`, `related → references`,
`contradicts → supersedes`, and the old CHECK.

## Consequences

- **Undirected storage for `related`.** `link_notes` / `unlink_notes`
  canonicalise endpoints to `parent < child` (by id string) for `related`
  before writing or matching; the directional kinds store endpoints as
  given. Callers must not infer direction from a `related` row.
- **Interchangeable mindmap handles for `related`.** The two connection
  handles on a mindmap node are interchangeable when drawing a `related`
  edge (the server canonicalises regardless of which end the user
  dragged from); for the directional kinds the handle the user starts
  from sets parent vs child.
- **Edge-weight profile updated** (`_KIND_WEIGHT`, `graph.py`, feeding
  the ADR-0031 soft-OR): `hypha_of 0.85 > supersedes 0.70 > contradicts
  0.65 > related 0.45`. Derivation is the strongest tie; the plain
  associative weave the weakest. These feed `w_kind` exactly as ADR-0031
  specifies; the layout and stroke formulae are unchanged.
- **The lifecycle gains a second driver.** Until now only time and
  manual judgement moved a note toward dormant (ADR-0029 D2); now
  `supersedes` / `contradicts` do too. The garden composts obsolete and
  refuted ideas as a side effect of the user naming the relation.
- **ADR-0039's reversibility is preserved and clarified.** The
  distillation thread is a `hypha_of`, inspectable from either end;
  pooled humus is intentionally anonymous (D3).
- **Lossy migration.** The cite/reply distinction and any directional
  meaning a user attached to old `references` / `replies_to` rows are
  discarded. Judged acceptable: the distinction was never load-bearing.

## Alternatives rejected

- **Keep `references` and `replies_to` separate.** Rejected: the
  distinction never fed retrieval or layout, and it taxed the user with a
  near-synonym choice at every link. One undirected `related` is the
  honest shape.
- **Make `supersedes` / `contradicts` hard-delete or hide the target.**
  Rejected: the garden never deletes on a relation; it composts. A
  one-way nudge toward `dormant` keeps the obsolete note readable, lets
  it feed the humus layer (ADR-0039), and respects manual override.
- **Directed PageRank (authority flows from parent to child, or child to
  parent).** Rejected: it re-imposes hierarchy on an explicitly
  anti-hierarchical model and would forbid a derived idea from becoming
  the keystone. Direction is for meaning, not authority (D4).
- **Keep `atom_of` as a distinct structural kind alongside `hypha_of`.**
  Rejected: a human index link and a derivation are the same relation
  ("this grew from that"); a separate kind would re-create the overload
  ADR-0039 already had to work around by linking distillations with
  `atom_of`.
- **Per-source `hypha_of` links from pooled humus back to every
  aggregated distillation.** Rejected: it would re-trace the genealogy
  the commons is meant to dissolve, and explode the link table on every
  Phase-2 synthesis. Pooled humus is anonymous by design (D3).

## Amendment (2026-07-17) — D3's "anonymized commons" is superseded by the implementation

Status: Accepted. Task: c5da112c (from the memory-system audit, note
`bdc62d7a`).

D3 decided that pooled Phase-2 humus (`pattern` / `season`) carries **no**
per-source link — "once thinking rots into the shared substrate it loses
its individual genealogy" — and the last "alternative rejected" above
explicitly refuses per-source `hypha_of` links from pooled humus.

The implementation went the other way, deliberately:
`decomposition._synthesise_humus` writes a `hypha_of` link from **every**
source note to the pattern/season note, with a comment citing this ADR's
decompression thread. The audit (2026-07-17) confirmed the divergence and
this amendment records the reversal instead of leaving it silent, because
the code is right and the poetry was wrong:

- **The ADR-0041 originals guard keys on `hypha_of` parenthood.** The
  autonomous retention sweep spares a soft-deleted note only if it is
  humus or a `hypha_of` parent. An anonymous commons would let the timer
  hard-delete the sources a pattern grew from — exactly the "original
  destroyed by an autonomous act" §12 forbids.
- **`restore_source` (ADR-0043 review surface) walks the same thread** to
  decompress an atom back to the notes it grew from. Anonymity would make
  a pooled atom irreversible, breaking the reversibility precondition
  ADR-0039 set for Phase-3 autonomy.
- The link-table growth feared here is bounded in practice: one link per
  aggregated source per synthesis, the same order as the distillation
  thread D3 already accepts.

The genealogy of pooled humus is therefore **traceable by design**, like
the 1:1 distillation thread. What remains of the commons idea is the
metaphor's kernel, not the schema: pooled atoms carry no per-claim
attribution to individual sources in their TEXT — the prose is communal
even though the provenance edges are not.

## Amendment (2026-09-11) — "do not infer direction" binds the surfaces, not only the store

Status: Accepted. Reported from production: a `related` edge showed on one
of its two notes and not on the other.

The Consequences above end with "callers must not infer direction from a
`related` row". The store and the services honoured that; the REST routes
and the note panel did not, and the three symptoms were the same mistake
wearing different clothes:

- The "Linked ideas" panel rendered, for an undirected kind, only the rows
  where the open note was the parent. Since the service canonicalises the
  pair to `parent < child` by id string, that is half the edges, on the
  half of the notes whose id sorts higher — invisible, unpredictable, and
  contradicted by the same edge showing correctly on the other note.
- `DELETE /notes/{note_id}/links` took the parent from the path, so an
  edge stored with the anchor as child could not be named at all. Every
  `related` edge is such an edge from one of its two ends. GET listed
  them; DELETE answered 404.
- `POST /notes/{note_id}/links` required the body's parent to equal the
  path id, which refused the panel's own "swap" affordance for the
  directional kinds ("this note grew from that one").

The correction: the path id is the note the caller has **open**, not the
parent. Both routes now require it to be one of the two endpoints and take
the endpoints themselves from body or query, which is the only address
that names the same edge from either of its ends.

The read rule that follows is a single one, and it lives in one place
(`web/src/shared/noteLinks.ts`, asserted against the model's
`NOTE_NOTE_LINK_UNDIRECTED_KINDS`): for an undirected kind a note has
**neighbours**, not parents and children, and the stored orientation is
not shown. Which kinds those are is the model's answer, not a surface's —
duplicating that table was what let a client disagree with the store while
both kept working.
