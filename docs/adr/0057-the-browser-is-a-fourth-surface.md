# ADR-0057: The browser is a fourth surface, and it holds a scoped credential

> **Decision 5 of this document is SUPERSEDED by
> [ADR-0059](0059-the-extension-asks-to-be-let-in.md) (2026-09-11).** The
> credential is no longer minted by the settings page and pushed into the
> extension over `externally_connectable`: the extension opens a device
> authorization request, shows a code to compare, and collects what it was
> granted. Everything else here stands. The reasoning below is kept as
> written, including the alternatives it rejected -- which did not include
> the device grant, and that omission is the subject of 0059.

Status: Accepted (2026-09-03). Amended 2026-09-09 twice: a deployment serves
its own package, and the credential is bound to the account rather than to one
workspace (see the amendments at the end).

Relates to: ADR-0001 (one domain, thin adapters — this is a fourth adapter and
holds no business logic), ADR-0003 (the unified typed tag, which is what a
focus is), ADR-0017 (English-only source and message catalogues), ADR-0035
(search-click telemetry, which this surface must emit or it drags the recall
sensor down for everyone), ADR-0038 (the UUID-prefix resolver, which is the
panel's fastest branch), ADR-0050 (a task carries exactly one client and one
project). Surfaces table in `docs/cli.md`; user- and operator-facing
statement in `docs/extension.md`.

## Context

Mycelium's fastest path from "I am reading something" to "it is in my system"
runs entirely inside the app: open a tab, wait for the SPA, press Cmd+K,
search, click. Every step needs the app to be the thing in front of you, and
the one thing the SPA structurally cannot do is read the page you are
actually on.

Three facts about this codebase shaped the answer more than any preference:

- **A focus is one tag id, not a list.** `services/tag_assignment._apply`
  gives every task both its client tag and its project tag, and the memory
  tag filter is a faceted AND — an entity must carry every requested tag. So
  the SPA's `scopeTagIds` (`[client, ...its projects]`) sent to `POST /search`
  matches nothing at all. That shape is compensation for
  `advisory.what-now`'s OR matching, not a domain rule, and copying it into a
  second client would have produced an empty panel with no error.

- **Two permissions were each doing two jobs.** `workflows:write` meant both
  "advance one task" and "create, edit and delete the state machine every
  task runs on"; `tags:write` meant both "file a task into an existing
  project" and "invent, rename and rescope the vocabulary every entity
  carries". A client that needed the small power had to be granted the large
  one, so a least-privilege browser credential was not expressible.

- **A scoped credential could not ask what it may do.** `GET /auth/me` and
  `GET /workspaces` are `HUMAN_ONLY` and rightly so, and MCP's `whoami` is a
  tool. A scoped client that speaks only HTTP had to hardcode the scope list
  it was minted with, which drifts silently the moment anyone edits the
  assistant in Settings.

The credential question had one further constraint that removed most of the
options: the app keeps the human's session JWT and its 90-day refresh token
in `localStorage` on its own origin. Any design placing extension code inside
that page can read them.

## Decision

**A Chrome MV3 extension in `/extension/`, a separate package with its own
lockfile, outside any pnpm workspace.** It ships no runtime dependency at
all: the panel is plain TypeScript over the DOM, which keeps a surface that
must paint between keystrokes from carrying a renderer and a router. The
whole package is 18 kB of worker and 11 kB of panel.

**The service worker is the only network point and the only token holder.**
Not a trust argument — the panels are the same origin — but four practical
ones: the omnibox handler exists only there, so search elsewhere would be two
implementations of one rule; Chrome destroys the popup on focus loss and a
write issued from it has an unknown outcome, while one issued by the worker
completes; one module then owns every stored key and the single function that
clears them; and the CORS exemption is a `host_permissions` property that is
only unambiguous in the worker. The dividend is that the on/off switch,
checked at that one seam before the handler table, is a provable guarantee
that nothing leaves the browser.

**A connect handshake over `externally_connectable`, minting a scoped agent
token.** The extension opens `/settings/extension` on the app origin with a
single-use nonce; the person approves there, where the app renders the exact
grant from `GET /ai-assistants/scope-catalog`; the app mints via
`POST /ai-assistants` and hands the secret back as a structured clone. Chrome
fills in the sender's origin, which the page cannot forge, and the extension
gets zero read access to that page. **There is no content script at all**, on
any origin including the app's own.

**An agent token is confined to the workspace it was minted for.** The claim
was already in the token and every client already believed it -- the CLI
stores a workspace beside each credential and refuses to switch -- but the
server took the tenant from the header, so the per-workspace model was a
convention held up by clients. Both functions that open a tenant now compare
the two, and a mismatch is a 403 naming the credential rather than a 404
hiding the workspace.

**A capture that timed out does not become two.** The invoice API's
idempotency claim is widened with a second principal: a person acting in a
workspace, keyed with `org_id`, alongside the issuer it already had. The
header is optional on `POST /tasks` and `POST /notes`, because every client
that exists creates without one; what it buys is a client that cannot tell a
create which never arrived from one whose answer was lost.

**Two permissions are split so the grant can be small**: `tasks:state` out of
`workflows:write`, and `tags:assign` out of `tags:write`, each with a
transitional any-of set on both the REST and the MCP maps so a credential
granted the wide key before the split keeps working. The extension holds
eleven keys and cannot read the account, enumerate other workspaces, create
or rename clients and projects, edit workflows, or delete anything.

**`GET /agent/self` answers what a credential is**, under a new `META`
sentinel — authenticated and tenant-scoped, callable under any scope
including the empty one. This supersedes half of a decision recorded in
`route_scopes.py`: `/auth/me` stays `HUMAN_ONLY`, and META answers the
narrower question of what the CREDENTIAL is rather than who the person is.

**Rules that belong to the API rather than to a client move to
`web/src/shared/`**, compiled into both packages: the error envelope reader,
the entity-code contract, the recents contract, the search-click payload, the
query grammar's tokenizer and key set, the connect handshake, and the
generated `schema.d.ts`. `web/scripts/check-shared-purity.mjs` refuses any
import that leaves that directory and any deep import into it.

## Consequences

The switch is real rather than decorative, and the tests say so: with it off,
every operation but the switch and the connection is refused at the seam, and
the context menus Chrome persists across restarts are torn down with it.

An extension built against a development server **cannot receive a
credential**. Chrome refuses an `externally_connectable` pattern whose host
has no second-level domain, so `localhost` cannot appear in one. The manifest
omits the entry and the panel says the build cannot connect, rather than
offering a button that can never succeed.

The build has no default origin. One environment variable decides which
deployment the package talks to, which origin it may fetch from and which may
hand it a credential, so a package cannot be permitted to reach one
deployment while its code talks to another.

Two lockfiles is the price of the boundary, and it is what makes the boundary
real: React is not merely unused in `extension/`, it is not installable.

The extension emits `POST /search/click` for ranked opens only. A surface
that opened ranked results without emitting it would not merely lose a
metric, it would drag the recall figure down for every other surface.

**What the design does not protect against**, written here and repeated on
the connect page and in `docs/extension.md`: anything running as the user's
Chrome profile, since `chrome.storage.local` is an unencrypted database in
the profile directory; a compromised update of the extension itself; and a
browser already compromised at connect time.

Two transitional any-of sets now exist, dated and with a removal date of
2026-12-01. Left in place the splits are decorative, because everything keeps
working with the wide key and nothing ever moves off it.

## Alternatives rejected

**A password login in the popup**, which the sibling project's extension does
and which its own ADR calls phishing-shaped. It would land the extension
holding a credential that can mint more credentials, and re-implementing the
MFA challenge, the lockout and the verify branch would be a second
implementation of the login state machine.

**Pasting a personal access token.** `POST /agent-tokens` yields
`assistant_scope = None`, which the gate treats as no restriction: the full
authority of the user. The secret would also transit the clipboard.

**`chrome.identity.launchWebAuthFlow` against the existing OAuth shim.** Dead
on a fact rather than a preference: `routers/oauth.py` requires
`client_secret` in the token request and returns it back as the access token,
and its authorize endpoint authenticates nobody. The caller must already hold
the secret before the flow starts.

**A content script on the app origin, handing over by `postMessage`.** It
would put our code inside the page that holds the human's session and refresh
token in `localStorage`, to save nothing: `externally_connectable` gives a
sender origin Chrome fills in, which is a stronger check than one we would
write ourselves.

**Widening `MYCELIUM_CORS_ORIGINS` to `chrome-extension://<id>`.**
Unnecessary — the worker's host permission covers its fetches — and it would
put a browser-controllable origin on an allowlist that also fronts the
invoice API and the MCP endpoint.

**Widening `GET /workspaces` or `GET /auth/me` away from `HUMAN_ONLY`.**
Enumerating a user's workspaces is a property of the account, not of a
workspace-bound credential; widening it would let a credential scoped to one
workspace learn the ids and names of the others. The connect page hands the
workspace over with the secret instead, so the extension never enumerates.

**An in-page overlay on `<all_urls>`.** Prettier, and it would put our code
in every page the user visits and an "read your data on all websites" warning
in front of every installer, to replace a panel that a keystroke already
opens.

**Reusing the SPA's i18next catalogue.** The objection to Chrome's own
`_locales` was that `chrome.i18n` follows the browser UI language while the
app follows its own, so the panel could show one language wrapped around a
server sentence in another. That objection is removed by deriving
`Accept-Language` from the same source, and `_locales` then needs no
cross-package import and no second copy of the key checker.

**A pnpm workspace with `web/`, `extension/` and a shared package.** The tidy
answer, rejected on four concrete costs: a workspace has one root lockfile,
which breaks `cache-dependency-path: web/pnpm-lock.yaml` in two CI jobs, the
frontend Dockerfile's three-file COPY, `web/.npmrc`'s per-package
justifications, and `pnpm install` under `working-directory: web`. Revisit
when a third JavaScript consumer appears.

**The cursor envelope on `GET /tasks`.** The route gained `ids`, `q`,
`limit`, `order_by` and the date windows the service had always accepted, but
not the `{items, next_cursor, truncated}` envelope: that is a breaking change
to a response every client reads, and its real blocker is that the SPA's
tasks route filters client-side over the fat `TaskOut` — moving that
filtering server-side is its own piece of work. Recorded as a gap rather than
half-done.

## Amendment (2026-09-09): a deployment serves the package it can be installed from

### Context

The item is still not on the Chrome Web Store, so the settings page said to
build it. The instruction it carried was not merely terse, it did not work:
it named `pnpm build` in `extension/` and nothing else, while the build has
no default origin and stops without `MYCELIUM_EXTENSION_ORIGIN`. Anyone
following the screen got an error; the complete recipe lived only in
`docs/extension.md`, which is not what somebody reads while looking at an
install button. The screen also asked for a Node toolchain to install a
browser extension, which is a strange thing to ask of the person the panel
was written for.

The obvious remedy -- publish one archive as a release asset and link it --
is the one this design cannot take. The origin is compiled into the package:
`host_permissions` and `externally_connectable` are static manifest
declarations, so an archive is for exactly one deployment, and a link to a
central copy would hand every self-hosted installer an extension talking to
`mycelium.xeno.garden`. That is the failure the no-default-origin rule above
exists to prevent, arriving by a different door.

### Decision

**A deployment serves its own package, or none.** The frontend image takes
`MYCELIUM_EXTENSION_ORIGIN` as a build argument with no default. Given one,
a stage builds `extension/` against it and the image serves
`/extension/mycelium-extension-<version>.zip` beside a stable
`/extension/release.json` naming the origin, both versions and the archive's
SHA-256. Given nothing, the image serves neither and stays
deployment-neutral, which is what a plain `docker build` still produces.

**The page shows the download only when the descriptor names the origin the
page is being served from.** Anything else -- no descriptor, a development
server answering with the SPA shell, a package built for a sibling
deployment -- reads as "no package", and the page then gives the build
recipe with this deployment's origin already in the command. A button that
leads to an extension Chrome would refuse to hand a credential is worse than
no button.

The descriptor is a contract between two packages built from two lockfiles,
so `extension/tests/release.test.ts` asserts that what the pack step writes
is what the SPA's reader accepts, field for field and in both directions. A
renamed field would otherwise make the download quietly disappear, which
looks exactly like a deployment that ships no package.

### Consequences

Installing still means "Load unpacked" with Developer mode on: Chrome does
not install a zip by double-click, and a `.crx` outside the store is refused.
What the download removes is the toolchain, which was the real obstacle.

A frontend image built with the argument is bound to one deployment. That is
new -- the image was previously neutral about where it is served -- and it is
why the argument has no default and the workflow passes it only to that
image.

The deployment is the trust anchor for an unpacked install, since there is no
store signature to check. The page therefore states the SHA-256 it published,
and the descriptor's archive field is constrained to a bare file name so a
descriptor cannot aim the download at another host.

`make extension-pack` never produced an archive: `pnpm pack` is a pnpm
built-in that shadowed the script of that name, so the target quietly built
an npm tarball of the sources instead. The script is now `zip` and the target
`make extension-zip`.

### Alternatives rejected

**One archive published as a GitHub release asset.** The reason above: it can
only be correct for one deployment, and it is silently wrong for every other.

**Serving the archive from the API rather than as a static file.** The bytes
are produced by the image build and are the same for every reader; putting
them behind an authenticated route would add a second answer to the question
nginx already answers, and the package holds nothing a credential protects.

**Reading the origin at runtime so one package could serve every
deployment.** Not available: Chrome requires both the host permission and the
`externally_connectable` pattern as static manifest entries.

## Amendment (2026-09-09): the credential is the holder's, across their workspaces

### Context

Two things were wrong at once, and they had the same root: the credential
was modelled on the MCP assistant it borrows its table from, rather than on
the surface it actually serves.

**Nobody could connect but the owner, and in practice not even the owner.**
`create_assistant` requires `Role.owner`, which is the right threshold for a
credential whose default scope reaches most of the workspace. The settings
page meanwhile said, in this document's own words, that installing a browser
extension is not an administrative act, and the page is available to
everyone. A member pressing Connect got a refusal the screen could not
explain. So did the owner: the SPA sends `X-Workspace-Role` as a *downgrade*
and defaults to `member`, so an owner who had not flipped the "act as" lever
was refused on their own workspace.

**A panel over one workspace is not a panel.** A person moves between their
workspaces on a single login; the panel opens over whatever page they are
reading. Connecting once per workspace would have meant one long-lived
secret per workspace in the same Chrome profile — more credentials, more
things to revoke, and no more authority in any of them, since each is
already bounded by what that person may do.

### Decision

**The threshold follows the capability.** `SELF_SERVICE_SCOPES` names the
fixed, narrow set the panel asks for. A credential whose scope is a subset of
it may be created, rotated and revoked by any member for themselves; anything
wider is an assistant and stays owner-gated. The test is on what is being
asked for, never on the `provider` label the caller chooses, so it cannot be
talked around. Managing follows minting deliberately: a person who may create
a credential must be able to destroy it, or a lost browser stays connected.

**The credential binds to the account.** `agent_tokens.workspace_binding` is
`workspace` (the default, and what every existing credential is) or
`account`, decided by the service from the scope and refused for anything
wider than the self-service set. `_confine_agent_token` lets an
account-bound credential through the REST door; everything after it is
unchanged, which is the whole of the security argument:

- the workspace arrives per request, as it does for a session;
- `_tenant_scope` resolves the HOLDER's membership in that workspace and
  refuses one they do not belong to;
- the effective role is clamped to that membership, so the credential is a
  guest where its holder is a guest;
- the scope list still applies on top.

The binding is read at authentication time, out of the SECURITY DEFINER
`authenticate_agent_token` (migration 0012), not looked up afterwards: a
credential's tenancy is part of authenticating it, and a second query would
be a second place for the answer to come from.

**`GET /agent/workspaces` is the second META route.** A credential that may
act in several workspaces cannot name one in a header before it knows which
ones it may name. It is pre-tenant, answers only about the caller's own
workspaces, and for a confined credential answers with the single workspace
it was minted for. `GET /workspaces` stays HUMAN_ONLY.

**The extension asks, and is not told.** After the handover it calls that
route and files one row per workspace over the one secret. The panel's state
— scope selection, recents, caches, the pinned task — is per workspace and
would be wrong if it were shared, so the fan-out happens in storage rather
than as a special case in each of those places. A failed lookup leaves the
connection working in the workspace it was made in.

### Consequences

The blast radius of the secret is now the panel's scope across every
workspace its holder belongs to, rather than in one. Bounded by their role in
each, revoked in one act, and stated on the consent screen and in
`docs/extension.md` rather than left to be discovered.

The MCP surface is untouched: there the tenant comes from the principal, and
`agent_tokens.org_id` still names the workspace a credential was minted in.

`attachments:write` is catalogued `danger` and is in the self-service set,
because it is the whole of "file the page you are on". It is the only one, and
a test pins that as an exact set rather than a waiver.

A rotation inherits the tenancy of the credential it replaces: a new secret
for the same credential, never a change to what that credential reaches.

### Alternatives rejected

**Leaving the owner gate and fixing the page's wording instead.** It would
have made the product true by making it worse: the fastest surface reachable
only by whoever runs the deployment, for a credential that can do less than
the person holding it.

**Deciding the binding from the `provider` string.** The caller chooses it,
so a wide-scoped assistant labelled `mycelium-extension` would have walked
through both the threshold and the reach.

**Widening `GET /workspaces` to scoped credentials.** It answers a question
about the ACCOUNT — every workspace, its status, the switcher's data. The
narrower question deserved its own route, and the fence around the account
row stays where it was.

**Minting one credential per workspace behind the scenes at connect time.**
The same reach, several secrets, and a consent screen that would have had to
explain a fan-out nobody asked for. Revoking would then be N acts, and the
one people forget is the one that matters.
