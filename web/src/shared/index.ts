// Rules shared by every browser client of Mycelium.
//
// Two packages compile this directory: the SPA (web/) and the browser
// extension (extension/). Both are thin adapters over the same REST API,
// and a handful of rules are properties of that API rather than of
// either client: how its error envelope reads, what an entity code looks
// like, what a recents row is, what the search-click payload contains,
// and what the query grammar's tokens mean. Written twice, those drift,
// and each drift is invisible until someone's query returns the wrong
// rows or an error renders as [object Object].
//
// THE PURITY RULE. Nothing here may import anything outside this
// directory. Not React, not i18next, not the API client, not
// localStorage, not chrome.*. A module that reaches one of those cannot
// be compiled into the other package, and the failure is not a build
// error you would notice while working on the SPA -- it is a build error
// somebody else meets later. web/scripts/check-shared-purity.mjs makes
// it mechanical.
//
// The consequence is a shape, and it is the point: everything here takes
// what it needs as an argument and returns a value. Storage, transport,
// catalogues and clocks belong to the caller, so each client keeps its
// own and neither has to pretend to be the other.
//
// THE ENTRY POINT RULE. This barrel is the only path a consumer outside
// web/src/shared may import. Deep imports are refused by the extension's
// lint configuration, so the surface stays something that can be changed
// without auditing two packages.

export type { paths, components, operations } from './schema'

export type { ApiError } from './errors'
export { errCode, errMessage } from './errors'

export type { LookupMatch, LookupOut, LookupOpts } from './prefix'
export { RESOLVE_ID, isPrefixCandidate, isFullUuid, lookupCacheKey, lookupPath } from './prefix'

export type { RecentItem } from './recents'
export {
  RECENTS_MAX,
  RECENTS_LEGACY_FLAT_KEY,
  recentsKey,
  isRecentItem,
  parseRecents,
  withRecent,
} from './recents'

export type { SearchClickEvent, SearchClickBody } from './telemetry'
export { SEARCH_CLICK_PATH, searchClickBody } from './telemetry'

export type { FilterKey, DueKeyword } from './query'
export {
  RE_PREDICATE,
  FILTER_KEYS,
  isFilterKey,
  DUE_KEYWORDS,
  RE_DAY_OFFSET,
  RE_YMD,
  tokenize,
} from './query'

export type { ConnectMessage, ConnectReply, ExtensionRelease } from './extension'
export {
  CONNECT_EXTENSION_ID_PARAM,
  CONNECT_MESSAGE_KIND,
  CONNECT_ROUTE,
  CONNECT_STATE_PARAM,
  EXTENSION_PACKAGE_DIR,
  EXTENSION_PROVIDER,
  EXTENSION_RELEASE_URL,
  EXTENSION_SCOPES,
  extensionReleaseServes,
  parseExtensionRelease,
} from './extension'
