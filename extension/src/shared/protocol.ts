// The seam between the panel documents and the service worker.
//
// The panels render and take input. The worker owns the credential, every
// fetch, every cache and the on/off switch. Nothing crosses that line
// except a message described here, and the map below is what makes the
// crossing checkable: `send` reads the request and response types off the
// operation name, so a call site cannot pass the wrong arguments or
// misread the answer.
//
// Why the worker owns it all, since the panels are the same origin and so
// this is not a trust boundary:
//
//   - the omnibox handler fires in the worker and nowhere else, so search
//     living in a panel would mean two implementations of one rule;
//   - Chrome destroys the popup the instant it loses focus, and a write
//     issued from there has an unknown outcome. Issued by the worker, it
//     completes. That is the promise: you may close the panel mid-write;
//   - one module then owns every stored key and the single function that
//     clears them;
//   - and there is exactly one place that can reach the network, which is
//     what makes the on/off switch a guarantee rather than a hope.

import type { ConnectReply } from '@shared'

export type FailureCode =
  // No response at all: offline, DNS, connection reset.
  | 'network'
  // Our deadline expired. THE WRITE MAY HAVE HAPPENED. This is its own
  // code because it is the one failure where telling the user to press
  // the button again can duplicate their work.
  | 'timeout'
  // 401. An agent token has no refresh, so there is nothing to retry.
  | 'unauthenticated'
  // 403: scope or role. Carries the server's own sentence.
  | 'forbidden'
  | 'not_found'
  // 409, with the version the caller should have presented.
  | 'conflict'
  // 400 / 422: the server's own sentence again.
  | 'invalid'
  | 'server'
  // Refused by the HOST, not the server, and the two imply opposite next
  // actions: reconnect or turn the switch on, versus ask for scope.
  | 'disconnected'
  | 'disabled'

export interface Failure {
  code: FailureCode
  /** Already localized when it came from the server; a catalogue sentence
   *  when it did not. Never branch on it -- branch on `code`. */
  message: string
  /** Whether pressing the same button again can succeed AND cannot
   *  duplicate anything. A version-guarded write is retryable even after
   *  a timeout, because a first write that landed moves the version and
   *  the retry conflicts. A create is not. */
  retryable: boolean
  /** Populated on `conflict`: what the row's version actually is now. */
  currentVersion?: number
  /** The identifier the server logged this under, verbatim, so a person
   *  can quote it. Absent on a timeout, which has no response to read --
   *  the client's own value is then all there is, and it resolves in the
   *  access log if the request arrived and nowhere if it did not. */
  correlationId?: string
}

export type Result<T> = { ok: true; data: T } | { ok: false; error: Failure }

/** Where the panel is looking. ONE tag id, never a list: every task
 *  carries both its client tag and its project tag, and the server's tag
 *  filter is a faceted AND, so sending a client together with its
 *  projects matches nothing at all. Null is "everything". */
export interface ScopeSel {
  workspaceId: string | null
  focus: { tagId: string; kind: 'client' | 'project'; name: string } | null
}

export interface Connection {
  workspaceId: string
  workspaceName: string
  assistantId: string
  scope: string[]
  /** Which workspaces the credential behind this row reaches, as the
   *  SERVER reports it. ``account`` rows share one secret across every
   *  workspace their holder belongs to, so there is one row per
   *  workspace and one credential behind all of them -- which is why
   *  revoking is per credential and forgetting is per row. */
  binding?: 'workspace' | 'account'
  /** Set when the credential stopped authenticating, so the panel can
   *  say which workspace needs reconnecting rather than logging
   *  everything out. */
  revoked?: boolean
}

export interface EntityRow {
  kind: 'task' | 'note'
  id: string
  title: string
  /** The 8-hex code a person pastes into a note. */
  code: string
  route: string
  snippet?: string | null
  stateId?: string | null
  state?: string | null
  priority?: number | null
  dueDate?: string | null
  isArchived?: boolean
  version?: number
  projectName?: string | null
  /** 1-based rank in the RANKED list, and absent for a recents row, a
   *  code resolution or a locally filtered row. Reporting a fabricated
   *  rank would poison the recall sensor rather than merely miss it. */
  rank?: number
}

/** The three lists the side panel keeps, chosen so that none of them
 *  requires the client to NAME a workflow state. The state machine is
 *  per workspace, so a panel asking for "in progress" would be a second
 *  definition of it -- the one that drifts the day somebody renames a
 *  state. Ordering and openness the server already understands. */
export interface Sections {
  /** Open, ordered by due date, earliest first. */
  due: EntityRow[]
  /** Open, ordered by the derived priority. */
  pressing: EntityRow[]
  /** Whatever moved most recently, open or not. */
  touched: EntityRow[]
}

export interface FindResult {
  rows: EntityRow[]
  /** How many ranked hits there were, so a rank reads as a fraction. */
  rankedCount: number
  /** True when the semantic leg timed out and only text matches are
   *  shown. The panel says so rather than presenting a thinner answer as
   *  the whole answer. */
  degraded: boolean
  /** Atoms the query line could not honour. Dropped from the request and
   *  shown, because the query that ran is not the query that was typed
   *  and only the person can tell whether the difference matters. */
  unresolved?: string[]
  /** The scope the search actually ran under, after any inline `in:`
   *  override. The panel reads its chips back from this rather than from
   *  what it asked for, so the line cannot claim a narrowing the server
   *  did not apply. */
  scope?: ScopeSel['focus']
}

export interface PageContext {
  url: string | null
  title: string | null
  selection: string | null
  /** Why the selection is absent: Chrome refuses injection on its own
   *  pages, the store and the PDF viewer. Capture still works from the
   *  title and the URL, so this is a notice, not an error. */
  selectionBlocked: boolean
}

export interface CaptureDraft {
  kind: 'task' | 'note'
  /** Minted once when the sheet opens, not per request. A retry of the
   *  SAME capture has to present the SAME key or the server cannot tell
   *  it from a second capture, and the claim buys nothing. */
  idempotencyKey: string
  title: string
  body: string
  projectTagId: string | null
  clientTagId: string | null
  /** Data URLs, from a screenshot of the visible tab or a file the user
   *  chose. Uploaded after the entity exists, one request each. */
  attachments: { name: string; mime: string; dataUrl: string }[]
}

export interface CaptureResult {
  kind: 'task' | 'note'
  id: string
  code: string
  route: string
  /** A create followed by N uploads is two phases, and the second half
   *  can fail on its own. Saying "created, one file did not attach" is
   *  the truth; a blanket failure would send someone hunting for a task
   *  that is already there. */
  attachmentsFailed: string[]
}

export interface TaskPatch {
  title?: string
  importance?: number
  urgency?: number
  dueDate?: string | null
  assigneeId?: string | null
  projectTagId?: string
  clientTagId?: string
}

/** Request and response payload for every operation, keyed by name. */
export interface Operations {
  'switch/get': { req: void; res: boolean }
  'switch/set': { req: { on: boolean }; res: boolean }

  'conn/list': { req: void; res: Connection[] }
  'conn/begin': { req: void; res: { url: string } }
  'conn/forget': { req: { workspaceId: string }; res: Connection[] }
  'conn/self': { req: { workspaceId: string }; res: { scope: string[] | null } }

  'scope/get': { req: void; res: ScopeSel }
  'scope/set': { req: ScopeSel; res: ScopeSel }
  'scope/clients': { req: { q: string }; res: { id: string; name: string }[] }
  'scope/projects': {
    req: { q: string; clientTagId?: string }
    res: { id: string; name: string; clientTagId: string }[]
  }

  'find/query': { req: { q: string; gen: number }; res: FindResult }
  'find/recents': { req: void; res: EntityRow[] }
  'find/opened': { req: { row: EntityRow; q: string; rankedCount: number }; res: void }

  'task/states': { req: { id: string }; res: { id: string; name: string; isTerminal: boolean }[] }
  'task/patch': { req: { id: string; expectedVersion: number; fields: TaskPatch }; res: EntityRow }
  'task/setState': { req: { id: string; expectedVersion: number; stateId: string }; res: EntityRow }
  'task/attachPage': { req: { id: string; kind: 'task' | 'note' }; res: void }

  'panel/sections': { req: void; res: Sections }
  'pin/get': { req: void; res: EntityRow | null }
  'pin/set': { req: { row: EntityRow | null }; res: EntityRow | null }

  'capture/context': { req: void; res: PageContext }
  'capture/screenshot': { req: void; res: { name: string; mime: string; dataUrl: string } }
  'capture/create': { req: CaptureDraft; res: CaptureResult }

  /** Outcomes of writes that completed after the panel closed. The
   *  promise that a write survives a dismissed popup is only credible if
   *  the landing is reported, so successes are here too, not just
   *  failures. */
  'log/sinceYouLeft': { req: void; res: { ok: number; failed: string[] } }
}

export type OperationName = keyof Operations

export interface Envelope<K extends OperationName = OperationName> {
  op: K
  payload: Operations[K]['req']
}

/** Operations that stay available with the switch OFF, because they are
 *  the switch, or the connection it gates. Everything else is refused at
 *  the seam -- so an operation added later is off by default, which is
 *  the direction a mistake should fall. */
export const ALWAYS_AVAILABLE: ReadonlySet<OperationName> = new Set<OperationName>([
  'switch/get',
  'switch/set',
  'conn/list',
  'conn/begin',
  'conn/forget',
  'scope/get',
])

/** Call the worker. NEVER rejects: a dead worker, a thrown handler and a
 *  refused operation all come back as a Failure, because every call site
 *  has to render an outcome either way and a throw would mean each one
 *  invents its own. */
export async function send<K extends OperationName>(
  op: K,
  payload: Operations[K]['req'] = undefined as Operations[K]['req'],
): Promise<Result<Operations[K]['res']>> {
  try {
    const reply: unknown = await chrome.runtime.sendMessage({ op, payload } satisfies Envelope<K>)
    if (!reply || typeof reply !== 'object') {
      return {
        ok: false,
        error: { code: 'server', message: 'no reply from the worker', retryable: true },
      }
    }
    return reply as Result<Operations[K]['res']>
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

export type { ConnectReply }
