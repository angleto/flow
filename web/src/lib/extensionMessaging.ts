// The one browser API this app uses to hand a secret to an extension.
//
// Declared here rather than by adding @types/chrome to the SPA: the
// surface needed is one function and one error field, and the SPA is not
// an extension.
//
// The property is ``chrome.runtime``, and it is worth naming why that
// matters. Chrome exposes it to a PAGE only when some installed
// extension lists this page's origin in ``externally_connectable``, so
// its absence is a real and common state -- no extension installed, or
// one built against a different deployment -- and the page has to be
// able to tell that apart from a refusal. Reading it from the wrong
// place makes every browser look like that state: the page mints a
// credential, finds no channel, and tells the reader to revoke it. That
// shipped, and it is why the connect flow could never complete.

import type { ConnectReply } from '../shared'

type ExtensionMessaging = {
  chrome?: {
    runtime?: {
      sendMessage?: (
        extensionId: string,
        message: unknown,
        callback: (reply: ConnectReply | undefined) => void,
      ) => void
      lastError?: { message?: string }
    }
  }
}

export type ChromeRuntime = NonNullable<NonNullable<ExtensionMessaging['chrome']>['runtime']>

/** The messaging API, or undefined where this browser offers none. */
export function chromeRuntime(): ChromeRuntime | undefined {
  return (globalThis as unknown as ExtensionMessaging).chrome?.runtime
}

/** Hand the message over and wait for the extension's answer.
 *
 *  ``lastError`` is read INSIDE the callback, which is the only place it
 *  is defined: Chrome clears it as soon as the callback returns, so a
 *  read afterwards is always undefined and the page loses the one
 *  sentence that says what went wrong. */
export function handOver(
  runtime: ChromeRuntime,
  extensionId: string,
  message: unknown,
): Promise<{ reply: ConnectReply | undefined; lastError: string | undefined }> {
  return new Promise((resolve) => {
    if (!runtime.sendMessage) {
      resolve({ reply: undefined, lastError: undefined })
      return
    }
    runtime.sendMessage(extensionId, message, (reply) => {
      resolve({ reply, lastError: runtime.lastError?.message })
    })
  })
}
