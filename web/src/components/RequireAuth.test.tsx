// What the guard hands to the login form when it turns someone away.
//
// One assertion, and it is the half of the connect handshake that has no
// other home: the browser extension's request lives entirely in the QUERY
// of ``/settings/extension?state=&id=``, so a guard that redirects to the
// login form without carrying that query has not postponed the request,
// it has destroyed it.
//
// Scope, stated so the next reader does not over-trust this file: the
// guard is the REAL component here, the login form is not. What the form
// does with the destination is asserted end to end in
// e2e/connect-deep-link.spec.ts, and the hostile inputs to the shared
// helper in lib/returnTo.test.ts.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { RequireAuth } from './RequireAuth'
import { safeReturnTo } from '../lib/returnTo'

const CONNECT = '/settings/extension?state=0123456789abcdef&id=abcdefghijklmnop'

// Stands where the login form stands, and reports only what it was
// given. Deliberately not the real form: that one authenticates, and
// this file is about what reaches it.
function LoginProbe() {
  const state = useLocation().state as { returnTo?: unknown } | null
  return <p id="probe">{safeReturnTo(state?.returnTo, 'NOTHING-CARRIED')}</p>
}

function Protected() {
  return <p id="protected">the guarded page</p>
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  // No session in storage: auth/session reads localStorage at import and
  // getSession() answers null, which is the state the guard reacts to.
  localStorage.clear()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

function renderAt(path: string) {
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<LoginProbe />} />
          <Route element={<RequireAuth />}>
            <Route path="/settings/extension" element={<Protected />} />
            <Route path="/notes" element={<Protected />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
  })
}

describe('RequireAuth', () => {
  it('carries the refused address, query and all, to the login form', () => {
    renderAt(CONNECT)
    expect(host.querySelector('#protected')).toBeNull()
    expect(host.querySelector('#probe')?.textContent).toBe(CONNECT)
  })

  it('carries a plain route too, so this is not a special case for one page', () => {
    renderAt('/notes')
    expect(host.querySelector('#probe')?.textContent).toBe('/notes')
  })
})
