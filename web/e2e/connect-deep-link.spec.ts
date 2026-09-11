import { test, expect } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// A deep link survives the login it is interrupted by.
//
// This is the whole of the browser extension's connect handshake seen
// from the app's side. The extension opens
// ``/settings/extension?state=<nonce>&id=<extension id>`` and waits; the
// request IS those two parameters. An anonymous visitor is sent to the
// login form first, and for a long time the form then landed everyone on
// the default page, which did not postpone the request -- it destroyed
// it. The person logged in, saw their notes, and the extension waited for
// an approval that could no longer be given. It reads exactly like an
// extension that needs a login of its own, which is the one thing this
// design exists to avoid.
//
// Asserted end to end rather than by unit test because the defect lived
// in the JOIN between the guard and the form: each half was defensible on
// its own while the address was dropped between them. The hostile inputs
// to the same machinery are in src/lib/returnTo.test.ts, which is where
// they belong -- a browser will not type "//evil.example" for us.

// Shaped like a real request, and inert: the nonce is meaningful only to
// an extension that minted it, and no extension is installed here. What
// is under test is whether the QUERY arrives, not what it means.
const STATE = '0123456789abcdef0123456789abcdef'
const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop'
const CONNECT = `/settings/extension?state=${STATE}&id=${EXTENSION_ID}`

test('the connect request survives the login it is interrupted by', async ({ page }) => {
  // A context with no session: the guard must turn this away.
  await page.goto(CONNECT)
  await page.waitForURL('**/login', { timeout: 15_000 })

  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()

  // Back on the address that was refused, with both parameters intact.
  await page.waitForURL(`**${CONNECT}`, { timeout: 15_000 })
  const url = new URL(page.url())
  expect(url.pathname).toBe('/settings/extension')
  expect(url.searchParams.get('state')).toBe(STATE)
  expect(url.searchParams.get('id')).toBe(EXTENSION_ID)

  // And the page is showing the PENDING request rather than the idle
  // page. That control renders only on the pending branch, so its
  // presence asserts both halves at once: the parameters arrived, and
  // the page read them as a request to approve. Selected by role and
  // name, and the name in either locale -- the suite does not pin the
  // browser's language, and pinning it here would assert less.
  await expect(
    page.getByRole('button', { name: /^(connect|collega)$/i }),
  ).toBeVisible({ timeout: 15_000 })
})

test('a login nobody deep-linked into still lands on the default page', async ({ page }) => {
  // The other half of the contract, and the one a regression would take
  // out silently: carrying a destination must not become a requirement
  // to have one.
  await page.goto('/login')
  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()
  await page.waitForURL('**/notes', { timeout: 15_000 })
})
