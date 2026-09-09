// The credential handover, at the seam where it is decided.
//
// This is the one place a secret enters the extension, so the checks are
// asserted in the order they run and each one is asserted alone: a test
// that only proves the happy path passes just as well when every guard
// has been removed.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CONNECT_MESSAGE_KIND } from '../../web/src/shared'
import { installFakeChrome, type FakeChrome } from './fake-chrome'

const ORIGIN = 'https://mycelium.test'

let fake: FakeChrome
let acceptHandover: typeof import('../src/bg/connection')['acceptHandover']
let beginConnect: typeof import('../src/bg/connection')['beginConnect']

async function load() {
  // A fresh fake AND a fresh module graph per test: the worker modules
  // hold no state of their own, but the storage they read does, and a
  // test that inherits the previous one's credential proves nothing.
  vi.resetModules()
  fake = installFakeChrome()
  const mod = await import('../src/bg/connection')
  acceptHandover = mod.acceptHandover
  beginConnect = mod.beginConnect
}

function handover(state: string, workspaceId = 'ws-1') {
  return {
    kind: CONNECT_MESSAGE_KIND,
    state,
    secret: 'mycelium_at_secret',
    workspace: { id: workspaceId, name: 'Personal' },
    assistantId: 'assistant-1',
    scope: ['tasks:read'],
  }
}

async function currentNonce(): Promise<string> {
  const held = (await fake.session.peek()).nonce as { value: string } | undefined
  if (!held) throw new Error('no nonce was minted')
  return held.value
}

describe('the connect handshake', () => {
  beforeEach(load)

  it('opens the app with a nonce it is holding, and nothing secret in the url', async () => {
    const { url } = await beginConnect()
    const parsed = new URL(url)
    expect(parsed.origin).toBe(ORIGIN)
    expect(parsed.pathname).toBe('/settings/extension')
    expect(parsed.searchParams.get('state')).toBe(await currentNonce())
    expect(parsed.searchParams.get('id')).toBe('fakeextensionidfakeextensionidaa')
    expect(url).not.toContain('secret')
  })

  it('refuses a handover from any origin but the app', async () => {
    await beginConnect()
    const state = await currentNonce()
    for (const origin of ['https://evil.test', 'http://mycelium.test', undefined]) {
      expect(await acceptHandover(handover(state), origin)).toEqual({
        ok: false,
        reason: 'wrong-origin',
      })
    }
    // And nothing was stored on the way to refusing.
    expect(Object.keys(await fake.local.peek())).not.toContain('conn:ws-1')
  })

  it('refuses a nonce it never minted', async () => {
    await beginConnect()
    expect(await acceptHandover(handover('a-nonce-from-somewhere-else'), ORIGIN)).toEqual({
      ok: false,
      reason: 'unknown-state',
    })
  })

  it('refuses a REPLAY of a nonce it already spent', async () => {
    await beginConnect()
    const state = await currentNonce()
    expect(await acceptHandover(handover(state), ORIGIN)).toEqual({ ok: true })
    // The second attempt carries a nonce that was valid once. A page
    // that kept the link must not be able to mint a second credential.
    expect(await acceptHandover(handover(state, 'ws-2'), ORIGIN)).toEqual({
      ok: false,
      reason: 'unknown-state',
    })
    expect(Object.keys(await fake.local.peek())).not.toContain('conn:ws-2')
  })

  it('refuses an expired nonce and forgets it', async () => {
    await beginConnect()
    const state = await currentNonce()
    await fake.session.set({ nonce: { value: state, expiresAt: Date.now() - 1 } })
    expect(await acceptHandover(handover(state), ORIGIN)).toEqual({ ok: false, reason: 'expired' })
    expect((await fake.session.peek()).nonce).toBeUndefined()
  })

  it('refuses a second credential for a workspace that already has a live one', async () => {
    await beginConnect()
    await acceptHandover(handover(await currentNonce()), ORIGIN)
    await beginConnect()
    expect(await acceptHandover(handover(await currentNonce()), ORIGIN)).toEqual({
      ok: false,
      reason: 'already-connected',
    })
  })

  it('refuses a message that is not a handover at all', async () => {
    await beginConnect()
    for (const junk of [null, 'hello', {}, { kind: 'other' }]) {
      expect(await acceptHandover(junk, ORIGIN)).toEqual({ ok: false, reason: 'unknown-state' })
    }
  })

  it('stores what the SERVER granted, not what the extension asked for', async () => {
    await beginConnect()
    const message = handover(await currentNonce())
    message.scope = ['tasks:read', 'notes:read']
    await acceptHandover(message, ORIGIN)
    const stored = (await fake.local.peek())['conn:ws-1'] as { scope: string[]; secret: string }
    expect(stored.scope).toEqual(['tasks:read', 'notes:read'])
    expect(stored.secret).toBe('mycelium_at_secret')
  })

  it('spends the nonce on success, so the same link cannot be used twice', async () => {
    await beginConnect()
    await acceptHandover(handover(await currentNonce()), ORIGIN)
    expect((await fake.session.peek()).nonce).toBeUndefined()
  })

  it('files one row per workspace when the SERVER says the credential reaches them', async () => {
    // The page is not asked and could not be trusted to answer: what a
    // credential reaches is the server's fact about it, so the extension
    // asks after the handover rather than reading a field out of it.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          binding: 'account',
          workspaces: [
            { id: 'ws-1', name: 'Personal', role: 'owner' },
            { id: 'ws-2', name: 'Studio', role: 'guest' },
          ],
        }),
      })),
    )
    await beginConnect()
    await acceptHandover(handover(await currentNonce()), ORIGIN)
    const bag = await fake.local.peek()
    const first = bag['conn:ws-1'] as { secret: string; binding?: string; workspaceName: string }
    const second = bag['conn:ws-2'] as { secret: string; binding?: string; workspaceName: string }
    expect(second).toBeDefined()
    expect(second.workspaceName).toBe('Studio')
    // ONE credential behind both rows. The rows exist because everything
    // the panel keeps -- recents, caches, the pinned task -- is per
    // workspace; the secret is not.
    expect(second.secret).toBe(first.secret)
    expect([first.binding, second.binding]).toEqual(['account', 'account'])
    vi.unstubAllGlobals()
  })

  it('stays connected to the one workspace when it cannot ask', async () => {
    // A failed lookup is not a failed connection: the credential works,
    // and the row for the workspace it was connected in is already
    // stored. Anything else would turn a flaky network into a handover
    // the person has to repeat.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network')
      }),
    )
    await beginConnect()
    expect(await acceptHandover(handover(await currentNonce()), ORIGIN)).toEqual({ ok: true })
    const bag = await fake.local.peek()
    expect(bag['conn:ws-1']).toBeDefined()
    expect(bag['conn:ws-2']).toBeUndefined()
    vi.unstubAllGlobals()
  })
})
