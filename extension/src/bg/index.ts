// The service worker: the only network point, the only token holder, and
// the one place the on/off switch has to be checked.
//
// The switch is checked HERE, before the handler table, so an operation
// added later is off by default when the extension is off. That is the
// direction a mistake should fall, and it is only a guarantee because
// nothing else in this package can reach the network.

import { ALWAYS_AVAILABLE, type Envelope, type OperationName, type Operations, type Result } from '../shared/protocol'
import { acceptHandover, beginConnect } from './connection'
import { call } from './api'
import { config } from './config'
import { create, pageContext, screenshot, sourceLine } from './capture'
import { findClients, findProjects, readScope, writeScope } from './scope'
import { opened, query } from './find'
import { sections } from './sections'
import { appendPage, patch, setState, states } from './tasks'
import { clearCaches, storage, type StoredConnection } from './storage'

// --------------------------------------------------------------------------
// Browser-level surfaces
// --------------------------------------------------------------------------

const MENU = { task: 'myc-capture-task', note: 'myc-capture-note' } as const

/** Bring the surfaces Chrome PERSISTS in line with the switch.
 *
 *  Context menus survive a browser restart, so an extension turned off
 *  would otherwise keep live entries that answer "disabled" when clicked
 *  -- which reads as broken rather than as off. Run on install, on
 *  browser start, and on every flip. */
async function syncSurfaces(): Promise<void> {
  const on = await storage.isOn()
  await chrome.contextMenus.removeAll()
  if (!on) return
  chrome.contextMenus.create({
    id: MENU.task,
    title: chrome.i18n.getMessage('menuCaptureTask'),
    contexts: ['selection', 'page', 'link'],
  })
  chrome.contextMenus.create({
    id: MENU.note,
    title: chrome.i18n.getMessage('menuCaptureNote'),
    contexts: ['selection', 'page', 'link'],
  })
}

chrome.runtime.onInstalled.addListener(() => void syncSurfaces())
chrome.runtime.onStartup.addListener(() => void syncSurfaces())

// The side panel opens on a user gesture, and a keyboard command is one.
// It deliberately cannot open itself on navigation: a panel that appears
// over someone's page uninvited is hostile.
chrome.commands.onCommand.addListener((command, tab) => {
  void (async () => {
    if (!(await storage.isOn())) return
    if (command === 'open-side-panel' && tab?.windowId !== undefined) {
      await chrome.sidePanel.open({ windowId: tab.windowId })
    }
  })()
})

chrome.contextMenus.onClicked.addListener((info, tab) => {
  void (async () => {
    if (!(await storage.isOn())) return
    if (info.menuItemId !== MENU.task && info.menuItemId !== MENU.note) return
    if (tab?.windowId === undefined) return
    // The draft is assembled by the panel, which can show it and let a
    // person correct it before anything is written.
    await chrome.storage.session.set({
      'draft:pending': {
        kind: info.menuItemId === MENU.task ? 'task' : 'note',
        // Chrome hands the menu a TRUNCATED selection. The panel asks for
        // the full one, and falls back to this when injection is refused.
        selection: info.selectionText ?? null,
        url: info.linkUrl ?? info.pageUrl ?? null,
      },
    })
    await chrome.sidePanel.open({ windowId: tab.windowId })
  })()
})

// The omnibox handler exists in the worker and nowhere else, which is the
// reason search lives here rather than in a panel.
chrome.omnibox.setDefaultSuggestion({
  description: chrome.i18n.getMessage('omniboxHint'),
})

chrome.omnibox.onInputChanged.addListener((text, suggest) => {
  void (async () => {
    const conn = await activeConnection()
    if (!conn || !text.trim()) return suggest([])
    const scope = await readScope()
    const res = await query(conn, scope, text)
    if (!res.ok) return suggest([])
    suggest(
      res.data.rows.slice(0, 6).map((row) => ({
        content: row.route,
        // Omnibox descriptions are parsed as markup, so anything that
        // came from a task title has to be escaped.
        description: `${escapeXml(row.code)} — ${escapeXml(row.title)}`,
      })),
    )
  })()
})

chrome.omnibox.onInputEntered.addListener((text, disposition) => {
  void (async () => {
    if (!(await storage.isOn())) return
    // The default suggestion is never a guess: free text opens a SEARCH
    // that exists rather than a permalink built from words, which is how
    // a palette lands someone on a 404.
    const target = text.startsWith('http')
      ? text
      : `${config.origin}/tasks?q=${encodeURIComponent(text.trim())}`
    if (disposition === 'currentTab') await chrome.tabs.update({ url: target })
    else
      await chrome.tabs.create({
        url: target,
        active: disposition === 'newForegroundTab',
      })
  })()
})

function escapeXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// --------------------------------------------------------------------------
// The connect handover
// --------------------------------------------------------------------------

chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  // sender.origin is filled in by Chrome and cannot be forged by the
  // page, which is the whole reason this is externally_connectable and
  // not a content script reading the document.
  void acceptHandover(message, sender.origin).then(sendResponse)
  return true
})

// --------------------------------------------------------------------------
// The operation seam
// --------------------------------------------------------------------------

async function activeConnection(): Promise<StoredConnection | null> {
  const workspaceId = await storage.activeWorkspace()
  if (!workspaceId) return null
  const conn = await storage.connection(workspaceId)
  if (!conn || conn.revoked || !conn.secret) return null
  return conn
}

type Handler<K extends OperationName> = (
  payload: Operations[K]['req'],
  conn: StoredConnection,
) => Promise<Result<Operations[K]['res']>>

const ok = <T>(data: T): Result<T> => ({ ok: true, data })

const handlers: { [K in OperationName]: Handler<K> } = {
  'switch/get': async () => ok(await storage.isOn()),
  'switch/set': async ({ on }) => {
    await storage.setOn(on)
    await syncSurfaces()
    return ok(on)
  },

  'conn/list': async () => ok((await storage.connections()).map(({ secret: _s, ...rest }) => rest)),
  'conn/begin': async () => ok(await beginConnect()),
  'conn/forget': async ({ workspaceId }) => {
    // Forgets the secret HERE. It does not revoke on the server, and the
    // panel says so: the app's settings page is where a credential
    // actually ends, and that difference matters if a machine is lost.
    //
    // Forgetting one row of an account-bound credential would forget a
    // name and keep the secret, under the other rows it also stands
    // behind. "Disconnect" has to mean the secret is gone from this
    // machine, so every row of that credential goes with it.
    const target = await storage.connection(workspaceId)
    const rows = await storage.connections()
    const doomed = target
      ? rows.filter((r) => r.assistantId === target.assistantId).map((r) => r.workspaceId)
      : [workspaceId]
    for (const id of doomed) await storage.forgetConnection(id)
    const active = await storage.activeWorkspace()
    if (active !== null && doomed.includes(active)) await storage.setActiveWorkspace(null)
    return ok((await storage.connections()).map(({ secret: _s, ...rest }) => rest))
  },
  'conn/self': async (_p, conn) => {
    const res = await call<{ scope: string[] | null }>(conn, '/agent/self')
    if (!res.ok) return res
    // What the SERVER says this credential may do, rather than what was
    // granted at connect time: an assistant's scope can be edited in the
    // app afterwards, and a panel offering a control the server will
    // refuse is advertising a capability that does not exist.
    await storage.putConnection({ ...conn, scope: res.data.scope ?? [] })
    return ok({ scope: res.data.scope })
  },

  'scope/get': async () => ok(await readScope()),
  'scope/set': async (next) => {
    const previous = await storage.activeWorkspace()
    if (previous && previous !== next.workspaceId) await clearCaches(previous)
    return ok(await writeScope(next))
  },
  'scope/clients': async ({ q }, conn) => findClients(conn, q),
  'scope/projects': async ({ q, clientTagId }, conn) => findProjects(conn, q, clientTagId),

  'find/query': async ({ q }, conn) => query(conn, await readScope(), q),
  'find/recents': async (_p, conn) => {
    const res = await query(conn, await readScope(), '')
    if (!res.ok) return res
    return ok(res.data.rows)
  },
  'find/opened': async ({ row, q, rankedCount }, conn) => {
    await opened(conn, row, q, rankedCount)
    return ok(undefined)
  },

  'task/states': async ({ id }, conn) => states(conn, id),
  'task/patch': async ({ id, expectedVersion, fields }, conn) => {
    const res = await patch(conn, id, expectedVersion, fields)
    await storage.noteOutcome(res.ok, res.ok ? '' : `task ${id.slice(0, 8)}`)
    return res
  },
  'task/setState': async ({ id, expectedVersion, stateId }, conn) => {
    const res = await setState(conn, id, expectedVersion, stateId, '')
    await storage.noteOutcome(res.ok, res.ok ? '' : `task ${id.slice(0, 8)}`)
    return res
  },
  'task/attachPage': async ({ id, kind }, conn) => {
    const page = await pageContext()
    const line = sourceLine(page.url, page.title)
    if (!line) {
      return {
        ok: false,
        error: { code: 'invalid', message: 'nothing linkable on this page', retryable: false },
      }
    }
    return appendPage(conn, id, kind, line)
  },

  'panel/sections': async (_p, conn) => sections(conn),
  'pin/get': async (_p, conn) => ok(await storage.pinned(conn.workspaceId)),
  'pin/set': async ({ row }, conn) => {
    await storage.setPinned(conn.workspaceId, row)
    return ok(row)
  },

  'capture/context': async () => ok(await pageContext()),
  'capture/screenshot': async () => screenshot(),
  'capture/create': async (draft, conn) => {
    const res = await create(conn, draft)
    await storage.noteOutcome(res.ok, res.ok ? '' : draft.title)
    return res
  },

  'log/sinceYouLeft': async () => {
    const seen = await storage.sinceYouLeft()
    await storage.clearSinceYouLeft()
    return ok(seen)
  },
}

async function dispatch(envelope: Envelope): Promise<Result<unknown>> {
  const handler = handlers[envelope.op] as Handler<OperationName> | undefined
  if (!handler) {
    return {
      ok: false,
      error: { code: 'not_found', message: `unknown operation ${envelope.op}`, retryable: false },
    }
  }
  // The switch, before the table. An operation added later inherits the
  // refusal rather than the exemption.
  if (!ALWAYS_AVAILABLE.has(envelope.op) && !(await storage.isOn())) {
    return { ok: false, error: { code: 'disabled', message: 'switched off', retryable: false } }
  }
  const conn = await activeConnection()
  if (!conn && !ALWAYS_AVAILABLE.has(envelope.op)) {
    return {
      ok: false,
      error: { code: 'disconnected', message: 'no credential', retryable: false },
    }
  }
  try {
    return await handler(envelope.payload, conn as StoredConnection)
  } catch (cause) {
    return {
      ok: false,
      error: {
        code: 'server',
        message: cause instanceof Error ? cause.message : String(cause),
        retryable: true,
      },
    }
  }
}

chrome.runtime.onMessage.addListener((message: Envelope, _sender, sendResponse) => {
  void dispatch(message).then(sendResponse)
  // Keeps the channel open for the async answer above. Returning nothing
  // here closes it and every caller sees `undefined`.
  return true
})
