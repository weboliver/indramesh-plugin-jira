// Jira Conflicts page (Phase 295, JUI-05, D-13..D-15, UI-SPEC S7).
//
// The plugin owns this page; the host mounts it through the `pages` family at
// `/plugins/jira/conflicts` and draws no frame of its own. Pages carry no host
// permission gate, so the page SELF-gates on `conflicts:read` (useCan) exactly
// like the nav item's `permission` hides the entry. The server stays the
// authority: every list/count/name/resolve call is record-gated, and the page
// never treats visibility as authorization.
//
// Task 1 delivered the pending list, the two-sided comparison detail, the
// endpoint-bound account selector and the equal-weight resolution actions.
// Task 2 adds the URL filters (D-13/D-15), the status vocabulary, the
// empty/no-results/loading/error states, the server-owned pagination, the
// keyboard/aria contract, and the 409/502 recovery paths.
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Button,
  EmptyState,
  PageHeader,
  SelectInput,
  Table,
  TableSkeleton,
  api,
  extractErrorMessage,
  interpolateDefault,
  pluginText,
  serializeRepeatedParams,
  showToast,
  useCan,
  useFormatDate,
  usePluginTranslation,
  useSchemas,
  useServiceAccounts,
} from '@indramesh/plugin-api'
import {
  STATUS_LABEL_KEYS,
  compareSnapshots,
  filterQueryParams,
  filtersFromSearch,
  isDateDataType,
  mergeConflictPages,
  referenceIds,
  searchFromFilters,
} from './conflict-logic.js'

const STATUS_OPTIONS = [
  'pending',
  'resolved_keep_local',
  'resolved_keep_remote',
  'resolved_auto',
]

// --- data hooks -------------------------------------------------------------

async function fetchConflictPage(filters, offset) {
  const { data } = await api.get('plugins/jira/conflicts', {
    params: filterQueryParams(filters, offset),
  })
  return data
}

async function fetchObjectNames(ids) {
  if (!ids.length) return new Map()
  const qs = serializeRepeatedParams({ ids })
  const { data } = await api.get(`plugins/jira/conflicts/object-names?${qs}`)
  const map = new Map()
  ;(data?.items || []).forEach((item) => map.set(item.id, item.name))
  return map
}

// One batch read per opened detail; missing/denied ids fall back to the Jira
// object id (R5-03), never a per-item request.
function useObjectNames(conflict) {
  const ids = useMemo(() => {
    const unique = new Set(
      referenceIds(conflict.local).concat(referenceIds(conflict.remote))
    )
    return [...unique]
  }, [conflict])
  return useQuery({
    queryKey: ['jira-conflict-names', conflict.id, ids],
    queryFn: () => fetchObjectNames(ids),
    enabled: ids.length > 0,
  })
}

function formatScalar(value, dataType, t, formatDate) {
  if (value === null || value === undefined || value === '') {
    return t('conflicts.no_value', 'No value')
  }
  if (typeof value === 'boolean') {
    return value
      ? t('conflicts.value.yes', 'Yes')
      : t('conflicts.value.no', 'No')
  }
  if (isDateDataType(dataType)) return formatDate(value)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function resolutionErrorMessage(error, t) {
  const status = error?.response?.status
  const detail = error?.response?.data?.detail
  if (status === 409) {
    if (detail?.error?.code === 'CONFLICT_DIRECTION_NOT_BOTH') {
      return t(
        'conflicts.error.direction',
        "This attribute no longer syncs both ways, so the conflict can't be resolved here. Set its direction to Both directions first."
      )
    }
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail || '')
    if (/link|binding/i.test(text)) {
      return t(
        'conflicts.error.no_link',
        "This object isn't linked to Jira anymore, or its link is out of date. Run a pull for its Source, then review the conflict again."
      )
    }
    return t(
      'conflicts.error.stale_version',
      'Someone else changed this conflict. The list is refreshed, so review it again.'
    )
  }
  if (status === 502) {
    return interpolateDefault(
      t(
        'conflicts.error.jira',
        "Jira didn't accept the change: {{message}}. The conflict stays open."
      ),
      { message: extractErrorMessage(error) }
    )
  }
  return interpolateDefault(
    t('conflicts.error.generic', 'The conflict could not be resolved: {{message}}.'),
    { message: extractErrorMessage(error) }
  )
}

// --- sub-components ---------------------------------------------------------

function ComparisonItem({ item, side, names, dataType, t, formatDate }) {
  const fallback = interpolateDefault(
    t('conflicts.reference_fallback', 'Jira object {{id}}'),
    { id: item.jiraObjectId || item.objectId || '' }
  )
  const label =
    item.kind === 'reference'
      ? (item.objectId && names.get(item.objectId)) || fallback
      : formatScalar(item.value, dataType, t, formatDate)
  const tag =
    side === 'left'
      ? t('conflicts.item.not_in_jira', 'Not in Jira')
      : t('conflicts.item.not_in_indramesh', 'Not in IndraMesh')

  return (
    <li className="space-y-0.5">
      {item.shared ? (
        <span className="whitespace-pre-wrap break-words text-muted-foreground">
          {label}
        </span>
      ) : (
        <span className="inline-block whitespace-pre-wrap break-words rounded-sm bg-muted px-2 py-1 text-foreground">
          {label}
        </span>
      )}
      {!item.shared && <p className="text-xs text-muted-foreground">{tag}</p>}
    </li>
  )
}

function DetailPanel({ conflict, canManage, onResolved, t, formatDate }) {
  const names = useObjectNames(conflict)
  const nameMap = names.data || new Map()
  const comparison = useMemo(
    () => compareSnapshots(conflict.local, conflict.remote),
    [conflict]
  )
  const accounts = useServiceAccounts(conflict.source_id, 0, 50, conflict.target_id)
  const accountOptions = (accounts.data || []).map((account) => ({
    value: account.id,
    label: account.name || account.username || account.id,
  }))
  const [accountId, setAccountId] = useState(
    accountOptions.length === 1 ? accountOptions[0].value : ''
  )
  // The eligible accounts load asynchronously, so the initializer above runs
  // before they arrive. Default-select the only bound account once it is known
  // (never overwrite a choice the user already made).
  const singleAccountId = accountOptions.length === 1 ? accountOptions[0].value : ''
  useEffect(() => {
    if (!accountId && singleAccountId) setAccountId(singleAccountId)
  }, [accountId, singleAccountId])
  const [pendingAction, setPendingAction] = useState(null)
  const [error, setError] = useState(null)
  const queryClient = useQueryClient()

  const resolve = useMutation({
    mutationFn: (action) =>
      api.post(`plugins/jira/conflicts/${conflict.id}/resolve`, {
        resolution: action,
        version: conflict.version,
        service_account_id: accountId,
      }),
    onSuccess: (_data, action) => {
      queryClient.invalidateQueries({ queryKey: ['jira-conflicts'] })
      queryClient.invalidateQueries({ queryKey: ['jira-conflict-count'] })
      queryClient.invalidateQueries({ queryKey: ['jira-conflicts-schemas'] })
      showToast(
        action === 'keep_local'
          ? interpolateDefault(
              t(
                'conflicts.toast.keep_local',
                'Kept IndraMesh values for {{attribute}} on “{{object}}”.'
              ),
              { attribute: conflict.attribute_name, object: conflict.object_name }
            )
          : interpolateDefault(
              t(
                'conflicts.toast.keep_remote',
                'Kept Jira values for {{attribute}} on “{{object}}”.'
              ),
              { attribute: conflict.attribute_name, object: conflict.object_name }
            )
      )
      onResolved()
    },
    onError: (requestError) => {
      const message = resolutionErrorMessage(requestError, t)
      if (requestError?.response?.status === 409) {
        // Refresh + collapse so the user reviews the changed row again; no
        // optimistic resolution is ever claimed.
        queryClient.invalidateQueries({ queryKey: ['jira-conflicts'] })
        queryClient.invalidateQueries({ queryKey: ['jira-conflict-count'] })
        showToast(message)
        onResolved()
        return
      }
      // 502 and other failures leave the pending row and the detail open.
      setError(message)
    },
    onSettled: () => setPendingAction(null),
  })

  const singleValue = conflict.local?.cardinality === 'single'
  const summary = singleValue
    ? t('conflicts.detail.summary_plain', 'IndraMesh and Jira have different values.')
    : comparison.differCount === 1
      ? interpolateDefault(
          t(
            'conflicts.detail.summary_one',
            '{{count}} value differs between IndraMesh and Jira.'
          ),
          { count: 1 }
        )
      : interpolateDefault(
          t(
            'conflicts.detail.summary_other',
            '{{count}} values differ between IndraMesh and Jira.'
          ),
          { count: comparison.differCount }
        )

  const columns = [
    {
      side: 'left',
      title: t('conflicts.detail.left', 'In IndraMesh ({{count}})'),
      items: comparison.left,
      dataType: conflict.local?.data_type,
    },
    {
      side: 'right',
      title: t('conflicts.detail.right', 'In Jira ({{count}})'),
      items: comparison.right,
      dataType: conflict.remote?.data_type,
    },
  ]

  const resolvedKey = {
    resolved_keep_local: 'conflicts.resolved.keep_local',
    resolved_keep_remote: 'conflicts.resolved.keep_remote',
    resolved_auto: 'conflicts.resolved.auto',
  }[conflict.status]

  return (
    <div className="space-y-4" data-testid={`jira-conflicts-detail-${conflict.id}`}>
      <p className="text-sm">{summary}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        {columns.map((column) => (
          <div key={column.side}>
            <h2 className="text-sm font-semibold">
              {interpolateDefault(column.title, { count: column.items.length })}
            </h2>
            {column.items.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">
                {t('conflicts.no_value', 'No value')}
              </p>
            ) : (
              <ul className="mt-2 max-h-80 space-y-2 overflow-y-auto">
                {column.items.map((item, index) => (
                  <ComparisonItem
                    key={`${item.key}-${index}`}
                    item={item}
                    side={column.side}
                    names={nameMap}
                    dataType={column.dataType}
                    t={t}
                    formatDate={formatDate}
                  />
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>

      {conflict.status === 'pending' && canManage && (
        <div className="space-y-2">
          <SelectInput
            id={`jira-conflicts-account-${conflict.id}`}
            data-testid={`jira-conflicts-account-${conflict.id}`}
            aria-label={t('conflicts.account.label', 'Service account')}
            value={accountId}
            disabled={accountOptions.length === 0}
            placeholder={t('conflicts.account.placeholder', 'Select a service account…')}
            options={accountOptions}
            onChange={(event) => setAccountId(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {accountOptions.length === 0
              ? interpolateDefault(
                  t(
                    'conflicts.account.none_help',
                    'This conflict has no eligible service account yet. Add one under Service accounts.'
                  ),
                  {}
                )
              : t('conflicts.account.help', 'Used to write the kept values.')}
          </p>
          {accountOptions.length === 0 && (
            <Link
              to="/configuration/service-accounts"
              className="text-xs text-primary underline-offset-4 hover:underline"
            >
              {t('conflicts.account.manage', 'Manage service accounts')}
            </Link>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              data-testid={`jira-conflicts-keep-local-${conflict.id}`}
              disabled={!accountId || resolve.isPending}
              loading={pendingAction === 'keep_local'}
              onClick={() => {
                setError(null)
                setPendingAction('keep_local')
                resolve.mutate('keep_local')
              }}
            >
              {t('conflicts.action.keep_local', 'Keep IndraMesh values')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              data-testid={`jira-conflicts-keep-remote-${conflict.id}`}
              disabled={!accountId || resolve.isPending}
              loading={pendingAction === 'keep_remote'}
              onClick={() => {
                setError(null)
                setPendingAction('keep_remote')
                resolve.mutate('keep_remote')
              }}
            >
              {t('conflicts.action.keep_remote', 'Keep Jira values')}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            {t(
              'conflicts.action.consequence',
              'Keep IndraMesh values writes these values to Jira. Keep Jira values replaces them in IndraMesh.'
            )}
          </p>
          {error && (
            <p className="text-xs text-destructive" role="alert">
              {error}
            </p>
          )}
        </div>
      )}

      {conflict.status !== 'pending' && resolvedKey && (
        <p className="text-xs text-muted-foreground">
          {interpolateDefault(t(resolvedKey, 'Resolved on {{date}}.'), {
            date: formatDate(conflict.resolved_at),
          })}
        </p>
      )}
    </div>
  )
}

function ConflictRow({ conflict, open, onToggle, onResolved, names, canManage, t, formatDate }) {
  const isPending = conflict.status === 'pending'
  return (
    <>
      <tr data-testid={`jira-conflicts-row-${conflict.id}`}>
        <td className="px-4 py-3 text-sm font-semibold">{conflict.object_name}</td>
        <td className="px-4 py-3 text-sm text-muted-foreground">{conflict.attribute_name}</td>
        <td className="px-4 py-3 text-sm text-muted-foreground">
          {names.get(conflict.schema_id) || ''}
        </td>
        <td className="px-4 py-3 text-sm">
          {conflict.direction === 'pull'
            ? t('conflicts.direction.pull', 'Pull')
            : t('conflicts.direction.push', 'Push')}
        </td>
        <td className="px-4 py-3 text-sm">{formatDate(conflict.updated_at)}</td>
        <td className="px-4 py-3 text-sm">
          {t(STATUS_LABEL_KEYS[conflict.status], conflict.status)}
        </td>
        <td className="px-4 py-3 text-right">
          <Button
            variant="outline"
            size="sm"
            aria-expanded={open}
            aria-controls={`jira-conflicts-detail-${conflict.id}`}
            aria-label={interpolateDefault(
              isPending
                ? t('conflicts.row.review_aria', 'Review conflict for {{attribute}} on “{{object}}”')
                : t('conflicts.row.details_aria', 'Show details for {{attribute}} on “{{object}}”'),
              { attribute: conflict.attribute_name, object: conflict.object_name }
            )}
            data-testid={`jira-conflicts-review-${conflict.id}`}
            onClick={onToggle}
          >
            {isPending
              ? t('conflicts.row.review', 'Review')
              : t('conflicts.row.details', 'Details')}
          </Button>
        </td>
      </tr>
      {open && (
        <tr>
          <td
            colSpan={7}
            id={`jira-conflicts-detail-${conflict.id}`}
            className="border-l-2 border-primary bg-muted/20 px-4 py-4"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.stopPropagation()
                onToggle()
              }
            }}
          >
            <DetailPanel
              conflict={conflict}
              canManage={canManage}
              onResolved={onResolved}
              t={t}
              formatDate={formatDate}
            />
          </td>
        </tr>
      )}
    </>
  )
}

export function useJiraConflictCount() {
  const { data } = useQuery({
    queryKey: ['jira-conflict-count'],
    queryFn: async () => (await api.get('plugins/jira/conflicts/count')).data,
    // D-04/D-05: a 30s stale window and NO polling interval (the hook adds none).
    staleTime: 30_000,
  })
  return data?.pending ?? 0
}

// S7/D-07: the nav pill's accessible name comes from the plugin's own copy, not
// the host's generic "N items". This is a plain function (the host calls
// `badgeLabel(count)` outside React), so it resolves through `pluginText` on the
// plugin name (it derives the host-assigned namespace itself). `count` is the raw
// pending value (>=100 is the capped overflow signal), mirroring the host's 99+
// display rule.

export function jiraConflictBadgeLabel(count) {
  if (count >= 100) {
    return pluginText('jira', 'conflicts.nav.badge_overflow', {
      defaultValue: 'More than 99 open Jira conflicts',
    })
  }
  const key = count === 1 ? 'conflicts.nav.badge_one' : 'conflicts.nav.badge_other'
  return pluginText('jira', key, {
    count,
    defaultValue: count === 1 ? '{{count}} open Jira conflict' : '{{count}} open Jira conflicts',
  })
}

function focusRowAction(id) {
  if (typeof document === 'undefined') return
  requestAnimationFrame(() => {
    const element = document.querySelector(
      `[data-testid="jira-conflicts-review-${id}"]`
    )
    element?.focus?.()
  })
}

export default function JiraConflictsPage() {
  const { t } = usePluginTranslation('jira')
  const formatDate = useFormatDate()
  const canRead = useCan('conflicts', 'read')
  const canManage = useCan('conflicts', 'manage')
  const [searchParams, setSearchParams] = useSearchParams()
  const [expandedId, setExpandedId] = useState(null)
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState([])

  const filters = useMemo(
    () => filtersFromSearch(searchParams.toString()),
    [searchParams]
  )
  const hasActiveFilters =
    filters.status !== 'pending' || Boolean(filters.direction) || Boolean(filters.schemaId)

  const schemasQuery = useSchemas(null, 0, 200, { enabled: canRead })
  const schemaNames = useMemo(() => {
    const map = new Map()
    ;(schemasQuery.data?.items || schemasQuery.data || []).forEach((schema) =>
      map.set(schema.id, schema.name)
    )
    return map
  }, [schemasQuery.data])

  const list = useQuery({
    queryKey: ['jira-conflicts', filters, offset],
    queryFn: () => fetchConflictPage(filters, offset),
    enabled: canRead,
    placeholderData: (previous) => previous,
  })

  const merged = useMemo(
    () => mergeConflictPages(rows, list.data?.items),
    [rows, list.data]
  )

  const applyFilters = (next) => {
    setExpandedId(null)
    setOffset(0)
    setRows([])
    setSearchParams(searchFromFilters({ ...filters, ...next }), { replace: true })
  }

  const closeDetail = () => setExpandedId(null)

  const handleResolved = (resolvedId) => {
    const index = merged.findIndex((conflict) => conflict.id === resolvedId)
    const next = index >= 0 ? merged[index + 1] : null
    setExpandedId(null)
    if (next) {
      focusRowAction(next.id)
    } else if (typeof document !== 'undefined') {
      document.querySelector('[data-testid="jira-conflicts-page"]')?.focus?.()
    }
  }

  if (!canRead) {
    return (
      <div className="space-y-6" data-testid="jira-conflicts-page">
        <PageHeader title={t('conflicts.page.title', 'Jira conflicts')} icon="git-compare" />
        <EmptyState
          variant="error"
          data-testid="jira-conflicts-no-access"
          title={t('conflicts.page.no_access_title', "You don't have access to Jira conflicts")}
          description={t(
            'conflicts.page.no_access',
            'Ask an administrator for the Conflicts read permission.'
          )}
        />
      </div>
    )
  }

  const firstLoad = list.isPending && merged.length === 0

  return (
    <div className="space-y-6" tabIndex={-1} data-testid="jira-conflicts-page">
      <PageHeader
        title={t('conflicts.page.title', 'Jira conflicts')}
        description={t(
          'conflicts.page.description',
          'Values that changed in both IndraMesh and Jira since the last sync. Compare both sides, then choose which values to keep.'
        )}
        icon="git-compare"
      />

      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t('conflicts.filter.schema', 'Schema')}</span>
          <SelectInput
            aria-label={t('conflicts.filter.schema', 'Schema')}
            data-testid="jira-conflicts-filter-schema"
            value={filters.schemaId || ''}
            options={[
              { value: '', label: t('conflicts.filter.schema_all', 'All schemas') },
              ...(schemasQuery.data?.items || schemasQuery.data || []).map((schema) => ({
                value: schema.id,
                label: schema.name,
              })),
            ]}
            onChange={(event) => applyFilters({ schemaId: event.target.value || null })}
          />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t('conflicts.filter.direction', 'Found during')}</span>
          <SelectInput
            aria-label={t('conflicts.filter.direction', 'Found during')}
            data-testid="jira-conflicts-filter-direction"
            value={filters.direction || ''}
            options={[
              { value: '', label: t('conflicts.filter.direction_all', 'Pulls and pushes') },
              { value: 'pull', label: t('conflicts.filter.direction_pull', 'Pulls') },
              { value: 'push', label: t('conflicts.filter.direction_push', 'Pushes') },
            ]}
            onChange={(event) => applyFilters({ direction: event.target.value || null })}
          />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t('conflicts.filter.status', 'Status')}</span>
          <SelectInput
            aria-label={t('conflicts.filter.status', 'Status')}
            data-testid="jira-conflicts-filter-status"
            value={filters.status}
            options={STATUS_OPTIONS.map((value) => ({
              value,
              label: t(STATUS_LABEL_KEYS[value], value),
            }))}
            onChange={(event) => applyFilters({ status: event.target.value })}
          />
        </label>
      </div>

      {firstLoad ? (
        <TableSkeleton rows={5} cols={7} />
      ) : list.isError ? (
        <EmptyState
          variant="error"
          data-testid="jira-conflicts-error"
          title={t('conflicts.page.load_error_title', "Couldn't load Jira conflicts")}
          description={interpolateDefault(
            t('conflicts.page.load_error', "Couldn't load Jira conflicts: {{message}}. Reload the page to try again."),
            { message: extractErrorMessage(list.error) }
          )}
        />
      ) : merged.length === 0 ? (
        hasActiveFilters ? (
          <EmptyState
            variant="no-results"
            data-testid="jira-conflicts-no-results"
            title={t('conflicts.page.no_results_title', 'No conflicts match these filters')}
            description={t(
              'conflicts.page.no_results_body',
              'Change the schema, direction or status filter to see other conflicts.'
            )}
          />
        ) : (
          <EmptyState
            variant="empty"
            data-testid="jira-conflicts-empty"
            title={t('conflicts.page.empty_title', 'No open Jira conflicts')}
            description={t(
              'conflicts.page.empty_body',
              'When a value changes in both IndraMesh and Jira between syncs, the conflict appears here for you to decide.'
            )}
          />
        )
      ) : (
        <Table data-testid="jira-conflicts-table">
          <thead>
            <tr className="border-b">
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.object', 'Object')}
              </th>
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.attribute', 'Attribute')}
              </th>
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.schema', 'Schema')}
              </th>
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.direction', 'Found during')}
              </th>
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.changed', 'Last changed')}
              </th>
              <th className="text-left px-4 py-2 text-xs font-semibold">
                {t('conflicts.column.status', 'Status')}
              </th>
              <th className="px-4 py-2 text-right text-xs font-semibold">
                <span className="sr-only">{t('conflicts.column.actions', 'Actions')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {merged.map((conflict) => (
              <ConflictRow
                key={conflict.id}
                conflict={conflict}
                open={expandedId === conflict.id}
                onToggle={() => {
                  if (expandedId === conflict.id) {
                    closeDetail()
                    focusRowAction(conflict.id)
                  } else {
                    setExpandedId(conflict.id)
                  }
                }}
                onResolved={() => handleResolved(conflict.id)}
                names={schemaNames}
                canManage={canManage}
                t={t}
                formatDate={formatDate}
              />
            ))}
          </tbody>
        </Table>
      )}

      {!firstLoad && list.data?.next_offset !== null && list.data?.next_offset !== undefined && (
        <Button
          variant="outline"
          size="sm"
          data-testid="jira-conflicts-load-more"
          loading={list.isFetching && offset > 0}
          onClick={() => {
            setRows(merged)
            setOffset(list.data.next_offset)
          }}
        >
          {t('conflicts.load_more', 'Load more conflicts')}
        </Button>
      )}
    </div>
  )
}
