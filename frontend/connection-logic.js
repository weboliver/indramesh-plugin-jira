// Phase 295 (JUI-02, D-05..D-08, R2-03/R2-04/R2-06): pure, host-free Jira
// connection logic.
//
// This module MUST NOT import `react`, `@indramesh/plugin-api`, or JSON — the
// host import map cannot be resolved by `node --test`, so the React editor keeps
// only rendering while every write/test decision lives here and is unit-testable
// (R2-06). Nothing here stores or reads a credential: the test only ever takes a
// bound Core ServiceAccount id.

// The closed strategy vocabulary the plugin route accepts for
// `default_conflict_strategy` (connections.py ConnectionUpsert Literal). `null`
// (represented as the empty select value) means manual resolution.
export const CONFLICT_STRATEGIES = ['prefer_local', 'prefer_remote']

// The closed connection-test outcome vocabulary (connections.py
// CONNECTION_TEST_CATEGORIES).
export const CONNECTION_TEST_CATEGORIES = [
  'ok',
  'auth',
  'network',
  'permission',
  'not_found',
  'error',
]

// A <select> value is always a string; the wire value is `null` for manual.
export function selectValueToStrategy(value) {
  return CONFLICT_STRATEGIES.includes(value) ? value : null
}

export function strategyToSelectValue(strategy) {
  return CONFLICT_STRATEGIES.includes(strategy) ? strategy : ''
}

// R2-03: the connection GET on open resolves one of loading/error/unbound/live/
// stale; `loaded` is true only for a resolved 200 or 404. A failed or still
// pending GET must never write (it cannot know a stored strategy).
//
// R2-03: after a successful Core save `afterCoreSave` PUTs the connection only
// when (a) no connection exists yet — a new OR an existing-but-unbound Jira
// record, so pre-Phase-295 rows become bindable — or (b) the strategy or the
// default service account (261004) changed. `serviceAccountId` is `undefined`
// when the editor never offered a choice; the body then omits the field so the
// stored default stays untouched. An empty choice clears it (`null`).
//
// R2-04: a stale binding is refused (there is no rebind route in v20) and a URL
// change on a live binding makes it stale, so neither may trigger a write.
export function decideConnectionWrite({
  loaded = false,
  fetchFailed = false,
  connection = null,
  strategy = null,
  serviceAccountId = undefined,
  urlChanged = false,
} = {}) {
  if (!loaded || fetchFailed) return { action: 'skip' }
  const body = { default_conflict_strategy: strategy }
  const account = serviceAccountId === undefined ? undefined : serviceAccountId || null
  if (account !== undefined) body.default_service_account_id = account
  if (connection === null) return { action: 'write', body }
  if (connection.binding === 'stale') return { action: 'skip' }
  if (urlChanged) return { action: 'skip' }
  const strategyChanged = (connection.default_conflict_strategy ?? null) !== strategy
  const accountChanged =
    account !== undefined && (connection.default_service_account_id ?? null) !== account
  if (!strategyChanged && !accountChanged) return { action: 'skip' }
  return { action: 'write', body }
}

// 261004: the stored default account preselects a picker while it is still one
// of the eligible accounts offered there; without a usable default, the only
// eligible account is preselected; otherwise nothing is.
export function defaultAccountSelection(defaultAccountId, accounts = []) {
  if (defaultAccountId && accounts.some((account) => account.id === defaultAccountId)) {
    return defaultAccountId
  }
  return accounts.length === 1 ? accounts[0].id : ''
}

// R2-05: the editor compares the modal's current (unsaved) URL field with the
// saved record URL so it can say which address the test actually probes.
export function hasUnsavedUrlChange({ draftUrl = '', savedUrl = '' } = {}) {
  return String(draftUrl ?? '').trim() !== String(savedUrl ?? '').trim()
}

// D-06: each server category has its own visible copy; an unrecognised category
// falls back to the generic error line rather than showing nothing.
export function connectionTestKey(category) {
  return CONNECTION_TEST_CATEGORIES.includes(category)
    ? `connector.result_${category}`
    : 'connector.result_error'
}

// D-05/D-07: the test needs a saved record id and an eligible bound account, and
// only one request may be in flight at a time.
export function canRunConnectionTest({ recordId = null, serviceAccountId = '', testing = false } = {}) {
  return Boolean(recordId) && Boolean(serviceAccountId) && !testing
}
