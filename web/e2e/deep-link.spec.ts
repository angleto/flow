import { test, expect } from '@playwright/test'
import { E2E_EMAIL as EMAIL, E2E_PASSWORD as PASSWORD } from './global-setup'

// A deep link survives the login it is interrupted by.
//
// The guard used to send an anonymous visitor to the login form and drop
// the address it had refused, and the form landed everyone on the default
// page. On most routes a small annoyance; on the extension's connect page
// it destroyed the request, because the request was the query.
//
// The connect flow no longer depends on that: the device authorization
// grant keeps its request on the server, keyed by a code the extension
// holds, so a lost URL costs a person some typing and nothing more. The
// deep link is still worth asserting -- every guarded route has one, a
// task link in an email is one -- and this page stays the case to assert
// it on, because it is the one where the loss was visible.
//
// Asserted end to end rather than by unit test because the defect lived
// in the JOIN between the guard and the form: each half was defensible on
// its own while the address was dropped between them. The hostile inputs
// to the same machinery are in src/lib/returnTo.test.ts, which is where
// they belong -- a browser will not type "//evil.example" for us.

// Shaped like a real code and inert: no request stands behind it, so the
// page will say it found none. What is under test is whether the QUERY
// ARRIVES, not what it means.
const CODE = 'K7QP-3MTX'
const CONNECT = `/settings/extension?code=${CODE}`

test('the connect request survives the login it is interrupted by', async ({ page }) => {
  // A context with no session: the guard must turn this away.
  await page.goto(CONNECT)
  await page.waitForURL('**/login', { timeout: 15_000 })

  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.locator('button[type=submit]').click()

  // Back on the address that was refused, with the parameter intact.
  await page.waitForURL(`**${CONNECT}`, { timeout: 15_000 })
  const url = new URL(page.url())
  expect(url.pathname).toBe('/settings/extension')
  expect(url.searchParams.get('code')).toBe(CODE)

  // And the page ACTED on it: it looked the code up and reported that
  // nothing is waiting behind it. That sentence renders only after a
  // lookup, so its presence asserts both halves at once -- the parameter
  // arrived, and the page read it as a code to resolve. A page that had
  // simply forgotten the query would show the idle instructions instead.
  await expect(
    page.getByText(/no request is waiting|nessuna richiesta in attesa/i),
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
