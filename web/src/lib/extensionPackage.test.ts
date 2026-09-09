// Deciding whether this deployment has an extension to hand out.
//
// The probe reads a static file, and every way it can fail has to land on
// the same answer -- "no package" -- because the fallback (build it
// yourself) is always correct and a wrong download is not. The cases
// below are the ones that actually occur: an image built without an
// extension origin (404), a development server answering every path with
// the SPA shell (200, HTML), and an archive built for a sibling
// deployment (200, valid, wrong origin).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadRelease } from './extensionPackage'

const here = window.location.origin

function respond(body: unknown, ok = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok,
      json: async () => {
        if (typeof body === 'string') throw new SyntaxError('not json')
        return body
      },
    })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

const served = {
  origin: here,
  version: '2.3.9',
  versionName: 'v2.3.9',
  zip: 'mycelium-extension-2.3.9.zip',
  sha256: 'c'.repeat(64),
}

describe('loadRelease', () => {
  it('returns the package this deployment serves', async () => {
    respond(served)
    await expect(loadRelease()).resolves.toEqual(served)
  })

  it('returns null when the deployment serves none', async () => {
    respond(null, false)
    await expect(loadRelease()).resolves.toBeNull()
  })

  it('returns null when the path answers with the app shell', async () => {
    respond('<!doctype html><html></html>')
    await expect(loadRelease()).resolves.toBeNull()
  })

  it('returns null for a package built for another deployment', async () => {
    // It would install and then never be able to receive a credential:
    // the origin is in the manifest it was compiled with, and Chrome
    // refuses the handover from anywhere else.
    respond({ ...served, origin: 'https://elsewhere.test' })
    await expect(loadRelease()).resolves.toBeNull()
  })

  it('returns null when the request itself fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network')
      }),
    )
    await expect(loadRelease()).resolves.toBeNull()
  })
})
