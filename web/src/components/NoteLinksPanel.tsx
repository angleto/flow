import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, authFetch, errMessage, workspaceHeader } from '../api/client'
import type { components, NoteLinkKind } from '../shared'
import {
  groupNoteLinks,
  isUndirectedNoteLinkKind,
  linkedNeighbourIds,
  otherEndpoint,
} from '../shared'
import { NotePickList } from './NotePickList'

type NoteLinkOut = components['schemas']['NoteLinkOut']
type Note = components['schemas']['NoteListOut']

// Note side: the "Linked ideas" panel driving the four note-to-note
// verbs of ADR-0040. Structural mirror of LinkedTasksPanel (per-kind
// sections, ``adding`` toggle, NotePickList inside
// ``linkedpanel__picker``, authFetch POST/DELETE, per-kind grouping,
// canAdd/canRemove). The difference that matters is directionality, and
// it is not decided here: ``groupNoteLinks`` owns it, because the store
// canonicalises an undirected edge to parent < child and a panel that
// listed only the parent side showed such an edge on one of its two
// notes and not on the other.
export function NoteLinksPanel({ noteId }: { noteId: string }) {
  const { t } = useTranslation()
  const [outgoing, setOutgoing] = useState<NoteLinkOut[]>([])
  const [incoming, setIncoming] = useState<NoteLinkOut[]>([])
  const [notes, setNotes] = useState<Note[]>([])
  const [adding, setAdding] = useState<NoteLinkKind | null>(null)
  // When adding a directional link, false = this-note-as-parent
  // (default), true = this-note-as-child (swapped).
  const [addAsChild, setAddAsChild] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setErr(null)
    const { data, error } = await api.GET('/notes/{note_id}/links', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    if (data) {
      setOutgoing(data.outgoing ?? [])
      setIncoming(data.incoming ?? [])
    }
  }, [noteId])

  useEffect(() => {
    let active = true
    void (async () => {
      const [linksRes, nt] = await Promise.all([
        api.GET('/notes/{note_id}/links', {
          params: { header: workspaceHeader(), path: { note_id: noteId } },
        }),
        api.GET('/notes', { params: { header: workspaceHeader() } }),
      ])
      if (!active) return
      if (linksRes.data) {
        setOutgoing(linksRes.data.outgoing ?? [])
        setIncoming(linksRes.data.incoming ?? [])
      }
      if (nt.data) setNotes(nt.data)
    })()
    return () => {
      active = false
    }
  }, [noteId])

  const titleOf = useCallback(
    (id: string) => {
      const n = notes.find((x) => x.id === id)
      if (!n) return t('noteLinks.unknownNote')
      return (
        n.title?.trim() ||
        (n.preview ?? '').trim() ||
        t('noteLinks.unknownNote')
      )
    },
    [notes, t],
  )

  // The two stored orientations, read from this note: directional kinds
  // keep parent and child apart, the undirected ones collapse into one
  // neighbour list whichever way the row happens to be stored.
  const groups = useMemo(
    () => groupNoteLinks(outgoing, incoming),
    [outgoing, incoming],
  )

  const otherId = useCallback(
    (link: NoteLinkOut) => otherEndpoint(link, noteId),
    [noteId],
  )

  // System-generated links (e.g. decomposition) carry no created_by;
  // mirror the LinkedTasksPanel ``canRemove`` discipline by marking
  // them read-only rather than offering a delete that the service
  // refuses.
  const isSystem = (link: NoteLinkOut) => !link.created_by

  async function addLink(kind: NoteLinkKind, targetId: string) {
    setErr(null)
    // Directional kinds honour the swap toggle; for the undirected ones
    // the order is immaterial (the server canonicalises the endpoints),
    // and the route accepts this note at either end.
    const swap = !isUndirectedNoteLinkKind(kind) && addAsChild
    const body = {
      parent_note_id: swap ? targetId : noteId,
      child_note_id: swap ? noteId : targetId,
      kind,
    }
    const res = await authFetch(`/notes/${noteId}/links`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      try {
        setErr(errMessage((await res.json()) as unknown))
      } catch {
        setErr(t('error.generic'))
      }
      return
    }
    setAdding(null)
    setAddAsChild(false)
    await reload()
  }

  async function removeLink(kind: NoteLinkKind, link: NoteLinkOut) {
    setErr(null)
    // Both endpoints as stored, so the edge is named the same way from
    // either of its notes: this note is the anchor in the path, not
    // necessarily the parent.
    const qs = new URLSearchParams({
      parent_note_id: link.parent_note_id,
      child_note_id: link.child_note_id,
      kind,
    })
    const res = await authFetch(`/notes/${noteId}/links?${qs.toString()}`, {
      method: 'DELETE',
    })
    if (!res.ok && res.status !== 404) {
      try {
        setErr(errMessage((await res.json()) as unknown))
      } catch {
        setErr(t('error.generic'))
      }
      return
    }
    await reload()
  }

  function renderItems(kind: NoteLinkKind, links: NoteLinkOut[]) {
    if (links.length === 0) {
      return <p className="hint linkedpanel__empty">{t('noteLinks.empty')}</p>
    }
    return (
      <ul className="linkedpanel__list">
        {links.map((link) => {
          const system = isSystem(link)
          return (
            <li key={link.id} className="linkedpanel__item">
              <Link
                className="linkedpanel__title"
                to={`/notes/${otherId(link)}`}
              >
                {titleOf(otherId(link))}
              </Link>
              <button
                type="button"
                className="btn--ghost btn--sm"
                disabled={system}
                title={
                  system
                    ? t('noteLinks.systemReadonly')
                    : t('noteLinks.remove')
                }
                onClick={() => !system && void removeLink(kind, link)}
              >
                ×
              </button>
            </li>
          )
        })}
      </ul>
    )
  }

  return (
    <div className="linkedpanel">
      <div className="linkedpanel__head">
        <h3 className="linkedpanel__h">{t('noteLinks.title')}</h3>
        <span className="muted">{t('noteLinks.headHint')}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {groups.map((group) => {
        const kind = group.kind
        const isAdding = adding === kind
        const directional = group.directional
        const excluded = linkedNeighbourIds(group, noteId)
        return (
          <section key={kind} className="linkedpanel__section">
            <header className="linkedpanel__sectionhead">
              <span
                className={`chip chip--kind chip--kind-${kind}`}
                title={t(`garden.mindmap.linkKindHint.${kind}`)}
              >
                {t(`garden.mindmap.linkKind.${kind}`)}
              </span>
              <span
                className="muted"
                title={t(`garden.mindmap.linkKindHint.${kind}`)}
              >
                (i)
              </span>
              <span className="muted">({group.total})</span>
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={() => {
                  setAddAsChild(false)
                  setAdding(isAdding ? null : kind)
                }}
                title={t('noteLinks.add')}
              >
                {isAdding ? t('common.cancel') : '+'}
              </button>
            </header>
            {group.directional ? (
              <>
                <div className="linkedpanel__direction">
                  <span className="muted">{t('noteLinks.asParent')}</span>
                  {renderItems(kind, group.asParent)}
                </div>
                <div className="linkedpanel__direction">
                  <span className="muted">{t('noteLinks.asChild')}</span>
                  {renderItems(kind, group.asChild)}
                </div>
              </>
            ) : (
              renderItems(kind, group.neighbours)
            )}
            {isAdding && (
              <div className="linkedpanel__picker">
                {directional && (
                  <div className="row linkedpanel__directionpick">
                    <span className="muted">
                      {addAsChild
                        ? t('noteLinks.asChild')
                        : t('noteLinks.asParent')}
                    </span>
                    <button
                      type="button"
                      className="btn--ghost btn--sm"
                      onClick={() => setAddAsChild((v) => !v)}
                      title={t('noteLinks.swap')}
                    >
                      {t('noteLinks.swap')}
                    </button>
                  </div>
                )}
                <NotePickList
                  notes={notes.filter(
                    (n) => n.id !== noteId && !excluded.has(n.id),
                  )}
                  value={null}
                  onPick={(id) => void addLink(kind, id)}
                  placeholder={t('noteLinks.pickerPh')}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}
