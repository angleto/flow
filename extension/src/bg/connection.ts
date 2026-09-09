// The connect handshake, from this side.
//
// The extension never sees a password and never mints anything itself. It
// opens the app's own settings page with a nonce, the person approves
// there -- where the app can show them exactly what is being granted, in
// the server's own words -- and the app hands back a credential scoped to
// that grant.
//
// The handover arrives over `externally_connectable`, which means Chrome
// fills in the sender's origin and the page cannot claim to be somewhere
// it is not. The alternative, a content script on the app origin talking
// by postMessage, would put our code inside the page that holds the
// human's own session in localStorage. This way the extension cannot read
// that page at all.

import {
  CONNECT_EXTENSION_ID_PARAM,
  CONNECT_MESSAGE_KIND,
  CONNECT_ROUTE,
  CONNECT_STATE_PARAM,
  type ConnectMessage,
  type ConnectReply,
} from '@shared'
import { call } from './api'
import { config } from './config'
import { storage } from './storage'
import type { StoredConnection } from './storage'

/** Long enough to read the grant and think about it, short enough that an
 *  abandoned request cannot be completed by a page opened much later. */
const NONCE_TTL_MS = 5 * 60 * 1000

export async function beginConnect(): Promise<{ url: string }> {
  const nonce = crypto.randomUUID().replace(/-/g, '')
  await storage.setNonce(nonce, NONCE_TTL_MS)
  const target = new URL(CONNECT_ROUTE, config.origin)
  // Neither value is a secret. The nonce only has to be unguessable and
  // short-lived; the id is public even for an unlisted listing, and it is
  // there so the page can name what is asking.
  target.searchParams.set(CONNECT_STATE_PARAM, nonce)
  target.searchParams.set(CONNECT_EXTENSION_ID_PARAM, chrome.runtime.id)
  return { url: target.toString() }
}

function isConnectMessage(value: unknown): value is ConnectMessage {
  if (!value || typeof value !== 'object') return false
  const m = value as Record<string, unknown>
  return (
    m.kind === CONNECT_MESSAGE_KIND &&
    typeof m.state === 'string' &&
    typeof m.secret === 'string' &&
    typeof m.assistantId === 'string' &&
    Array.isArray(m.scope) &&
    typeof m.workspace === 'object' &&
    m.workspace !== null
  )
}

/** Checks in this order, and refuses at the first failure. The origin
 *  comes first because it is the only one the page cannot influence. */
export async function acceptHandover(
  message: unknown,
  senderOrigin: string | undefined,
): Promise<ConnectReply> {
  if (senderOrigin !== config.origin) return { ok: false, reason: 'wrong-origin' }
  if (!isConnectMessage(message)) return { ok: false, reason: 'unknown-state' }

  const held = await storage.nonce()
  if (!held || held.value !== message.state) {
    // A page we did not send here, or one replaying an old link. Either
    // way this extension did not ask for this credential.
    return { ok: false, reason: 'unknown-state' }
  }
  if (held.expiresAt < Date.now()) {
    await storage.clearNonce()
    return { ok: false, reason: 'expired' }
  }

  const workspace = message.workspace
  const existing = await storage.connection(workspace.id)
  if (existing && !existing.revoked && existing.secret) {
    // Not an error to hide: minting a second credential for a workspace
    // that already has a live one leaves the first one live and
    // unattributed on the server.
    await storage.clearNonce()
    return { ok: false, reason: 'already-connected' }
  }

  await storage.putConnection({
    workspaceId: workspace.id,
    workspaceName: workspace.name,
    assistantId: message.assistantId,
    // What the SERVER granted, from its own answer, rather than what the
    // extension asked for. If the two ever differ, the extension reports
    // the truth.
    scope: message.scope,
    secret: message.secret,
  })
  await storage.clearNonce()
  if ((await storage.activeWorkspace()) === null) {
    await storage.setActiveWorkspace(workspace.id)
  }
  await spreadAcrossWorkspaces({
    workspaceId: workspace.id,
    workspaceName: workspace.name,
    assistantId: message.assistantId,
    scope: message.scope,
    secret: message.secret,
  })
  return { ok: true }
}

/** Ask the server which workspaces this credential reaches, and file one
 *  row per workspace when the answer is more than one.
 *
 *  ASKED, never told: the page could claim anything, and what a
 *  credential reaches is a fact the server holds about it. The panel's
 *  whole model is per workspace -- the scope selection, the recents, the
 *  caches and the pinned task are all keyed by workspace and would be
 *  wrong if they were shared -- so an account-bound credential becomes
 *  several rows over one secret rather than a special case in every one
 *  of those places.
 *
 *  A failure here is not a failed connection. The credential works and
 *  the row for the workspace it was connected in is already stored; the
 *  panel opens there and picks the rest up on its next refresh. */
export async function spreadAcrossWorkspaces(conn: StoredConnection): Promise<void> {
  const res = await call<{
    binding: string
    workspaces: { id: string; name: string; role: string }[]
  }>(conn, '/agent/workspaces')
  if (!res.ok) return
  if (res.data.binding !== 'account') return
  const listed = new Set<string>()
  for (const ws of res.data.workspaces) {
    listed.add(ws.id)
    const existing = await storage.connection(ws.id)
    if (existing && existing.assistantId !== conn.assistantId && !existing.revoked) {
      // Another credential is already filed for this workspace -- one
      // this person connected deliberately, before. Overwriting it would
      // revoke nothing on the server and make the panel forget which
      // credential it had been using, so it is left alone.
      continue
    }
    await storage.putConnection({
      ...conn,
      workspaceId: ws.id,
      workspaceName: ws.name,
      binding: 'account',
    })
  }
  // A workspace its holder has left is not reachable any more, and a row
  // for it would offer a switch that fails. Only rows belonging to THIS
  // credential are dropped: another credential's row is not ours.
  for (const row of await storage.connections()) {
    if (row.assistantId !== conn.assistantId) continue
    if (listed.has(row.workspaceId)) continue
    await storage.forgetConnection(row.workspaceId)
  }
}
