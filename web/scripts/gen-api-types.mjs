// The SPA's API types, generated from the API's own OpenAPI document.
//
// Two toolchains, one artifact. The document comes from
// ``scripts/dump_openapi.py`` (Python: it calls ``create_app().openapi()``
// offline, so it needs no server, no database and no lifespan -- see that
// file for why fetching it from a running uvicorn was worse), and the
// TypeScript comes from ``openapi-typescript`` (Node). Neither half is
// optional and neither can run the other's, which is the whole awkwardness
// of this file.
//
// Why it is a script and not a line in package.json. It was a shell
// string, and a shell string cannot be called twice with two
// destinations: the pipeline has to produce the types SOMEWHERE ELSE to
// compare them against the committed ones, and a check that writes into
// the working tree first is a check that has already changed what it was
// asked to judge. Splitting it here gives one definition of HOW the types
// are generated -- the dump, the codegen, and the flags that shape it --
// and two entry points over it: write (``pnpm gen:api``) and verify
// (``pnpm check:api-types``). Duplicating the flags instead would let the
// check pass on output nobody generates.
//
// The intermediate document lands in a temp directory rather than in
// web/: the old command finished with ``rm -f openapi.json``, which does
// not run when the codegen fails, so the failure that most wants a clean
// tree was the one that left a 2 MB stray file in it.

import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const REPO = resolve(WEB, '..')

/** The committed artifact. Compiled into the SPA and into the browser
 *  extension, both of which must build without a Python toolchain, which
 *  is why it is committed rather than generated at build time. */
export const API_TYPES = join(WEB, 'src', 'shared', 'schema.d.ts')

// ``--default-non-nullable false`` keeps a field with a server-side
// default OPTIONAL in the generated types. The server fills it in, so a
// caller may omit it on the way in; marking it required would force every
// call site to restate defaults the API already owns (task ed720f5b).
const CODEGEN_FLAGS = ['--default-non-nullable', 'false']

/** Generate the types into ``outPath``. Throws with the child's own
 *  output on the terminal if either half fails. */
export function generateApiTypes(outPath) {
  const scratch = mkdtempSync(join(tmpdir(), 'mycelium-openapi-'))
  const document = join(scratch, 'openapi.json')
  try {
    // Resolved from node_modules/.bin rather than through a package
    // manager: npm and pnpm both put it there, and this script should not
    // care which one installed the tree.
    const codegen = join(WEB, 'node_modules', '.bin', 'openapi-typescript')
    execFileSync('uv', ['run', 'python', 'scripts/dump_openapi.py', document], {
      cwd: REPO,
      stdio: ['ignore', 'ignore', 'inherit'],
    })
    execFileSync(codegen, [document, '-o', outPath, ...CODEGEN_FLAGS], {
      cwd: WEB,
      stdio: ['ignore', 'inherit', 'inherit'],
    })
  } finally {
    rmSync(scratch, { recursive: true, force: true })
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  generateApiTypes(API_TYPES)
}
