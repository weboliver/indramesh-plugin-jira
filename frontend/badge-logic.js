// Phase 295 (JUI-04, D-12): pure, host-free Jira item-badge logic.
//
// This module MUST NOT import `react` or `@indramesh/plugin-api` — the host
// import map cannot be resolved by `node --test`, so every resolver predicate
// and descriptor lives here (mirroring direction-logic.js) while the
// React/plugin-api wiring lives in JiraBadges.jsx. Host pills are neutral and
// host-drawn; this module returns data only (T-295-04-S).
import { chunkIds } from './direction-logic.js'

// D-12/T-295-04-S: provenance is matched EXACTLY. `managed_by` is a free-form
// lowercase connector name; a generic `is_external` flag would also match other
// connectors and must never be presented as a Jira-owned item.
export const JIRA_MANAGED_BY = 'jira'

export function isJiraOwned(managedBy) {
  return managedBy === JIRA_MANAGED_BY
}

// S6: the single comment/file origin pill. Labels/titles resolve through the
// plugin's own locale namespace (FEEXT-04); the literal values are the English
// fallback the host uses until the plugin catalog supplies the key.
const MEDIA_BADGE = {
  label: 'Jira',
  labelKey: 'badge.media.label',
  title: 'Imported from Jira. Read-only here.',
  titleKey: 'badge.media.title',
  icon: 'link',
}

// Pure predicate: an already-read item DTO (or its managed_by value) yields the
// one neutral Jira origin badge, or null for native/other-connector items.
export function jiraBadgeForItem(managedBy) {
  return isJiraOwned(managedBy) ? { ...MEDIA_BADGE } : null
}

// The comment/file `itemBadges` resolver. It reads provenance ONLY from the
// sanitized, already-loaded host context (`itemsById[id] = managed_by`, R4-03)
// and issues NO request — so an Editor without `sync:read` still sees Jira
// origin without consuming a privileged link route (T-295-04-E).
export function resolveJiraMediaBadges({ ids, context } = {}) {
  const itemsById = context?.itemsById || {}
  const map = new Map()
  for (const id of ids || []) {
    const badge = jiraBadgeForItem(itemsById[id])
    if (badge) map.set(id, [badge])
  }
  return map
}

// ---------------------------------------------------------------------------
// Permission-gated object/attribute link badges (S6/RBAC, T-295-04-D).
//
// The route rejects a batch above 100 ids (routes.py MAX_BATCH_IDS), so a list
// is read in bounded chunks — never one request per row.

export const LINK_BATCH_SIZE = 100

export function planLinkBatches(ids, size = LINK_BATCH_SIZE) {
  return chunkIds((ids || []).filter(Boolean), size)
}

// An object link item (routes.py /links/object) -> the S6 object badge, or null
// when the object is unlinked/denied (reported as `linked: false`).
export function objectBadgeForLink(item) {
  if (!item?.linked || !item.jira_id) return null
  const jiraId = String(item.jira_id)
  // The list pill stays a short "Jira"; the pulled "Jira Key" and the numeric
  // id go into the tooltip (the object section shows the key itself).
  const jiraKey = item.jira_key ? String(item.jira_key) : null
  const jiraRef = jiraKey ? `${jiraKey} (id ${jiraId})` : jiraId
  if (item.is_detached) {
    return {
      label: 'Detached',
      labelKey: 'badge.object.detached.label',
      // The Jira id is runtime data; the host pill cannot interpolate a locale
      // key, so the id stays in the (English) tooltip/accessible name.
      title: `Detached from Jira object ${jiraRef}. Syncs skip this object.`,
      icon: 'unlink',
    }
  }
  return {
    label: 'Jira',
    labelKey: 'badge.object.linked.label',
    title: `Linked to Jira object ${jiraRef}.`,
    icon: 'link',
  }
}

// An attribute link item (routes.py /links/attribute) -> the S6 pull-only
// badge, or null unless the link exists AND its effective direction is pull.
export function attributeBadgeForLink(item) {
  if (!item?.linked || item.effective_direction !== 'pull') return null
  return {
    label: 'Pull only',
    labelKey: 'badge.attribute.pull_only.label',
    title: 'Changes only through a Jira pull. Detach the object to edit it here.',
    titleKey: 'badge.attribute.pull_only.title',
  }
}
