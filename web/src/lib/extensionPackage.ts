// What this deployment can hand a person who wants the browser extension.
//
// Two answers, and the page shows one or the other: an archive it serves
// itself, or the commands to build one. Which one is not a preference —
// the origin is compiled into the package (`host_permissions` and
// `externally_connectable` are static manifest declarations), so an
// archive belongs to exactly one deployment and a deployment that does
// not serve its own has nothing to offer but the build.

import {
  EXTENSION_RELEASE_URL,
  type ExtensionRelease,
  extensionReleaseServes,
  parseExtensionRelease,
} from '../shared'

// Where the package is built from when a deployment serves none. The
// product's own repository: not a property of this deployment.
const SOURCE_URL = 'https://github.com/angleto/mycelium'

/** The build, with this deployment's origin already in it.
 *
 *  Not in the message catalogue, and deliberately: it is a command line,
 *  not a sentence, and a translated copy of it is a second place for the
 *  variable name to go stale. The prose around it is translated; this is
 *  the same for every reader.
 *
 *  ``MYCELIUM_EXTENSION_ORIGIN`` is the whole reason the page cannot just
 *  say "run pnpm build": the build has no default origin and stops
 *  without one, so an instruction that omits it does not work. The page
 *  said exactly that for one release. */
export function buildCommandsFor(origin: string): string {
  return [
    `git clone ${SOURCE_URL}`,
    'cd mycelium/extension',
    'pnpm install',
    `MYCELIUM_EXTENSION_ORIGIN=${origin} pnpm build`,
  ].join('\n')
}

/** The package this deployment serves, or null if it serves none.
 *
 *  Static bytes beside the bundle rather than an API call: the archive is
 *  produced by the image build and is the same for every reader, so
 *  asking the domain about a file nginx is already holding would put a
 *  second answer in play. Absence is the normal case and not an error —
 *  an image built without an extension origin has no package, and a
 *  development server answers this path with the SPA shell — so
 *  everything that is not a descriptor for THIS origin reads as null and
 *  the page falls back to the build. */
export async function loadRelease(): Promise<ExtensionRelease | null> {
  try {
    const res = await fetch(EXTENSION_RELEASE_URL, { headers: { Accept: 'application/json' } })
    if (!res.ok) return null
    const release = parseExtensionRelease(await res.json())
    if (!release) return null
    // A package compiled against another deployment cannot connect to
    // this one: Chrome would refuse the handover, because the origin is
    // in the manifest the archive was built with. A button that ends
    // there is worse than no button.
    return extensionReleaseServes(release, window.location.origin) ? release : null
  } catch {
    return null
  }
}
