# The browser extension

Mycelium in a panel that opens on a keystroke over any page: find a task or a
note, change its state or its due date, and file the page you are reading
without leaving it. It is a thin adapter over the REST API and holds no
business logic (ADR-0001); the decision record is
[ADR-0057](adr/0057-the-browser-is-a-fourth-surface.md).

Chrome and Chromium-based browsers (Edge, Brave, Arc). Firefox and Safari use
a different extension format and are not supported.

## Installing it

**Settings → Browser extension** in the app is the page for this, and it is
available to everyone: installing a browser extension is not an
administrative act. It carries the install steps, the connect flow, the list
of browsers currently connected, and the limits below.

The item is not on the Chrome Web Store yet, so it installs by hand. There
are two paths and the page picks between them for you.

**When the deployment serves a package** it offers the archive for download.
Unzip it, open `chrome://extensions`, switch on **Developer mode**, choose
**Load unpacked**, and pick the folder you unzipped. Open the panel with
`Ctrl+Shift+K` (`Cmd+Shift+K` on a Mac) and press **Connect**.

The archive is served at `/extension/mycelium-extension-<version>.zip`,
beside `/extension/release.json` which names the origin it was built for,
both versions, and its SHA-256. The page shows the download **only if that
origin is the one it is itself being served from**: an archive built for
another deployment cannot receive a credential here, because the origin is
compiled into its manifest, so offering it would be offering a dead end.

**Otherwise** the page shows the build, with this deployment's origin already
in the command:

```sh
git clone https://github.com/angleto/mycelium
cd mycelium/extension
pnpm install
MYCELIUM_EXTENSION_ORIGIN=https://mycelium.xeno.garden pnpm build
```

Then **Load unpacked** on `extension/dist/unpacked`.

`MYCELIUM_EXTENSION_ORIGIN` has no default and the build fails without it. It
decides three things at once — which deployment the package talks to, which
origin it may fetch from, and which origin may hand it a credential — so that
a package cannot end up permitted to reach one deployment while its code
talks to another.

## Connecting a browser

A connection always starts in the extension, never in the app. The extension
opens the settings page with a single-use nonce; the app shows which
extension is asking, which workspace it would be granted, and the exact list
of permissions, rendered from the server's own catalogue so the screen and
the grant cannot disagree. Approving mints a credential scoped to that list
and hands it to the extension.

You never type a password into the extension, and the extension never reads
the app's page.

**A credential is per workspace.** Connecting a second workspace is a
separate, deliberate act performed from inside that workspace.

### What it may do

```
tasks:read  tasks:write  tasks:state
notes:read  notes:write
tags:read   tags:assign
workflows:read
search:read search:write
attachments:write
```

It cannot read your account, list your other workspaces, create or rename
clients and projects, edit or delete workflows, or delete anything. Two of
those keys exist because this list was written: `tasks:state` was carved out
of `workflows:write`, and `tags:assign` out of `tags:write`, because each of
the wide keys was doing two jobs and a client that needed the small power had
to be granted the large one.

### Disconnecting is not revoking

**Disconnect** in the extension forgets the secret on that machine. It does
not end the credential on the server. **Revoke**, on the settings page, is
what does — and it is the one that matters if a machine is lost.

## What it does not protect against

- **Anything running as your Chrome profile.** The credential lives in
  `chrome.storage.local`, a database in the profile directory with no
  additional encryption. Full-disk encryption is the control, and it is the
  operating system's.
- **A compromised update of the extension itself.** The publishing account is
  the trust anchor.
- **A browser already compromised when you connect.** The handshake grants
  exactly what the logged-in person could already do.
- **Replay at speed.** There is no rate limiting on the token surface, and
  nothing reads a credential's last-used timestamp, so there is no "this was
  used from somewhere new" signal.

## Using the panel

`Ctrl/Cmd+Shift+K` opens the popup, `Ctrl/Cmd+Shift+L` the side panel,
`Ctrl/Cmd+Shift+S` the capture sheet, and `Ctrl/Cmd+Shift+F` searches for the
text you have selected on the page. All four are rebindable at
`chrome://extensions/shortcuts`, which the panel footer links to.

**Two keyboard modes.** The query line owns every bare key while you are
typing in it, so row commands live one `Tab` away. In list mode a row has
real focus and bare letters are commands: `e` expands the editor, `x`
advances the task to the first terminal state its workflow offers, `d` sets a
due date, `a` attaches the page you are on, `c` copies the 8-hex code, `p`
keeps it above the list, and `/` or `Esc` goes back to typing.

**The query line** understands a strict subset of the `/tasks` filter
language plus two sigils of its own:

| Token | Effect |
|---|---|
| `in:Acme` | look inside that client or project for this query only, without changing your pinned focus |
| `in:*` | look everywhere, same caveat |
| `is:task`, `is:note` | one kind |
| `is:archived` | include the archive shelf |
| `@legale` | narrow by tag, ANDed, exactly as on `/tasks` |

Anything else that looks structured (`state:verify`, `due:today`, a union
`|`) is **dropped from the request and shown as an unresolved chip** rather
than quietly treated as free text. On `/tasks` that degradation is harmless,
because the predicate runs over a list the page already holds; here the
server has already cut its answer to the top twenty, so a silently
reinterpreted token changes which twenty came back. A drift gate
(`pnpm check:grammar`) refuses a key this surface handles that the `/tasks`
grammar does not know, so the two cannot become two languages.

**The side panel** is a working surface rather than a bigger popup. It keeps
the page you are on in a strip at the top, one task pinned above the list,
and three sections: due or overdue, most pressing, recently touched. None of
them names a workflow state, because the state machine is per workspace and a
panel asking for "in progress" would hold a second definition of it. There is
no push, so the footer says when it last looked and offers to look again; it
refreshes when the panel becomes visible and, while you are watching it,
every thirty seconds.

**Attaching a file** is a side-panel capability only: opening an OS file
dialog dismisses a Chrome popup and would take the draft with it. A screenshot
works in both, of the tab you invoked the extension from.

**A capture that timed out does not become two.** The sheet mints one
idempotency key when it opens and presents the same one on a retry, so the
server replays its first answer instead of filing a second task. That is also
what lets the panel offer "retry" after a timed-out capture rather than only
"go and check".

## Permissions, and why each one

| Permission | For |
|---|---|
| `storage` | the credential, the scope selection, the recents list |
| `sidePanel` | the persistent working surface |
| `contextMenus` | capture from a selection or a link |
| `activeTab` | read the page **only** at the moment you invoke capture, and only the tab you invoked it from |
| `scripting` | what `activeTab` is exercised through |
| `host_permissions` on the deployment | the API requests, all of them from the service worker |
| `externally_connectable` on the deployment | the one origin allowed to hand over a credential |

Not requested: `<all_urls>` (there is no in-page overlay, so no code of ours
runs on the pages you visit), `alarms`, `notifications`, `tabs`,
`unlimitedStorage`. There is **no content script**, on any origin including
the app's own — a script there could read your session out of the page.

## Building and shipping

| Command | Result |
|---|---|
| `make extension-check` | the gate CI runs: lint, message catalogue, design tokens, typecheck, unit tests |
| `MYCELIUM_EXTENSION_ORIGIN=… make extension-build` | `extension/dist/unpacked`, loadable |
| `MYCELIUM_EXTENSION_ORIGIN=… make extension-zip` | `extension/dist/mycelium-extension-<version>.zip` and `release.json` |

`extension-zip` refuses a non-https origin: the store would accept a
localhost build, and every installer would get an extension that talks to
their own machine. The script behind it is `pnpm zip` and not `pnpm pack`
because `pack` is a pnpm built-in that shadows a script of that name — the
target used to produce an npm tarball of the sources, silently, and never the
archive it named.

**A deployment serves its own archive.** The frontend image takes
`MYCELIUM_EXTENSION_ORIGIN` as a build argument with no default: given one it
builds the package and serves it under `/extension/`, given nothing it serves
none and the settings page falls back to the build instructions. `docker
build` without the argument therefore still produces a deployment-neutral
image. In an image build the version comes from `MYCELIUM_VERSION` rather
than from `git describe`, because the build context carries no `.git`.

**The version comes from the release tag**, never from a file:
`v2.3.10` becomes `2.3.10` in the manifest, and the full `git describe`
string survives in `version_name`, which is what `chrome://extensions` shows.
Two consequences follow. A **re-tag cannot produce a shippable package**,
because the store refuses a second upload at the same version. And every
monorepo tag bumps the extension's version even when nothing in it changed,
which is why upload is a human decision rather than a step in the release
workflow.

### A development build cannot connect

Chrome refuses an `externally_connectable` pattern whose host has no
second-level domain, so a package built against `localhost` cannot receive
the credential handover at all. The manifest omits the entry and the panel
says so, rather than offering a button that can never succeed. To exercise
the extension against a local stack, connect it to a deployment with a real
hostname.

## Where things are

| Piece | Path |
|---|---|
| The package | `extension/` |
| The service worker: the only network point and token holder | `extension/src/bg/` |
| The panel, shared by the popup and the side panel | `extension/src/ui/` |
| The typed panel↔worker seam | `extension/src/shared/protocol.ts` |
| Rules shared with the SPA (error envelope, entity code, recents, query grammar, handshake) | `web/src/shared/` |
| The settings page, the download and the connect flow | `web/src/routes/SettingsExtensionRoute.tsx` |
| What a deployment publishes about its package | `extension/scripts/release.mjs`, read by `web/src/shared/extension.ts` |
| The privacy statement the store listing points at | [`extension-privacy.md`](extension-privacy.md) |
