// Phase 295 (JUI-08, D-10/D-11): pure, host-free Jira sync-direction logic.
//
// This module MUST NOT import `react`, `@indramesh/plugin-api`, or JSON — the
// host import map cannot be resolved by `node --test`, so the React control keeps
// only rendering while every direction mapping and the immediate-save rollback
// live here and are unit-testable (R2-06). The server stays the single authority
// for the effective direction; nothing here re-implements inheritance.

// The closed explicit-direction vocabulary the route accepts (links.py
// SYNC_DIRECTIONS). `null` means "inherit from the parent level".
export const DIRECTION_VALUES = ['none', 'pull', 'push', 'both']

// A <select> value is always a string; the wire value is `null` for inherit.
export function selectValueToDirection(value) {
  return DIRECTION_VALUES.includes(value) ? value : null
}

export function directionToSelectValue(direction) {
  return DIRECTION_VALUES.includes(direction) ? direction : ''
}

// D-10: the schema level has no parent, so its inherit option reads "Default
// (both directions)" instead of "Inherit".
export function directionLabelKey(direction, { schemaLevel = false } = {}) {
  if (direction === null || direction === undefined) {
    return schemaLevel ? 'direction.default_both' : 'direction.inherit'
  }
  return `direction.${direction}`
}

// The origin vocabulary is exactly attribute/object_type/schema/default.
export function originLabelKey(origin) {
  if (origin === 'attribute' || origin === 'object_type' || origin === 'schema') {
    return `direction.origin_${origin}`
  }
  return 'direction.origin_default'
}

// D-11: the immediate-save state machine. The select shows the requested value
// while the PUT runs; the effective line only changes from the server response
// (never optimistically), and an error reverts the select to the previous value.
export function directionSaveStart({ value }) {
  return { status: 'saving', value, error: null }
}

export function directionSaveSuccess({
  explicit_direction,
  effective_direction,
  direction_origin,
} = {}) {
  return {
    status: 'saved',
    value: explicit_direction ?? null,
    effective: effective_direction,
    origin: direction_origin,
    error: null,
  }
}

export function directionSaveError({ previousValue, message } = {}) {
  return { status: 'error', value: previousValue ?? null, error: message }
}

// GET /links/attribute accepts at most 100 ids per call (routes.py
// MAX_BATCH_IDS); a type's attributes are read in bounded chunks.
export function chunkIds(ids, size = 100) {
  if (!Number.isInteger(size) || size < 1) throw new RangeError('chunk size must be an integer >= 1')
  const out = []
  for (let i = 0; i < ids.length; i += size) out.push(ids.slice(i, i + size))
  return out
}

// ---------------------------------------------------------------------------
// Verified Jira deep links (D-09, R3-02).
//
// The URL patterns below are the documented Jira Data Center forms and work
// with the numeric Jira IDs this plugin stores (never an object key like
// `ITSM-123`):
//   * schema      /secure/ObjectSchema.jspa?id={schemaId}
//   * object type /secure/ObjectSchema.jspa?id={schemaId}&typeId={typeId}
//   * object      /secure/ShowObject.jspa?id={objectId}
//
// Sources (read-only confirmation, 2026-09-30): Atlassian Support KB
// "How to Find Object Schema, Object Type, and Object IDs in Jira Assets"
// (Cloud and Data Center), and the Assets REST API reference, where an object's
// `_links.self` is `/secure/ShowObject.jspa?id={objectId}`. Anything that cannot
// be built from a verified pattern stays plain text on the caller side.

const JIRA_NUMERIC_ID = /^[0-9]{1,20}$/

function numericId(value) {
  const text = String(value ?? '')
  return JIRA_NUMERIC_ID.test(text) ? text : null
}

export function schemaJiraPath(schemaJiraId) {
  const id = numericId(schemaJiraId)
  return id === null ? null : `/secure/ObjectSchema.jspa?id=${id}`
}

export function objectTypeJiraPath(schemaJiraId, objectTypeJiraId) {
  const schemaId = numericId(schemaJiraId)
  const typeId = numericId(objectTypeJiraId)
  if (schemaId === null || typeId === null) return null
  return `/secure/ObjectSchema.jspa?id=${schemaId}&typeId=${typeId}`
}

export function objectJiraPath(objectJiraId) {
  const id = numericId(objectJiraId)
  return id === null ? null : `/secure/ShowObject.jspa?id=${id}`
}

// Combine the saved connection base URL with one of the verified entity paths.
// Returns an absolute same-origin http(s) URL, or `null` for anything unsafe:
// a non-http(s) scheme, an embedded credential, or a path that escapes the saved
// origin. A Jira context path (`https://host/jira`) is preserved, never dropped.
export function safeJiraLink(baseUrl, path) {
  if (typeof baseUrl !== 'string' || typeof path !== 'string') return null
  if (!path.startsWith('/')) return null
  let base
  try {
    base = new URL(baseUrl)
  } catch {
    return null
  }
  if (base.protocol !== 'http:' && base.protocol !== 'https:') return null
  if (base.username || base.password) return null
  const basePath = base.pathname.endsWith('/') ? base.pathname : `${base.pathname}/`
  let target
  try {
    target = new URL(path.replace(/^\/+/, ''), `${base.origin}${basePath}`)
  } catch {
    return null
  }
  if (target.protocol !== 'http:' && target.protocol !== 'https:') return null
  if (target.username || target.password) return null
  if (target.origin !== base.origin) return null
  return target.toString()
}
