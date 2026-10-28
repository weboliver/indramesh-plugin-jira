// Phase 295 (JUI-05, D-13..D-15): pure, host-free Jira conflict logic. This
// module must not import `react` or `@indramesh/plugin-api`, so `node --test`
// can run it without the host import map (mirrors badge-logic.test.js).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DEFAULT_FILTERS,
  STATUS_VALUES,
  compareSnapshots,
  decodeScalar,
  decodeSnapshot,
  filterQueryParams,
  filtersFromSearch,
  itemKey,
  mergeConflictPages,
  referenceIds,
  resetViewState,
  searchFromFilters,
} from './conflict-logic.js'

// The server's canonical_repr wraps a scalar as { <typed_column>: value }.
const scalar = (value) => JSON.stringify({ value_simple: value })

test('an invalid or absent envelope decodes to an empty side, never raw JSON', () => {
  assert.equal(decodeSnapshot(null), null)
  assert.equal(decodeSnapshot(undefined), null)
  assert.equal(decodeSnapshot({}), null)
  assert.equal(decodeSnapshot({ v: 2, items: [] }), null)
  assert.equal(decodeSnapshot({ v: 1, items: 'nope' }), null)
  assert.deepEqual(compareSnapshots(undefined, null), {
    left: [],
    right: [],
    differCount: 0,
  })
})

test('a canonical scalar repr is decoded exactly once for its data_type', () => {
  assert.equal(decodeScalar(scalar('hello'), 'text'), 'hello')
  // A bare struct (json/status) must not be unwrapped by its first key.
  assert.deepEqual(decodeScalar(JSON.stringify({ id: '10', name: 'Open' }), 'status'), {
    id: '10',
    name: 'Open',
  })
  assert.deepEqual(decodeScalar(JSON.stringify(['a', 'b']), 'json'), ['a', 'b'])
  // Invalid JSON falls back to the raw string instead of throwing.
  assert.equal(decodeScalar('{not json', 'text'), '{not json')
})

test('[a,a,b] vs [a,c] keeps both a copies shared and tags b-only and c-only', () => {
  const local = {
    v: 1,
    kind: 'scalar',
    cardinality: 'multi',
    data_type: 'text',
    items: [scalar('a'), scalar('a'), scalar('b')],
  }
  const remote = {
    v: 1,
    kind: 'scalar',
    cardinality: 'multi',
    data_type: 'text',
    items: [scalar('a'), scalar('c')],
  }
  const result = compareSnapshots(local, remote)

  assert.equal(result.left.length, 3)
  assert.equal(result.right.length, 2)
  // Both `a` copies survive and are shared; b is the only differing left item.
  assert.deepEqual(
    result.left.map((item) => [item.value, item.shared]),
    [
      ['b', false],
      ['a', true],
      ['a', true],
    ]
  )
  assert.deepEqual(
    result.right.map((item) => [item.value, item.shared]),
    [
      ['c', false],
      ['a', true],
    ]
  )
  assert.equal(result.differCount, 2)
})

test('an empty side is distinct from a differing side', () => {
  const local = {
    v: 1,
    kind: 'scalar',
    cardinality: 'single',
    data_type: 'text',
    items: [],
  }
  const remote = {
    v: 1,
    kind: 'scalar',
    cardinality: 'single',
    data_type: 'text',
    items: [scalar('only-jira')],
  }
  const result = compareSnapshots(local, remote)
  assert.equal(result.left.length, 0)
  assert.equal(result.right.length, 1)
  assert.equal(result.right[0].shared, false)
  assert.equal(result.differCount, 1)
})

test('reference items keep both ids and compare as a multiset', () => {
  const local = {
    v: 1,
    kind: 'reference',
    cardinality: 'multi',
    data_type: 'reference',
    items: [{ object_id: 'core-1', jira_object_id: '8844' }],
  }
  const remote = {
    v: 1,
    kind: 'reference',
    cardinality: 'multi',
    data_type: 'reference',
    items: [{ object_id: null, jira_object_id: '8844' }],
  }
  const localItem = compareSnapshots(local, local).left[0]
  assert.equal(itemKey(localItem, 'reference'), 'ref:core-1:8844')
  assert.deepEqual(referenceIds(local), ['core-1'])
  assert.deepEqual(referenceIds(remote), [])

  // The differing object_id makes each side a one-off; differCount counts both.
  const result = compareSnapshots(local, remote)
  assert.equal(result.differCount, 2)
})

test('filters come from the URL, default to pending, and reject bad values', () => {
  assert.deepEqual(filtersFromSearch(''), DEFAULT_FILTERS)
  assert.deepEqual(filtersFromSearch('?status=bogus&direction=sideways&schema=abc'), {
    status: 'pending',
    direction: null,
    schemaId: 'abc',
  })
  assert.deepEqual(
    filtersFromSearch('?status=resolved_keep_local&direction=push&schema=s1'),
    { status: 'resolved_keep_local', direction: 'push', schemaId: 's1' }
  )
  assert.ok(STATUS_VALUES.includes('resolved_auto'))
})

test('the default pending filter is omitted from the query string, others kept', () => {
  assert.equal(searchFromFilters({ ...DEFAULT_FILTERS }), '')
  const search = searchFromFilters({
    status: 'resolved_keep_remote',
    direction: 'push',
    schemaId: 's1',
  })
  const params = new URLSearchParams(search)
  assert.equal(params.get('status'), 'resolved_keep_remote')
  assert.equal(params.get('direction'), 'push')
  assert.equal(params.get('schema'), 's1')
  // Unrelated query params survive a filter change.
  const kept = searchFromFilters({ ...DEFAULT_FILTERS }, '?status=x&schema=y&keep=1')
  assert.equal(new URLSearchParams(kept).get('keep'), '1')
  assert.equal(new URLSearchParams(kept).get('status'), null)
  assert.equal(new URLSearchParams(kept).get('schema'), null)
})

test('the query carries the raw offset and only the active filters', () => {
  assert.deepEqual(filterQueryParams(DEFAULT_FILTERS, 0), {
    status: 'pending',
    limit: 50,
    offset: 0,
  })
  assert.deepEqual(
    filterQueryParams({ status: 'pending', direction: 'pull', schemaId: 's9' }, 50),
    { status: 'pending', limit: 50, offset: 50, direction: 'pull', schema_id: 's9' }
  )
})

test('a filter change resets the offset and collapses the open detail', () => {
  assert.deepEqual(resetViewState(), { offset: 0, expandedId: null })
})

test('pages merge by id without losing the server cursor', () => {
  const first = [{ id: 'a' }, { id: 'b' }]
  const second = [{ id: 'b' }, { id: 'c' }]
  assert.deepEqual(
    mergeConflictPages(first, second).map((item) => item.id),
    ['a', 'b', 'c']
  )
  assert.deepEqual(mergeConflictPages([], undefined), [])
})
