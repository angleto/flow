// The descriptor a deployment serves beside the archive.
//
// Two packages have to agree about it: this one writes it at pack time,
// the SPA reads it to decide whether to offer a download. They are built
// separately, from separate lockfiles, and nothing at runtime reports a
// disagreement -- a renamed field makes the download button quietly stop
// appearing, which looks exactly like a deployment that ships no package.
// So the agreement is asserted here, in both directions.

import { describe, expect, it } from 'vitest'
import { extensionReleaseServes, parseExtensionRelease } from '@shared'
import { readBuildEnv } from '../scripts/env.mjs'
import { RELEASE_FILE, releaseFor } from '../scripts/release.mjs'

const env = readBuildEnv({
  MYCELIUM_EXTENSION_ORIGIN: 'https://mycelium.test',
  MYCELIUM_VERSION: 'v2.3.9-1-gabc',
})
const sha256 = 'a'.repeat(64)
const built = releaseFor(env, { zip: `mycelium-extension-${env.version}.zip`, sha256 })

describe('the release descriptor', () => {
  it('is named the same thing the app asks for', () => {
    expect(RELEASE_FILE).toBe('release.json')
  })

  it('is accepted by the reader in the app, field for field', () => {
    // The whole point: what this package writes is what the other one
    // parses. A field renamed on either side fails here.
    expect(parseExtensionRelease(built)).toEqual(built)
  })

  it('names the deployment the package inside was compiled against', () => {
    expect(built.origin).toBe(env.baseUrl)
    expect(extensionReleaseServes(built, 'https://mycelium.test')).toBe(true)
    expect(extensionReleaseServes(built, 'https://elsewhere.test')).toBe(false)
  })

  it('carries both versions Chrome distinguishes', () => {
    expect(built.version).toBe('2.3.9')
    expect(built.versionName).toBe('v2.3.9-1-gabc')
  })

  it('is rejected whole when any single field is missing', () => {
    // A stale field is the same failure as a missing one from the
    // reader's side, so dropping each in turn covers both directions.
    for (const key of Object.keys(built)) {
      const partial = { ...built } as Record<string, unknown>
      delete partial[key]
      expect(parseExtensionRelease(partial), key).toBeNull()
    }
  })
})
