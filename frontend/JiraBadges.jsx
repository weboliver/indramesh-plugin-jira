// Jira `itemBadges` resolver module (Phase 295, JUI-03/04/07, UI-SPEC S6).
//
// Two data sources, deliberately separated (D-12):
//  * comment/file origin is resolved PURELY from the sanitized host context
//    (`badge-logic.js`) — no request, so every item reader sees Jira origin
//    without a `sync:read` link GET (T-295-04-E).
//  * object/attribute badges read the record-gated `/links/{entity}` batch
//    routes. Their descriptor entries declare `permission: 'sync:read'`, so the
//    host never calls them for a caller without it; the server enforces the
//    per-record gate as well (T-295-04-I).
import { api, serializeRepeatedParams } from '@indramesh/plugin-api'
import {
  attributeBadgeForLink,
  objectBadgeForLink,
  planLinkBatches,
  resolveJiraMediaBadges,
} from './badge-logic.js'

// One bounded batch per link family: the route caps a request at 100 ids, so a
// larger list is split (never one HTTP request per row, T-295-04-D).
async function fetchLinkItems(entity, ids) {
  const items = []
  for (const chunk of planLinkBatches(ids)) {
    const qs = serializeRepeatedParams({ ids: chunk })
    const response = await api.get(`plugins/jira/links/${entity}?${qs}`)
    items.push(...(response.data?.items || []))
  }
  return items
}

export async function resolveJiraObjectBadges({ ids }) {
  const items = await fetchLinkItems('object', ids)
  const map = new Map()
  for (const item of items) {
    const badge = objectBadgeForLink(item)
    if (badge) map.set(item.id, [badge])
  }
  return map
}

// S6: an attribute shows pull-only ONLY while its object is linked and attached.
// The object's record-gated attached state is read first, and the attribute
// batch is skipped entirely when the object is detached or unlinked.
export async function resolveJiraAttributeBadges({ ids, context }) {
  const objectId = context?.objectId
  if (!objectId) return new Map()
  const objectItems = await fetchLinkItems('object', [objectId])
  const objectItem = objectItems.find((item) => item.id === objectId)
  if (!objectItem?.linked || objectItem.is_detached) return new Map()
  const attributeItems = await fetchLinkItems('attribute', ids)
  const map = new Map()
  for (const item of attributeItems) {
    const badge = attributeBadgeForLink(item)
    if (badge) map.set(item.id, [badge])
  }
  return map
}

export { resolveJiraMediaBadges }
