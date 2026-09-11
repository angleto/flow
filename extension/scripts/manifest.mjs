// The manifest, derived rather than maintained.
//
// Every value that could disagree with the bundle -- the version, the
// origin it may reach, the origin that may talk to it -- is computed from
// the same build environment the code is compiled against, so the two
// cannot drift. What is left here is the part that is genuinely a
// decision: which permissions the extension asks for, and why.

/** @typedef {import('./env.mjs').BuildEnv} BuildEnv */

/** @param {BuildEnv} env */
export function manifestFor(env) {
  return {
    manifest_version: 3,
    name: '__MSG_extName__',
    description: '__MSG_extDescription__',
    default_locale: 'en',
    version: env.version,
    // The full `git describe` string. `version` is a number Chrome
    // compares; this is what a person reads when reporting a problem.
    version_name: env.versionName,
    minimum_chrome_version: '116',
    icons: {
      16: 'icons/icon-16.png',
      32: 'icons/icon-32.png',
      48: 'icons/icon-48.png',
      128: 'icons/icon-128.png',
    },
    action: {
      default_popup: 'popup.html',
      default_title: '__MSG_extName__',
      default_icon: { 16: 'icons/icon-16.png', 32: 'icons/icon-32.png' },
    },
    background: { service_worker: 'background.js', type: 'module' },
    side_panel: { default_path: 'sidepanel.html' },
    // Each of these is justified in the store listing, and the list is
    // deliberately short:
    //
    //   storage      the credential, the scope selection, the recents
    //   sidePanel    the persistent working surface
    //   contextMenus capture from a selection or a link
    //   activeTab    read the page ONLY at the moment capture is invoked,
    //                and only the tab it was invoked from
    //   scripting    what activeTab is exercised through
    //   alarms       the net under the connect wait. Chrome shuts an idle
    //                service worker down, so the fast poll chain cannot be
    //                the only thing waiting for somebody to approve, and
    //                the alarm is what wakes the worker to ask again
    //
    // NOT requested, and each absence is a decision:
    //
    //   <all_urls>          no in-page overlay, so no code of ours runs on
    //                       every page you visit
    //   notifications       a write that lands after the panel closed is
    //                       reported the next time the panel opens, not by
    //                       interrupting the desktop
    //   tabs                chrome.tabs.create and .query need no
    //                       permission for what this does
    //   unlimitedStorage    the caches are bounded by count on purpose
    permissions: ['storage', 'sidePanel', 'contextMenus', 'activeTab', 'scripting', 'alarms'],
    host_permissions: [env.hostPermission],
    // NO externally_connectable, and its absence is the point.
    //
    // It used to be here, because the app's settings page minted a
    // credential and pushed it into this extension over Chrome's
    // messaging. That is a standing right for a web origin to send
    // messages to this extension, held permanently to save a few seconds
    // during a ceremony performed once. It also could not work at all on
    // a build against localhost -- Chrome refuses a pattern whose host
    // has no second-level domain -- so a development package could never
    // connect.
    //
    // The extension now ASKS instead: it opens a device-authorization
    // request, shows a code, and collects what it was granted. Nothing
    // needs to be able to talk to it, so nothing may.
    commands: {
      _execute_action: {
        suggested_key: { default: 'Ctrl+Shift+K', mac: 'Command+Shift+K' },
        description: '__MSG_cmdOpenPanel__',
      },
      'open-side-panel': {
        suggested_key: { default: 'Ctrl+Shift+L', mac: 'Command+Shift+L' },
        description: '__MSG_cmdOpenSidePanel__',
      },
      capture: {
        suggested_key: { default: 'Ctrl+Shift+S', mac: 'Command+Shift+S' },
        description: '__MSG_cmdCapture__',
      },
      'search-selection': {
        suggested_key: { default: 'Ctrl+Shift+F', mac: 'Command+Shift+F' },
        description: '__MSG_cmdSearchSelection__',
      },
    },
    omnibox: { keyword: 'myc' },
  }
}
