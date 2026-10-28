// Phase 295 (JUI-01, D-01..D-04): pure, host-free Jira task-form logic.
//
// This module MUST NOT import `react`, `@indramesh/plugin-api`, or JSON — the
// host import map cannot be resolved by `node --test`, so the React forms keep
// only rendering while every scope/filter/preflight decision lives here and is
// unit-testable (R-07). Nothing here stores or reads a credential.
//
// The persisted account lives in the task SCOPE (`service_account_id`) — never
// in `plugin_parameters` — because every Jira runner reads `Task.service_account_id`.

// --- Task scope/parameter builders -----------------------------------------

// S1 Pull (D-01/D-04): Source + persisted account are required; the Jira schema
// is optional (blank = the whole Source); the comments/attachments flag is a
// boolean that always rides in plugin_parameters, default false.
export function buildPullFormState({
  sourceId = '',
  serviceAccountId = '',
  jiraSchemaId = '',
  targetSchemaId = '',
  includeCommentsFiles = false,
  connectionReady = false,
} = {}) {
  return {
    scope: { source_id: sourceId, service_account_id: serviceAccountId },
    plugin_parameters: {
      ...(jiraSchemaId ? { jira_schema_id: jiraSchemaId } : {}),
      // 261004: an adopted existing empty schema only makes sense for one
      // selected (unlinked) Jira schema.
      ...(jiraSchemaId && targetSchemaId ? { target_schema_id: targetSchemaId } : {}),
      include_comments_files: !!includeCommentsFiles,
    },
    valid: Boolean(sourceId && serviceAccountId && connectionReady),
  }
}

// S1 Push (D-02): whole selected schema, no ObjectType filter — the scope
// carries schema/target/account and plugin_parameters stays empty so
// `object_type_id` is never transmitted.
export function buildPushFormState({ schemaId = '', targetId = '', serviceAccountId = '' } = {}) {
  return {
    scope: { schema_id: schemaId, target_id: targetId, service_account_id: serviceAccountId },
    plugin_parameters: {},
    valid: Boolean(schemaId && targetId && serviceAccountId),
  }
}

// S1 Export (D-03): schema/target/account/destination and a destination proven
// empty. `destinationEmpty` is the ONLY thing that unlocks submission; anything
// else (empty, failed, loading, stale) keeps `valid` false (fail closed).
export function buildExportFormState({
  schemaId = '',
  targetId = '',
  serviceAccountId = '',
  jiraSchemaId = '',
  destinationEmpty = false,
} = {}) {
  return {
    scope: { schema_id: schemaId, target_id: targetId, service_account_id: serviceAccountId },
    plugin_parameters: jiraSchemaId ? { jira_schema_id: jiraSchemaId } : {},
    valid: Boolean(schemaId && targetId && serviceAccountId && jiraSchemaId) && destinationEmpty === true,
  }
}

// --- Picker filters (R-05, mirroring admission.py) --------------------------

export function filterJiraSources(sources) {
  return (sources || []).filter((source) => source && source.type === 'jira')
}

// A push/export Target must be a jira connector, active, and either unbound or
// bound to the chosen Core schema (admission refuses "bound to a different Core
// schema").
export function filterJiraTargets(targets, schemaId = '') {
  return (targets || []).filter((target) => {
    if (!target || target.connector_type !== 'jira') return false
    if (target.is_active === false) return false
    if (target.schema_id && schemaId && String(target.schema_id) !== String(schemaId)) return false
    return true
  })
}

// Active and not past its expiry. The server `_load_account` check stays
// authoritative; this only keeps the obviously-unusable accounts out.
export function filterEligibleAccounts(accounts, now = Date.now()) {
  return (accounts || []).filter((account) => {
    if (!account || account.is_active === false) return false
    if (account.expires_at) {
      const expiry = Date.parse(account.expires_at)
      if (!Number.isNaN(expiry) && expiry <= now) return false
    }
    return true
  })
}

// --- Export destination preflight (D-03) ------------------------------------

// The selection a preflight result belongs to. Any change to target,
// destination or account yields a different key; only a full selection has one.
export function exportPreflightKey({ targetId = '', jiraSchemaId = '', serviceAccountId = '' } = {}) {
  if (!targetId || !jiraSchemaId || !serviceAccountId) return ''
  return `${targetId}:${jiraSchemaId}:${serviceAccountId}`
}

// Resolve a preflight result to a fail-closed status. A reply whose request key
// no longer matches the current selection is treated as still loading, so an
// out-of-order answer can never unlock the export (T-295-01-T).
export function exportPreflightState({
  currentKey = '',
  requestKey = '',
  loading = false,
  data = null,
  error = null,
} = {}) {
  if (!currentKey) return { status: 'idle', count: null, empty: false }
  if (error && requestKey === currentKey) return { status: 'failed', count: null, empty: false }
  if (loading || !data || requestKey !== currentKey) {
    return { status: 'loading', count: null, empty: false }
  }
  const count = typeof data.object_type_count === 'number' ? data.object_type_count : null
  if (data.empty === true) return { status: 'empty', count, empty: true }
  return { status: 'not_empty', count, empty: false }
}
