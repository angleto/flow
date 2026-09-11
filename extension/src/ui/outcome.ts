// One mechanism for loading, error and empty. Every call site goes
// through it, and a lint rule stops any of them reaching the worker
// directly.
//
// Handled per call site, these three states drift immediately: one place
// shows nothing when a list is empty, another spins forever because empty
// was never distinguished from loading, a third reports an error. The
// person meets three behaviours for one situation.
//
// Two states here are not in the usual set and both earn their place.
//
// `notice` is recoverable and NON-BLOCKING: it carries data, so a
// conflict shows the refreshed row underneath the sentence rather than
// replacing the panel with an apology.
//
// `unknown` is a write whose deadline expired. The outcome is genuinely
// unknown -- the server may have committed it -- so the honest text says
// so instead of inviting a second press. Whether a retry is offered
// depends on whether repeating it could duplicate anything, which the
// failure itself carries.

import type { Failure } from '../shared/protocol'
import { clear, el, on } from './dom'
import { m } from './i18n'

export type EmptyKind =
  | 'no-query'
  | 'recents'
  | 'no-results'
  | 'no-results-in-scope'
  | 'not-connected'
  | 'offline'
  | 'disabled'

export type Outcome<T> =
  | { phase: 'idle' }
  | { phase: 'loading'; since: number }
  | { phase: 'ready'; data: T; empty?: EmptyKind }
  | { phase: 'notice'; message: string; data?: T }
  | { phase: 'error'; failure: Failure }
  | { phase: 'unknown'; failure: Failure }

/** Loading has three forms and the boundary is time, not opinion: below
 *  300ms nothing (a flash reads as a glitch), then a quiet line, and past
 *  two seconds a WORDED one that names the reason. That last one is not
 *  decoration: the server's semantic leg is time-boxed at two seconds and
 *  the first search after a deployment is genuinely slow, so "the index
 *  is waking up" turns a bug report into a wait. */
export function loadingText(since: number, now: number): string | null {
  const elapsed = now - since
  if (elapsed < 300) return null
  if (elapsed < 2000) return m('searching')
  return m('warmingUp')
}

const EMPTY_TEXT: Record<EmptyKind, () => string> = {
  'no-query': () => m('emptyNoQuery'),
  recents: () => m('emptyRecents'),
  'no-results': () => m('emptyNoResults'),
  'no-results-in-scope': () => m('emptyNoResultsInScope'),
  'not-connected': () => m('notConnectedBody'),
  offline: () => m('offline'),
  disabled: () => m('switchOffBody'),
}

const FAILURE_TEXT: Record<Failure['code'], () => string> = {
  network: () => m('errNetwork'),
  timeout: () => m('outcomeUnknown'),
  unauthenticated: () => m('errUnauthenticated'),
  server: () => m('errServer'),
  disabled: () => m('errDisabled'),
  disconnected: () => m('errDisconnected'),
  // The server knows what it refused and says so in the reader's own
  // language. Replacing that with a catalogue sentence would throw away
  // the only part of the message that is specific.
  forbidden: () => '',
  not_found: () => '',
  conflict: () => '',
  invalid: () => '',
  // The connect ceremony's three answers never reach this table: they are
  // rendered by the panel's waiting view, which is the only surface that
  // knows what to offer next for each. They are here because the record
  // is exhaustive over the codes, and that exhaustiveness is what makes a
  // new code a compile error rather than a blank line on screen.
  pending: () => '',
  denied: () => '',
  expired: () => '',
}

export function failureText(failure: Failure): string {
  return FAILURE_TEXT[failure.code]() || failure.message
}

export interface OutcomeActions {
  retry?: () => void
  check?: () => void
}

/** Render one outcome into a host element. `renderReady` is called only
 *  in `ready` and only with data, so a call site cannot accidentally read
 *  a half-loaded value. */
export function renderOutcome<T>(
  host: HTMLElement,
  outcome: Outcome<T>,
  renderReady: (data: T) => void,
  actions: OutcomeActions = {},
): void {
  clear(host)
  switch (outcome.phase) {
    case 'idle':
      return
    case 'loading': {
      const text = loadingText(outcome.since, Date.now())
      if (text) host.appendChild(el('p', { class: 'hypha__hint', role: 'status', text }))
      return
    }
    case 'ready': {
      renderReady(outcome.data)
      if (outcome.empty) {
        host.appendChild(
          el('p', { class: 'hypha__hint', text: EMPTY_TEXT[outcome.empty]() }),
        )
      }
      return
    }
    case 'notice': {
      // Non-blocking: the data stays on screen under the sentence.
      host.appendChild(el('p', { class: 'hypha__notice', role: 'status', text: outcome.message }))
      if (outcome.data !== undefined) renderReady(outcome.data)
      return
    }
    case 'error':
    case 'unknown': {
      const box = el('div', { class: 'hypha__failure', role: 'alert' })
      box.appendChild(el('p', { text: failureText(outcome.failure) }))
      if (outcome.failure.correlationId) {
        const ref = el('p', { class: 'hypha__ref' }, [
          `${m('reference')} `,
          // Verbatim and selectable: it is the only thing a person can
          // usefully report, and one they cannot copy they will describe
          // instead.
          el('code', { text: outcome.failure.correlationId.slice(0, 8) }),
        ])
        const copy = el('button', { type: 'button', class: 'hypha__linkbtn' }, [m('copyReference')])
        on(copy, 'click', () => {
          void navigator.clipboard.writeText(outcome.failure.correlationId ?? '')
        })
        ref.appendChild(copy)
        box.appendChild(ref)
      }
      if (outcome.phase === 'unknown' && actions.check) {
        const check = el('button', { type: 'button' }, [m('check')])
        on(check, 'click', actions.check)
        box.appendChild(check)
      }
      // A retry is offered only where repeating cannot duplicate: a
      // version-guarded write conflicts on the second attempt, a create
      // does not, and the failure itself carries which it is.
      if (actions.retry && outcome.failure.retryable) {
        const retry = el('button', { type: 'button' }, [m('retry')])
        on(retry, 'click', actions.retry)
        box.appendChild(retry)
      }
      host.appendChild(box)
      return
    }
  }
}
