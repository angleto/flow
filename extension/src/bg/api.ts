// The only place in this extension that reaches the network.
//
// Everything else asks the worker, and the worker asks the server. That
// is what makes the on/off switch a guarantee rather than a hope: there
// is one function to gate, and gating it provably stops every outbound
// request. It is also why no CORS grant is needed on the server -- the
// worker's host permission covers its fetches, so the deployment's
// allowlist, which also fronts the machine-to-machine surfaces, does not
// have to learn about a browser-controlled origin.
//
// Four things happen here and nowhere else:
//
//   a deadline          fetch has none of its own and will wait as long
//                       as the network will, holding a promise, a loading
//                       state and whatever the panel mounted around it;
//   a correlation id    minted per request and sent, so one user action
//                       that fans out is one thread through the server's
//                       log, and a timeout -- which has no response to
//                       read -- still resolves in the access log if the
//                       request arrived;
//   failure mapping     one status-to-code translation, so a caller
//                       branches on a stable code and never on prose;
//   the 401 answer      an agent token has no refresh, so there is
//                       nothing to retry and the credential is dropped.

import { errCode, errMessage } from '@shared'
import { config } from './config'
import { clearCaches, storage, type StoredConnection } from './storage'
import type { Failure, FailureCode, Result } from '../shared/protocol'

/** Read deadline. Comfortably above the server's own 2s semantic-search
 *  budget, so a query that degrades to text matches still ARRIVES: a
 *  deadline near that number turns the same query into a coin flip
 *  between "no results" and "lexical results". */
const READ_DEADLINE_MS = 8_000
/** Writes and uploads. Longer, because an attachment is bytes and the
 *  wrong answer here is telling someone their save failed when it
 *  landed. */
const WRITE_DEADLINE_MS = 20_000

export interface CallOptions {
  method?: string
  body?: unknown
  /** Multipart, for an attachment. Sent instead of `body`. */
  form?: FormData
  query?: Record<string, string | string[] | undefined>
  /** Coalesces consecutive autosaves into one revision. Omitting it on an
   *  edit surface fragments the history into one revision per keystroke
   *  batch. */
  editSessionId?: string
  /** Narrows note retrieval to one project; the server scopes note hits
   *  by it and cannot express a client-wide note scope in one call. */
  projectId?: string
  /** Claimed by the server in the same transaction as the mutation, so a
   *  retry replays the first answer instead of creating a second thing.
   *  Its presence is what makes a timed-out create safe to repeat. */
  idempotencyKey?: string
  signal?: AbortSignal
  deadlineMs?: number
}

function statusToCode(status: number): FailureCode {
  if (status === 401) return 'unauthenticated'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) return 'conflict'
  if (status === 429) return 'server'
  if (status >= 400 && status < 500) return 'invalid'
  return 'server'
}

/** A retry can succeed AND cannot duplicate anything. A read always
 *  qualifies. A version-guarded write does too, even after a timeout: if
 *  the first attempt landed, the version moved and the retry conflicts
 *  rather than writing twice. A create does not, which is why the panel
 *  offers "check" instead of "retry" there. */
function retryable(code: FailureCode, method: string, idempotent: boolean): boolean {
  if (code === 'network' || code === 'server') return true
  // A read may always be repeated. A write may not, UNLESS it carried an
  // idempotency key: the server claimed it in the same transaction as
  // the mutation, so a second attempt replays the first answer rather
  // than creating a second thing. That is the difference between
  // offering "check" and offering "retry" after a timeout.
  if (code === 'timeout') return method === 'GET' || idempotent
  return false
}

function url(path: string, query: CallOptions['query']): string {
  const target = new URL(config.apiUrl + path)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined) continue
    if (Array.isArray(value)) for (const v of value) target.searchParams.append(key, v)
    else target.searchParams.set(key, value)
  }
  return target.toString()
}

function language(): string {
  // The same value the panel renders in, so its own text and the
  // server's error sentence agree. Better than the app's fixed English:
  // there, a localized `detail` can arrive wrapped in English chrome.
  return chrome.i18n.getUILanguage().startsWith('it') ? 'it' : 'en'
}

/** Mints the request's correlation id. Hex, so it survives the server's
 *  own validation, which replaces anything unbounded or newline-bearing
 *  rather than writing it into a log line. */
function mintCorrelationId(): string {
  return crypto.randomUUID().replace(/-/g, '')
}

async function readBody(res: Response): Promise<unknown> {
  try {
    return await res.json()
  } catch {
    return null
  }
}

/** Never throws. Every caller has to render an outcome either way, and a
 *  throw would mean each one invents its own. */
export async function call<T>(
  conn: StoredConnection,
  path: string,
  options: CallOptions = {},
): Promise<Result<T>> {
  const method = options.method ?? 'GET'
  const idempotent = options.idempotencyKey !== undefined
  const correlationId = mintCorrelationId()
  const deadline = options.deadlineMs ?? (method === 'GET' ? READ_DEADLINE_MS : WRITE_DEADLINE_MS)

  const headers: Record<string, string> = {
    Accept: 'application/json',
    Authorization: `Bearer ${conn.secret}`,
    'X-Workspace-Id': conn.workspaceId,
    'X-Correlation-Id': correlationId,
    'Accept-Language': language(),
  }
  if (options.projectId) headers['X-Project-Id'] = options.projectId
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey
  if (options.editSessionId) headers['X-Edit-Session-Id'] = options.editSessionId
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  // Never X-Admin-Mode, never X-Workspace-Role: this extension operates
  // at the credential's own authority and has no business asking for
  // elevation.

  const timer = new AbortController()
  const deadlineTimer = setTimeout(() => timer.abort(), deadline)
  // The caller's signal (a superseded keystroke) and ours (the deadline)
  // both have to be able to stop the request.
  const signal = options.signal
    ? AbortSignal.any([options.signal, timer.signal])
    : timer.signal

  let res: Response
  try {
    res = await fetch(url(path, options.query), {
      method,
      headers,
      body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
      signal,
    })
  } catch {
    clearTimeout(deadlineTimer)
    // Which abort it was decides the code, not the exception: fetch
    // reports both the same way.
    // A caller that aborted on purpose is not a failure to report.
    if (options.signal?.aborted) {
      return { ok: false, error: { code: 'network', message: 'superseded', retryable: false } }
    }
    const timedOut = timer.signal.aborted
    const code: FailureCode = timedOut ? 'timeout' : 'network'
    return {
      ok: false,
      error: {
        code,
        message: timedOut ? 'deadline' : 'unreachable',
        retryable: retryable(code, method, idempotent),
        // No response, so no server-side value: ours is all there is.
        correlationId,
      },
    }
  }
  clearTimeout(deadlineTimer)

  if (res.ok) {
    if (res.status === 204) return { ok: true, data: undefined as T }
    return { ok: true, data: (await readBody(res)) as T }
  }

  const body = await readBody(res)
  const code = statusToCode(res.status)

  if (code === 'unauthenticated') {
    // An agent token has no refresh, and the server collapses unknown,
    // revoked, expired and deactivated into one answer. There is nothing
    // to single-flight and no retry that can succeed, so the credential
    // is dropped now rather than replayed. Every row it stands behind,
    // and no more: one CREDENTIAL dying says nothing about another, but
    // an account-bound one stands behind a row per workspace and marking
    // only the one that happened to make the call would leave the panel
    // offering switches that all fail.
    await markRevoked(conn)
  }

  const params = (body as { params?: Record<string, unknown> } | null)?.params
  const currentVersion =
    typeof params?.current_version === 'number' ? params.current_version : undefined

  const failure: Failure = {
    code,
    // The server's own sentence, already localized by Accept-Language.
    // The catalogue key is only reached when it said nothing usable.
    message: errMessage(body, errCode(body) ?? `HTTP ${res.status}`),
    retryable: retryable(code, method, idempotent),
    // The value the SERVER logged, which is the one worth quoting; ours
    // is the fallback for the case where there was no response at all.
    correlationId:
      (body as { correlation_id?: string } | null)?.correlation_id ??
      res.headers.get('X-Correlation-Id') ??
      correlationId,
  }
  if (currentVersion !== undefined) failure.currentVersion = currentVersion
  return { ok: false, error: failure }
}

async function markRevoked(dead: StoredConnection): Promise<void> {
  for (const row of await storage.connections()) {
    if (row.assistantId !== dead.assistantId) continue
    // The secret goes, the row stays: the panel has to be able to say
    // WHICH workspaces need reconnecting, by name, rather than showing
    // an empty list and letting the person work out what happened.
    await storage.putConnection({ ...row, secret: '', revoked: true })
    await clearCaches(row.workspaceId)
  }
}
