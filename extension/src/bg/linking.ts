// Being let in without being handed anything.
//
// The extension opens a request, shows a short code, and sends the person
// to the application they are already signed into. They compare the code
// and approve. The extension, still asking, collects an ordinary scoped
// credential.
//
// WHY NOT A PASSWORD FIELD IN THE PANEL: it would put the password into a
// second process, it teaches people to type it into whatever asks, and it
// would fall behind the day signing in wants a second factor. Here that
// second factor, when it exists, has already been satisfied by the session
// that approves.
//
// WHY NOT THE HANDSHAKE THIS REPLACES, which had the page mint a
// credential and push it in over `externally_connectable`:
//
//   the request lived in the URL the page was opened with, so any redirect
//   between that URL and the reader destroyed it. The login redirect did,
//   and "press Connect" became "the extension is broken";
//
//   the credential was minted before anyone knew this extension would take
//   it, so a refused handover left one live and unheld on the server;
//
//   and it required a web page to hold the standing right to send messages
//   to this extension. That entry is gone from the manifest.
//
// WHAT WAITS IS THIS WORKER, not the panel: the panel closes the moment
// somebody switches to the tab to approve. A fast chain runs while the
// worker is alive and an alarm is the net under it, because Chrome shuts
// an idle worker down and will not schedule below half a minute.

import { CONNECT_CODE_PARAM } from '@shared'
import { callUnauthenticated } from './api'
import { config } from './config'
import { storage } from './storage'
import type { FailureCode, LinkRequest, Result } from '../shared/protocol'

/** Between one question and the next, while the worker is alive. The
 *  server suggests this value; it is not enforced, and asking faster
 *  gets the same answer. */
const FAST_POLL_MS = 3_000

/** The net under the fast chain. Chrome will not go below half a minute,
 *  which is fine for a net and would not be fine as the only way of
 *  waiting. */
export const LINK_ALARM = 'link-poll'
export const LINK_ALARM_MINUTES = 0.5

interface OpenedRequest {
  device_code: string
  user_code: string
  verification_path: string
  expires_at: string
  interval: number
}

interface CollectedCredential {
  secret: string
  assistant_id: string
  workspace_id: string
  workspace_name: string
  scope: string[]
  expires_at: string | null
}

/** Open a request and send the person to answer it.
 *
 *  The tab is a convenience and never the only path: the panel shows the
 *  code, and the settings page takes it typed by hand. A browser that
 *  refuses to open the tab, or a link that lands in another profile, must
 *  not be the end of the ceremony. */
export async function startLink(): Promise<Result<LinkRequest>> {
  const reply = await callUnauthenticated<OpenedRequest>('/auth/device/authorize', {
    client: 'extension',
  })
  if (!reply.ok) return reply

  const opened = reply.data
  const verification = new URL(opened.verification_path, config.origin)
  // Not a secret, and the page says so: the short code collects nothing
  // on its own. It is in the URL to save typing, and typing it is the
  // path that always works.
  verification.searchParams.set(CONNECT_CODE_PARAM, opened.user_code)
  const link: LinkRequest = {
    userCode: opened.user_code,
    expiresAt: opened.expires_at,
    verificationUrl: verification.toString(),
    failure: null,
  }
  await storage.setLink({ ...link, deviceCode: opened.device_code })
  await chrome.alarms.create(LINK_ALARM, { periodInMinutes: LINK_ALARM_MINUTES })
  await chrome.tabs.create({ url: link.verificationUrl })
  return { ok: true, data: link }
}

export async function cancelLink(): Promise<void> {
  await storage.clearLink()
  await chrome.alarms.clear(LINK_ALARM)
}

/** The open request as the panel reads it: without the half that
 *  collects. The panel renders the short code, and a panel that also held
 *  the long one would be putting a collecting secret into a surface that
 *  has no reason to hold one. */
export async function currentLink(): Promise<LinkRequest | null> {
  const stored = await storage.getLink()
  if (!stored) return null
  const { deviceCode: _collects, ...visible } = stored
  return visible
}

async function fail(code: FailureCode): Promise<void> {
  const stored = await storage.getLink()
  if (!stored) return
  await chrome.alarms.clear(LINK_ALARM)
  // The request STAYS, with the reason on it. Clearing it would take the
  // panel back to how it looked before anyone pressed anything, which
  // reads as "nothing happened" rather than as "this was refused".
  await storage.setLink({ ...stored, failure: code })
}

/** Ask whether anybody has answered. True once the matter is settled,
 *  which is when there is something new for the panel to show. */
export async function pollLink(): Promise<boolean> {
  const stored = await storage.getLink()
  if (!stored || stored.failure) return false

  if (new Date(stored.expiresAt).getTime() <= Date.now()) {
    await fail('expired')
    return true
  }

  const reply = await callUnauthenticated<CollectedCredential>('/auth/device/token', {
    device_code: stored.deviceCode,
  })

  if (!reply.ok) {
    // "Still waiting" arrives as a 4xx carrying auth.device_pending,
    // because the server refuses to answer it with a 2xx: every HTTP
    // client reads a 2xx as "it worked", and a client that stores an
    // empty answer and calls itself connected is a real bug rather than
    // a hypothetical one. Here it is the ordinary answer for most of a
    // request's life, and it is not a failure.
    if (reply.error.code === 'pending') return false
    await fail(reply.error.code)
    return true
  }

  const granted = reply.data
  if (!granted?.secret || !granted?.workspace_id || !granted?.assistant_id) {
    // Belt and braces on the shape as well as the status. The status is
    // the server's contract and this is the client's own guard: between
    // them, "connected with nothing" cannot be reached by one mistake.
    await fail('server')
    return true
  }

  await storage.putConnection({
    workspaceId: granted.workspace_id,
    workspaceName: granted.workspace_name,
    assistantId: granted.assistant_id,
    // What the SERVER granted, from its own answer rather than from
    // anything compiled into this package. If the two ever differ, the
    // panel reports the truth.
    scope: granted.scope,
    binding: 'account',
    expiresAt: granted.expires_at ?? undefined,
    secret: granted.secret,
  })
  if ((await storage.activeWorkspace()) === null) {
    await storage.setActiveWorkspace(granted.workspace_id)
  }
  await cancelLink()
  return true
}

/** Keep asking for as long as this worker lives.
 *
 *  A fetch in flight keeps the worker awake, so this chain covers the
 *  ordinary case: the person approves while the tab is still open. If the
 *  worker is shut down first, the alarm takes over. */
export function pollUntilSettled(): void {
  void (async () => {
    for (;;) {
      try {
        if (await pollLink()) return
        const stored = await storage.getLink()
        if (!stored || stored.failure) return
        if (new Date(stored.expiresAt).getTime() <= Date.now()) return
      } catch {
        // Whatever went wrong, the alarm asks again in half a minute. A
        // loop that throws its way out would take the worker with it.
        return
      }
      await new Promise((resolve) => setTimeout(resolve, FAST_POLL_MS))
    }
  })()
}
