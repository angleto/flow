// A stand-in for the browser APIs this extension uses.
//
// It implements the CONTRACT, not just the call. Storage is
// asynchronous, an absent key answers with an empty bag exactly as Chrome
// does, `local` and `session` are separate stores that do not see each
// other, and `i18n` resolves against the real catalogue the package
// ships. A test that passes here is testing the behaviour the browser
// would exercise -- which is the whole difference between a contract test
// and an assertion that a function was called.

import enCatalogue from '../_locales/en/messages.json'

type Bag = Record<string, unknown>

class FakeArea {
  private data: Bag = {}

  get = async (keys?: string | string[] | null): Promise<Bag> => {
    if (keys === null || keys === undefined) return { ...this.data }
    const wanted = typeof keys === 'string' ? [keys] : keys
    const out: Bag = {}
    for (const key of wanted) {
      // Chrome omits an absent key rather than answering undefined for
      // it, and code that reads `(await get(k))[k]` depends on exactly
      // that shape.
      if (key in this.data) out[key] = this.data[key]
    }
    return out
  }

  set = async (items: Bag): Promise<void> => {
    Object.assign(this.data, items)
  }

  remove = async (keys: string | string[]): Promise<void> => {
    for (const key of typeof keys === 'string' ? [keys] : keys) delete this.data[key]
  }

  clear = async (): Promise<void> => {
    this.data = {}
  }

  /** Test-only window onto the store, so an assertion can name a key
   *  rather than going through the module under test to read it back. */
  peek = (): Bag => ({ ...this.data })
}

export interface Recorded {
  tabs: { url: string; active?: boolean }[]
  alarms: { name: string; periodInMinutes: number }[]
  menus: string[]
  fetches: { url: string; init: RequestInit | undefined }[]
}

export interface FakeChrome {
  chrome: typeof chrome
  recorded: Recorded
  local: FakeArea
  session: FakeArea
  /** Fires the listener the worker registered for a page handover. */
  external: (message: unknown, origin: string | undefined) => Promise<unknown>
  /** Fires the listener the worker registered for a panel message. */
  message: (message: unknown) => Promise<unknown>
}

export function installFakeChrome(): FakeChrome {
  const local = new FakeArea()
  const session = new FakeArea()
  const recorded: Recorded = { tabs: [], menus: [], fetches: [], alarms: [] }

  let externalListener:
    | ((message: unknown, sender: { origin?: string }, respond: (r: unknown) => void) => boolean)
    | null = null
  let messageListener:
    | ((message: unknown, sender: unknown, respond: (r: unknown) => void) => boolean)
    | null = null

  const fake = {
    runtime: {
      id: 'fakeextensionidfakeextensionidaa',
      getManifest: () => ({ version: '0.0.0' }),
      onMessage: {
        addListener: (fn: typeof messageListener) => {
          messageListener = fn
        },
      },
      onMessageExternal: {
        addListener: (fn: typeof externalListener) => {
          externalListener = fn
        },
      },
      onInstalled: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      sendMessage: async () => undefined,
    },
    storage: { local, session },
    i18n: {
      // The real catalogue, so a test that renders a label fails when the
      // label is removed rather than passing on a stub.
      getMessage: (key: string, subs?: string[]) => {
        const entry = (enCatalogue as Record<string, { message: string }>)[key]
        if (!entry) return ''
        let out = entry.message
        ;(subs ?? []).forEach((value, index) => {
          out = out.replace(new RegExp(`\\$[A-Za-z0-9_]+\\$`), value).replace(`$${index + 1}`, value)
        })
        return out
      },
      getUILanguage: () => 'en-GB',
    },
    tabs: {
      create: async (options: { url: string; active?: boolean }) => {
        recorded.tabs.push(options)
        return options
      },
      query: async () => [{ id: 1, url: 'https://example.test/page', title: 'A page' }],
      update: async (options: { url: string }) => {
        recorded.tabs.push(options)
      },
      captureVisibleTab: async () => 'data:image/png;base64,AAAA',
    },
    scripting: { executeScript: async () => [{ result: '' }] },
    contextMenus: {
      removeAll: async () => {
        recorded.menus = []
      },
      create: (options: { id: string }) => {
        recorded.menus.push(options.id)
      },
      onClicked: { addListener: () => {} },
    },
    sidePanel: { open: async () => {} },
    alarms: {
      // Recorded rather than driven by a clock: what a test needs to
      // assert is that the net UNDER the fast poll chain was armed and
      // later cleared, not that Chrome's scheduler fires. Firing is
      // exercised by calling the poll directly.
      create: async (name: string, info: { periodInMinutes: number }) => {
        recorded.alarms.push({ name, ...info })
      },
      clear: async (name: string) => {
        recorded.alarms = recorded.alarms.filter((a) => a.name !== name)
        return true
      },
      onAlarm: { addListener: () => {} },
    },
    commands: { onCommand: { addListener: () => {} } },
    omnibox: {
      setDefaultSuggestion: () => {},
      onInputChanged: { addListener: () => {} },
      onInputEntered: { addListener: () => {} },
    },
  }

  ;(globalThis as { chrome?: unknown }).chrome = fake

  return {
    chrome: fake as unknown as typeof chrome,
    recorded,
    local,
    session,
    external: (message, origin) =>
      new Promise((resolve) => {
        if (!externalListener) throw new Error('no external listener registered')
        externalListener(message, { origin }, resolve)
      }),
    message: (message) =>
      new Promise((resolve) => {
        if (!messageListener) throw new Error('no message listener registered')
        messageListener(message, {}, resolve)
      }),
  }
}
