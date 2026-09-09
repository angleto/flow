// Finding the messaging API, and failing to.
//
// The page reads one property to decide whether a credential can be
// handed over at all. Read from the wrong place it is undefined in every
// browser, and the page then mints a secret, finds no channel, and tells
// the reader to revoke it -- which is what shipped, and what made the
// connect flow impossible to complete rather than merely awkward. Its
// absence is also a REAL state (no extension installed, or one built
// against another deployment), so the two cases are asserted apart.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { chromeRuntime, handOver, type ChromeRuntime } from './extensionMessaging'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('chromeRuntime', () => {
  it('reads chrome.runtime, which is where the browser puts it', () => {
    const sendMessage = vi.fn()
    vi.stubGlobal('chrome', { runtime: { sendMessage } })
    expect(chromeRuntime()?.sendMessage).toBe(sendMessage)
  })

  it('is undefined where the browser exposes no messaging at all', () => {
    vi.stubGlobal('chrome', undefined)
    expect(chromeRuntime()).toBeUndefined()
  })

  it('is undefined where chrome exists but no extension may be messaged', () => {
    // Chrome exposes runtime to a page only when an installed extension
    // lists this origin in externally_connectable. This is the honest
    // "install it first" case, and it must stay distinguishable.
    vi.stubGlobal('chrome', {})
    expect(chromeRuntime()).toBeUndefined()
  })

  it('is not confused by a global named runtime', () => {
    // The bug: a bare `runtime` is not the API, and reading it made every
    // browser look unable to receive a credential.
    vi.stubGlobal('runtime', { sendMessage: vi.fn() })
    vi.stubGlobal('chrome', undefined)
    expect(chromeRuntime()).toBeUndefined()
  })
})

describe('handOver', () => {
  it('resolves with what the extension answered', async () => {
    const runtime: ChromeRuntime = {
      sendMessage: (_id, _message, callback) => callback({ ok: true }),
    }
    await expect(handOver(runtime, 'ext-1', { a: 1 })).resolves.toEqual({
      reply: { ok: true },
      lastError: undefined,
    })
  })

  it('reads lastError inside the callback, where it still exists', async () => {
    // Chrome clears lastError as soon as the callback returns, so a read
    // afterwards is always undefined and the page falls back to a
    // generic sentence instead of saying what actually happened.
    const runtime: ChromeRuntime = {
      sendMessage: (_id, _message, callback) => {
        runtime.lastError = { message: 'Could not establish connection.' }
        callback(undefined)
        runtime.lastError = undefined
      },
    }
    await expect(handOver(runtime, 'ext-1', {})).resolves.toEqual({
      reply: undefined,
      lastError: 'Could not establish connection.',
    })
  })
})
