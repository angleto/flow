// Reading a note's typed note↔note edges from one note's point of view.
//
// The store keeps every edge as an ordered pair (parent, child), and for
// the undirected kinds that order is a CANONICALISATION, not a direction:
// the service sorts the two ids and writes the smaller one as parent, so
// (a related b) and (b related a) collapse to one row. A surface that
// renders the stored pair as if it meant something therefore shows an
// undirected edge on one of its two endpoints and not on the other, which
// is not an ordering detail -- it is half the edges missing from half the
// notes, by id comparison, which no user can predict.
//
// So the anchor-relative reading lives here once: which kinds have a
// direction, which endpoint is the far one, and how an edge set splits
// per kind for a note that may sit at either end of any of them.
//
// Pure by contract: this directory is compiled into both packages and
// imports nothing.

/** The mycelial 4-verb model (ADR-0040). */
export type NoteLinkKind = 'hypha_of' | 'related' | 'supersedes' | 'contradicts'

export const NOTE_LINK_KINDS: readonly NoteLinkKind[] = [
  'hypha_of',
  'related',
  'supersedes',
  'contradicts',
] as const

/** Kinds whose endpoints are unordered. Mirrors
 *  ``NOTE_NOTE_LINK_UNDIRECTED_KINDS`` in the core model, which is the
 *  authority; a test asserts the two agree, because drift here is
 *  invisible (the surface keeps rendering, just wrongly). */
export const UNDIRECTED_NOTE_LINK_KINDS: readonly NoteLinkKind[] = ['related'] as const

export function isUndirectedNoteLinkKind(kind: string): boolean {
  return (UNDIRECTED_NOTE_LINK_KINDS as readonly string[]).includes(kind)
}

/** The shape this module needs from a link row: both endpoints and the
 *  verb. Callers pass their own richer row type through. */
export type NoteEdge = {
  parent_note_id: string
  child_note_id: string
  kind: string
}

/** The endpoint that is not the anchor. Self-edges do not exist (a CHECK
 *  constraint forbids them), so this is total. */
export function otherEndpoint(link: NoteEdge, anchorId: string): string {
  return link.parent_note_id === anchorId ? link.child_note_id : link.parent_note_id
}

/** One kind's edges, seen from the anchor. Directional kinds keep the two
 *  roles apart; undirected ones expose a single neighbour set, because
 *  "parent" and "child" are storage order there and naming them would be
 *  inventing a direction the domain does not have. */
export type NoteLinkGroup<T> = { kind: NoteLinkKind; total: number } & (
  | { directional: true; asParent: T[]; asChild: T[] }
  | { directional: false; neighbours: T[] }
)

/** Split the two stored orientations of a note's edges into one group per
 *  kind, in the canonical kind order. ``outgoing`` are the rows with that
 *  note as parent, ``incoming`` those with it as child -- exactly the two
 *  lists ``GET /notes/{id}/links`` returns, so the anchor's own id is not
 *  needed here: it is what those two lists are already relative to. */
export function groupNoteLinks<T extends NoteEdge>(
  outgoing: readonly T[],
  incoming: readonly T[],
): NoteLinkGroup<T>[] {
  return NOTE_LINK_KINDS.map((kind) => {
    const asParent = outgoing.filter((l) => l.kind === kind)
    const asChild = incoming.filter((l) => l.kind === kind)
    if (isUndirectedNoteLinkKind(kind)) {
      const neighbours = [...asParent, ...asChild]
      return { kind, directional: false, neighbours, total: neighbours.length }
    }
    return {
      kind,
      directional: true,
      asParent,
      asChild,
      total: asParent.length + asChild.length,
    }
  })
}

/** Every note already linked to the anchor by this kind, either
 *  orientation: what a picker must exclude so it cannot offer a duplicate
 *  the unique constraint would collapse anyway. */
export function linkedNeighbourIds(
  group: NoteLinkGroup<NoteEdge>,
  anchorId: string,
): Set<string> {
  const rows = group.directional
    ? [...group.asParent, ...group.asChild]
    : group.neighbours
  return new Set(rows.map((l) => otherEndpoint(l, anchorId)))
}
