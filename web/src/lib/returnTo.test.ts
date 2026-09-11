// Carrying a refused destination across the login form, and refusing to
// carry it anywhere else.
//
// The round trip is asserted whole -- guard to form and back -- because
// the defect this prevents lives in the JOIN: each half looked right on
// its own while the address was being dropped between them, and the
// symptom (the extension's connect request silently evaporating) was
// three screens away from either.

import { describe, expect, it } from 'vitest'
import { returnToFrom, safeReturnTo } from './returnTo'

const CONNECT = {
  pathname: '/settings/extension',
  search: '?state=8d4e1c4a2f&id=abcdefghijklmnopabcdefghijklmnop',
  hash: '',
}

describe('returnToFrom', () => {
  it('keeps the query, which on the connect route IS the request', () => {
    expect(returnToFrom(CONNECT)).toBe(
      '/settings/extension?state=8d4e1c4a2f&id=abcdefghijklmnopabcdefghijklmnop',
    )
  })

  it('keeps the hash, which other routes put their parameters in', () => {
    expect(returnToFrom({ pathname: '/notes/abc', search: '', hash: '#L42' })).toBe(
      '/notes/abc#L42',
    )
  })

  it('is just the path where there is nothing else to keep', () => {
    expect(returnToFrom({ pathname: '/tasks', search: '', hash: '' })).toBe('/tasks')
  })
})

describe('safeReturnTo', () => {
  it('returns an in-app path unchanged, query and all', () => {
    const whole = returnToFrom(CONNECT)
    expect(safeReturnTo(whole)).toBe(whole)
  })

  it('falls back when nothing was remembered', () => {
    expect(safeReturnTo(undefined)).toBe('/')
    expect(safeReturnTo(null)).toBe('/')
    expect(safeReturnTo('')).toBe('/')
  })

  it('falls back on anything that is not a string', () => {
    expect(safeReturnTo({ pathname: '/tasks' })).toBe('/')
    expect(safeReturnTo(['/tasks'])).toBe('/')
    expect(safeReturnTo(42)).toBe('/')
  })

  it('refuses another origin however it is spelled', () => {
    // Each of these is read by a browser as a host, not as a path.
    expect(safeReturnTo('//evil.example/steal')).toBe('/')
    expect(safeReturnTo('/\\evil.example/steal')).toBe('/')
    expect(safeReturnTo('https://evil.example/steal')).toBe('/')
    expect(safeReturnTo('http://evil.example')).toBe('/')
    expect(safeReturnTo('javascript:alert(1)')).toBe('/')
  })

  it('refuses a path carrying the control characters a browser strips', () => {
    // "/\t/evil.example" resolves as "//evil.example" once the tab is
    // removed, so the two-slash test above has to run on a value that
    // cannot grow a second slash afterwards.
    expect(safeReturnTo('/\t/evil.example')).toBe('/')
    expect(safeReturnTo('/\n/evil.example')).toBe('/')
    expect(safeReturnTo('/\r/evil.example')).toBe('/')
  })

  it('honours a caller that wants somewhere other than the root', () => {
    expect(safeReturnTo(undefined, '/notes')).toBe('/notes')
  })
})
