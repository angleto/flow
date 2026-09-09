// The single network seam.
//
// Everything here is a rule about a FAILURE, because the successes are
// the easy half. What the panel does with a 409, a 401 or an expired
// deadline is the difference between a write surface people trust and one
// they stop using.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { installFakeChrome } from './fake-chrome'

let call: typeof import('../src/bg/api')['call']
let storage: typeof import('../src/bg/storage')['storage']

const conn = {
  workspaceId: 'ws-1',
  workspaceName: 'Personal',
  assistantId: 'a-1',
  scope: ['tasks:read'],
  secret: 'mycelium_at_secret',
}

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  return vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json', ...headers },
    }),
  )
}

/** A fetch that never resolves on its own, so only an abort ends it --
 *  which is what makes the deadline and the supersession branches real
 *  rather than simulated. Honours an ALREADY-aborted signal, because a
 *  listener added after the fact never fires and the test would hang
 *  instead of failing. */
function hangingFetch() {
  return vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new DOMException('', 'AbortError'))
        if (init.signal?.aborted) return abort()
        init.signal?.addEventListener('abort', abort)
      }),
  )
}

async function load() {
  vi.resetModules()
  // Installed for its side effect: the modules read chrome.* at import.
  installFakeChrome()
  const api = await import('../src/bg/api')
  const store = await import('../src/bg/storage')
  call = api.call
  storage = store.storage
  await storage.putConnection(conn)
}

describe('the network seam', () => {
  beforeEach(load)

  it('sends the credential, the tenant and a correlation id on every request', async () => {
    const fetchMock = respond(200, { ok: true })
    vi.stubGlobal('fetch', fetchMock)
    await call(conn, '/tasks')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('https://mycelium.test/api/tasks')
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer mycelium_at_secret')
    expect(headers['X-Workspace-Id']).toBe('ws-1')
    expect(headers['X-Correlation-Id']).toMatch(/^[0-9a-f]{32}$/)
    // Never elevation: this credential operates at its own authority.
    expect(headers['X-Admin-Mode']).toBeUndefined()
    expect(headers['X-Workspace-Role']).toBeUndefined()
  })

  it('carries the version a conflict came back with, so the panel need not re-read', async () => {
    vi.stubGlobal(
      'fetch',
      respond(409, {
        code: 'concurrency.stale_version',
        detail: 'Stale version write',
        params: { current_version: 7 },
        correlation_id: 'abc123def456',
      }),
    )
    const res = await call(conn, '/tasks/x', { method: 'PATCH', body: { expected_version: 3 } })
    expect(res.ok).toBe(false)
    if (res.ok) return
    expect(res.error.code).toBe('conflict')
    expect(res.error.currentVersion).toBe(7)
    // A conflict must NEVER be retryable: replaying a stale write is how
    // somebody else's change gets overwritten.
    expect(res.error.retryable).toBe(false)
    // The server's own identifier, not ours: it is the one in its log.
    expect(res.error.correlationId).toBe('abc123def456')
  })

  it('prefers the server sentence over any catalogue text', async () => {
    vi.stubGlobal('fetch', respond(403, { code: 'agent.scope_denied', detail: 'Ambito negato' }))
    const res = await call(conn, '/tasks', { method: 'POST' })
    if (res.ok) throw new Error('expected a refusal')
    expect(res.error.message).toBe('Ambito negato')
  })

  it('drops the credential on 401, and leaves another credential alone', async () => {
    // A DIFFERENT credential, which is what the isolation is about: the
    // second row must carry its own assistantId, or it is the same
    // credential under another name and dies with the first (see the
    // account-bound case below).
    await storage.putConnection({
      ...conn,
      workspaceId: 'ws-2',
      workspaceName: 'Other',
      assistantId: 'a-2',
    })
    await storage.writeCache(storage.cacheKey('ws-1', 'x'), { a: 1 })
    await storage.writeCache(storage.cacheKey('ws-2', 'x'), { a: 1 })

    vi.stubGlobal('fetch', respond(401, { code: 'agent.token_invalid', detail: 'no' }))
    const res = await call(conn, '/tasks')
    expect(res.ok).toBe(false)

    const dead = await storage.connection('ws-1')
    expect(dead?.revoked).toBe(true)
    // The secret goes; the row stays, so the panel can name WHICH
    // workspace needs reconnecting instead of showing an empty list.
    expect(dead?.secret).toBe('')
    expect(dead?.workspaceName).toBe('Personal')

    const alive = await storage.connection('ws-2')
    expect(alive?.revoked).toBeUndefined()
    expect(alive?.secret).toBe('mycelium_at_secret')

    expect(await storage.readCache(storage.cacheKey('ws-1', 'x'))).toBeUndefined()
    expect(await storage.readCache(storage.cacheKey('ws-2', 'x'))).toEqual({ a: 1 })
  })

  it('drops every row an account-bound credential stands behind', async () => {
    // One secret, one row per workspace, because the panel's caches and
    // recents are per workspace. When that secret dies they all do, and
    // a panel that marked only the row which happened to make the call
    // would offer switches that fail one after another.
    await storage.putConnection({
      ...conn,
      workspaceId: 'ws-2',
      workspaceName: 'Other',
      binding: 'account',
    })
    await storage.putConnection({ ...conn, binding: 'account' })

    vi.stubGlobal('fetch', respond(401, { code: 'agent.token_invalid', detail: 'no' }))
    await call(conn, '/tasks')

    for (const id of ['ws-1', 'ws-2']) {
      const row = await storage.connection(id)
      expect(row?.revoked, id).toBe(true)
      expect(row?.secret, id).toBe('')
      // The name survives, so the panel can say which workspaces need
      // reconnecting rather than showing an empty list.
      expect(row?.workspaceName, id).toBeTruthy()
    }
  })

  it('tells a timeout apart from an unreachable server', async () => {
    vi.stubGlobal('fetch', hangingFetch())
    const res = await call(conn, '/tasks', { deadlineMs: 10 })
    if (res.ok) throw new Error('expected a failure')
    expect(res.error.code).toBe('timeout')
    // A read may be repeated freely; the panel offers a retry.
    expect(res.error.retryable).toBe(true)
    // No response, so no server-side identifier: ours is all there is,
    // and it resolves in the access log if the request arrived.
    expect(res.error.correlationId).toMatch(/^[0-9a-f]{32}$/)
  })

  it('does not offer to repeat a WRITE whose outcome is unknown', async () => {
    vi.stubGlobal('fetch', hangingFetch())
    const res = await call(conn, '/tasks', { method: 'POST', body: {}, deadlineMs: 10 })
    if (res.ok) throw new Error('expected a failure')
    expect(res.error.code).toBe('timeout')
    // A create is not idempotent. The panel offers "check", never
    // "retry", or a flaky connection duplicates somebody's work.
    expect(res.error.retryable).toBe(false)
  })

  it('offers to repeat a timed-out create ONLY when it carried a key', async () => {
    // The whole payoff of the idempotency claim. Without a key a create
    // is not repeatable and the panel must offer "check"; with one, the
    // server claimed it in the same transaction as the mutation, so a
    // second attempt replays the first answer rather than filing a
    // second task -- and the panel can honestly offer "retry".
    vi.stubGlobal('fetch', hangingFetch())
    const bare = await call(conn, '/tasks', { method: 'POST', body: {}, deadlineMs: 10 })
    if (bare.ok) throw new Error('expected a failure')
    expect(bare.error.code).toBe('timeout')
    expect(bare.error.retryable).toBe(false)

    const keyed = await call(conn, '/tasks', {
      method: 'POST',
      body: {},
      idempotencyKey: 'a-key',
      deadlineMs: 10,
    })
    if (keyed.ok) throw new Error('expected a failure')
    expect(keyed.error.code).toBe('timeout')
    expect(keyed.error.retryable).toBe(true)
  })

  it('sends the key as the header the server claims on', async () => {
    const fetchMock = respond(200, { id: 'x' })
    vi.stubGlobal('fetch', fetchMock)
    await call(conn, '/tasks', { method: 'POST', body: {}, idempotencyKey: 'a-key' })
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('a-key')
  })

  it('does not report a superseded keystroke as a failure worth showing', async () => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', hangingFetch())
    // Aborted while in flight, the way a next keystroke does it.
    setTimeout(() => controller.abort(), 5)
    const res = await call(conn, '/search', { signal: controller.signal })
    if (res.ok) throw new Error('expected a failure')
    expect(res.error.retryable).toBe(false)
  })
})
