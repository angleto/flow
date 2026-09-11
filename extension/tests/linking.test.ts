// The connect ceremony, from this side.
//
// The assertions that matter are about WHAT THE EXTENSION HOLDS at each
// step, because that is what the mechanism this replaces got wrong. In
// particular: while a request is open the panel can see the code to
// compare and cannot see the code that collects, and waiting is not a
// failure. A test that only drove the happy path would pass just as well
// against a version that stored a credential it never received.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { installFakeChrome, type FakeChrome } from './fake-chrome'

let fake: FakeChrome

const OPENED = {
  device_code: 'the-long-one-that-collects',
  user_code: 'K7QP-3MTX',
  verification_path: '/settings/extension',
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  interval: 3,
}

const COLLECTED = {
  secret: 'mycelium_at_granted',
  assistant_id: '11111111-1111-4111-8111-111111111111',
  workspace_id: '22222222-2222-4222-8222-222222222222',
  workspace_name: 'Studio',
  scope: ['tasks:read', 'notes:write'],
  expires_at: new Date(Date.now() + 90 * 86_400_000).toISOString(),
}

/** chrome.storage answers with a bag and OMITS an absent key, so a read
 *  of one key is a lookup into that bag rather than the value itself. */
async function stored(key: string): Promise<unknown> {
  return (await fake.local.get(key))[key]
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** Queue one reply per call, in order. */
function replies(...queued: Response[]) {
  const rest = [...queued]
  return vi.fn(async () => rest.shift() ?? json({ detail: 'unexpected call' }, 500))
}

async function load(fetchImpl: ReturnType<typeof replies>) {
  vi.resetModules()
  fake = installFakeChrome()
  vi.stubGlobal('fetch', fetchImpl)
  return import('../src/bg/linking')
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('opening a request', () => {
  it('shows the code to compare, sends the person to approve, and arms the net', async () => {
    const linking = await load(replies(json(OPENED)))
    const opened = await linking.startLink()

    expect(opened.ok).toBe(true)
    if (!opened.ok) return
    expect(opened.data.userCode).toBe('K7QP-3MTX')
    // The tab is a convenience carrying the code that is not a secret.
    const tab = fake.recorded.tabs.at(-1)
    expect(tab?.url).toContain('/settings/extension')
    expect(tab?.url).toContain('code=K7QP-3MTX')
    // Chrome shuts an idle worker down, so the alarm is what wakes it to
    // ask again. Without it the ceremony stalls whenever the person takes
    // longer than the worker lives.
    expect(fake.recorded.alarms.map((a) => a.name)).toContain('link-poll')
  })

  it('never lets the panel see the half that collects', async () => {
    const linking = await load(replies(json(OPENED)))
    await linking.startLink()

    const visible = await linking.currentLink()
    expect(visible?.userCode).toBe('K7QP-3MTX')
    // The worker waits; the panel has no reason to hold a collecting
    // secret, so it is not in what the panel is given.
    expect(JSON.stringify(visible)).not.toContain(OPENED.device_code)
  })
})

describe('waiting', () => {
  it('is not a failure and is not settled', async () => {
    const linking = await load(
      replies(json(OPENED), json({ code: 'auth.device_pending', detail: 'Waiting' }, 400)),
    )
    await linking.startLink()

    // False = "nothing new to show", which is the ordinary answer for
    // most of a request's life. The request stays open and unmarked.
    expect(await linking.pollLink()).toBe(false)
    expect((await linking.currentLink())?.failure).toBeNull()
  })
})

describe('collecting', () => {
  it('stores what the server granted, and only then', async () => {
    const linking = await load(
      replies(
        json(OPENED),
        json({ code: 'auth.device_pending', detail: 'Waiting' }, 400),
        json(COLLECTED),
      ),
    )
    await linking.startLink()

    // Still waiting: nothing stored.
    await linking.pollLink()
    expect(await stored('conn:' + COLLECTED.workspace_id)).toBeUndefined()

    expect(await linking.pollLink()).toBe(true)
    const row = (await stored('conn:' + COLLECTED.workspace_id)) as {
      secret: string
      scope: string[]
      expiresAt?: string
      binding?: string
    }
    expect(row.secret).toBe(COLLECTED.secret)
    // The SERVER's answer, not a list compiled into this package.
    expect(row.scope).toEqual(['tasks:read', 'notes:write'])
    expect(row.expiresAt).toBe(COLLECTED.expires_at)
    expect(row.binding).toBe('account')
    // The ceremony is over: nothing left open, and the net stood down.
    expect(await linking.currentLink()).toBeNull()
    expect(fake.recorded.alarms.map((a) => a.name)).not.toContain('link-poll')
  })

  it('refuses an answer that arrived without the parts that matter', async () => {
    // The server answers a 2xx only on success, and this is the client's
    // own guard on the SHAPE. Between them, "connected with nothing"
    // needs two independent mistakes rather than one.
    const linking = await load(replies(json(OPENED), json({ ...COLLECTED, secret: '' })))
    await linking.startLink()

    expect(await linking.pollLink()).toBe(true)
    expect(await stored('conn:' + COLLECTED.workspace_id)).toBeUndefined()
    expect((await linking.currentLink())?.failure).toBe('server')
  })
})

describe('being refused', () => {
  it('tells the panel, instead of leaving it on a spinner', async () => {
    const linking = await load(
      replies(json(OPENED), json({ code: 'auth.device_denied', detail: 'Refused' }, 400)),
    )
    await linking.startLink()

    expect(await linking.pollLink()).toBe(true)
    // The request STAYS, marked. Clearing it would take the panel back to
    // how it looked before anyone pressed anything, which reads as
    // "nothing happened" rather than "this was refused".
    expect((await linking.currentLink())?.failure).toBe('denied')
    expect(fake.recorded.alarms.map((a) => a.name)).not.toContain('link-poll')
  })

  it('stops asking once a request has lapsed', async () => {
    const linking = await load(replies(json({ ...OPENED, expires_at: new Date(0).toISOString() })))
    await linking.startLink()

    expect(await linking.pollLink()).toBe(true)
    expect((await linking.currentLink())?.failure).toBe('expired')
    // Settled without asking the server at all: the deadline is known
    // here, and a question whose answer cannot change is not worth a
    // round trip.
    expect(fake.recorded.fetches.filter((f) => f.url.includes('/token'))).toHaveLength(0)
  })
})
