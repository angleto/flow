import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, errCode, errMessage, establishSession } from '../api/client'
import { safeReturnTo } from '../lib/returnTo'

// Login is email+password only (never a workspace choice, ADR-0024).
// /auth/login answers 401 auth.mfa_required when MFA is active: we
// pivot to the TOTP form and call /auth/login-mfa. 403
// email_not_verified offers a resend; 423 means locked.
export function LoginRoute() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  // Where RequireAuth was standing when it sent this person here, or the
  // default landing when they came to the form by themselves. Read once:
  // it is fixed for the life of the form, and the MFA step must not lose
  // it. See lib/returnTo.ts for why it travels in navigation state.
  const location = useLocation()
  const returnTo = safeReturnTo((location.state as { returnTo?: unknown } | null)?.returnTo)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [mfa, setMfa] = useState(false)
  const [notVerified, setNotVerified] = useState(false)
  const [busy, setBusy] = useState(false)
  const [info, setInfo] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function finish(token: string, refreshToken?: string) {
    await establishSession(token, refreshToken)
    navigate(returnTo, { replace: true })
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    setInfo(null)
    const { data, error, response } = await api.POST('/auth/login', {
      body: { email, password },
    })
    if (response.ok && data) {
      await finish(data.token, data.refresh_token ?? undefined)
      return
    }
    setBusy(false)
    const code = errCode(error)
    if (response.status === 401 && code === 'auth.mfa_required') {
      setMfa(true)
    } else if (response.status === 403 && code === 'auth.email_not_verified') {
      setNotVerified(true)
      setErr(t('login.emailNotVerified'))
    } else if (response.status === 423) {
      setErr(t('login.locked'))
    } else {
      setErr(errMessage(error))
    }
  }

  async function onSubmitMfa(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    const { data, error, response } = await api.POST('/auth/login-mfa', {
      body: { email, password, totp_code: totp },
    })
    if (response.ok && data) {
      await finish(data.token, data.refresh_token ?? undefined)
      return
    }
    setBusy(false)
    setErr(errMessage(error))
  }

  async function onResend() {
    setBusy(true)
    await api.POST('/auth/resend-verification', { body: { email } })
    setBusy(false)
    setInfo(t('login.resent'))
  }

  if (mfa) {
    return (
      <form className="card" onSubmit={(e) => void onSubmitMfa(e)}>
        <h1>{t('login.mfaTitle')}</h1>
        <label>
          {t('login.mfaCode')}
          <input
            required
            value={totp}
            onChange={(e) => setTotp(e.target.value)}
            autoComplete="one-time-code"
          />
        </label>
        {err && <p className="err">{err}</p>}
        <button type="submit" disabled={busy}>
          {busy ? t('auth.working') : t('login.verify')}
        </button>
      </form>
    )
  }

  return (
    <form className="card" onSubmit={(e) => void onSubmit(e)}>
      <h1>{t('login.title')}</h1>
      <label>
        {t('auth.email')}
        <input
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        {t('auth.password')}
        <input
          type="password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {err && <p className="err">{err}</p>}
      {info && <p className="ok">{info}</p>}
      <button type="submit" disabled={busy}>
        {busy ? t('auth.working') : t('auth.signIn')}
      </button>
      {notVerified && (
        <button type="button" onClick={() => void onResend()} disabled={busy}>
          {t('login.resend')}
        </button>
      )}
      <p className="hint">
        <Link to="/forgot-password">{t('auth.forgotLink')}</Link>
        {' · '}
        <Link to="/register">{t('auth.toRegister')}</Link>
      </p>
    </form>
  )
}
