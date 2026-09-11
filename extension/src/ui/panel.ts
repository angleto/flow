// The panel: one component, two hosts.
//
// The popup and the side panel render the SAME thing, because an action
// that exists in both must have the same keystroke and the same result;
// what differs is lifetime. The popup dies on focus loss, so it holds no
// unsaved state and hands every write to the worker, which survives it.
// The side panel persists, so it is the one that may hold a draft and the
// one that can attach a file -- opening an OS file dialog dismisses a
// popup and would take the draft with it.

import { send } from '../shared/protocol'
import { renderCapture } from './capture'
import { clear, el, headline, on } from './dom'
import { renderEditor } from './editor'
import { m } from './i18n'
import { type Outcome, renderOutcome } from './outcome'
import type {
  Connection,
  EntityRow,
  Failure,
  FailureCode,
  Host,
  LinkRequest,
  PageContext,
  ScopeSel,
  Sections,
} from './types'

export type { Host }

interface State {
  on: boolean
  connections: Connection[]
  /** The connect request in flight, if any. Held so a panel reopened
   *  mid-ceremony shows the code again instead of a button that would
   *  open a second request. */
  link: LinkRequest | null
  scope: ScopeSel
  q: string
  /** Monotonic, so a slower earlier query cannot overwrite a fresher
   *  answer. The worker aborts the superseded request; this discards a
   *  reply that raced past the abort. */
  gen: number
  find: Outcome<EntityRow[]>
  rankedCount: number
  degraded: boolean
  /** Atoms the query line could not honour, and the scope the search
   *  actually ran under. Both come back from the worker rather than
   *  being inferred here: the chips must show what the SERVER was asked,
   *  not what the panel hoped to ask. */
  unresolved: string[]
  effectiveFocus: ScopeSel['focus']
  selected: number
  /** Query mode owns every bare key, so row commands are impossible
   *  while it has focus. List mode gives the row real focus and turns
   *  bare letters into commands. The boundary is visible: the input dims
   *  and the row grows an accent rail. */
  mode: 'query' | 'list'
  expanded: string | null
  /** Side panel only. The three things it keeps beside you while you
   *  browse, and the page you are on. A popup dies on focus loss, so it
   *  can have none of this: by the time you switched tabs it is gone. */
  sections: Sections | null
  pinned: EntityRow | null
  page: PageContext | null
  syncedAt: number | null
  /** The capture sheet replaces the panel body while it is open. In the
   *  popup that is the whole surface; in the side panel it survives a tab
   *  switch, which is why the file input only exists there. */
  capture: HTMLElement | null
}

export function mountPanel(root: HTMLElement, host: Host): void {
  const state: State = {
    on: true,
    connections: [],
    link: null,
    scope: { workspaceId: null, focus: null },
    q: '',
    gen: 0,
    find: { phase: 'idle' },
    rankedCount: 0,
    degraded: false,
    unresolved: [],
    effectiveFocus: null,
    selected: 0,
    mode: 'query',
    expanded: null,
    sections: null,
    pinned: null,
    page: null,
    syncedAt: null,
    capture: null,
  }

  const header = el('header', { class: 'hypha__header' })
  const scopeBar = el('div', { class: 'hypha__chips', role: 'group', 'aria-label': m('scopeLabel') })
  const input = el('input', {
    class: 'hypha__input',
    type: 'search',
    role: 'combobox',
    'aria-expanded': 'true',
    'aria-controls': 'hypha-list',
    'aria-label': m('searchLabel'),
    placeholder: m('searchPlaceholder'),
    autocomplete: 'off',
    spellcheck: false,
  })
  const list = el('ul', { class: 'hypha__list', id: 'hypha-list', role: 'listbox', 'aria-label': m('resultsLabel') })
  const body = el('div', { class: 'hypha__body' })
  const live = el('p', { class: 'hypha__sr', role: 'status', 'aria-live': 'polite' })
  const alerts = el('p', { class: 'hypha__sr', role: 'alert', 'aria-live': 'assertive' })
  const footer = el('footer', { class: 'hypha__footer' })

  root.append(header, scopeBar, input, body, footer, live, alerts)

  // ------------------------------------------------------------------
  // Header: the switch, first control after the query line in tab order.
  // ------------------------------------------------------------------

  function renderHeader(): void {
    clear(header)
    const label = el('label', { class: 'hypha__switch' })
    const box = el('input', { type: 'checkbox', checked: state.on })
    on(box, 'change', () => {
      void (async () => {
        const res = await send('switch/set', { on: box.checked })
        if (res.ok) {
          state.on = res.data
          renderAll()
          announce(state.on ? m('switchLabel') : m('switchOffTitle'))
        }
      })()
    })
    label.append(box, document.createTextNode(m('switchLabel')))
    header.appendChild(label)

    const conn = current()
    if (conn) header.appendChild(el('span', { class: 'hypha__ws', text: conn.workspaceName }))
  }

  function current(): Connection | undefined {
    return state.connections.find((c) => c.workspaceId === state.scope.workspaceId)
  }

  // ------------------------------------------------------------------
  // Scope: the persistent selection is always visible, so the line
  // always reads back what you are actually looking at. Tenancy is never
  // implicit, even with one workspace.
  // ------------------------------------------------------------------

  function renderScope(): void {
    clear(scopeBar)
    const conn = current()
    if (!conn) return
    // The workspace, and a way to change it when there is more than one
    // to change to. One credential now reaches every workspace its
    // holder belongs to, so the panel would otherwise open in whichever
    // one it was connected in and offer no way out of it. With a single
    // workspace it stays a chip: a select with one option is a control
    // that does nothing.
    if (state.connections.length > 1) {
      const picker = el('select', {
        class: 'hypha__chip hypha__chip--ws hypha__ws-pick',
        'aria-label': m('workspaceLabel'),
      }) as HTMLSelectElement
      for (const row of state.connections) {
        const option = el('option', { value: row.workspaceId, text: row.workspaceName })
        if (row.workspaceId === conn.workspaceId) option.setAttribute('selected', '')
        picker.appendChild(option)
      }
      picker.addEventListener('change', () => {
        // The focus is a selection INSIDE a workspace (a client, a
        // project), so it cannot survive the move: carrying it over
        // would pin a filter naming something the new workspace does
        // not have.
        void setScope({ workspaceId: picker.value, focus: null })
      })
      scopeBar.appendChild(picker)
    } else {
      scopeBar.appendChild(
        el('span', { class: 'hypha__chip hypha__chip--ws', text: conn.workspaceName }),
      )
    }
    // What the LAST search actually ran under, falling back to the
    // pinned selection before one has run. An inline `in:` overrides for
    // one query and never writes the pinned scope, so reading the chips
    // off the request would make the line claim a narrowing the server
    // was never asked for.
    const focus = state.effectiveFocus ?? state.scope.focus
    scopeBar.appendChild(
      el('span', {
        class: `hypha__chip hypha__chip--${focus ? focus.kind : 'all'}`,
        text: focus ? focus.name : m('scopeEverything'),
      }),
    )
    for (const atom of state.unresolved) {
      // Shown in error ink and dropped from the request. On /tasks an
      // unknown atom degrades to free text, which is harmless over a
      // list the page holds; here the server has already truncated, so
      // the same degradation would silently change which rows came back.
      scopeBar.appendChild(
        el('span', { class: 'hypha__chip hypha__chip--bad', title: m('emptyNoResults'), text: atom }),
      )
    }
    if (focus?.kind === 'client') {
      // Note retrieval is scoped by project, not by client, so a client
      // focus cannot narrow notes in one call. Saying so beats quietly
      // returning fewer notes than the scope implies.
      scopeBar.appendChild(el('span', { class: 'hypha__hint', text: m('scopeNotesClientWarning') }))
    }
  }

  // ------------------------------------------------------------------
  // Results
  // ------------------------------------------------------------------

  function rowNode(row: EntityRow, index: number): HTMLElement {
    const li = el('li', {
      class: `hypha__row${index === state.selected ? ' hypha__row--sel' : ''}`,
      id: `hypha-opt-${index}`,
      role: 'option',
      'aria-selected': index === state.selected,
      'aria-expanded': state.expanded === row.id,
      tabindex: state.mode === 'list' && index === state.selected ? 0 : -1,
    })
    const line = el('div', { class: 'hypha__line' })
    line.appendChild(el('span', { class: 'hypha__glyph', text: row.kind === 'task' ? '✓' : '◆', 'aria-hidden': true }))
    line.appendChild(el('span', { class: 'hypha__title', text: row.title }))
    if (index === state.selected || state.expanded === row.id) {
      line.appendChild(el('code', { class: 'hypha__code', text: row.code }))
    }
    if (row.state) line.appendChild(el('span', { class: 'hypha__chip', text: row.state }))
    li.appendChild(line)

    if (row.snippet) {
      const snip = el('p', { class: 'hypha__snippet' })
      snip.appendChild(headline(row.snippet))
      li.appendChild(snip)
    }

    const meta = el('div', { class: 'hypha__meta' })
    if (row.dueDate) meta.appendChild(el('span', { class: 'hypha__due', text: row.dueDate.slice(0, 10) }))
    if (row.priority != null) meta.appendChild(el('span', { class: 'hypha__prio', text: `P${row.priority}` }))
    if (row.projectName) meta.appendChild(el('span', { class: 'hypha__project', text: row.projectName }))
    if (row.isArchived) meta.appendChild(el('span', { class: 'hypha__hint', text: m('close') }))
    if (host === 'sidepanel' && row.kind === 'task' && index >= 0) {
      // The keyboard has `p`; a control only the keyboard can reach is
      // not a control for everyone.
      const pin = el('button', {
        type: 'button',
        class: 'hypha__linkbtn',
        'aria-pressed': state.pinned?.id === row.id,
      })
      pin.textContent = state.pinned?.id === row.id ? m('unpin') : m('pin')
      on(pin, 'click', (event) => {
        event.stopPropagation()
        void setPinned(state.pinned?.id === row.id ? null : row)
      })
      meta.appendChild(pin)
    }
    if (meta.childElementCount) li.appendChild(meta)

    on(li, 'click', () => {
      state.selected = index
      open(row, false)
    })

    if (state.expanded === row.id) {
      li.appendChild(
        renderEditor(row, {
          onRow: (fresh) => {
            const list = rows()
            const at = list.findIndex((r) => r.id === fresh.id)
            // The server's answer replaces the row wholesale. Merging
            // fields into the local copy is how two versions of one task
            // start to disagree.
            if (at >= 0) list[at] = fresh
            renderBody()
          },
          onConflict: () => {
            state.find = { phase: 'notice', message: m('conflict'), data: rows() }
            renderBody()
            void runQuery()
          },
          onFailure: fail,
          announce,
        }),
      )
    }
    return li
  }

  function renderRows(rows: EntityRow[]): void {
    clear(list)
    rows.forEach((row, index) => list.appendChild(rowNode(row, index)))
    if (!list.parentElement) body.appendChild(list)
    input.setAttribute('aria-activedescendant', `hypha-opt-${state.selected}`)
    if (state.mode === 'list') {
      const node = list.children[state.selected]
      if (node instanceof HTMLElement) node.focus()
    }
  }

  function renderBody(): void {
    clear(body)
    if (!state.on) {
      body.appendChild(el('h2', { text: m('switchOffTitle') }))
      body.appendChild(el('p', { class: 'hypha__hint', text: m('switchOffBody') }))
      return
    }
    const conn = current()
    if (!conn) {
      renderNotConnected()
      return
    }
    if (conn.revoked) {
      body.appendChild(el('h2', { text: m('revokedTitle', conn.workspaceName) }))
      body.appendChild(el('p', { class: 'hypha__hint', text: m('revokedBody') }))
      body.appendChild(connectButton())
      body.appendChild(forgetButton(conn))
      return
    }
    if (state.capture) {
      body.appendChild(state.capture)
      return
    }
    if (host === 'sidepanel') {
      renderPageStrip()
      renderPinned()
    }
    // The sections are what the panel shows when nothing is being
    // searched. A query replaces them: two lists competing for the same
    // space is how a person loses track of which one answered.
    if (host === 'sidepanel' && !state.q.trim() && state.sections) {
      renderSections(state.sections)
      return
    }
    body.appendChild(list)
    renderOutcome(body, state.find, (rows) => renderRows(rows))
    if (state.degraded) body.appendChild(el('p', { class: 'hypha__notice', text: m('degraded') }))
    if (state.find.phase === 'ready' && state.find.empty === 'no-results-in-scope') {
      const wide = el('button', { type: 'button' }, [m('searchEverything')])
      on(wide, 'click', () => {
        void setScope({ ...state.scope, focus: null })
      })
      body.appendChild(wide)
    }
  }

  /** Forgetting the secret HERE is not revoking it THERE, and the panel
   *  says so: the credential ends on the server, in the app's settings,
   *  and that is the difference that matters if a machine is lost. */
  function forgetButton(conn: Connection): HTMLElement {
    const box = el('div')
    const button = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('disconnect')])
    on(button, 'click', () => {
      void (async () => {
        const res = await send('conn/forget', { workspaceId: conn.workspaceId })
        if (!res.ok) return fail(res.error)
        state.connections = res.data
        state.scope = { workspaceId: null, focus: null }
        renderAll()
      })()
    })
    box.append(button, el('p', { class: 'hypha__hint', text: m('disconnectNote') }))
    return box
  }

  function connectButton(): HTMLElement {
    const button = el('button', { type: 'button' }, [m('connect')])
    on(button, 'click', () => {
      void (async () => {
        const res = await send('conn/begin')
        if (!res.ok) return fail(res.error)
        // The worker opened the tab and is already waiting. This panel
        // closes the moment the person switches to that tab, which is
        // exactly why it is not the thing that waits.
        state.link = res.data
        renderAll()
      })()
    })
    return button
  }

  /** A request in flight: the code to compare, and what it is for.
   *
   *  The code is rendered large and on its own because it exists to be
   *  READ and checked against the approval screen. A control nobody reads
   *  defends nothing, and this one is the whole defence against approving
   *  a request somebody else opened.
   *
   *  The link is offered again rather than assumed: the tab may have been
   *  closed, landed in another profile, or never opened. Typing the code
   *  into the settings page is the path that always works, and the panel
   *  says so instead of leaving a reader with a code and nowhere to put
   *  it. */
  function renderWaiting(link: LinkRequest): void {
    if (link.failure) {
      body.appendChild(el('h2', { text: m('linkFailedTitle') }))
      body.appendChild(
        el('p', { class: 'hypha__hint', text: linkFailureMessage(link.failure) }),
      )
      body.appendChild(startOverButton())
      return
    }
    body.appendChild(el('h2', { text: m('linkWaitingTitle') }))
    body.appendChild(el('p', { class: 'hypha__code', text: link.userCode }))
    body.appendChild(el('p', { class: 'hypha__hint', text: m('linkCompare') }))
    const open = el('button', { type: 'button' }, [m('linkOpenPage')])
    on(open, 'click', () => {
      void chrome.tabs.create({ url: link.verificationUrl })
    })
    body.appendChild(open)
    body.appendChild(startOverButton(m('linkCancel')))
  }

  /** Each failure means a different next action, so each gets its own
   *  sentence rather than one "it did not work".
   *
   *  Static ``m()`` calls, one per branch, rather than returning a key
   *  the caller resolves: the catalogue checker can only verify a key it
   *  can READ in the source, and a key assembled or returned as a value
   *  is invisible to it. Written this way both halves are checked -- the
   *  key exists, and nothing is defined that nobody asks for. */
  function linkFailureMessage(code: FailureCode): string {
    if (code === 'denied') return m('linkDenied')
    if (code === 'expired') return m('linkExpired')
    if (code === 'network' || code === 'timeout') return m('linkUnreachable')
    return m('linkFailed')
  }

  function startOverButton(label = m('linkStartOver')): HTMLElement {
    const button = el('button', { type: 'button', class: 'hypha__linkbtn' }, [label])
    on(button, 'click', () => {
      void (async () => {
        await send('conn/cancelLink')
        state.link = null
        renderAll()
      })()
    })
    return button
  }

  function renderNotConnected(): void {
    if (state.link) {
      renderWaiting(state.link)
      return
    }
    body.appendChild(el('h2', { text: m('notConnectedTitle') }))
    body.appendChild(el('p', { class: 'hypha__hint', text: m('notConnectedBody') }))
    body.appendChild(connectButton())
  }

  function renderFooter(): void {
    clear(footer)
    const capture = el('button', { type: 'button' }, [m('captureTitle')])
    on(capture, 'click', () => void openCapture())
    if (state.on && current() && !current()?.revoked) footer.appendChild(capture)
    footer.appendChild(el('span', { class: 'hypha__hint', text: m('hintKeys') }))
    if (host === 'sidepanel') {
      // There is no push, so the panel says when it last looked and
      // offers to look again rather than implying it is live.
      const refresh = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('refresh')])
      on(refresh, 'click', () => void refreshSections())
      footer.appendChild(refresh)
    }
    const shortcuts = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('shortcuts')])
    on(shortcuts, 'click', () => {
      void chrome.tabs.create({ url: 'chrome://extensions/shortcuts' })
    })
    footer.appendChild(shortcuts)
  }

  function renderAll(): void {
    renderHeader()
    renderScope()
    renderBody()
    renderFooter()
  }

  function announce(text: string): void {
    live.textContent = text
  }

  function fail(failure: Failure): void {
    alerts.textContent = failure.message
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------

  function rows(): EntityRow[] {
    return state.find.phase === 'ready' ? state.find.data : []
  }

  function open(row: EntityRow, background: boolean): void {
    void send('find/opened', { row, q: state.q, rankedCount: state.rankedCount })
    // A new tab, never this one: the panel floats over a page somebody
    // was reading, and replacing it is destructive in a way the app's own
    // palette never is.
    void chrome.tabs.create({ url: row.route, active: !background })
  }

  async function setScope(next: ScopeSel): Promise<void> {
    const res = await send('scope/set', next)
    if (!res.ok) return fail(res.error)
    state.scope = res.data
    renderAll()
    void runQuery()
  }

  let debounce: ReturnType<typeof setTimeout> | undefined

  function scheduleQuery(): void {
    clearTimeout(debounce)
    // One debounce, here. The worker must not add a second: Chrome
    // already debounces omnibox input, and two layers is the classic
    // sluggish-by-300ms bug.
    debounce = setTimeout(() => void runQuery(), 150)
  }

  async function runQuery(): Promise<void> {
    const gen = (state.gen += 1)
    state.find = { phase: 'loading', since: Date.now() }
    renderBody()
    const res = await send('find/query', { q: state.q, gen })
    if (gen !== state.gen) return
    if (!res.ok) {
      state.find = { phase: 'error', failure: res.error }
      renderBody()
      return
    }
    state.rankedCount = res.data.rankedCount
    state.degraded = res.data.degraded
    state.unresolved = res.data.unresolved ?? []
    state.effectiveFocus = res.data.scope ?? null
    state.selected = 0
    const empty =
      res.data.rows.length > 0
        ? undefined
        : state.q.trim() === ''
          ? 'recents'
          : state.scope.focus
            ? 'no-results-in-scope'
            : 'no-results'
    state.find = { phase: 'ready', data: res.data.rows, ...(empty ? { empty } : {}) }
    renderScope()
    renderBody()
    announce(String(res.data.rows.length))
  }

  async function advance(row: EntityRow): Promise<void> {
    if (row.version === undefined) return
    const st = await send('task/states', { id: row.id })
    if (!st.ok) return fail(st.error)
    // The lowest-ordered terminal state the workflow offers, never a
    // state named "done" in this code: the machine is per workspace and
    // naming one here would be a second definition of it.
    const target = st.data.find((s) => s.isTerminal)
    if (!target) return
    const res = await send('task/setState', {
      id: row.id,
      expectedVersion: row.version,
      stateId: target.id,
    })
    if (!res.ok) {
      if (res.error.code === 'conflict') {
        // Never an auto-retry: replaying a stale write overwrites
        // somebody else's change. Reload and say so; the intent stays on
        // the same key.
        state.find = { phase: 'notice', message: m('conflict'), data: rows() }
        renderBody()
        void runQuery()
        return
      }
      return fail(res.error)
    }
    announce(m('saved'))
    void runQuery()
  }

  async function attach(row: EntityRow): Promise<void> {
    const res = await send('task/attachPage', { id: row.id, kind: row.kind })
    if (!res.ok) return fail(res.error)
    announce(m('attached'))
  }


  // ------------------------------------------------------------------
  // The side panel's own surface
  // ------------------------------------------------------------------

  /** The page you are on, updated from tab events rather than polled:
   *  a local read, no request. This is the thing a popup structurally
   *  cannot have -- by the time you have switched tabs it is closed. */
  function renderPageStrip(): void {
    const page = state.page
    if (!page?.url) return
    const strip = el('div', { class: 'hypha__strip' })
    /** @type {string} */
    let host_
    try {
      host_ = new URL(page.url).host
    } catch {
      // A tab whose URL we cannot parse still has a title worth showing.
      host_ = ''
    }
    strip.appendChild(el('p', { class: 'hypha__striptitle', text: page.title ?? host_ }))
    if (host_) strip.appendChild(el('p', { class: 'hypha__hint', text: host_ }))

    const actions = el('div', { class: 'hypha__field' })
    const asTask = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('captureAsTask')])
    on(asTask, 'click', () => void openCapture())
    actions.appendChild(asTask)
    const pin = state.pinned
    if (pin) {
      const attach = el('button', { type: 'button', class: 'hypha__linkbtn' }, [
        m('pageStripAttach'),
      ])
      on(attach, 'click', () => void attachTo(pin))
      actions.appendChild(attach)
    }
    strip.appendChild(actions)
    body.appendChild(strip)
  }

  /** One task kept above the list. Filtered out of the sections below,
   *  because repeating it costs a row in a panel that has few. */
  function renderPinned(): void {
    const pin = state.pinned
    if (!pin) return
    const box = el('section', { class: 'hypha__pinned' })
    box.appendChild(el('h2', { class: 'hypha__section', text: m('pinned') }))
    box.appendChild(rowNode(pin, -1))
    const unpin = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('unpin')])
    on(unpin, 'click', () => void setPinned(null))
    box.appendChild(unpin)
    body.appendChild(box)
  }

  function renderSections(all: Sections): void {
    const pinnedId = state.pinned?.id
    let index = 0
    for (const [title, rows_] of [
      [m('sectionDue'), all.due],
      [m('sectionPressing'), all.pressing],
      [m('sectionTouched'), all.touched],
    ] as [string, EntityRow[]][]) {
      const visible = rows_.filter((r) => r.id !== pinnedId)
      if (!visible.length) continue
      body.appendChild(el('h2', { class: 'hypha__section', text: title }))
      const ul = el('ul', { class: 'hypha__list', role: 'list' })
      for (const row of visible) {
        ul.appendChild(rowNode(row, index))
        index += 1
      }
      body.appendChild(ul)
    }
    if (state.syncedAt) {
      const at = new Date(state.syncedAt).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      })
      // Legible staleness beats pretended freshness: there is no push,
      // so the panel says when it last looked instead of implying now.
      body.appendChild(el('p', { class: 'hypha__hint', text: m('synced', at) }))
    }
  }

  async function setPinned(row: EntityRow | null): Promise<void> {
    const res = await send('pin/set', { row })
    if (!res.ok) return fail(res.error)
    state.pinned = res.data
    renderBody()
  }

  async function attachTo(row: EntityRow): Promise<void> {
    const res = await send('task/attachPage', { id: row.id, kind: row.kind })
    if (!res.ok) return fail(res.error)
    announce(m('attached'))
  }

  async function refreshSections(): Promise<void> {
    if (host !== 'sidepanel') return
    const [secs, pin, page] = await Promise.all([
      send('panel/sections'),
      send('pin/get'),
      send('capture/context'),
    ])
    if (secs.ok) {
      state.sections = secs.data
      state.syncedAt = Date.now()
    }
    if (pin.ok) state.pinned = pin.data
    if (page.ok) state.page = page.data
    renderBody()
  }

  async function openCapture(): Promise<void> {
    const page = await send('capture/context')
    if (!page.ok) return fail(page.error)
    state.capture = renderCapture(page.data, host, {
      onDone: (message) => {
        announce(message)
        state.capture = null
        renderBody()
      },
      onFailure: fail,
      onClose: () => {
        state.capture = null
        renderBody()
      },
    })
    renderBody()
  }

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------

  function move(delta: number): void {
    const count = rows().length
    if (!count) return
    state.selected = (state.selected + delta + count) % count
    renderBody()
  }

  on(input, 'input', () => {
    state.q = input.value
    scheduleQuery()
  })

  on(input, 'keydown', (event) => {
    const rowList = rows()
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        move(1)
        return
      case 'ArrowUp':
        event.preventDefault()
        move(-1)
        return
      case 'Enter': {
        const row = rowList[state.selected]
        if (!row) return
        event.preventDefault()
        if (event.shiftKey) {
          state.expanded = state.expanded === row.id ? null : row.id
          renderBody()
          return
        }
        open(row, event.metaKey || event.ctrlKey)
        return
      }
      case 'Tab':
        if (event.shiftKey || !rowList.length) return
        event.preventDefault()
        state.mode = 'list'
        renderBody()
        announce(m('resultsLabel'))
        return
      case 'Escape':
        if (state.capture) {
          event.preventDefault()
          state.capture = null
          renderBody()
          return
        }
        if (state.q) {
          // Clear, then close. The app's palette closes on the first
          // Escape because it lives inside the app; here the first one
          // has somewhere useful to go.
          event.preventDefault()
          state.q = ''
          input.value = ''
          void runQuery()
          return
        }
        if (host === 'popup') window.close()
        return
      default:
        return
    }
  })

  on(list, 'keydown', (event) => {
    if (state.mode !== 'list') return
    const row = rows()[state.selected]
    if (!row) return
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        return move(1)
      case 'ArrowUp':
        event.preventDefault()
        return move(-1)
      case 'Enter':
        event.preventDefault()
        return open(row, event.metaKey || event.ctrlKey)
      case '/':
      case 'Escape':
        event.preventDefault()
        state.mode = 'query'
        renderBody()
        input.focus()
        return
      case 'x':
        event.preventDefault()
        void advance(row)
        return
      case 'a':
        event.preventDefault()
        void attach(row)
        return
      case 'c':
        event.preventDefault()
        void navigator.clipboard.writeText(row.code)
        announce(m('copyCode'))
        return
      case 'e':
        event.preventDefault()
        state.expanded = state.expanded === row.id ? null : row.id
        renderBody()
        return
      case 'p':
        if (host !== 'sidepanel') return
        event.preventDefault()
        void setPinned(state.pinned?.id === row.id ? null : row)
        return
      default:
        return
    }
  })

  // ------------------------------------------------------------------
  // Start
  // ------------------------------------------------------------------

  void (async () => {
    const [sw, conns, scope, left] = await Promise.all([
      send('switch/get'),
      send('conn/list'),
      send('scope/get'),
      send('log/sinceYouLeft'),
    ])
    if (sw.ok) state.on = sw.data
    if (conns.ok) state.connections = conns.data
    // Asked on every open, because the worker is what waits: a person who
    // approves in the tab and comes back finds the panel connected, and
    // one who comes back early finds the code still on screen.
    const link = await send('conn/link')
    if (link.ok) state.link = link.data
    if (scope.ok) state.scope = scope.data
    renderAll()
    // A write that finished after the panel closed has nobody to tell.
    // Reporting it on the next open is what makes "you may close it
    // mid-write" a promise rather than a hope.
    if (left.ok && (left.data.ok > 0 || left.data.failed.length > 0)) {
      announce(
        left.data.failed.length
          ? m('sinceYouLeftFailed', left.data.failed.join(', '))
          : m('sinceYouLeftOk', String(left.data.ok)),
      )
    }
    input.focus()
    if (state.on && current() && !current()?.revoked) {
      void runQuery()
      void refreshSections()
    }
  })()

  if (host === 'sidepanel') {
    // No websocket and no push, so freshness is bounded and SAID rather
    // than pretended. The timer runs only while the panel is visible and
    // the window has focus: a panel nobody is looking at must not poll.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void refreshSections()
    })
    setInterval(() => {
      if (document.visibilityState === 'visible' && document.hasFocus()) void refreshSections()
    }, 30_000)
  }
}
