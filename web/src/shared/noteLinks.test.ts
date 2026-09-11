import { describe, expect, it } from 'vitest'
import {
  groupNoteLinks,
  linkedNeighbourIds,
  otherEndpoint,
  type NoteEdge,
} from './noteLinks'

// The ids are literal and ordered on purpose: the service canonicalises
// an undirected edge to parent < child, so which endpoint a note is
// depends on how its id sorts. 'aaa' < 'bbb'.
const edge = (parent: string, child: string, kind: string): NoteEdge => ({
  parent_note_id: parent,
  child_note_id: child,
  kind,
})

describe('groupNoteLinks', () => {
  it('shows an undirected edge on the endpoint stored as child', () => {
    // The reported defect: a panel that rendered only the parent side
    // left this list empty on 'bbb' while 'aaa' showed the edge.
    const groups = groupNoteLinks([], [edge('aaa', 'bbb', 'related')])
    const related = groups.find((g) => g.kind === 'related')!
    expect(related.directional).toBe(false)
    expect(related.directional === false && related.neighbours).toHaveLength(1)
    expect(related.total).toBe(1)
  })

  it('shows the same undirected edge on the endpoint stored as parent', () => {
    const groups = groupNoteLinks([edge('aaa', 'bbb', 'related')], [])
    const related = groups.find((g) => g.kind === 'related')!
    expect(related.total).toBe(1)
  })

  it('counts an undirected kind once per edge, from either side', () => {
    const stored = edge('aaa', 'bbb', 'related')
    const fromParent = groupNoteLinks([stored], [])
    const fromChild = groupNoteLinks([], [stored])
    const total = (gs: ReturnType<typeof groupNoteLinks>) =>
      gs.reduce((n, g) => n + g.total, 0)
    expect(total(fromParent)).toBe(1)
    expect(total(fromChild)).toBe(total(fromParent))
  })

  it('keeps the two roles apart for a directional kind', () => {
    const groups = groupNoteLinks(
      [edge('me', 'derived', 'hypha_of')],
      [edge('origin', 'me', 'hypha_of')],
    )
    const hypha = groups.find((g) => g.kind === 'hypha_of')!
    expect(hypha.directional).toBe(true)
    if (!hypha.directional) throw new Error('unreachable')
    expect(hypha.asParent.map((l) => l.child_note_id)).toEqual(['derived'])
    expect(hypha.asChild.map((l) => l.parent_note_id)).toEqual(['origin'])
    expect(hypha.total).toBe(2)
  })

  it('returns one group per kind, in the canonical order, even when empty', () => {
    expect(groupNoteLinks([], []).map((g) => g.kind)).toEqual([
      'hypha_of',
      'related',
      'supersedes',
      'contradicts',
    ])
  })

  it('does not leak an edge of one kind into another kind group', () => {
    const groups = groupNoteLinks([edge('me', 'x', 'supersedes')], [])
    const related = groups.find((g) => g.kind === 'related')!
    expect(related.total).toBe(0)
  })
})

describe('otherEndpoint', () => {
  it('names the far note whichever end the anchor is', () => {
    const stored = edge('aaa', 'bbb', 'related')
    expect(otherEndpoint(stored, 'aaa')).toBe('bbb')
    expect(otherEndpoint(stored, 'bbb')).toBe('aaa')
  })
})

describe('linkedNeighbourIds', () => {
  it('excludes an undirected neighbour stored on either side', () => {
    const groups = groupNoteLinks(
      [edge('bbb', 'ccc', 'related')],
      [edge('aaa', 'bbb', 'related')],
    )
    const related = groups.find((g) => g.kind === 'related')!
    expect(linkedNeighbourIds(related, 'bbb')).toEqual(new Set(['aaa', 'ccc']))
  })

  it('excludes a directional neighbour from both roles', () => {
    const groups = groupNoteLinks(
      [edge('me', 'derived', 'hypha_of')],
      [edge('origin', 'me', 'hypha_of')],
    )
    const hypha = groups.find((g) => g.kind === 'hypha_of')!
    expect(linkedNeighbourIds(hypha, 'me')).toEqual(
      new Set(['derived', 'origin']),
    )
  })
})
