// Every key this extension writes, in one module, with what each one is.
//
// Where a value lives is a decision, not a convenience:
//
//   local    survives a browser restart. The credential is here, because
//            the alternative is re-running the connect ceremony every
//            morning -- which trains a person to click through a security
//            screen, the exact habit it exists to prevent.
//   session  dies with the browser process. The connect request, the
//            caches, and an unsent draft.
//
// ATTRIBUTABLE marks a key that holds something about the person: their
// workspace names, the titles of what they opened, their unsent work.
// `clearForWorkspace` drops those and leaves the rest.
//
// One exception, and it is deliberate: a token expiry does NOT clear an
// unsent draft. An expiry is routine on a bad connection; it is not a
// request to erase, and the draft is the person's own work rather than a
// copy of their data.
//
// What this does NOT protect against: anything running as this Chrome
// profile. chrome.storage.local is a database in the profile directory,
// not additionally encrypted, so a process that can read the profile
// reads a live credential. Full-disk encryption is the control, and it is
// the operating system's, not ours.

import type { RecentItem } from '@shared'
import type { Connection, EntityRow, LinkRequest, ScopeSel } from '../shared/protocol'

const LOCAL = {
  /** Master switch. Absent means ON: a fresh install works, and turning
   *  it off is a deliberate act. */
  switch: 'switch',
  /** `conn:<workspaceId>` -> Connection & { secret }. ATTRIBUTABLE. */
  connPrefix: 'conn:',
  /** `scope:<workspaceId>` -> the focus selection. ATTRIBUTABLE. */
  scopePrefix: 'scope:',
  /** `recents:<workspaceId>` -> EntityRow[]. Carries TITLES, so it is
   *  attributable and is per workspace: one flat key would list
   *  workspace A's titles while you are in B. */
  recentsPrefix: 'recents:',
  /** The workspace the panel is currently looking at. */
  activeWorkspace: 'activeWorkspace',
  /** `pin:<workspaceId>` -> one task kept above the list. Carries a
   *  TITLE, so it is attributable and per workspace. */
  pinPrefix: 'pin:',
} as const

const SESSION = {
  /** The connect request in flight: the short code, where the person was
   *  sent, when it lapses, and the long code that collects. SESSION, not
   *  local: an unanswered ceremony is not worth surviving a browser
   *  restart, and the long code is the one value here that collects
   *  anything. */
  link: 'link',
  /** `cache:<workspaceId>:<key>` -> a TTL entry. ATTRIBUTABLE. */
  cachePrefix: 'cache:',
  /** `draft:<kind>` -> an unsent capture. The person's own work. */
  draftPrefix: 'draft:',
  /** Outcomes of writes that finished after the panel closed. */
  sinceYouLeft: 'sinceYouLeft',
} as const

/** The stored half of a connect request: what the panel sees, plus the
 *  long code, which it does not. */
export interface StoredLink extends LinkRequest {
  deviceCode: string
}

export interface StoredConnection extends Connection {
  secret: string
}

async function readLocal<T>(key: string): Promise<T | undefined> {
  return (await chrome.storage.local.get(key))[key] as T | undefined
}

async function readSession<T>(key: string): Promise<T | undefined> {
  return (await chrome.storage.session.get(key))[key] as T | undefined
}

export const storage = {
  async isOn(): Promise<boolean> {
    return (await readLocal<boolean>(LOCAL.switch)) ?? true
  },
  async setOn(on: boolean): Promise<void> {
    await chrome.storage.local.set({ [LOCAL.switch]: on })
  },

  async connections(): Promise<StoredConnection[]> {
    const all = await chrome.storage.local.get(null)
    return Object.entries(all)
      .filter(([k]) => k.startsWith(LOCAL.connPrefix))
      .map(([, v]) => v as StoredConnection)
  },
  async connection(workspaceId: string): Promise<StoredConnection | undefined> {
    return readLocal<StoredConnection>(LOCAL.connPrefix + workspaceId)
  },
  async putConnection(conn: StoredConnection): Promise<void> {
    await chrome.storage.local.set({ [LOCAL.connPrefix + conn.workspaceId]: conn })
  },
  async forgetConnection(workspaceId: string): Promise<void> {
    await chrome.storage.local.remove([
      LOCAL.connPrefix + workspaceId,
      LOCAL.scopePrefix + workspaceId,
      LOCAL.recentsPrefix + workspaceId,
      LOCAL.pinPrefix + workspaceId,
    ])
    await clearCaches(workspaceId)
  },

  async activeWorkspace(): Promise<string | null> {
    return (await readLocal<string>(LOCAL.activeWorkspace)) ?? null
  },
  async setActiveWorkspace(workspaceId: string | null): Promise<void> {
    if (workspaceId === null) await chrome.storage.local.remove(LOCAL.activeWorkspace)
    else await chrome.storage.local.set({ [LOCAL.activeWorkspace]: workspaceId })
  },

  async scope(workspaceId: string): Promise<ScopeSel['focus']> {
    return (await readLocal<ScopeSel['focus']>(LOCAL.scopePrefix + workspaceId)) ?? null
  },
  async setScope(workspaceId: string, focus: ScopeSel['focus']): Promise<void> {
    await chrome.storage.local.set({ [LOCAL.scopePrefix + workspaceId]: focus })
  },

  /** Stored in the SHARED shape, the same one the app's palette keeps,
   *  so the rule about what a recents row is has one definition. The
   *  panel derives its display fields from it. */
  async pinned(workspaceId: string): Promise<EntityRow | null> {
    return (await readLocal<EntityRow>(LOCAL.pinPrefix + workspaceId)) ?? null
  },
  async setPinned(workspaceId: string, row: EntityRow | null): Promise<void> {
    if (row === null) await chrome.storage.local.remove(LOCAL.pinPrefix + workspaceId)
    else await chrome.storage.local.set({ [LOCAL.pinPrefix + workspaceId]: row })
  },

  async recents(workspaceId: string): Promise<RecentItem[]> {
    return (await readLocal<RecentItem[]>(LOCAL.recentsPrefix + workspaceId)) ?? []
  },
  async setRecents(workspaceId: string, rows: RecentItem[]): Promise<void> {
    await chrome.storage.local.set({ [LOCAL.recentsPrefix + workspaceId]: rows })
  },

  async getLink(): Promise<StoredLink | undefined> {
    return readSession<StoredLink>(SESSION.link)
  },
  async setLink(link: StoredLink): Promise<void> {
    await chrome.storage.session.set({ [SESSION.link]: link })
  },
  async clearLink(): Promise<void> {
    await chrome.storage.session.remove(SESSION.link)
  },

  async sinceYouLeft(): Promise<{ ok: number; failed: string[] }> {
    return (await readSession<{ ok: number; failed: string[] }>(SESSION.sinceYouLeft)) ?? {
      ok: 0,
      failed: [],
    }
  },
  async noteOutcome(ok: boolean, what: string): Promise<void> {
    const cur = await storage.sinceYouLeft()
    const next = ok
      ? { ok: cur.ok + 1, failed: cur.failed }
      : // Bounded: a ring, not a log. Ten is more than anyone reads and
        // small enough that a stuck loop cannot fill session storage.
        { ok: cur.ok, failed: [...cur.failed, what].slice(-10) }
    await chrome.storage.session.set({ [SESSION.sinceYouLeft]: next })
  },
  async clearSinceYouLeft(): Promise<void> {
    await chrome.storage.session.remove(SESSION.sinceYouLeft)
  },

  cacheKey(workspaceId: string, key: string): string {
    return `${SESSION.cachePrefix}${workspaceId}:${key}`
  },
  async readCache<T>(fullKey: string): Promise<T | undefined> {
    return readSession<T>(fullKey)
  },
  async writeCache<T>(fullKey: string, value: T): Promise<void> {
    await chrome.storage.session.set({ [fullKey]: value })
  },
}

/** Drop every cached answer for one workspace. Called on disconnect, on a
 *  401 and on a workspace switch: a cached row from workspace A must
 *  never be shown while looking at B, because an id is only unique inside
 *  a tenant. */
export async function clearCaches(workspaceId: string): Promise<void> {
  const all = await chrome.storage.session.get(null)
  const doomed = Object.keys(all).filter((k) =>
    k.startsWith(`${SESSION.cachePrefix}${workspaceId}:`),
  )
  if (doomed.length) await chrome.storage.session.remove(doomed)
}

/** Everything attributable, for every workspace. Sign-out, not expiry:
 *  drafts go too, because this is a deliberate act. */
export async function clearEverything(): Promise<void> {
  const local = await chrome.storage.local.get(null)
  const doomed = Object.keys(local).filter(
    (k) =>
      k.startsWith(LOCAL.connPrefix) ||
      k.startsWith(LOCAL.scopePrefix) ||
      k.startsWith(LOCAL.recentsPrefix) ||
      k.startsWith(LOCAL.pinPrefix) ||
      k === LOCAL.activeWorkspace,
  )
  if (doomed.length) await chrome.storage.local.remove(doomed)
  await chrome.storage.session.clear()
}
