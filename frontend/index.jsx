// Jira plugin frontend bundle descriptor (Phase 295, JUI-01/JUI-07).
//
// The host loads this default export through the runtime loader and validates
// it against `[frontend].extension_points` in plugin.toml (bidirectional). Only
// the families this bundle actually registers are declared there. Phase 296
// removes the transitional Core Jira UI; until then this bundle is additive.
//
// Jira-visible copy ships in the plugin's own locale files (FEEXT-04/D9) — no
// Jira string is added to the Core `systemui` catalogs.
import { ExportForm, PullForm, PushForm } from './TaskForms.jsx'
import JiraConnectionEditor, { afterCoreSaveJiraConnection } from './JiraConnectionEditor.jsx'
import { JiraObjectSection, JiraObjectTypeSection, JiraSchemaSection } from './JiraSections.jsx'
import JiraConflictsPage, {
  jiraConflictBadgeLabel,
  useJiraConflictCount,
} from './JiraConflictsPage.jsx'
import {
  resolveJiraAttributeBadges,
  resolveJiraMediaBadges,
  resolveJiraObjectBadges,
} from './JiraBadges.jsx'
import en from './locales/en.json'
import de from './locales/de.json'
import es from './locales/es.json'
import fr from './locales/fr.json'
import pt from './locales/pt.json'

export default {
  translations: { en, de, es, fr, pt },
  // S7/D-13: the conflicts page and its nav entry. Pages carry no host
  // permission gate, so the page component self-gates on `conflicts:read`
  // (useCan); the nav item declares the same permission so its entry is hidden
  // (not disabled) for a caller without it. The badge counts only via the
  // record-gated count route with a 30s stale window and no polling (D-04/D-05).
  pages: [
    {
      id: 'jira-conflicts',
      path: 'conflicts',
      component: JiraConflictsPage,
    },
  ],
  navItems: [
    {
      id: 'jira-conflicts',
      label: 'Jira conflicts',
      labelKey: 'conflicts.nav.label',
      path: '/plugins/jira/conflicts',
      icon: 'git-compare',
      group: 'Integration',
      permission: 'conflicts:read',
      badge: useJiraConflictCount,
      // S7/D-07: the pill's accessible name is plugin copy (one/other/overflow),
      // never the host's generic "N items".
      badgeLabel: jiraConflictBadgeLabel,
    },
  ],
  taskForms: [
    {
      id: 'jira-task-form-pull',
      tasktype: 'jira.sync-pull',
      component: PullForm,
    },
    {
      id: 'jira-task-form-push',
      tasktype: 'jira.sync-push',
      component: PushForm,
    },
    {
      id: 'jira-task-form-export',
      tasktype: 'jira.sync-export',
      component: ExportForm,
    },
  ],
  connectorConfigEditors: [
    {
      id: 'jira-connector-editor',
      connector_type: 'jira',
      label: 'Jira',
      roles: ['source', 'target'],
      component: JiraConnectionEditor,
      // R2-01/R2-03: bind/update the plugin-owned connection after the Core
      // Source/Target is saved, and never present a false full success.
      afterCoreSave: afterCoreSaveJiraConnection,
    },
  ],
  schemaSettingsSections: [
    {
      id: 'jira-schema-sync',
      label: 'Jira sync',
      labelKey: 'section.schema.title',
      permission: 'sync:read',
      component: JiraSchemaSection,
    },
  ],
  objectTypeSettingsSections: [
    {
      id: 'jira-object-type-sync',
      label: 'Jira sync',
      labelKey: 'section.schema.title',
      permission: 'sync:read',
      component: JiraObjectTypeSection,
    },
  ],
  objectDetailSections: [
    {
      id: 'jira-object-sync',
      label: 'Jira',
      permission: 'sync:read',
      component: JiraObjectSection,
    },
  ],
  itemBadges: [
    {
      // S6/RBAC: comment/file origin has NO permission gate — provenance is a
      // read-only fact of an item the caller can already see. Resolved from the
      // host context only; it never issues a `sync:read` link request.
      id: 'jira-media-badges',
      kinds: ['comment', 'file'],
      resolve: resolveJiraMediaBadges,
    },
    {
      // S6: object origin is a permissioned link fact; only a `sync:read`
      // holder gets it. One bounded batch per rendered list.
      id: 'jira-object-badges',
      kinds: ['object'],
      permission: 'sync:read',
      resolve: resolveJiraObjectBadges,
    },
    {
      // S6: attribute pull-only hint, permissioned and shown only while the
      // object is linked and attached.
      id: 'jira-attribute-badges',
      kinds: ['attribute'],
      permission: 'sync:read',
      resolve: resolveJiraAttributeBadges,
    },
  ],
}
