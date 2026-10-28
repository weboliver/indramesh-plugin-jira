// Phase 295 (JUI-08, D-10/D-11): pure direction-mapping and immediate-save
// rollback logic. Host-free — no react/plugin-api/JSON import — so node:test can
// run it without the host import map.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DIRECTION_VALUES,
  chunkIds,
  directionLabelKey,
  directionSaveError,
  directionSaveStart,
  directionSaveSuccess,
  directionToSelectValue,
  objectJiraPath,
  objectTypeJiraPath,
  originLabelKey,
  safeJiraLink,
  schemaJiraPath,
  selectValueToDirection,
} from './direction-logic.js'

test('select values map to the closed direction vocabulary', () => {
  assert.deepEqual(DIRECTION_VALUES, ['none', 'pull', 'push', 'both'])
  assert.equal(selectValueToDirection('pull'), 'pull')
  assert.equal(selectValueToDirection('both'), 'both')
  assert.equal(selectValueToDirection(''), null)
  assert.equal(selectValueToDirection('bogus'), null)
  assert.equal(selectValueToDirection(null), null)
})

test('a null explicit direction renders as the inherit select value', () => {
  assert.equal(directionToSelectValue(null), '')
  assert.equal(directionToSelectValue(undefined), '')
  assert.equal(directionToSelectValue('none'), 'none')
  assert.equal(directionToSelectValue('bogus'), '')
})

test('the inherit label is schema-aware and the set labels are keyed', () => {
  assert.equal(directionLabelKey(null, { schemaLevel: false }), 'direction.inherit')
  assert.equal(directionLabelKey(null, { schemaLevel: true }), 'direction.default_both')
  assert.equal(directionLabelKey('push'), 'direction.push')
  assert.equal(directionLabelKey('none'), 'direction.none')
})

test('origin keys cover the four levels and fall back to the default sentence', () => {
  assert.equal(originLabelKey('attribute'), 'direction.origin_attribute')
  assert.equal(originLabelKey('object_type'), 'direction.origin_object_type')
  assert.equal(originLabelKey('schema'), 'direction.origin_schema')
  assert.equal(originLabelKey('default'), 'direction.origin_default')
  assert.equal(originLabelKey(null), 'direction.origin_default')
})

test('an immediate save starts in saving and clears any prior error', () => {
  const state = directionSaveStart({ value: 'pull' })
  assert.equal(state.status, 'saving')
  assert.equal(state.value, 'pull')
  assert.equal(state.error, null)
})

test('a successful save takes the server explicit/effective/origin values', () => {
  const state = directionSaveSuccess({
    explicit_direction: 'push',
    effective_direction: 'push',
    direction_origin: 'attribute',
  })
  assert.equal(state.status, 'saved')
  assert.equal(state.value, 'push')
  assert.equal(state.effective, 'push')
  assert.equal(state.origin, 'attribute')
  assert.equal(state.error, null)
})

test('a failed save reverts the select to the previous value and reports why', () => {
  const state = directionSaveError({ previousValue: 'both', message: 'boom' })
  assert.equal(state.status, 'error')
  assert.equal(state.value, 'both')
  assert.equal(state.error, 'boom')
})

test('a failed save from inherit reverts to inherit (null)', () => {
  const state = directionSaveError({ previousValue: null, message: 'denied' })
  assert.equal(state.value, null)
})

test('chunkIds keeps attribute batches at or below the route limit', () => {
  assert.deepEqual(chunkIds([], 100), [])
  assert.deepEqual(chunkIds([1, 2, 3], 100), [[1, 2, 3]])
  assert.deepEqual(chunkIds(Array.from({ length: 250 }, (_, i) => i), 100), [
    Array.from({ length: 100 }, (_, i) => i),
    Array.from({ length: 100 }, (_, i) => i + 100),
    Array.from({ length: 50 }, (_, i) => i + 200),
  ])
})

test('chunkIds rejects a size that could never advance', () => {
  for (const size of [0, -1, 1.5, Number.NaN, '100']) {
    assert.throws(() => chunkIds([1, 2, 3], size), RangeError)
  }
})

test('verified Jira paths are built from numeric ids only', () => {
  assert.equal(schemaJiraPath('5'), '/secure/ObjectSchema.jspa?id=5')
  assert.equal(objectTypeJiraPath('5', '135'), '/secure/ObjectSchema.jspa?id=5&typeId=135')
  assert.equal(objectJiraPath('213273'), '/secure/ShowObject.jspa?id=213273')
})

test('a non-numeric Jira id yields no path so the caller renders plain text', () => {
  assert.equal(schemaJiraPath('ITSM-5'), null)
  assert.equal(schemaJiraPath(''), null)
  assert.equal(schemaJiraPath(null), null)
  assert.equal(objectTypeJiraPath('5', 'Business-Services'), null)
  assert.equal(objectTypeJiraPath(null, '135'), null)
  assert.equal(objectJiraPath('ABC-127'), null)
})

test('safeJiraLink resolves a verified path on the saved http(s) origin', () => {
  assert.equal(
    safeJiraLink('https://jira.example.com', '/secure/ShowObject.jspa?id=1'),
    'https://jira.example.com/secure/ShowObject.jspa?id=1'
  )
  // A Jira context path must be preserved, not dropped for the bare origin.
  assert.equal(
    safeJiraLink('https://host.example.com/jira', '/secure/ObjectSchema.jspa?id=5'),
    'https://host.example.com/jira/secure/ObjectSchema.jspa?id=5'
  )
  assert.equal(
    safeJiraLink('http://jira.internal:8080/jira/', '/secure/ShowObject.jspa?id=2'),
    'http://jira.internal:8080/jira/secure/ShowObject.jspa?id=2'
  )
})

test('safeJiraLink refuses unsafe schemes, credentials, origins and paths', () => {
  assert.equal(safeJiraLink('javascript:alert(1)', '/secure/ShowObject.jspa?id=1'), null)
  assert.equal(safeJiraLink('ftp://jira.example.com', '/secure/ShowObject.jspa?id=1'), null)
  assert.equal(safeJiraLink('https://user:pass@jira.example.com', '/secure/ShowObject.jspa?id=1'), null)
  assert.equal(safeJiraLink('not a url', '/secure/ShowObject.jspa?id=1'), null)
  assert.equal(safeJiraLink(null, '/secure/ShowObject.jspa?id=1'), null)
  assert.equal(safeJiraLink('https://jira.example.com', 'https://evil.example.com/x'), null)
  // A protocol-relative path is stripped to a same-origin path, never followed.
  assert.equal(
    safeJiraLink('https://jira.example.com', '//evil.example.com/x'),
    'https://jira.example.com/evil.example.com/x'
  )
  // An origin-relative path without a leading slash is never accepted.
  assert.equal(safeJiraLink('https://jira.example.com', 'secure/ShowObject.jspa?id=1'), null)
})
