import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import {
  CONNECT_EXTENSION_ID_PARAM,
  CONNECT_MESSAGE_KIND,
  CONNECT_STATE_PARAM,
  type ConnectMessage,
  type ConnectReply,
  EXTENSION_PACKAGE_DIR,
  EXTENSION_PROVIDER,
  EXTENSION_SCOPES,
  type ExtensionRelease,
} from '../shared'
import { useMyWorkspace } from '../auth/useMyWorkspace'
import { type Assistant, type Scope, aiApi } from '../lib/aiAssistants'
import { buildCommandsFor, loadRelease } from '../lib/extensionPackage'
import { getTheme } from '../lib/theme'

// Settings -> Browser extension.
//
// A fourth scope alongside Account, Workspace and Platform, and it is a
// real one rather than a drawer: this page is about THIS BROWSER. Which
// browser holds a credential is not a property of you (it does not follow
// you to another machine), not a property of the workspace (the workspace
// does not care where you read it from), and not a property of the
// deployment. That is also why "Disconnect" in the extension and "Revoke"
// here are different acts, and the page says so instead of leaving the
// difference to be discovered.
//
// Available to everyone. Installing a browser extension is not an
// administrative act, and gating it behind elevation would mean the only
// people who could use the product's fastest surface are the ones who
// happen to run the deployment.
//
// ONE page, reached two ways: a person clicking through Settings, and the
// extension opening it with ``?state=&id=``. A second "consent page" would
// be a second place the disclosure lives, and the one that drifts is
// always the one nobody opens by hand.

// The store listing is a property of the PRODUCT, not of a deployment:
// there is one item, and every deployment's users install the same one.
// Null until it is published, and the page then tells the truth about
// that rather than linking somewhere that 404s.
const STORE_URL: string | null = null

/** Just enough of the extension messaging API to hand over one secret.
 *  Declared locally rather than by adding @types/chrome to the whole SPA:
 *  this is the only file that touches it, and the surface it needs is two
 *  functions wide. */
type ChromeRuntime = {
  runtime?: {
    sendMessage?: (
      extensionId: string,
      message: unknown,
      callback: (reply: ConnectReply | undefined) => void,
    ) => void
    lastError?: { message?: string }
  }
}

function chromeRuntime(): ChromeRuntime['runtime'] | undefined {
  return (globalThis as unknown as ChromeRuntime).runtime
}

export function SettingsExtensionRoute() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const { ws } = useMyWorkspace()

  const requestState = params.get(CONNECT_STATE_PARAM)
  const requestExtensionId = params.get(CONNECT_EXTENSION_ID_PARAM)
  const pending = !!requestState && !!requestExtensionId

  const [catalog, setCatalog] = useState<Scope[]>([])
  const [connections, setConnections] = useState<Assistant[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // undefined while the probe is in flight. The two settled answers put
  // opposite instructions on the screen, so rendering either one early
  // would show a reader steps for the wrong path and then swap them.
  const [release, setRelease] = useState<ExtensionRelease | null | undefined>(undefined)

  const reload = useCallback(async () => {
    try {
      const rows = await aiApi.list()
      setConnections(rows.filter((a) => a.provider === EXTENSION_PROVIDER))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setConnections([])
    }
  }, [])

  useEffect(() => {
    let live = true
    void (async () => {
      const found = await loadRelease()
      if (live) setRelease(found)
    })()
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    let live = true
    void (async () => {
      await reload()
      try {
        const rows = await aiApi.scopeCatalog()
        if (live) setCatalog(rows)
      } catch {
        // The grant is disclosed from the catalogue so its wording
        // cannot drift from what each key actually permits. Losing it is
        // not fatal: the list below falls back to the bare keys, which
        // is less friendly and still true.
        if (live) setCatalog([])
      }
    })()
    return () => {
      live = false
    }
  }, [reload])

  const buildCommands = useMemo(() => buildCommandsFor(window.location.origin), [])

  const granted = useMemo(() => {
    const byKey = new Map(catalog.map((s) => [s.key, s]))
    return EXTENSION_SCOPES.map((key) => ({ key, def: byKey.get(key) }))
  }, [catalog])

  function clearRequest() {
    const next = new URLSearchParams(params)
    next.delete(CONNECT_STATE_PARAM)
    next.delete(CONNECT_EXTENSION_ID_PARAM)
    setParams(next, { replace: true })
  }

  async function onConnect() {
    if (!requestState || !requestExtensionId || !ws) return
    setBusy(true)
    setErr(null)
    setNotice(null)
    try {
      const created = await aiApi.create({
        // Not named after a workspace: this credential is not one. The
        // label is what the person recognises in the list of credentials
        // when they come back to revoke it.
        label: t('ext.connect.label'),
        provider: EXTENSION_PROVIDER,
        scope: [...EXTENSION_SCOPES],
        // The panel sits over the account, not over one workspace: a
        // person moves between their workspaces on one login, and one
        // credential per workspace would put several long-lived secrets
        // in the same browser profile to buy nothing. The server accepts
        // this only for the scope list above, and every operation is
        // still authorized by this person's own role in whichever
        // workspace the request names.
        workspace_binding: 'account',
      })
      const message: ConnectMessage = {
        kind: CONNECT_MESSAGE_KIND,
        state: requestState,
        secret: created.raw_secret,
        workspace: { id: ws.id, name: ws.name },
        assistantId: created.assistant.id,
        // What the SERVER granted, not what this file asked for. If the
        // two ever differ, the extension must report the truth.
        scope: created.assistant.scope,
        theme: getTheme(),
      }
      const runtime = chromeRuntime()
      if (!runtime?.sendMessage) {
        // The credential now exists and nobody can hold it. Say so, and
        // leave the row visible below so it can be revoked -- silently
        // dropping it would leave a live credential nobody knows about.
        setErr(t('ext.connect.noRuntime'))
        await reload()
        return
      }
      const reply = await new Promise<ConnectReply | undefined>((resolve) => {
        runtime.sendMessage?.(requestExtensionId, message, resolve)
      })
      if (!reply?.ok) {
        // Static t() calls, one per reason, rather than a key built from
        // the reason: the i18n gate can only verify a key it can read in
        // the source, and Record<> makes a missing reason a compile
        // error, so both halves are checked instead of neither.
        const refusal: Record<NonNullable<ConnectReply['reason']>, () => string> = {
          'unknown-state': () => t('ext.connect.refused.unknown-state'),
          expired: () => t('ext.connect.refused.expired'),
          'already-connected': () => t('ext.connect.refused.already-connected'),
          'wrong-origin': () => t('ext.connect.refused.wrong-origin'),
        }
        setErr(
          reply?.reason
            ? refusal[reply.reason]()
            : (runtime.lastError?.message ?? t('ext.connect.noReply')),
        )
        await reload()
        return
      }
      setNotice(t('ext.connect.done', { workspace: ws.name }))
      clearRequest()
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onRevoke(a: Assistant) {
    setBusy(true)
    setErr(null)
    try {
      await aiApi.remove(a.id)
      setNotice(t('ext.connections.revoked'))
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <section className="card">
        <h2>{t('ext.title')}</h2>
        <p className="muted">{t('ext.intro')}</p>
      </section>

      <section className="card">
        <h2>{t('ext.install.title')}</h2>
        {STORE_URL ? (
          <p>
            <a href={STORE_URL} target="_blank" rel="noreferrer noopener">
              {t('ext.install.store')}
            </a>
          </p>
        ) : release === undefined ? null : release ? (
          <>
            <p className="muted">{t('ext.install.packaged', { origin: release.origin })}</p>
            <p>
              <a href={`${EXTENSION_PACKAGE_DIR}${release.zip}`} download>
                {t('ext.install.download', { version: release.versionName })}
              </a>
            </p>
            <p className="hint ext__checksum">
              {t('ext.install.checksum')} {release.sha256}
            </p>
            <ol>
              <li>{t('ext.install.unzip')}</li>
              <li>{t('ext.install.devMode')}</li>
              <li>{t('ext.install.loadUnzipped')}</li>
              <li>{t('ext.install.connect')}</li>
            </ol>
          </>
        ) : (
          <>
            <p className="muted">{t('ext.install.unpublished')}</p>
            <pre className="ext__commands">{buildCommands}</pre>
            <p className="hint">{t('ext.install.buildHint')}</p>
            <ol>
              <li>{t('ext.install.devMode')}</li>
              <li>{t('ext.install.loadBuilt')}</li>
              <li>{t('ext.install.connect')}</li>
            </ol>
          </>
        )}
        <p className="hint">{t('ext.install.chromeOnly')}</p>
      </section>

      <section className="card">
        <h2>{t('ext.connect.title')}</h2>
        {pending ? (
          <>
            <p>
              {t('ext.connect.asking', {
                id: requestExtensionId,
                workspace: ws?.name ?? '',
              })}
            </p>
            <p className="muted">{t('ext.connect.grantIntro')}</p>
            <ul className="ext__scopes">
              {granted.map(({ key, def }) => (
                <li key={key}>
                  <code>{key}</code>
                  {def ? <span className="muted"> — {def.description}</span> : null}
                </li>
              ))}
            </ul>
            <p className="hint">{t('ext.connect.reach')}</p>
            <p className="hint">{t('ext.connect.notGranted')}</p>
            <button type="button" disabled={busy || !ws} onClick={() => void onConnect()}>
              {t('ext.connect.approve')}
            </button>
            <button type="button" className="link" disabled={busy} onClick={clearRequest}>
              {t('common.cancel')}
            </button>
          </>
        ) : (
          <p className="muted">{t('ext.connect.startFromExtension')}</p>
        )}
        {notice && (
          <p className="ok" role="status">
            {notice}
          </p>
        )}
        {err && (
          <p className="err" role="alert">
            {err}
          </p>
        )}
      </section>

      <section className="card">
        <h2>{t('ext.connections.title')}</h2>
        <p className="muted">{t('ext.connections.help')}</p>
        {connections === null && <p className="hint">{t('common.loading')}</p>}
        {connections !== null && connections.length === 0 && (
          <p className="hint">{t('ext.connections.empty')}</p>
        )}
        {connections !== null && connections.length > 0 && (
          <ul className="ext__list">
            {connections.map((a) => (
              <li key={a.id}>
                <span>{a.label}</span>{' '}
                <code className="muted">{a.token_prefix ?? t('common.dashEmpty')}</code>{' '}
                {!a.is_active && <span className="muted">{t('ext.connections.paused')}</span>}
                <button type="button" disabled={busy} onClick={() => void onRevoke(a)}>
                  {t('ext.connections.revoke')}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>{t('ext.limits.title')}</h2>
        <ul>
          <li>{t('ext.limits.profile')}</li>
          <li>{t('ext.limits.disconnect')}</li>
          <li>{t('ext.limits.workspace')}</li>
        </ul>
      </section>
    </>
  )
}
