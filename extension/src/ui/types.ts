// Re-exported for the panel modules, so a UI file never reaches past the
// protocol into the worker's own modules. The lint rule refuses that
// import; this is the door it is meant to use.

export type {
  CaptureDraft,
  CaptureResult,
  Connection,
  EntityRow,
  Failure,
  FailureCode,
  FindResult,
  LinkRequest,
  PageContext,
  Result,
  ScopeSel,
  Sections,
  TaskPatch,
} from '../shared/protocol'

export type Host = 'popup' | 'sidepanel'
