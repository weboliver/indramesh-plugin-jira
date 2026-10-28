// Pure Jira conflict helpers (Phase 295, JUI-05, D-13..D-15).
//
// This module is host-free and React-free so `node --test` can import it
// directly. It owns everything the conflicts page must decide WITHOUT a
// request: snapshot decoding (with a safe fallback for a malformed envelope),
// the multiset-aware two-sided comparison, the URL filter vocabulary and the
// server-owned page merge. The React page and every HTTP call live in
// JiraConflictsPage.jsx.

export const SNAPSHOT_VERSION = 1

export const STATUS_VALUES = [
  'pending',
  'resolved_keep_local',
  'resolved_keep_remote',
  'resolved_auto',
]

export const DEFAULT_FILTERS = {
  status: 'pending',
  direction: null,
  schemaId: null,
}

// Label keys per status, resolved through the plugin namespace by the page.
export const STATUS_LABEL_KEYS = {
  pending: 'conflicts.status.pending',
  resolved_keep_local: 'conflicts.status.resolved_keep_local',
  resolved_keep_remote: 'conflicts.status.resolved_keep_remote',
  resolved_auto: 'conflicts.status.resolved_auto',
}

const TYPED_COLUMN_BY_DATA_TYPE = {
  integer: 'value_integer',
  double: 'value_float',
  float: 'value_float',
  number: 'value_float',
  boolean: 'value_boolean',
  date: 'value_datetime',
  datetime: 'value_datetime',
  json: 'value_struct',
  status: 'value_struct',
}

export function typedColumnFor(dataType) {
  return TYPED_COLUMN_BY_DATA_TYPE[dataType] || 'value_simple'
}

export function isDateDataType(dataType) {
  return dataType === 'date' || dataType === 'datetime'
}

// A snapshot envelope is usable only when it is a version-1 object with an
// items array; anything else is treated as an empty side (never a raw JSON
// dump — the prohibition on opaque JSON in the UI).
export function decodeSnapshot(snapshot) {
  if (
    !snapshot ||
    typeof snapshot !== 'object' ||
    snapshot.v !== SNAPSHOT_VERSION ||
    !Array.isArray(snapshot.items)
  ) {
    return null
  }
  return snapshot
}

// Decode one canonical scalar repr string. `canonical_repr` wraps a scalar as
// `{ <typed_column>: value }` but stores a struct (json/status) as the BARE
// value, so the data_type disambiguates. Invalid JSON falls back to the raw
// string rather than throwing.
export function decodeScalar(rep, dataType) {
  let decoded
  try {
    decoded = JSON.parse(rep)
  } catch {
    return String(rep)
  }
  const column = typedColumnFor(dataType)
  if (column === 'value_struct') return decoded
  if (decoded && typeof decoded === 'object' && !Array.isArray(decoded)) {
    if (Object.prototype.hasOwnProperty.call(decoded, column)) return decoded[column]
    const keys = Object.keys(decoded)
    if (keys.length === 1) return decoded[keys[0]]
  }
  return decoded
}

// Every snapshot item as a display descriptor. References keep both ids; the
// page resolves a name through ONE batched read and falls back to the Jira
// object id.
export function decodeItems(snapshot) {
  const usable = decodeSnapshot(snapshot)
  if (!usable) return []
  if (usable.kind === 'reference') {
    return usable.items.map((raw) => ({
      kind: 'reference',
      objectId: raw?.object_id || null,
      jiraObjectId: raw?.jira_object_id || null,
      value: null,
    }))
  }
  return usable.items.map((raw) => ({
    kind: 'scalar',
    objectId: null,
    jiraObjectId: null,
    value: decodeScalar(raw, usable.data_type),
  }))
}

export function itemKey(item, dataType) {
  if (item.kind === 'reference') {
    return `ref:${item.objectId || ''}:${item.jiraObjectId || ''}`
  }
  return `val:${dataType}:${JSON.stringify(item.value)}`
}

export function referenceIds(snapshot) {
  return decodeItems(snapshot)
    .filter((item) => item.objectId)
    .map((item) => item.objectId)
}

function countBy(items, dataType) {
  const counts = new Map()
  items.forEach((item) => {
    const key = itemKey(item, dataType)
    counts.set(key, (counts.get(key) || 0) + 1)
  })
  return counts
}

// A multiset-aware comparison. Duplicates are KEPT (a scalar attribute is a
// multiset); an occurrence is `shared` when the other side holds at least one
// copy; an empty side stays distinct from a merely-different one. Differing
// items are listed first, then shared ones, each preserving snapshot order.
export function compareSnapshots(local, remote) {
  const localItems = decodeItems(local)
  const remoteItems = decodeItems(remote)
  const localCounts = countBy(localItems, local?.data_type)
  const remoteCounts = countBy(remoteItems, remote?.data_type)

  const decorate = (items, ownCounts, otherCounts, dataType) =>
    items
      .map((item) => {
        const key = itemKey(item, dataType)
        return { ...item, key, shared: (otherCounts.get(key) || 0) > 0 }
      })
      .sort((a, b) => Number(a.shared) - Number(b.shared))

  let differCount = 0
  localCounts.forEach((_count, key) => {
    if ((remoteCounts.get(key) || 0) === 0) differCount += 1
  })
  remoteCounts.forEach((_count, key) => {
    if ((localCounts.get(key) || 0) === 0) differCount += 1
  })

  return {
    left: decorate(localItems, localCounts, remoteCounts, local?.data_type),
    right: decorate(remoteItems, remoteCounts, localCounts, remote?.data_type),
    differCount,
  }
}

// --- URL filters (D-13/D-15) -----------------------------------------------

export function filtersFromSearch(search) {
  const params = new URLSearchParams(search || '')
  const status = params.get('status')
  const direction = params.get('direction')
  return {
    status: STATUS_VALUES.includes(status) ? status : 'pending',
    direction: direction === 'pull' || direction === 'push' ? direction : null,
    schemaId: params.get('schema') || null,
  }
}

export function searchFromFilters(filters, currentSearch = '') {
  const params = new URLSearchParams(currentSearch || '')
  if (filters.status && filters.status !== 'pending') params.set('status', filters.status)
  else params.delete('status')
  if (filters.direction) params.set('direction', filters.direction)
  else params.delete('direction')
  if (filters.schemaId) params.set('schema', filters.schemaId)
  else params.delete('schema')
  return params.toString()
}

export function filterQueryParams(filters, offset) {
  const params = { status: filters.status || 'pending', limit: 50, offset }
  if (filters.direction) params.direction = filters.direction
  if (filters.schemaId) params.schema_id = filters.schemaId
  return params
}

// A filter change always resets pagination and collapses any open detail.
export function resetViewState() {
  return { offset: 0, expandedId: null }
}

// Server-owned pagination: a page is appended, never re-cursored. Duplicate ids
// across pages collapse to the first occurrence.
export function mergeConflictPages(previous, pageItems) {
  const seen = new Set()
  const next = []
  ;[...(previous || []), ...(pageItems || [])].forEach((item) => {
    if (!item || seen.has(item.id)) return
    seen.add(item.id)
    next.push(item)
  })
  return next
}
