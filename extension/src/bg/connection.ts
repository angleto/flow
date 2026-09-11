// What a credential reaches, once the extension is holding one.
//
// The connect ceremony itself lives in linking.ts: the extension opens a
// request, shows a code, and collects. This module is the question that
// comes immediately after, and it is a separate one -- what a credential
// can reach is a fact the SERVER holds about it, and the answer shapes
// every per-workspace thing the panel keeps.

import { call } from './api'
import { storage } from './storage'
import type { StoredConnection } from './storage'

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
