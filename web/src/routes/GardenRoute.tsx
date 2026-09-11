/**
 * Garden route (docs/adr/0029 P2).
 *
 * Three tabs over the maturity lifecycle: Inbox (seed) / Garden
 * (growing + mature) / Cemetery (dormant + transplanted). Each tab
 * lists notes with quick gardening actions: change maturity, promote
 * to task (transplant), derive a fruit task, expand to plant view
 * with backlinks + atomic children + fruit tasks.
 *
 * Pure client-side filtering: a single GET /notes load drives the
 * three tabs (notes are few-per-workspace, no pagination yet).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { api, errMessage, workspaceHeader } from '../api/client'
import { MarkdownView } from '../components/Markdown'
import { GardenMindmap } from '../components/GardenMindmap'
import { useFocus } from '../lib/focus'
import { getSession } from '../auth/session'
import type { components } from '../shared'
import { groupNoteLinks, otherEndpoint } from '../shared'

type Note = components['schemas']['NoteListOut']
type NoteWithLinks = components['schemas']['NoteWithLinksOut']
type NoteLink = components['schemas']['NoteLinkOut']
type TaskBrief = { id: string; title: string }

type Tab = 'inbox' | 'garden' | 'cemetery' | 'mindmap'

const MATURITY_OPTIONS = ['seed', 'growing', 'mature', 'dormant'] as const

// Garden glyphs: the maturity stage is the row's identity, so render
// it as a single emoji + accessible label rather than a chip + text.
// (The keys map to garden.maturity.* in the i18n catalog.)
const MATURITY_GLYPH: Record<string, string> = {
  seed: '🌱',
  growing: '🌿',
  mature: '🌳',
  dormant: '🍂',
}

function bucketOf(n: Note): Tab {
  if (n.promoted_at) return 'cemetery'
  if (n.maturity === 'seed') return 'inbox'
  if (n.maturity === 'dormant') return 'cemetery'
  return 'garden'
}

function shortPreview(n: Note): string {
  // ``preview`` is the first non-empty line, already capped server-side
  // (services/notes._previews_by_note): the list no longer ships the
  // body, so there is nothing left to slice here. ``summary`` stays as
  // the fallback for a note that has one but no text yet.
  const line = (n.preview || '').trim()
  if (line) return line
  const summary = (n.summary || '').trim()
  if (!summary) return ''
  const first = summary.split('\n').find((l) => l.trim().length > 0) || ''
  return first.length > 220 ? first.slice(0, 219) + '…' : first
}

const TAB_GLYPH: Record<Tab, string> = {
  inbox: '🌱',
  garden: '🌿',
  cemetery: '🍂',
  mindmap: '🍄',
}

// Tab persistence (per-workspace): the mindmap tab is heavier to
// switch into (graph layout), so keeping the user's choice across
// reloads avoids the "wait, I was in mindmap" surprise. Scoped per
// workspace because the same browser may host multiple tenants.
function activeTabKey(workspaceId: string | null): string {
  return `mycelium.garden.activeTab.${workspaceId ?? '_'}`
}

function loadActiveTab(workspaceId: string | null): Tab {
  try {
    const raw = localStorage.getItem(activeTabKey(workspaceId))
    if (raw === 'inbox' || raw === 'garden' || raw === 'cemetery' || raw === 'mindmap') {
      return raw
    }
  } catch {
    // ignore
  }
  return 'inbox'
}

export function GardenRoute() {
  const { t } = useTranslation()
  const [notes, setNotes] = useState<Note[]>([])
  const [allTasks, setAllTasks] = useState<TaskBrief[]>([])
  const session = getSession()
  const workspaceId = session?.workspaceId ?? null
  const [tab, setTab] = useState<Tab>(() => loadActiveTab(workspaceId))
  const [openId, setOpenId] = useState<string | null>(null)
  const [openData, setOpenData] = useState<NoteWithLinks | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [idCopied, setIdCopied] = useState(false)
  // First-load flag: show the empty-state copy only once the fetch has
  // resolved, never during the initial (possibly slow) load.
  const [loading, setLoading] = useState(true)
  // Suggested-work count for the Review sub-nav badge: distillation
  // candidates (nodes + edges) + autonomous atoms awaiting approval. A
  // cheap read; the panel itself lives on /garden/review.
  const [suggestCount, setSuggestCount] = useState<number | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(activeTabKey(workspaceId), tab)
    } catch {
      // quota full: tab simply won't persist this session
    }
  }, [tab, workspaceId])

  // Focus (sidebar): a client (all its projects) or one project.
  // Same predicate as NotesRoute so /garden and /notes always agree
  // on what "in scope" means.
  const {
    projectId: focusProject,
    clientId: focusClient,
    focusIds,
    active: focusActive,
  } = useFocus()

  const reload = useCallback(async () => {
    const { data, error } = await api.GET('/notes', {
      params: { header: workspaceHeader() },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setNotes(data ?? [])
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const [n, tk] = await Promise.all([
          api.GET('/notes', { params: { header: workspaceHeader() } }),
          api.GET('/tasks', { params: { header: workspaceHeader() } }),
        ])
        if (!active) return
        if (n.error) setErr(errMessage(n.error))
        else setNotes(n.data ?? [])
        if (tk.data)
          setAllTasks(tk.data.map((x) => ({ id: x.id, title: x.title })))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    let active = true
    void (async () => {
      const [cand, rev] = await Promise.all([
        api.GET('/garden/candidates', {
          params: { header: workspaceHeader(), query: { kind: 'all', limit: 50 } },
        }),
        api.GET('/garden/review/pending', {
          params: { header: workspaceHeader(), query: { limit: 50 } },
        }),
      ])
      if (!active) return
      const nCand = (cand.data?.nodes?.length ?? 0) + (cand.data?.edges?.length ?? 0)
      const nRev = rev.data?.length ?? 0
      setSuggestCount(nCand + nRev)
    })()
    return () => {
      active = false
    }
  }, [])

  const inFocus = useCallback(
    (n: Note): boolean => {
      if (!focusActive) return true
      if (n.project_id != null && focusIds.includes(n.project_id)) return true
      const tagIds = (n.tags ?? []).map((g) => g.id)
      if (tagIds.some((id) => focusIds.includes(id))) return true
      // Client-only notes (no project) belong to /garden only when the
      // sidebar focuses the whole client — narrowing to one project
      // hides them, same rule as /notes.
      if (
        !focusProject &&
        focusClient &&
        (n.tags ?? []).some(
          (g) => g.kind === 'client' && g.id === focusClient,
        )
      )
        return true
      return false
    },
    [focusActive, focusIds, focusProject, focusClient],
  )

  const buckets = useMemo(() => {
    const out: Record<Tab, Note[]> = {
      inbox: [],
      garden: [],
      cemetery: [],
      mindmap: [],
    }
    for (const n of notes) {
      if (n.deleted_at || n.is_archived) continue
      if (!inFocus(n)) continue
      out[bucketOf(n)].push(n)
    }
    // The mindmap view spans every alive note in scope (all three
    // lifecycle buckets) — its count chip reflects the total set
    // it renders, not a separate bucket.
    out.mindmap = [...out.inbox, ...out.garden, ...out.cemetery]
    return out
  }, [notes, inFocus])

  const titleOf = (n: Note) =>
    (n.title && n.title.trim()) || shortPreview(n).slice(0, 80) || n.id.slice(0, 8)

  async function setMaturity(noteId: string, maturity: string) {
    setBusy(true)
    setErr('')
    const { error } = await api.POST('/notes/{note_id}/maturity', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { maturity },
    })
    setBusy(false)
    if (error) {
      setErr(errMessage(error))
      return
    }
    await reload()
  }

  async function onPromote(noteId: string) {
    if (!window.confirm(t('garden.promoteConfirm'))) return
    setBusy(true)
    setErr('')
    const { data, error } = await api.POST('/notes/{note_id}/promote', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { title: null },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await reload()
    window.location.href = `/tasks/${data.task_id}`
  }

  async function onDerive(noteId: string) {
    const title = window.prompt(t('garden.derivePrompt'))
    if (!title || !title.trim()) return
    setBusy(true)
    setErr('')
    const { data, error } = await api.POST('/notes/{note_id}/derive-task', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
      body: { title: title.trim(), description: null, estimate_effort_h: null },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await reload()
    window.location.href = `/tasks/${data.task_id}`
  }

  const openPlant = useCallback(async (noteId: string) => {
    setOpenId(noteId)
    setOpenData(null)
    const { data, error } = await api.GET('/notes/{note_id}/links', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    setOpenData(data ?? null)
  }, [])

  const closePlant = () => {
    setOpenId(null)
    setOpenData(null)
  }

  // Fungal decomposition (ADR-0034): distil a composting note into a
  // reusable atom. Idempotent server-side, so re-clicking lands on the
  // existing distillation. On success the plant modal opens on the
  // distilled note (the freshly grown hypha), not the source.
  async function onDistill(noteId: string) {
    setBusy(true)
    setErr('')
    const { data, error } = await api.POST('/notes/{note_id}/distill', {
      params: { header: workspaceHeader(), path: { note_id: noteId } },
    })
    setBusy(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    await reload()
    await openPlant(data.distilled_note_id)
  }

  // Esc closes the modal — same convention as /notes.
  useEffect(() => {
    if (!openId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePlant()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openId])

  const tabs: { id: Tab; label: string }[] = [
    { id: 'inbox', label: t('garden.tab.inbox') },
    { id: 'garden', label: t('garden.tab.garden') },
    { id: 'cemetery', label: t('garden.tab.cemetery') },
    { id: 'mindmap', label: t('garden.tab.mindmap') },
  ]
  const visible = buckets[tab]

  return (
    <section className="card garden">
      <h1>{t('garden.title')}</h1>
      <p className="hint">{t('garden.intro')}</p>

      <div className="garden__tabs" role="tablist">
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            role="tab"
            aria-selected={tab === x.id}
            className={
              'garden__tab' + (tab === x.id ? ' garden__tab--active' : '')
            }
            onClick={() => setTab(x.id)}
          >
            <span aria-hidden="true">{TAB_GLYPH[x.id]}</span>
            <span className="garden__tab-label">{x.label}</span>
            <span className="garden__tab-count">{buckets[x.id].length}</span>
          </button>
        ))}
        <Link to="/garden/health" className="garden__health-link">
          {t('gardenHealth.title')} →
        </Link>
        <Link to="/garden/audit" className="garden__health-link">
          {t('gardenAudit.title')} →
        </Link>
        <Link to="/garden/review" className="garden__health-link">
          {t('gardenReview.navTitle')}
          {suggestCount != null && suggestCount > 0 && (
            <span className="garden__tab-count">{suggestCount}</span>
          )}{' '}
          →
        </Link>
      </div>

      {err && <p className="err">{err}</p>}

      {loading ? (
        <p className="hint garden__empty">{t('garden.loading')}</p>
      ) : tab === 'mindmap' ? (
        <GardenMindmap
          notes={visible}
          workspaceId={workspaceId ?? '_'}
          onOpenNote={(id) => void openPlant(id)}
        />
      ) : visible.length === 0 ? (
        <p className="hint garden__empty">{t(`garden.empty.${tab}`)}</p>
      ) : (
        <ul className="garden__list">
          {visible.map((n) => (
            <li key={n.id} className="plant">
              <button
                type="button"
                className="plant__open"
                onClick={() => void openPlant(n.id)}
                title={t('garden.openPlant')}
              >
                <span
                  className={`plant__glyph plant__glyph--${n.maturity}`}
                  aria-hidden="true"
                >
                  {MATURITY_GLYPH[n.maturity ?? 'seed'] ?? '🌱'}
                </span>
                <span className="plant__body">
                  <span className="plant__title">{titleOf(n)}</span>
                  {shortPreview(n) && (
                    <span className="plant__preview">{shortPreview(n)}</span>
                  )}
                  {n.promoted_at && (
                    <span className="plant__chip plant__chip--promoted">
                      {t('garden.promotedChip')}
                    </span>
                  )}
                </span>
              </button>
              <div className="plant__actions">
                {!n.promoted_at && (
                  <>
                    <select
                      className="plant__maturity"
                      value={n.maturity}
                      disabled={busy}
                      aria-label={t('garden.changeMaturity')}
                      title={t('garden.changeMaturity')}
                      onChange={(e) => void setMaturity(n.id, e.target.value)}
                    >
                      {MATURITY_OPTIONS.map((m) => (
                        <option key={m} value={m}>
                          {MATURITY_GLYPH[m]} {t(`garden.maturity.${m}`)}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="plant__btn"
                      disabled={busy}
                      title={t('garden.promote')}
                      aria-label={t('garden.promote')}
                      onClick={() => void onPromote(n.id)}
                    >
                      <span aria-hidden="true">🌸</span>
                      <span className="plant__btn-label">
                        {t('garden.promote')}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="plant__btn plant__btn--ghost"
                      disabled={busy}
                      title={t('garden.derive')}
                      aria-label={t('garden.derive')}
                      onClick={() => void onDerive(n.id)}
                    >
                      <span aria-hidden="true">🍎</span>
                      <span className="plant__btn-label">
                        {t('garden.derive')}
                      </span>
                    </button>
                  </>
                )}
                {tab === 'cemetery' && (
                  <button
                    type="button"
                    className="plant__btn plant__btn--ghost"
                    disabled={busy}
                    title={t('garden.distillHint')}
                    aria-label={t('garden.distill')}
                    onClick={() => void onDistill(n.id)}
                  >
                    <span aria-hidden="true">⚗️</span>
                    <span className="plant__btn-label">
                      {t('garden.distill')}
                    </span>
                  </button>
                )}
                <Link
                  to={`/notes/${n.id}`}
                  className="plant__btn plant__btn--ghost"
                  title={t('garden.openNote')}
                  aria-label={t('garden.openNote')}
                >
                  <span aria-hidden="true">↗</span>
                  <span className="plant__btn-label">
                    {t('garden.openNote')}
                  </span>
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}

      {openId && (
        <div
          className="modal__backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={t('garden.title')}
          onClick={(e) => {
            if (e.target === e.currentTarget) closePlant()
          }}
        >
          <div className="modal__panel">
            <div className="modal__head">
              <strong>
                {openData
                  ? openData.note.title || t('notes.untitled')
                  : t('garden.loading')}
              </strong>
              {openData && (
                <span
                  className={`plant__glyph plant__glyph--${openData.note.maturity}`}
                  aria-hidden="true"
                >
                  {MATURITY_GLYPH[openData.note.maturity ?? 'seed'] ?? '🌱'}
                </span>
              )}
              {openId && (
                // Tiny clickable chip exposing the note id so the user
                // can paste it elsewhere (e.g. share a reference with an
                // assistant) without leaving the modal. Same affordance
                // used by /notes edit modal.
                <button
                  type="button"
                  className="chip"
                  title={idCopied ? t('notes.idCopied') : openId}
                  aria-label={t('notes.copyId')}
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(openId)
                      setIdCopied(true)
                      window.setTimeout(() => setIdCopied(false), 1500)
                    } catch {
                      setIdCopied(false)
                    }
                  }}
                >
                  {idCopied ? t('notes.idCopied') : `ID ${openId.slice(0, 8)}…`}
                </button>
              )}
              <span className="modal__sp" />
              {openId && (
                <Link
                  to={`/notes/${openId}`}
                  className="btn--ghost btn--sm"
                  title={t('garden.editNote')}
                >
                  {t('garden.editNote')}
                </Link>
              )}
              <button
                type="button"
                className="btn--ghost btn--sm"
                onClick={closePlant}
              >
                {t('notes.close')}
              </button>
            </div>
            <div className="modal__body">
              {openData === null ? (
                <p className="hint">{t('garden.loading')}</p>
              ) : (
                <PlantDetail
                  data={openData}
                  allNotes={notes}
                  allTasks={allTasks}
                  onUnlink={async (link) => {
                    await api.DELETE('/notes/{note_id}/links', {
                      params: {
                        header: workspaceHeader(),
                        path: { note_id: openId },
                        query: {
                          parent_note_id: link.parent_note_id,
                          child_note_id: link.child_note_id,
                          kind: link.kind,
                        },
                      },
                    })
                    await openPlant(openId)
                  }}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

function PlantDetail({
  data,
  allNotes,
  allTasks,
  onUnlink,
}: {
  data: NoteWithLinks
  allNotes: Note[]
  allTasks: TaskBrief[]
  onUnlink: (link: NoteLink) => Promise<void>
}) {
  const { t } = useTranslation()
  const titleById = (id: string) =>
    allNotes.find((x) => x.id === id)?.title?.trim() || id.slice(0, 8)
  const taskTitleById = (id: string) =>
    allTasks.find((x) => x.id === id)?.title?.trim() || id.slice(0, 8)
  const n = data.note
  // The schema marks default-valued arrays as optional (post fix to
  // gen:api --default-non-nullable false), so guard them with empty
  // fallbacks once at the top of the render.
  const taskLinks = data.task_links ?? []
  // The stored (parent, child) order is a direction only for the
  // directional kinds; for the undirected ones it is a canonicalisation
  // by id, so those edges get their own section instead of being filed
  // under a direction that would be this note's id sorting low or high.
  const groups = groupNoteLinks(data.outgoing ?? [], data.incoming ?? [])
  const asParent = groups.flatMap((g) => (g.directional ? g.asParent : []))
  const asChild = groups.flatMap((g) => (g.directional ? g.asChild : []))
  const undirected = groups.flatMap((g) => (g.directional ? [] : g.neighbours))
  const linkRow = (l: NoteLink) => {
    const other = otherEndpoint(l, n.id)
    return (
      <li key={l.id}>
        <span
          className="chip chip--linkkind"
          title={t(`garden.mindmap.linkKindHint.${l.kind}`)}
        >
          {t(`garden.mindmap.linkKind.${l.kind}`)}
        </span>{' '}
        <Link to={`/notes/${other}`}>{titleById(other)}</Link>{' '}
        <button
          type="button"
          className="btn--ghost btn--sm"
          onClick={() => void onUnlink(l)}
        >
          {t('garden.unlink')}
        </button>
      </li>
    )
  }
  const linkSection = (heading: string, links: NoteLink[]) => (
    <section className="plant-detail__section">
      <h3>{heading}</h3>
      {links.length === 0 ? (
        <p className="hint">{t('garden.none')}</p>
      ) : (
        <ul className="plant-detail__links">{links.map(linkRow)}</ul>
      )}
    </section>
  )
  return (
    <div className="plant-detail">
      <div className="plant-detail__chips">
        <span className={`chip chip--maturity chip--${n.maturity ?? 'seed'}`}>
          <span aria-hidden="true">{MATURITY_GLYPH[n.maturity ?? 'seed'] ?? '🌱'}</span>{' '}
          {t(`garden.maturity.${n.maturity ?? 'seed'}`)}
        </span>
        {n.promoted_at && (
          <span className="chip chip--promoted">{t('garden.promotedChip')}</span>
        )}
      </div>
      {n.transcript && (
        <div className="plant-detail__body md">
          <MarkdownView text={n.transcript} parent={{ kind: 'note', id: n.id }} />
        </div>
      )}
      {linkSection(t('garden.outgoing'), asParent)}
      {linkSection(t('garden.incoming'), asChild)}
      {linkSection(t('garden.undirected'), undirected)}
      <section className="plant-detail__section">
        <h3>{t('garden.fruits')}</h3>
        {taskLinks.length === 0 ? (
          <p className="hint">{t('garden.none')}</p>
        ) : (
          <ul className="plant-detail__links">
            {taskLinks.map((l) => (
              <li key={l.id}>
                <span className="chip chip--linkkind">{l.kind}</span>{' '}
                <Link to={`/tasks/${l.task_id}`}>
                  {taskTitleById(l.task_id)}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
