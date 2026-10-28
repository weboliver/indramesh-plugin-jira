// Phase 295 (JUI-01): pure, host-free Jira task-form logic. `node --test` can
// load this because it imports no react/@indramesh/plugin-api/JSON (R-07).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildPullFormState,
  buildPushFormState,
  buildExportFormState,
  filterJiraSources,
  filterJiraTargets,
  filterEligibleAccounts,
  exportPreflightKey,
  exportPreflightState,
} from './task-form-logic.js'

test('pull defaults to the whole Source and reports the persisted account in scope', () => {
  const empty = buildPullFormState({})
  assert.deepEqual(empty.scope, { source_id: '', service_account_id: '' })
  assert.deepEqual(empty.plugin_parameters, { include_comments_files: false })
  assert.equal(empty.valid, false)

  const ready = buildPullFormState({
    sourceId: 'src-1',
    serviceAccountId: 'acc-1',
    connectionReady: true,
  })
  assert.deepEqual(ready.scope, { source_id: 'src-1', service_account_id: 'acc-1' })
  assert.equal(ready.valid, true)
  // D-01: no Jira schema selected means the whole Source.
  assert.equal('jira_schema_id' in ready.plugin_parameters, false)
  assert.equal(ready.plugin_parameters.include_comments_files, false)
})

test('pull includes jira_schema_id and the boolean comments flag only when set', () => {
  const scoped = buildPullFormState({
    sourceId: 'src-1',
    serviceAccountId: 'acc-1',
    jiraSchemaId: '42',
    includeCommentsFiles: true,
    connectionReady: true,
  })
  assert.equal(scoped.plugin_parameters.jira_schema_id, '42')
  assert.equal(scoped.plugin_parameters.include_comments_files, true)
  assert.equal(scoped.valid, true)
})

test('pull without an account stays invalid', () => {
  assert.equal(buildPullFormState({ sourceId: 'src-1' }).valid, false)
})

test('pull stays blocked until the Source connection is verified', () => {
  for (const connectionReady of [undefined, false]) {
    assert.equal(buildPullFormState({ sourceId: 's', serviceAccountId: 'a', connectionReady }).valid, false)
  }
})

test('push carries schema/target/account and never an object_type_id', () => {
  const state = buildPushFormState({ schemaId: 'sch-1', targetId: 'tgt-1', serviceAccountId: 'acc-1' })
  assert.deepEqual(state.scope, { schema_id: 'sch-1', target_id: 'tgt-1', service_account_id: 'acc-1' })
  assert.deepEqual(state.plugin_parameters, {})
  assert.equal(state.valid, true)
  assert.equal('object_type_id' in state.plugin_parameters, false)
  assert.equal('object_type_id' in state.scope, false)

  assert.equal(buildPushFormState({ schemaId: 'sch-1', targetId: 'tgt-1' }).valid, false)
})

test('export stays invalid until all four are chosen and the destination is proven empty', () => {
  const partial = buildExportFormState({ schemaId: 'sch-1', targetId: 'tgt-1', serviceAccountId: 'acc-1', jiraSchemaId: '42' })
  assert.equal(partial.valid, false)
  assert.equal(partial.plugin_parameters.jira_schema_id, '42')

  const ready = buildExportFormState({ schemaId: 'sch-1', targetId: 'tgt-1', serviceAccountId: 'acc-1', jiraSchemaId: '42', destinationEmpty: true })
  assert.equal(ready.valid, true)
  assert.deepEqual(ready.scope, { schema_id: 'sch-1', target_id: 'tgt-1', service_account_id: 'acc-1' })
})

test('filterJiraSources keeps only jira Sources', () => {
  const result = filterJiraSources([
    { id: 'a', type: 'jira' },
    { id: 'b', type: 'internal' },
    null,
    { id: 'c', type: 'jira' },
  ])
  assert.deepEqual(result.map((s) => s.id), ['a', 'c'])
})

test('filterJiraTargets keeps active jira Targets bound to nothing or the chosen schema', () => {
  const schemaId = 'sch-1'
  const targets = [
    { id: 'ok-unbound', connector_type: 'jira', is_active: true, schema_id: null },
    { id: 'ok-same', connector_type: 'jira', is_active: true, schema_id: 'sch-1' },
    { id: 'other-schema', connector_type: 'jira', is_active: true, schema_id: 'sch-2' },
    { id: 'inactive', connector_type: 'jira', is_active: false, schema_id: null },
    { id: 'csv', connector_type: 'csv', is_active: true, schema_id: null },
  ]
  assert.deepEqual(filterJiraTargets(targets, schemaId).map((t) => t.id), ['ok-unbound', 'ok-same'])
})

test('filterJiraTargets without a schema only drops non-jira/inactive targets', () => {
  const targets = [
    { id: 'ok', connector_type: 'jira', is_active: true, schema_id: 'anything' },
    { id: 'inactive', connector_type: 'jira', is_active: false, schema_id: null },
  ]
  assert.deepEqual(filterJiraTargets(targets, '').map((t) => t.id), ['ok'])
})

test('filterEligibleAccounts drops inactive and expired accounts', () => {
  const now = Date.parse('2026-09-30T12:00:00Z')
  const accounts = [
    { id: 'active', is_active: true, expires_at: null },
    { id: 'future', is_active: true, expires_at: '2026-12-01T00:00:00Z' },
    { id: 'expired', is_active: true, expires_at: '2026-01-01T00:00:00Z' },
    { id: 'inactive', is_active: false, expires_at: null },
  ]
  assert.deepEqual(filterEligibleAccounts(accounts, now).map((a) => a.id), ['active', 'future'])
})

test('exportPreflightKey is empty until target, destination and account are all chosen', () => {
  assert.equal(exportPreflightKey({}), '')
  assert.equal(exportPreflightKey({ targetId: 't', jiraSchemaId: '42' }), '')
  assert.equal(
    exportPreflightKey({ targetId: 't', jiraSchemaId: '42', serviceAccountId: 'a' }),
    't:42:a'
  )
})

test('exportPreflightState fails closed on empty, failed, loading and stale replies', () => {
  assert.deepEqual(exportPreflightState({}), { status: 'idle', count: null, empty: false })

  assert.equal(
    exportPreflightState({ currentKey: 'k', requestKey: 'k', data: { object_type_count: 3, empty: false } }).status,
    'not_empty'
  )
  assert.equal(
    exportPreflightState({ currentKey: 'k', requestKey: 'k', data: { object_type_count: 0, empty: true } }).empty,
    true
  )
  assert.equal(
    exportPreflightState({ currentKey: 'k', requestKey: 'k', error: new Error('502') }).status,
    'failed'
  )
  assert.equal(
    exportPreflightState({ currentKey: 'k', requestKey: 'k', loading: true }).status,
    'loading'
  )

  // An out-of-order reply for a previous selection must never unlock the form.
  const stale = exportPreflightState({
    currentKey: 'new',
    requestKey: 'old',
    data: { object_type_count: 0, empty: true },
  })
  assert.equal(stale.status, 'loading')
  assert.equal(stale.empty, false)
})

test('carries an adopted target schema only together with one Jira schema (261004)', () => {
  const adopted = buildPullFormState({
    sourceId: 'src-1',
    serviceAccountId: 'sa-1',
    jiraSchemaId: '7',
    targetSchemaId: 'schema-1',
    connectionReady: true,
  })
  assert.equal(adopted.plugin_parameters.target_schema_id, 'schema-1')
  assert.equal(adopted.plugin_parameters.jira_schema_id, '7')
  const noJira = buildPullFormState({ sourceId: 'src-1', targetSchemaId: 'schema-1' })
  assert.equal('target_schema_id' in noJira.plugin_parameters, false)
})
