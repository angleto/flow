// What a deployment publishes about the package it serves.
//
// The zip alone is not enough to offer as a download. The origin is
// COMPILED INTO the package -- `host_permissions` is a static manifest
// declaration, so a package cannot be re-pointed at another deployment
// after the fact -- and a page
// that offered any zip it happened to find would hand a visitor of one
// deployment an extension that talks to a different one. So the archive
// travels with the origin it was built against, and the page compares
// that value with its own before showing a button.
//
// The shape is consumed by the SPA (`web/src/shared/extension.ts`), which
// is a separate package: `tests/release.test.ts` asserts the two agree in
// both directions rather than trusting that they do.

/** The file name, next to the zip it describes. Stable, because the page
 *  has to be able to ask for it without knowing the version first. */
export const RELEASE_FILE = 'release.json'

/** @typedef {import('./env.mjs').BuildEnv} BuildEnv */

/** @param {BuildEnv} env
 *  @param {{ zip: string, sha256: string }} archive
 *  @returns {{ origin: string, version: string, versionName: string, zip: string, sha256: string }} */
export function releaseFor(env, archive) {
  return {
    // The single value the whole package was derived from, carried out
    // so the reader can be told which deployment this talks to.
    origin: env.baseUrl,
    version: env.version,
    versionName: env.versionName,
    // A bare file name, resolved against the directory this document is
    // served from. An absolute path here would bake in a location the
    // deployment is free to change.
    zip: archive.zip,
    // Shown next to the download. The deployment is the trust anchor for
    // an unpacked install -- there is no store signature to check -- so
    // the least it can do is state what it published.
    sha256: archive.sha256,
  }
}
