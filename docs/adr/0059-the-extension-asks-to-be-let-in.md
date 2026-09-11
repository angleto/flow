# ADR-0059: The extension asks to be let in, it is not handed a secret

Records why the browser extension now connects through the device
authorization grant, what that replaces, why an alternative rejected in
ADR-0057 was reconsidered, and what the change does not cover.

Status: Accepted (2026-09-11)
Supersedes: decision 5 of [ADR-0057](0057-the-browser-is-a-fourth-surface.md)
(the `externally_connectable` handshake). The rest of 0057 stands: the
browser is still a fourth surface, the credential is still scoped and
account-bound, and Settings → Browser extension is still one page reached
two ways.
Relates to: migration `0013` (the request table), migration `0014` (the
cut), `docs/extension.md`.

## Context

ADR-0057 had the app's settings page mint a credential and push it into
the extension over Chrome's `externally_connectable` messaging. The
extension opened that page with a nonce in the query string; the person
approved; the page sent the secret back through Chrome.

It stopped working, and the way it stopped is the argument for this
document. An anonymous visitor pressing Connect was sent to the login
form, which discarded the URL it had refused and landed them on their
notes. The request was those query parameters, so logging in destroyed
it. From the outside this reads as "the extension needs its own login" —
which is precisely the conclusion it produced, and precisely the thing
0057 existed to avoid.

Repairing the redirect (the guard now carries the refused address to the
login form, and the form returns to it) was necessary for every guarded
route and is not in question here. What it did not repair is that the
request could be lost at all.

Two further defects have the same root, which is that the PAGE was the
active party:

- **A credential existed before anyone held it.** The mint happened
  before the handover was attempted, so every refused handover left a
  live credential nobody held and nobody knew to revoke. The page could
  only ask the reader to go and revoke it by hand.
- **A web origin held a standing right to message the extension.** Bought
  permanently, to save a few seconds of a ceremony performed once. It
  also could not work at all against `localhost`: Chrome refuses such a
  pattern for a host with no second-level domain, so a development
  package could never connect — a limitation documented and lived with
  for the life of that mechanism.

The sibling project (`costa_associati`) weighed the same trade in the
opposite direction and wrote it down in its ADR-0004, rejecting
`externally_connectable` as "a surface in more, outward, to save three
seconds". Its extension was verified working through the device grant. Our
own 0057 rejected a password field in the popup, a pasted personal access
token, `chrome.identity.launchWebAuthFlow` over the broken OAuth shim, a
content script with `postMessage`, and an `<all_urls>` overlay. It never
considered the device grant: the better alternative was not evaluated and
rejected, it was skipped.

## Decision

**The extension asks, and collects. Nothing is handed to it.** The device
authorization grant (RFC 8628), reduced to what a first-party surface
needs:

1. The extension opens a request (`POST /auth/device/authorize`, no
   credential) and receives two codes: a SHORT one it displays, and a
   LONG one it keeps.
2. It opens `/settings/extension?code=<short>`. A person with no live
   session goes through the login form and comes back, and losing that URL
   now costs some typing rather than the request: the page takes the code
   by hand, and the panel keeps showing it.
3. The page shows what is asking, from where, since when, the exact grant
   in the server's own words, and the code TO COMPARE. Approve or refuse.
4. The extension, still asking, collects (`POST /auth/device/token`).

**The credential is minted by the collection, not by the approval.**
Approval records who approved and in which workspace; `redeem` creates the
credential inside the transaction that marks the request spent. So
approved-but-uncollected has no representation: there is no window in
which an uncollected secret exists, none at rest between the halves, and
nothing to orphan.

**Waiting answers 400, never 202.** Every HTTP client reads a 2xx as "it
worked". The sibling shipped the 202 version and its test caught a client
that stored an empty answer and believed itself connected; the standard
grant uses 400 for the same reason. The extension also checks the SHAPE of
a success before storing it, so "connected with nothing" needs two
independent mistakes.

**Two codes, because they do two jobs.** The short one is read by a human
and compared, so it is short and drawn from an alphabet with no character
that reads as another (no I/1, L, O/0, U/V); on its own it collects
nothing, which is why it may sit in a URL, a log or a bookmark. The long
one is the only thing that collects, is never displayed, and only its
SHA-256 is stored — the discipline `agent_tokens` and `refresh_tokens`
already use.

**The authority model does NOT change.** The sibling issues an ordinary
session because it has no scope model; mycelium has one, so the extension
still receives a credential scoped to `EXTENSION_SCOPES`, account-bound,
acting as its holder in the workspaces they belong to. Adopting the
sibling's delivery mechanism is not a reason to adopt its authority model,
which here would be a widening. `EXTENSION_SCOPES` moves to the server, as
a SUBSET of `SELF_SERVICE_SCOPES` rather than an alias of it: one is a
policy ceiling and the other a request, and aliased the extension would
widen the day the ceiling did.

**The credential lasts 90 days**, against the 365 `agent_tokens` defaults
to for a forgotten machine-to-machine secret. This one sits in a browser
profile on a machine that may be shared, and renewing it is the same two
clicks that created it.

**A clean cut, not a coexistence.** Migration `0014` revokes every
credential the old handshake produced and deactivates its assistant rows;
the page that minted, the module that pushed, and the
`externally_connectable` entry are all deleted in the same release.
Everyone using the extension connects once more. The alternative was
running both mechanisms until the last old credential lapsed, which means
maintaining and reasoning about two authentication paths for a year, and
keeping the outward surface open that long, to save one ceremony.

## Consequences

- A development build connects. The `externally_connectable` pattern was
  the reason a `localhost` package could not, and it is gone, along with
  `connectMatchFor` and the `canConnect` flag that existed only to tell a
  reader the button could never work.
- The extension asks for one more permission, `alarms`, and the reason is
  visible: Chrome shuts an idle service worker down, so the fast poll
  chain cannot be the only thing waiting for somebody to approve.
- The panel is no longer able to be handed anything, so `handshake` tests,
  the `onMessageExternal` listener, and the SPA's `extensionMessaging`
  module are deleted rather than adapted.
- The shared contract shrinks to a route and a query parameter. The grant
  is disclosed from the server's answer (`GET /auth/device/pending`), so
  the consent screen and the mint cannot drift.
- A person can connect a browser that never opens the tab: the panel shows
  the code, and the settings page accepts it typed.

## What this does not cover

- **A person who approves without comparing the code.** The code exists to
  be compared and both screens say so; nothing can compel the reading.
  What it does defeat is a page that opens a request of its own and sends
  somebody to approve it, because the code on that screen is not the code
  on their panel.
- **Anything already executing as the person on their own machine.** It
  can open a request and approve it, exactly as it could use the session
  directly.
- **A second factor is inherited, never added.** Approval happens inside a
  session that already satisfied whatever login required, so this flow
  does not know or care whether MFA exists.
- **The credential at rest.** `chrome.storage.local` is a database in the
  profile directory, not additionally encrypted; full-disk encryption is
  the control and it is the operating system's. Unchanged by this
  decision, and stated because the 90-day bound narrows the window rather
  than closing it.
- **Rate limiting on an unresolvable origin.** The open-request limit
  counts per network origin, and an origin the server cannot attribute is
  not limited — behind a proxy chain with no configured trust anchor every
  caller would otherwise share one bucket and the first noisy client would
  lock out the rest. What closes it is configuring the trusted proxies.

## Alternatives rejected

**Keeping `externally_connectable` and repairing only the redirect.** The
redirect repair was made and stands on its own merits. It leaves the other
two defects untouched, and leaves the request in a URL where the next
redirect anyone adds can lose it again.

**Running both mechanisms for one release.** Two live authentication paths
into the same credential, for a year, to avoid one two-click ceremony for
a user base that could be counted on one hand at the time of writing.

**Issuing an ordinary session, as the sibling does.** It would hand the
extension the full authority of its holder and throw away the scope fence
0057 built and the settings page discloses. The sibling does it because it
has no scope model, not because it is better.

**Polling from the panel instead of the worker.** The panel closes the
moment somebody switches to the tab to approve, so it cannot be the thing
that waits.

**A shared cookie between app and extension.** The extension does not
share the app's origin and should not: that separation is what makes its
credential revocable on its own.
