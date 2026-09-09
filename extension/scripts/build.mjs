// One command produces a loadable unpacked directory; the same command
// with --zip produces the upload artifact.
//
// Order matters and is enforced here rather than by a comment: vite
// empties the output directory, so everything copied in afterwards would
// be deleted by a build that ran second.

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { readBuildEnv } from './env.mjs'
import { manifestFor } from './manifest.mjs'
import { RELEASE_FILE, releaseFor } from './release.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = join(ROOT, 'dist', 'unpacked')
const ZIP_DIR = join(ROOT, 'dist')

const env = readBuildEnv()
const zip = process.argv.includes('--zip')

console.log(`extension: building against ${env.baseUrl} as ${env.versionName}`)

// 1. The bundle. Empties dist/unpacked, so it goes first.
execFileSync('node', [join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'], {
  cwd: ROOT,
  stdio: 'inherit',
  env: process.env,
})

// 2. The manifest, generated from the same environment the bundle was
//    compiled against.
writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifestFor(env), null, 2)}\n`)

// 3. The catalogue Chrome reads itself, for the strings it renders
//    outside our pages: the name, the description, the context menus and
//    the shortcut descriptions at chrome://extensions/shortcuts.
cpSync(join(ROOT, '_locales'), join(OUT, '_locales'), { recursive: true })

// 4. Icons. Declared in the manifest, so a missing one is a package
//    Chrome refuses; a unit test reads their PNG headers to assert the
//    declared size is the real size.
const icons = join(ROOT, 'icons')
if (!existsSync(icons)) {
  throw new Error(`no icons/ directory at ${icons}; run "pnpm gen:icons" first`)
}
cpSync(icons, join(OUT, 'icons'), { recursive: true })

console.log(`extension: ${readdirSync(OUT).length} entries in dist/unpacked`)

if (zip) {
  if (!env.baseUrl.startsWith('https://')) {
    // A package built against localhost is for this machine. Refusing to
    // archive one is cheap insurance against uploading it: the store
    // would accept it, and every installer would get an extension that
    // talks to their own machine and silently finds nothing.
    throw new Error(`refusing to package a non-https build: ${env.baseUrl}`)
  }
  mkdirSync(ZIP_DIR, { recursive: true })
  const name = `mycelium-extension-${env.version}.zip`
  // -X drops extended attributes so the archive is byte-identical for
  // the same input, which is what makes "rebuild it and compare" a
  // usable answer during a rollback.
  execFileSync('zip', ['-r', '-q', '-X', join(ZIP_DIR, name), '.'], { cwd: OUT, stdio: 'inherit' })
  // The descriptor a deployment serves beside the archive, so its
  // settings page can offer the download and say which deployment the
  // package inside talks to. Written from the same build environment as
  // the manifest, for the same reason: two values that must agree are
  // derived from one.
  const sha256 = createHash('sha256').update(readFileSync(join(ZIP_DIR, name))).digest('hex')
  const release = releaseFor(env, { zip: name, sha256 })
  writeFileSync(join(ZIP_DIR, RELEASE_FILE), `${JSON.stringify(release, null, 2)}\n`)
  console.log(`extension: dist/${name} (sha256 ${sha256})`)
  console.log(`extension: dist/${RELEASE_FILE}`)
}
