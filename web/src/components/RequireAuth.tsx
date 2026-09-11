import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useSession } from '../auth/useSession'
import { returnToFrom } from '../lib/returnTo'

export function RequireAuth() {
  const session = useSession()
  const location = useLocation()
  // The address that was refused, carried to the login form so it can
  // put the person back on it. Not a convenience: a route whose QUERY is
  // the request -- /settings/extension?state=&id=, which is how the
  // browser extension asks for a credential -- is destroyed rather than
  // postponed when the detour forgets where it came from. See
  // lib/returnTo.ts.
  if (!session)
    return <Navigate to="/login" replace state={{ returnTo: returnToFrom(location) }} />
  // Keyed by workspace on purpose (task 805a569c). Switching workspace is an
  // in-app context switch, not a reload (auth/session.setActiveWorkspace only
  // rewrites the session and emits), so without this every component below
  // keeps the previous tenant's fetched rows -- project lists, tag catalogues,
  // client-search results -- until its own refetch lands, and indefinitely if
  // that refetch fails. Remounting the authenticated subtree makes tenant data
  // outliving its tenant unrepresentable, instead of leaving each component to
  // remember to clear itself.
  return <Outlet key={session.workspaceId} />
}
