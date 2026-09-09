// The contract between the app and the browser extension.
//
// Both sides need the same answers and neither can be the authority: the
// app MINTS the credential and must disclose exactly what it is granting,
// the extension RECEIVES it and must be able to say what it holds. Written
// twice, the disclosure and the grant drift, and the direction that drift
// takes is always the same one -- the consent screen keeps saying what the
// grant used to be.
//
// Pure by contract: this directory is compiled into both packages and
// imports nothing.

/** The scopes the extension asks for, and nothing beyond them.
 *
 *  Each line is a capability the panel actually uses. Two are deliberately
 *  ABSENT and the absence is the design:
 *
 *  - ``workflows:write`` would let it delete the state machine every task
 *    in the workspace runs on. Advancing one task is ``tasks:state``.
 *  - ``tags:write`` would let it invent, rename and rescope the taxonomy.
 *    Filing a task into a client or project that already exists is
 *    ``tags:assign``.
 *
 *  Both narrow keys exist because this list was written: the wide ones were
 *  each doing two jobs, and a client that needed the small power had to be
 *  granted the large one. */
export const EXTENSION_SCOPES: readonly string[] = [
  'tasks:read',
  'tasks:write',
  'tasks:state',
  'notes:read',
  'notes:write',
  'tags:read',
  'tags:assign',
  'workflows:read',
  'search:read',
  'search:write',
  'attachments:write',
] as const

/** What an assistant row minted for the extension carries, so the app can
 *  list "connected browsers" without guessing which rows are which and
 *  without a second table. */
export const EXTENSION_PROVIDER = 'mycelium-extension'

/** The route the extension opens to ask for a connection. It is a normal
 *  settings page, reached two ways: a person clicking through Settings,
 *  and the extension opening it with the two parameters below. One page,
 *  so the disclosure cannot differ between the two paths. */
export const CONNECT_ROUTE = '/settings/extension'

/** Query parameters of a connect request.
 *
 *  ``state`` is a single-use nonce the extension minted and is holding: it
 *  is what stops a page the user did not open from claiming a credential,
 *  because the extension refuses a handover whose nonce it does not
 *  recognise. It is NOT a secret and NOT an authorization input; it only
 *  has to be unguessable and short-lived.
 *
 *  ``id`` is the extension's own id, shown to the person so the screen can
 *  name what is asking. The extension verifies the ORIGIN of the message
 *  it receives rather than trusting anything in this URL. */
export const CONNECT_STATE_PARAM = 'state'
export const CONNECT_EXTENSION_ID_PARAM = 'id'

/** The message the app posts to the extension once a person has approved.
 *
 *  The secret travels as a structured clone between two contexts of one
 *  browser: never a URL, never a header, never the DOM, never a log. The
 *  extension checks the sender's origin -- which Chrome fills in, and the
 *  page cannot forge -- before it looks at anything in here. */
export const CONNECT_MESSAGE_KIND = 'mycelium/connect'

export interface ConnectMessage {
  kind: typeof CONNECT_MESSAGE_KIND
  /** Echo of the nonce, so the extension can tell its own request from
   *  one it never made. */
  state: string
  /** The raw ``mycelium_at_`` value, returned by the server exactly once. */
  secret: string
  workspace: { id: string; name: string }
  /** The assistant row behind the credential. The extension shows it so a
   *  person can find the right row to revoke, and revocation is the app's
   *  job -- disconnecting in the browser only forgets the secret. */
  assistantId: string
  /** What was actually granted, from the server's answer rather than from
   *  this file, so the extension reports the truth if the two ever differ. */
  scope: string[]
  /** The reader's current theme choice, carried once so a freshly
   *  connected panel does not open in the wrong palette. Not a setting the
   *  app owns afterwards: the extension has its own. */
  theme?: 'auto' | 'light' | 'dark'
}

export interface ConnectReply {
  ok: boolean
  /** Present when ok is false: why the extension refused, so the page can
   *  say something better than "it did not work". */
  reason?: 'unknown-state' | 'expired' | 'already-connected' | 'wrong-origin'
}

/** Where a deployment publishes the package it serves, and the descriptor
 *  that says what that package is.
 *
 *  The origin the extension talks to is compiled into it: `host_permissions`
 *  and `externally_connectable` are static manifest declarations, so one
 *  archive cannot serve two deployments, and an archive published centrally
 *  would point every installer at whichever deployment built it. The
 *  deployment therefore serves its own, and the page shows the download only
 *  when the descriptor names the origin the page is itself being served
 *  from: offering one that could not connect is worse than offering none.
 *
 *  The producer is `extension/scripts/release.mjs`, in the other package.
 *  Both directions are asserted by `extension/tests/release.test.ts`. */
export const EXTENSION_PACKAGE_DIR = '/extension/'
export const EXTENSION_RELEASE_URL = `${EXTENSION_PACKAGE_DIR}release.json`

export interface ExtensionRelease {
  /** The deployment the package inside the archive talks to, and the only
   *  origin that can hand it a credential. */
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
