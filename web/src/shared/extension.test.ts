// Reading the descriptor a deployment publishes about its extension.
//
// What is asserted here is refusal. The document arrives over HTTP from a
// path that may hold something else entirely -- a development server
// answers it with the SPA shell, an nginx without the package answers
// index.html or nothing -- and the page turns two of its fields into a
// download link. Anything that is not a descriptor has to read as "no
// package", never as a package with odd values.

import { describe, expect, it } from 'vitest'
import {
  EXTENSION_PACKAGE_DIR,
  EXTENSION_RELEASE_URL,
  extensionReleaseServes,
  parseExtensionRelease,
} from './extension'

const valid = {
  origin: 'https://mycelium.test',
  version: '2.3.9',
  versionName: 'v2.3.9',
  zip: 'mycelium-extension-2.3.9.zip',
  sha256: 'b'.repeat(64),
}

describe('parseExtensionRelease', () => {
  it('accepts a descriptor and drops nothing from it', () => {
    expect(parseExtensionRelease(valid)).toEqual(valid)
  })

  it('answers null for what a server sends when there is no package', () => {
    for (const notADescriptor of [
      null,
      undefined,
      '<!doctype html><html></html>',
      [],
      42,
      {},
    ]) {
      expect(parseExtensionRelease(notADescriptor)).toBeNull()
    }
  })

  it('refuses an archive name that is not a name', () => {
    // The value becomes an href under EXTENSION_PACKAGE_DIR. A path or a
    // URL here would aim the download somewhere the deployment did not
    // publish, and the reader has no way to notice.
    for (const zip of [
      '../../etc/passwd',
      '/somewhere/else.zip',
      'https://elsewhere.test/x.zip',
      'no-extension',
      '',
    ]) {
      expect(parseExtensionRelease({ ...valid, zip }), zip).toBeNull()
    }
  })

  it('refuses a checksum that is not one', () => {
    for (const sha256 of ['', 'deadbeef', 'B'.repeat(64), `${'a'.repeat(64)} `]) {
      expect(parseExtensionRelease({ ...valid, sha256 }), sha256).toBeNull()
    }
  })
})

describe('extensionReleaseServes', () => {
  it('is an exact origin comparison', () => {
    const release = { ...valid }
    expect(extensionReleaseServes(release, 'https://mycelium.test')).toBe(true)
    // Same host, different scheme or port: a different origin, and Chrome
    // would refuse the handover from it.
    expect(extensionReleaseServes(release, 'http://mycelium.test')).toBe(false)
    expect(extensionReleaseServes(release, 'https://mycelium.test:8443')).toBe(false)
    expect(extensionReleaseServes(release, 'https://app.mycelium.test')).toBe(false)
  })
})

describe('where the descriptor lives', () => {
  it('sits in the directory the archive is resolved against', () => {
    expect(EXTENSION_RELEASE_URL.startsWith(EXTENSION_PACKAGE_DIR)).toBe(true)
  })
})
