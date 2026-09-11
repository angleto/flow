// Where the login form sends a person once it is done.
//
// A guarded route that turns an anonymous visitor away has to remember
// what they were trying to reach, because the login form is the only
// thing that can put them back there. Landing everyone on the default
// page instead is a small annoyance on most routes and a broken feature
// on one: the browser extension asks for a credential by opening
// ``/settings/extension?state=<nonce>&id=<extension id>``, and the
// request IS those parameters. Drop them and the person logs in, lands
// on their notes, and the extension waits forever for an answer nobody
// can now give it -- which looks exactly like an extension that needs a
// login of its own, and it does not.
//
// The destination travels in navigation state rather than in the login
// URL. Two reasons, and the first is the one that decides it: a value in
// the URL is a value an attacker can write, so a query parameter would
// make this an open-redirect surface that has to be defended on every
// read. Navigation state is also not a place the connect nonce gets
// copied into. It survives a reload -- the browser restores
// ``history.state`` for the entry -- which matters here because logging
// in can take a second step at the authenticator.
//
// Both functions are pure and asserted in returnTo.test.ts; the
// interesting cases are the hostile ones.

/** What the guard hands to the login form: the whole address it refused,
 *  as one string.
 *
 *  Search AND hash, because a route's parameters can live in either and
 *  a guard standing in front of every route cannot know which. */
export function returnToFrom(location: {
  pathname: string
  search: string
  hash: string
}): string {
  return `${location.pathname}${location.search}${location.hash}`
}

/** The same value on the way back, refused unless it addresses a place
 *  inside this app.
 *
 *  Navigation state is not attacker-controlled the way a query parameter
 *  is, but it is still read back from the browser rather than held in
 *  memory, and a redirect target is worth validating wherever it came
 *  from: the cost is one function and the failure it prevents is sending
 *  a freshly authenticated person to somebody else's origin.
 *
 *  Everything that is not a plain in-app path falls back, including the
 *  forms that look like one:
 *
 *    ``//evil.example``   protocol-relative, so the browser reads it as
 *                         another ORIGIN rather than as a path;
 *    ``/\evil.example``   the same thing, which browsers accept with a
 *                         backslash and a naive ``startsWith('/')``
 *                         check lets through;
 *    ``https://…``        absolute, and an absolute URL is never
 *                         somewhere this app's router can send anyone. */
export function safeReturnTo(value: unknown, fallback = '/'): string {
  if (typeof value !== 'string' || value === '') return fallback
  if (!value.startsWith('/')) return fallback
  if (value.startsWith('//') || value.startsWith('/\\')) return fallback
  // A control character can smuggle one of the shapes above past the
  // tests above: browsers strip tab, newline and carriage return out of
  // a URL before resolving it, so a path whose second character is a tab
  // resolves as "//evil.example" once that tab is gone. Compared as
  // character codes rather than matched by a regex, because a regex over
  // this range is the one a linter cannot tell apart from the typo it
  // usually is.
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0
    if (code < 0x20 || code === 0x7f) return fallback
  }
  return value
}
