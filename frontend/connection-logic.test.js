// Phase 295 (JUI-02, R2-03/R2-04/R2-06): host-free unit tests for the Jira
// connection editor's pure decisions. The module and these tests import neither
// `react` nor `@indramesh/plugin-api`, so `node --test` runs them without a
// bundler (R-07).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  CONNECTION_TEST_CATEGORIES,
  CONFLICT_STRATEGIES,
  canRunConnectionTest,
  connectionTestKey,
  decideConnectionWrite,
  defaultAccountSelection,
  hasUnsavedUrlChange,
  selectValueToStrategy,
  strategyToSelectValue,
} from './connection-logic.js'

test('exposes the closed strategy and category vocabularies the routes accept', () => {
  assert.deepEqual(CONFLICT_STRATEGIES, ['prefer_local', 'prefer_remote'])
  assert.deepEqual(CONNECTION_TEST_CATEGORIES, [
    'ok',
    'auth',
    'network',
    'permission',
    'not_found',
    'error',
  ])
})

test('maps the strategy select value to and from the wire value', () => {
  assert.equal(selectValueToStrategy(''), null)
  assert.equal(selectValueToStrategy('prefer_local'), 'prefer_local')
  assert.equal(selectValueToStrategy('prefer_remote'), 'prefer_remote')
  assert.equal(selectValueToStrategy('manual'), null)
  assert.equal(strategyToSelectValue(null), '')
  assert.equal(strategyToSelectValue(undefined), '')
  assert.equal(strategyToSelectValue('prefer_local'), 'prefer_local')
  assert.equal(strategyToSelectValue('prefer_remote'), 'prefer_remote')
})

test('does not write while the connection GET has not resolved or failed (R2-03)', () => {
  assert.deepEqual(
    decideConnectionWrite({ loaded: false, connection: null, strategy: 'prefer_local' }),
    { action: 'skip' }
  )
  assert.deepEqual(
    decideConnectionWrite({
      loaded: true,
      connection: null,
      strategy: 'prefer_remote',
      fetchFailed: true,
    }),
    { action: 'skip' }
  )
})

test('binds an unbound endpoint with the chosen strategy, including manual (null)', () => {
  assert.deepEqual(decideConnectionWrite({ loaded: true, connection: null, strategy: null }), {
    action: 'write',
    body: { default_conflict_strategy: null },
  })
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: null, strategy: 'prefer_local' }),
    { action: 'write', body: { default_conflict_strategy: 'prefer_local' } }
  )
})

test('skips an unchanged live binding and writes only a changed strategy (R2-03)', () => {
  const live = { binding: 'live', default_conflict_strategy: 'prefer_local' }
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: 'prefer_local' }),
    { action: 'skip' }
  )
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: 'prefer_remote' }),
    { action: 'write', body: { default_conflict_strategy: 'prefer_remote' } }
  )
  // A live binding whose stored strategy is null is only rewritten when changed.
  const liveNull = { binding: 'live', default_conflict_strategy: null }
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: liveNull, strategy: null }),
    { action: 'skip' }
  )
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: liveNull, strategy: 'prefer_local' }),
    { action: 'write', body: { default_conflict_strategy: 'prefer_local' } }
  )
})

test('never writes to a stale binding and never rebinds it (R2-04)', () => {
  const stale = { binding: 'stale', default_conflict_strategy: 'prefer_local' }
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: stale, strategy: 'prefer_remote' }),
    { action: 'skip' }
  )
})

test('never writes when the Core URL changed in this save (the binding is becoming stale)', () => {
  const live = { binding: 'live', default_conflict_strategy: 'prefer_local' }
  assert.deepEqual(
    decideConnectionWrite({
      loaded: true,
      connection: live,
      strategy: 'prefer_remote',
      urlChanged: true,
    }),
    { action: 'skip' }
  )
})

test('detects an unsaved URL edit against the saved record URL (R2-05)', () => {
  assert.equal(hasUnsavedUrlChange({ draftUrl: ' https://a.example ', savedUrl: 'https://a.example' }), false)
  assert.equal(hasUnsavedUrlChange({ draftUrl: '', savedUrl: '' }), false)
  assert.equal(hasUnsavedUrlChange({ draftUrl: 'https://b.example', savedUrl: 'https://a.example' }), true)
  assert.equal(hasUnsavedUrlChange({ draftUrl: 'https://a.example', savedUrl: '' }), true)
  assert.equal(hasUnsavedUrlChange({ draftUrl: null, savedUrl: 'https://a.example' }), true)
})

test('maps every server test category to its own key and never falls off the vocabulary', () => {
  const seen = CONNECTION_TEST_CATEGORIES.map((category) => connectionTestKey(category))
  assert.deepEqual(seen, [
    'connector.result_ok',
    'connector.result_auth',
    'connector.result_network',
    'connector.result_permission',
    'connector.result_not_found',
    'connector.result_error',
  ])
  assert.equal(connectionTestKey('unknown'), 'connector.result_error')
})

test('runs the test only for a saved record with an eligible account and no request in flight (D-05/D-07)', () => {
  assert.equal(canRunConnectionTest({ recordId: 'id', serviceAccountId: 'acct' }), true)
  assert.equal(canRunConnectionTest({ recordId: null, serviceAccountId: 'acct' }), false)
  assert.equal(canRunConnectionTest({ recordId: 'id', serviceAccountId: '' }), false)
  assert.equal(
    canRunConnectionTest({ recordId: 'id', serviceAccountId: 'acct', testing: true }),
    false
  )
})

test('writes the default service account only when it is offered and changed (261004)', () => {
  const live = {
    binding: 'live',
    default_conflict_strategy: null,
    default_service_account_id: 'sa-1',
  }
  // Never offered: the body omits the field and an unchanged strategy skips.
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: null }),
    { action: 'skip' }
  )
  // Same account: skip.
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: null, serviceAccountId: 'sa-1' }),
    { action: 'skip' }
  )
  // Changed account: write strategy + account.
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: null, serviceAccountId: 'sa-2' }),
    { action: 'write', body: { default_conflict_strategy: null, default_service_account_id: 'sa-2' } }
  )
  // Cleared choice: write null.
  assert.deepEqual(
    decideConnectionWrite({ loaded: true, connection: live, strategy: null, serviceAccountId: '' }),
    { action: 'write', body: { default_conflict_strategy: null, default_service_account_id: null } }
  )
  // A stale binding still never writes.
  assert.deepEqual(
    decideConnectionWrite({
      loaded: true,
      connection: { ...live, binding: 'stale' },
      strategy: null,
      serviceAccountId: 'sa-2',
    }),
    { action: 'skip' }
  )
})

test('preselects the stored default account only while it is still eligible (261004)', () => {
  const accounts = [{ id: 'sa-1' }, { id: 'sa-2' }]
  assert.equal(defaultAccountSelection('sa-2', accounts), 'sa-2')
  assert.equal(defaultAccountSelection('sa-9', accounts), '')
  assert.equal(defaultAccountSelection(null, accounts), '')
  // Without a usable default the single eligible account is preselected.
  assert.equal(defaultAccountSelection(null, [{ id: 'sa-1' }]), 'sa-1')
  assert.equal(defaultAccountSelection('sa-9', [{ id: 'sa-1' }]), 'sa-1')
  assert.equal(defaultAccountSelection(null, []), '')
})
