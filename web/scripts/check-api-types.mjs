// The committed API types are the ones the API would generate today.
//
// ``src/shared/schema.d.ts`` is generated from the API's OpenAPI document
// and committed, because both packages that compile src/shared -- the SPA
// and the browser extension -- must build with no Python toolchain, and
// because a contract change is then visible in review as a diff rather
// than as a rebuild nobody sees.
//
// Committed means it can drift, and it did: several schema-changing
// commits shipped before this check existed (/readyz, /agent/workspaces,
// an agent token field, a counter field), leaving the client compiling
// against an API older than the one it calls. That failure is silent in the only
// direction that matters. Every type-check passes, because the types are
// internally consistent -- they are simply a different API's. What
// surfaces later is a caller using a field the types deny, or trusting one
// the server stopped sending, neither of which reads as "the types are
// stale" when it happens.
//
// So the pipeline generates them again, into a temp file, and compares.
// Into a temp file because a check that regenerates in place has already
// erased the evidence it was asked to weigh: the job would go green on a
// file the commit does not contain.
//
// This needs the Python toolchain in a job that is otherwise Node-only.
// The alternative was to commit the intermediate openapi.json so each job
// could verify half the chain with the toolchain it already has -- the
// backend that the document matches the code, the SPA that the types match
// the document. Rejected: it adds a second generated artifact, 2 MB of
// churn on every API change and a merge conflict magnet, to buy review
// value schema.d.ts already provides.

import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { API_TYPES, generateApiTypes } from './gen-api-types.mjs'

const scratch = mkdtempSync(join(tmpdir(), 'mycelium-api-types-'))
const candidate = join(scratch, 'schema.d.ts')
// Set before the temp directory is removed and read after: process.exit()
// skips finally blocks, so exiting from inside the try would leak the
// scratch directory on every run, green or red.
let stale = false

try {
  generateApiTypes(candidate)
  const committed = readFileSync(API_TYPES, 'utf8')
  const fresh = readFileSync(candidate, 'utf8')
  stale = committed !== fresh

  if (!stale) {
    const paths = (committed.match(/^ {4}"\/[^"]*": \{$/gm) ?? []).length
    console.log(`api types: schema.d.ts matches the API's OpenAPI document (${paths} paths).`)
  } else {
    console.error(
      `\n${relative(process.cwd(), API_TYPES)} is not what the API generates today:\n`,
    )
    try {
      // git is already a hard dependency of having this checkout;
      // --no-index diffs two arbitrary files and exits non-zero when they
      // differ, which is the reason for the catch rather than an error to
      // report.
      execFileSync('git', ['diff', '--no-index', '--stat', API_TYPES, candidate], {
        stdio: ['ignore', 'inherit', 'inherit'],
      })
    } catch {
      /* the stat is a courtesy; the remedy below is the point */
    }
    console.error(
      '\nThe API changed and the generated types did not follow. Run\n\n' +
        '    pnpm gen:api\n\n' +
        'in web/ and commit the result. Nothing else edits this file: it is\n' +
        'generated, and a hand-written correction to it is a lie the compiler\n' +
        'will believe.\n',
    )
  }
} finally {
  rmSync(scratch, { recursive: true, force: true })
}

if (stale) process.exit(1)
