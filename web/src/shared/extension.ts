// The contract between the app and the browser extension.
//
// Much smaller than it was, and the shrinking is the point. This file used
// to carry the whole handover: the message kind, the nonce parameter, the
// shape of the credential in flight, and a copy of the scope list so the
// consent screen could name what it was granting. All of it existed because
// the PAGE minted the credential and pushed it into the extension.
//
// The server mints it now, and hands it to the extension when the extension
// asks. So the grant is disclosed from the server's own answer
// (``/auth/device/pending`` returns the scope it will actually mint), and
// what is left here is a route, a query parameter, and the descriptor of the
// package a deployment serves. A list that cannot drift because it is no
// longer written twice.
//
// Pure by contract: this directory is compiled into both packages and
// imports nothing.

/** The parameter the extension puts in the URL when it sends somebody to
 *  approve it, and the route it sends them to.
 *
 *  ONE value, and it is not a secret: the short code the person compares
 *  against what the extension is showing. It collects nothing on its own
 *  -- only the long code the extension keeps can do that -- so losing it,
 *  logging it or bookmarking it grants nobody anything.
 *
 *  That is the difference from the handshake this replaces, which carried
 *  a nonce the extension was holding and could therefore be destroyed by
 *  any redirect between the URL and the reader. Here the request lives on
 *  the server, keyed by the code the extension kept, and the URL is only
 *  a convenience that saves typing.
 *
 *  The route is a normal settings page, reached two ways: a person
 *  clicking through Settings, and the extension opening it with the
 *  parameter. One page, so the disclosure cannot differ between them. */
export const CONNECT_ROUTE = '/settings/extension'
export const CONNECT_CODE_PARAM = 'code'

/** Where a deployment publishes the package it serves, and the descriptor
 *  that says what that package is.
 *
 *  The origin the extension talks to is compiled into it: `host_permissions`
 *  is a static manifest declaration, so one archive cannot serve two
 *  deployments, and an archive published centrally would point every
 *  installer at whichever deployment built it. The deployment therefore
 *  serves its own, and the page shows the download only when the descriptor
 *  names the origin the page is itself being served from: offering one that
 *  could not reach this deployment is worse than offering none.
 *
 *  The producer is `extension/scripts/release.mjs`, in the other package.
 *  Both directions are asserted by `extension/tests/release.test.ts`. */
export const EXTENSION_PACKAGE_DIR = '/extension/'
export const EXTENSION_RELEASE_URL = `${EXTENSION_PACKAGE_DIR}release.json`

export interface ExtensionRelease {
  /** The deployment the package inside the archive talks to. */
  origin: string
  /** What Chrome compares (`2.3.10`) and what a person reads on
   *  chrome://extensions (`v2.3.10-3-gabc1234`). */
  version: string
  versionName: string
  /** File name only, resolved against EXTENSION_PACKAGE_DIR. */
  zip: string
  sha256: string
}

/** A file name and nothing else. The page turns this value into a link, so
 *  a descriptor must not be able to aim that link at a path of its own
 *  choosing, or at another host. */
const ZIP_NAME = /^[A-Za-z0-9._-]+\.zip$/
const SHA256 = /^[0-9a-f]{64}$/

/** Reads a descriptor, or answers null for anything that is not one.
 *
 *  Null is the ordinary case, not an error: a deployment built without an
 *  extension origin serves no package, and a development server answers
 *  this path with the SPA shell. Both arrive here as "not a descriptor",
 *  and the page falls back to the build instructions. */
export function parseExtensionRelease(value: unknown): ExtensionRelease | null {
  if (typeof value !== 'object' || value === null) return null
  const raw = value as Record<string, unknown>
  const { origin, version, versionName, zip, sha256 } = raw
  if (typeof origin !== 'string' || typeof version !== 'string') return null
  if (typeof versionName !== 'string' || typeof zip !== 'string') return null
  if (typeof sha256 !== 'string') return null
  if (!origin || !version || !versionName) return null
  if (!ZIP_NAME.test(zip) || !SHA256.test(sha256)) return null
  return { origin, version, versionName, zip, sha256 }
}

/** Whether this package is the one for the deployment being read.
 *
 *  A string comparison of two origins, which is what `location.origin` and
 *  a manifest pattern are both derived from. Anything looser (host only,
 *  suffix matching) would accept a package built for a sibling deployment
 *  that the browser would then refuse to connect. */
export function extensionReleaseServes(release: ExtensionRelease, origin: string): boolean {
  return release.origin === origin
}
