// The AI-assistants API, in one place.
//
// Two surfaces now read it: the workspace settings card that manages
// assistants, and the browser-extension page, which mints one and lists
// the ones it minted. The second was written after the first, and copying
// the fetch wrapper into it would have made the error handling -- the part
// that is easy to get subtly wrong and impossible to notice when it is --
// two implementations of one rule.
//
// Errors: the envelope reader lives in src/shared/errors.ts, because the
// shape is the SERVER's contract and the extension reads the same one.
// What is here is the transport and the endpoint names.

import { authFetch } from '../api/client'
import { errMessage } from '../api/client'

export type ScopeCategory = 'read' | 'write' | 'danger'

export type Scope = {
  key: string
  category: ScopeCategory
  label: string
  description: string
}

export type ConnectorInfo = { mcp_url: string; instructions_md: string }

export type Assistant = {
  id: string
  label: string
  provider: string | null
  model_id: string | null
  notes: string | null
  scope: string[]
  is_active: boolean
  version: number
  created_at: string
  updated_at: string
  token_prefix: string | null
}

/** Returned exactly once, at create or rotate. ``raw_secret`` is the only
 *  moment the value exists outside the server; nothing re-reads it. */
export type AssistantCreated = { assistant: Assistant; raw_secret: string }

/** What a credential minted for the browser extension carries, so this
 *  page can list "connected browsers" without a second table.
 *
 *  Mirrors ``mycelium_core.mcp_scopes.EXTENSION_PROVIDER``, which is the
 *  authority: the server writes this value, nothing here does. It is a
 *  DISPLAY FILTER and not a boundary -- were the two ever to drift, the
 *  list on the page would come up empty, which is visible and harmless,
 *  rather than showing or granting anything it should not. */
export const EXTENSION_PROVIDER = 'mycelium-extension'

export const CATEGORY_ORDER: readonly ScopeCategory[] = ['read', 'write', 'danger']

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    // The server's own sentence when it sent one -- it is already
    // localized and it knows what was refused. errMessage falls back to
    // the catalogue only when there is nothing usable at all.
    let body: unknown
    try {
      body = await res.json()
    } catch {
      // No body, or not JSON: fall through to the status line, which is
      // all there is to say.
      body = null
    }
    throw new Error(body ? errMessage(body) : `HTTP ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const aiApi = {
  list: () => call<Assistant[]>('/ai-assistants'),
  connectorInfo: () => call<ConnectorInfo>('/ai-assistants/connector-info'),
  scopeCatalog: () => call<Scope[]>('/ai-assistants/scope-catalog'),
  create: (body: object) =>
    call<AssistantCreated>('/ai-assistants', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: object) =>
    call<Assistant>(`/ai-assistants/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  remove: (id: string) => call<void>(`/ai-assistants/${id}`, { method: 'DELETE' }),
  rotate: (id: string) => call<AssistantCreated>(`/ai-assistants/${id}/rotate`, { method: 'POST' }),
}

/** A device asking to be let in, as the person approving it sees it.
 *
 *  ``user_code`` is echoed back so the screen can show what to COMPARE:
 *  the defence against approving somebody else's request is that the code
 *  on the screen and the code on the device are the same one, and a page
 *  that could not display it would have nothing to compare.
 *
 *  ``scope`` comes from the server's own list for that client, never from
 *  a constant in this bundle, so the disclosure and the grant cannot
 *  disagree. */
export type DevicePending = {
  user_code: string
  client: string
  opened_at: string
  expires_at: string
  origin_ip: string | null
  scope: string[]
}

export const deviceApi = {
  pending: (userCode: string) =>
    call<DevicePending>(`/auth/device/pending?user_code=${encodeURIComponent(userCode)}`),
  approve: (userCode: string) =>
    call<void>('/auth/device/approve', {
      method: 'POST',
      body: JSON.stringify({ user_code: userCode }),
    }),
  deny: (userCode: string) =>
    call<void>('/auth/device/deny', {
      method: 'POST',
      body: JSON.stringify({ user_code: userCode }),
    }),
}
