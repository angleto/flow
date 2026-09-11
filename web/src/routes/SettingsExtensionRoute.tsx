import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import {
  CONNECT_CODE_PARAM,
  EXTENSION_PACKAGE_DIR,
  type ExtensionRelease,
} from '../shared'
import {
  type Assistant,
  type DevicePending,
  EXTENSION_PROVIDER,
  type Scope,
  aiApi,
  deviceApi,
} from '../lib/aiAssistants'
import { buildCommandsFor, loadRelease } from '../lib/extensionPackage'

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
// extension opening it with ``?code=``. A second "consent page" would be a
// second place the disclosure lives, and the one that drifts is always the
// one nobody opens by hand.
//
// WHAT THIS PAGE NO LONGER DOES, because it is the interesting half. It
// used to mint the credential itself and push it into the extension over
// Chrome's messaging. Three things came with that and all three are gone:
// a credential that existed before anyone knew the extension would take it
// (so a failed handover left one live and unheld), a request that lived in
// the query string (so any redirect could destroy it, and the login
// redirect did), and a standing right for this origin to send messages to
// an extension. The page now only ANSWERS: the server holds the request,
// and the extension collects what it was granted by asking.

// The store listing is a property of the PRODUCT, not of a deployment:
// there is one item, and every deployment's users install the same one.
// Null until it is published, and the page then tells the truth about
// that rather than linking somewhere that 404s.
const STORE_URL: string | null = null

export function SettingsExtensionRoute() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()

  const requestCode = params.get(CONNECT_CODE_PARAM)

  const [catalog, setCatalog] = useState<Scope[]>([])
  const [connections, setConnections] = useState<Assistant[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // undefined while the probe is in flight. The two settled answers put
  // opposite instructions on the screen, so rendering either one early
  // would show a reader steps for the wrong path and then swap them.
  const [release, setRelease] = useState<ExtensionRelease | null | undefined>(undefined)
  // The answer, TAGGED with the code it answers. Tagged rather than bare
  // so the three display states can be derived at render instead of
  // written by an effect: a page with no code has nothing pending without
  // anyone having to store that, and a code whose answer has not arrived
  // is distinguishable from one whose answer was "nothing". Storing it
  // instead meant an effect that set state synchronously to reset between
  // codes, which is the cascading-render shape the linter refuses and the
  // stale-answer bug it stands in front of.
  const [answer, setAnswer] = useState<{ code: string; value: DevicePending | null } | null>(
    null,
  )
  const [typedCode, setTypedCode] = useState('')

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
    if (!requestCode) return
    let live = true
    void (async () => {
      try {
        const found = await deviceApi.pending(requestCode)
        if (live) setAnswer({ code: requestCode, value: found })
      } catch {
        // Every shape of "not a live request" is one answer from the
        // server, on purpose: a person deciding has no use for the
        // difference between never-existed, already-answered and
        // expired, and the distinctions are only useful to somebody
        // sweeping the space. So the page says what the server says.
        if (live) setAnswer({ code: requestCode, value: null })
      }
    })()
    return () => {
      live = false
    }
  }, [requestCode])

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

  // null: nothing to approve (no code, or the code matched no live
  // request). undefined: a code whose answer has not come back yet.
  const pending: DevicePending | null | undefined = !requestCode
    ? null
    : answer?.code === requestCode
      ? answer.value
      : undefined

  // What the SERVER says it will grant for this request, decorated with
  // the catalogue's wording. Not a list in this bundle: the page that
  // discloses and the code that mints must not be able to disagree, and
  // the only way to guarantee that is to render the minting side's answer.
  const granted = useMemo(() => {
    const byKey = new Map(catalog.map((s) => [s.key, s]))
    return (pending?.scope ?? []).map((key) => ({ key, def: byKey.get(key) }))
  }, [catalog, pending])

  function clearRequest() {
    const next = new URLSearchParams(params)
    next.delete(CONNECT_CODE_PARAM)
    setParams(next, { replace: true })
  }

  async function onApprove() {
    if (!pending) return
    setBusy(true)
    setErr(null)
    setNotice(null)
    try {
      await deviceApi.approve(pending.user_code)
      // Nothing was minted by this click. The extension is still asking,
      // and what it collects will be created by that call -- which is why
      // this says "approved" and not "connected": the panel is the thing
      // that can report the second half, and it will, within seconds.
      setNotice(t('ext.connect.approved'))
      clearRequest()
      await reload()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function onDeny() {
    if (!pending) return
    setBusy(true)
    setErr(null)
    try {
      await deviceApi.deny(pending.user_code)
      // Told, rather than left to time out: somebody decided something,
      // and the extension should stop asking instead of showing a
      // spinner for ten minutes.
      setNotice(t('ext.connect.denied'))
      clearRequest()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function onLookUp(e: FormEvent) {
    e.preventDefault()
    const next = new URLSearchParams(params)
    next.set(CONNECT_CODE_PARAM, typedCode.trim())
    setParams(next, { replace: true })
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
        {pending === undefined ? (
          <p className="hint">{t('common.loading')}</p>
        ) : pending ? (
          <>
            <p>{t('ext.connect.asking')}</p>
            {/* The comparison, and it is the whole defence against
                approving somebody else's request: a page that opened a
                request of its own and sent you here shows a code that is
                not the one on your screen. Rendered large and on its own,
                because a control nobody reads defends nothing. */}
            <p className="ext__code" aria-label={t('ext.connect.codeLabel')}>
              {pending.user_code}
            </p>
            <p className="muted">{t('ext.connect.compare')}</p>
            <dl className="ext__facts">
              <dt>{t('ext.connect.openedAt')}</dt>
              <dd>{new Date(pending.opened_at).toLocaleString()}</dd>
              <dt>{t('ext.connect.from')}</dt>
              <dd>{pending.origin_ip ?? t('common.dashEmpty')}</dd>
              <dt>{t('ext.connect.expiresAt')}</dt>
              <dd>{new Date(pending.expires_at).toLocaleTimeString()}</dd>
            </dl>
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
            <p className="hint">{t('ext.connect.lifetime')}</p>
            <button type="button" disabled={busy} onClick={() => void onApprove()}>
              {t('ext.connect.approve')}
            </button>
            <button type="button" disabled={busy} onClick={() => void onDeny()}>
              {t('ext.connect.deny')}
            </button>
          </>
        ) : (
          <>
            <p className="muted">{t('ext.connect.startFromExtension')}</p>
            {/* Typing the code by hand is the path that always works. The
                extension opens this page with ?code= as a convenience, and
                a convenience is exactly the thing that can fail: a browser
                that would not open the tab, a link opened in another
                profile, a reader who closed it. */}
            <form onSubmit={onLookUp} className="ext__codeform">
              <label htmlFor="ext-code">{t('ext.connect.enterCode')}</label>
              <input
                id="ext-code"
                value={typedCode}
                onChange={(e) => setTypedCode(e.target.value)}
                placeholder={t('ext.connect.codePlaceholder')}
                autoComplete="off"
                spellCheck={false}
              />
              <button type="submit" disabled={busy || !typedCode.trim()}>
                {t('ext.connect.lookUp')}
              </button>
            </form>
            {requestCode && <p className="hint">{t('ext.connect.noSuchRequest')}</p>}
          </>
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
