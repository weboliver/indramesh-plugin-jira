// Phase 295 (JUI-04, D-12): pure, host-free Jira item-badge logic. This module
// must not import `react` or `@indramesh/plugin-api`, so `node --test` can run
// it without the host import map (mirrors direction-logic.test.js).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  JIRA_MANAGED_BY,
  attributeBadgeForLink,
  isJiraOwned,
  jiraBadgeForItem,
  objectBadgeForLink,
  planLinkBatches,
  resolveJiraMediaBadges,
} from './badge-logic.js'

test('only an exact lowercase jira managed_by is treated as Jira-owned', () => {
  assert.equal(JIRA_MANAGED_BY, 'jira')
  assert.equal(isJiraOwned('jira'), true)
  assert.equal(isJiraOwned('Jira'), false)
  assert.equal(isJiraOwned('confluence'), false)
  assert.equal(isJiraOwned(null), false)
  assert.equal(isJiraOwned(undefined), false)
})

test('a Jira-managed item yields one neutral Jira origin badge', () => {
  const badge = jiraBadgeForItem('jira')
  assert.equal(badge.label, 'Jira')
  assert.equal(badge.labelKey, 'badge.media.label')
  assert.equal(badge.icon, 'link')
  assert.ok(badge.title.length > 0)
})

test('a native or foreign-managed item yields no Jira badge', () => {
  assert.equal(jiraBadgeForItem(null), null)
  assert.equal(jiraBadgeForItem('confluence'), null)
})

test('the media resolver maps only Jira-owned ids from the sanitized context', () => {
  const map = resolveJiraMediaBadges({
    ids: ['c1', 'c2', 'c3'],
    context: { objectId: 'o1', itemsById: { c1: 'jira', c2: null, c3: 'confluence' } },
  })
  assert.deepEqual([...map.keys()], ['c1'])
  assert.equal(map.get('c1')[0].labelKey, 'badge.media.label')
})

test('the media resolver issues no request and returns nothing without provenance', () => {
  const map = resolveJiraMediaBadges({ ids: ['c1'], context: { objectId: 'o1' } })
  assert.equal(map.size, 0)
})

test('link batches cap each request at 100 ids and drop empty entries', () => {
  assert.deepEqual(planLinkBatches([], 100), [])
  assert.deepEqual(planLinkBatches(['a', null, 'b']), [['a', 'b']])
  const chunks = planLinkBatches(Array.from({ length: 101 }, (_, i) => `id-${i}`))
  assert.equal(chunks.length, 2)
  assert.equal(chunks[0].length, 100)
  assert.equal(chunks[1].length, 1)
})

test('an unlinked or absent object link item yields no object badge', () => {
  assert.equal(objectBadgeForLink({ id: 'o1', linked: false, jira_id: null }), null)
  assert.equal(objectBadgeForLink({ id: 'o1', linked: true, jira_id: null }), null)
  assert.equal(objectBadgeForLink(null), null)
})

test('a linked, attached object links to its Jira id', () => {
  const badge = objectBadgeForLink({ id: 'o1', linked: true, jira_id: '55', is_detached: false })
  assert.equal(badge.label, 'Jira')
  assert.equal(badge.icon, 'link')
  assert.match(badge.title, /55/)
})

test('a linked object with a pulled Jira key shows the key, id in the tooltip', () => {
  const badge = objectBadgeForLink({
    id: 'o1', linked: true, jira_id: '55', jira_key: 'ITSM-55', is_detached: false,
  })
  assert.equal(badge.label, 'Jira')
  assert.equal(badge.title, 'Linked to Jira object ITSM-55 (id 55).')
})

test('a linked, detached object shows the detached badge', () => {
  const badge = objectBadgeForLink({ id: 'o1', linked: true, jira_id: '55', is_detached: true })
  assert.equal(badge.label, 'Detached')
  assert.equal(badge.labelKey, 'badge.object.detached.label')
  assert.equal(badge.icon, 'unlink')
})

test('only a linked, pull-effective attribute yields the pull-only badge', () => {
  const pull = attributeBadgeForLink({ linked: true, effective_direction: 'pull' })
  assert.equal(pull.label, 'Pull only')
  assert.equal(pull.labelKey, 'badge.attribute.pull_only.label')
  assert.equal(attributeBadgeForLink({ linked: true, effective_direction: 'both' }), null)
  assert.equal(attributeBadgeForLink({ linked: true, effective_direction: null }), null)
  assert.equal(attributeBadgeForLink({ linked: false, effective_direction: 'pull' }), null)
})
