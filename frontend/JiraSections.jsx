// Jira plugin settings sections (Phase 295, JUI-03/06/08, D-09..D-16).
//
// The host renders these through PluginSectionRegion for the schema settings,
// object type settings and object detail families. The plugin owns the body and
// the data; the host draws the card frame, attribution and error boundary. Every
// visible string comes from the plugin's own locale namespace.
//
// Phase 295 Task 1 delivers the schema sync section (D-16). Later tasks add the
// ObjectType section (direction inheritance, D-10/D-11), the object detail
// section (detach, D-09) and the verified deep links (D-09).
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  LucideIcon,
  SelectInput,
  api,
  confirmDialog,
  extractErrorMessage,
  interpolateDefault,
  serializeRepeatedParams,
  showToast,
  useCan,
  useFormatDate,
  useObjectTypeAttributes,
  usePluginTranslation,
  useSchema,
  useServiceAccounts,
} from '@indramesh/plugin-api'
import { defaultAccountSelection } from './connection-logic.js'
import { filterEligibleAccounts } from './task-form-logic.js'
import {
  DIRECTION_VALUES,
  chunkIds,
  directionLabelKey,
  directionSaveError,
  directionSaveStart,
  directionSaveSuccess,
  directionToSelectValue,
  objectJiraPath as buildObjectJiraPath,
  objectTypeJiraPath as buildObjectTypeJiraPath,
  originLabelKey,
  safeJiraLink,
  schemaJiraPath as buildSchemaJiraPath,
  selectValueToDirection,
} from './direction-logic.js'

// Server run status -> the copy key suffix (UI-SPEC S3-S5 "Run states").
const RUN_STATE = {
  SUCCESS: 'completed',
  PARTIAL_SUCCESS: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
  RUNNING: 'running',
  PENDING: 'queued',
}

function useLinksQuery(entity, ids) {
  const key = (ids || []).join(',')
  return useQuery({
    queryKey: ['plugin-jira', 'links', entity, key],
    queryFn: async () => {
      const qs = serializeRepeatedParams({ ids: ids || [] })
      const response = await api.get(`plugins/jira/links/${entity}?${qs}`)
      return response.data?.items || []
    },
    enabled: (ids || []).length > 0,
    retry: false,
  })
}

function useSchemaStatusQuery(schemaId) {
  return useQuery({
    queryKey: ['plugin-jira', 'schema-status', schemaId],
    queryFn: async () =>
      (await api.get(`plugins/jira/schemas/${schemaId}/sync-status`)).data,
    enabled: !!schemaId,
    retry: false,
  })
}

// D-09: the saved live connection for a Source, read through the authorized
// plugin GET (permission `sync:read` plus the Source record gate on the server).
// Any denial or absence (403/404/None) resolves to no connection, so the caller
// keeps the Jira ID as plain text.
function useSourceConnectionQuery(sourceId, enabled) {
  return useQuery({
    queryKey: ['plugin-jira', 'source-connection', sourceId],
    queryFn: async () =>
      (await api.get(`plugins/jira/connections/source/${sourceId}`)).data
        ?.connection || null,
    enabled: !!sourceId && !!enabled,
    retry: false,
  })
}

// The href for one entity, or `null` for plain text: a verified path AND a saved
// connection that is the Source-bound, live one for this exact Source. A Target,
// a stale or re-pointed connection, or a missing/unsafe base URL never links.
function useJiraEntityHref(path, sourceId, enabled) {
  const connectionQuery = useSourceConnectionQuery(sourceId, enabled && !!path)
  const connection = connectionQuery.data
  if (!path || !connection) return null
  if (connection.endpoint_kind !== 'source') return null
  if (String(connection.endpoint_id) !== String(sourceId)) return null
  if (connection.binding !== 'live') return null
  return safeJiraLink(connection.base_url, path)
}

// A Jira ID is a link only when a verified same-origin href was built; every
// other case stays legible plain text (D-09).
function JiraIdLink({ jiraId, href }) {
  const { t } = usePluginTranslation('jira')
  if (!href) {
    return <span className="tabular-nums">{jiraId}</span>
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-primary hover:underline underline-offset-4 tabular-nums"
      data-testid="plugin-jira-id-link"
      aria-label={interpolateDefault(
        t('link.open_in_jira', 'Open {{id}} in Jira (opens in a new tab)'),
        { id: jiraId }
      )}
    >
      {jiraId}
      <LucideIcon name="external-link" size={12} aria-hidden="true" />
    </a>
  )
}

function LoadingLine() {
  const { t } = usePluginTranslation('jira')
  return (
    <p
      className="flex items-center gap-2 text-sm text-muted-foreground"
      data-testid="plugin-jira-section-loading"
    >
      <LucideIcon
        name="loader-circle"
        size={14}
        className="animate-spin motion-reduce:animate-none"
        aria-hidden="true"
      />
      {t('section.loading', 'Loading Jira details…')}
    </p>
  )
}

function ErrorLine({ error, onRetry }) {
  const { t } = usePluginTranslation('jira')
  return (
    <div className="space-y-2" data-testid="plugin-jira-section-error">
      <p role="alert" className="text-sm text-destructive">
        {interpolateDefault(
          t('section.load_error', "Couldn't load the Jira details: {{message}}."),
          { message: extractErrorMessage(error) }
        )}
      </p>
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        {t('section.retry', 'Try again')}
      </Button>
    </div>
  )
}

function DefinitionList({ children }) {
  return <dl className="grid gap-4 sm:grid-cols-2">{children}</dl>
}

function DefinitionPair({ label, children }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-sm">{children}</dd>
    </div>
  )
}

function RunStatusLine({ run, neverKey, neverFallback }) {
  const { t } = usePluginTranslation('jira')
  const formatDate = useFormatDate()
  if (!run) {
    return <span className="text-muted-foreground">{t(neverKey, neverFallback)}</span>
  }
  const state = RUN_STATE[run.status] || 'queued'
  const when = run.completed_at || run.started_at
  const icon = state === 'failed' ? 'circle-x' : state === 'running' ? 'loader-circle' : 'circle-check'
  const text = state === 'queued'
    ? t('section.run_queued', 'Queued')
    : t(`section.run_${state}`, `Completed ${'{{date}}'}`, { date: formatDate(when) })
  return (
    <span className="block space-y-1">
      <span className="flex items-center gap-2">
        <LucideIcon
          name={icon}
          size={12}
          className={state === 'running' ? 'animate-spin motion-reduce:animate-none' : undefined}
          aria-hidden="true"
        />
        {text}
      </span>
      {run.status === 'FAILED' && run.error_message && (
        <span className="block whitespace-pre-wrap break-words text-xs text-destructive">
          {run.error_message}
        </span>
      )}
    </span>
  )
}

function initialState(item) {
  return {
    status: 'idle',
    value: item?.explicit_direction ?? null,
    effective: item?.effective_direction ?? null,
    origin: item?.direction_origin ?? null,
    error: null,
  }
}

// C1 (D-10/D-11): the shared direction control for schema, ObjectType and
// attribute levels. Without `sync:trigger` it renders plain text with the
// effective line; a disabled select is never used for read-only users.
export function JiraDirectionControl({ entity, entityId, item, schemaLevel = false }) {
  const { t } = usePluginTranslation('jira')
  const canTrigger = useCan('sync', 'trigger')
  const queryClient = useQueryClient()
  const [state, setState] = useState(() => initialState(item))

  useEffect(() => {
    setState(initialState(item))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.explicit_direction, item?.effective_direction, item?.direction_origin])

  if (!item?.linked) return null

  const effective = state.effective
  const origin = state.origin
  const effectiveLabel = t(`direction.${effective}`, effective || '')

  const runSave = async (nextValue) => {
    const previousValue = state.value
    setState(directionSaveStart({ value: nextValue }))
    try {
      const response = await api.put(`plugins/jira/directions/${entity}/${entityId}`, {
        direction: nextValue,
      })
      setState(directionSaveSuccess(response.data || {}))
      // D-08: a pull direction changes write locks for everyone, so the link
      // queries and the descendants' attribute/object caches are invalidated.
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'links'] })
      queryClient.invalidateQueries({ queryKey: ['object-type-attributes'] })
      queryClient.invalidateQueries({ queryKey: ['objects'] })
    } catch (err) {
      setState(
        directionSaveError({ previousValue, message: extractErrorMessage(err) })
      )
    }
  }

  const effectiveLine = (
    <span className="block text-xs text-muted-foreground">
      {interpolateDefault(t('direction.effective', 'Effective: {{direction}}.'), {
        direction: effectiveLabel,
      })}{' '}
      {t(originLabelKey(origin), originFallback(origin))}
    </span>
  )

  return (
    <div className="space-y-1">
      {canTrigger ? (
        <div className="flex flex-wrap items-center gap-2">
          <SelectInput
            className="w-auto"
            aria-label={t('direction.label', 'Sync direction')}
            data-testid={`plugin-jira-direction-${entity}`}
            value={directionToSelectValue(state.value)}
            disabled={state.status === 'saving'}
            options={[
              {
                value: '',
                label: t(
                  directionLabelKey(null, { schemaLevel }),
                  schemaLevel ? 'Default (both directions)' : 'Inherit'
                ),
              },
              ...DIRECTION_VALUES.map((value) => ({
                value,
                label: t(directionLabelKey(value), value),
              })),
            ]}
            onChange={(event) => runSave(selectValueToDirection(event.target.value))}
          />
          {state.status === 'saving' && (
            <span className="text-xs text-muted-foreground">{t('direction.saving', 'Saving…')}</span>
          )}
          {state.status === 'saved' && (
            <span className="flex items-center gap-1 text-xs text-green-700 dark:text-green-400">
              <LucideIcon name="circle-check" size={12} aria-hidden="true" />
              {t('direction.saved', 'Saved')}
            </span>
          )}
        </div>
      ) : (
        <span className="block text-sm" data-testid={`plugin-jira-direction-${entity}-readonly`}>
          {t(directionLabelKey(state.value, { schemaLevel }), state.value || '')}
        </span>
      )}
      {effectiveLine}
      {state.status === 'error' && (
        <span role="alert" className="block text-xs text-destructive">
          {interpolateDefault(
            t('direction.save_error', "Couldn't save the direction: {{message}}. The previous setting still applies."),
            { message: state.error }
          )}
        </span>
      )}
      {effective === 'pull' && (
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <LucideIcon name="lock" size={12} aria-hidden="true" />
          {t('direction.lock_hint', 'Locked in IndraMesh: values change only through a Jira pull.')}
        </span>
      )}
    </div>
  )
}

function originFallback(origin) {
  switch (origin) {
    case 'attribute':
      return 'Set here.'
    case 'object_type':
      return 'Inherited from the object type.'
    case 'schema':
      return 'Inherited from the schema.'
    default:
      return 'Default because no level sets a direction.'
  }
}

function ConflictsLine({ conflicts, capped }) {
  const { t } = usePluginTranslation('jira')
  const canReadConflicts = useCan('conflicts', 'read')
  const pull = capped?.pull ? '100+' : String(conflicts?.pull ?? 0)
  const push = capped?.push ? '100+' : String(conflicts?.push ?? 0)
  const total = (conflicts?.pull ?? 0) + (conflicts?.push ?? 0)
  const isNone = total === 0 && !capped?.pull && !capped?.push
  return (
    <span className="block space-y-1" data-testid="plugin-jira-open-conflicts">
      {isNone
        ? t('section.conflicts_none', 'None')
        : interpolateDefault(
            t('section.conflicts_summary', '{{pull}} from pulls, {{push}} from pushes'),
            { pull, push }
          )}
      {!isNone && canReadConflicts && (
        <a
          href={`/plugins/jira/conflicts?status=pending`}
          className="block text-primary hover:underline underline-offset-4"
        >
          {t('section.review_conflicts', 'Review conflicts')}
        </a>
      )}
    </span>
  )
}

// 261004: link an existing EMPTY, unlinked schema of a Jira Source to a Jira
// schema without importing; the next pull fills it, keeping name and keygen.
function JiraSchemaLinkControl({ schema }) {
  const { t } = usePluginTranslation('jira')
  const queryClient = useQueryClient()
  const canTrigger = useCan('sync', 'trigger')
  const sourceId = schema?.source_id
  const schemaId = schema?.id
  const connectionQuery = useSourceConnectionQuery(sourceId, canTrigger)
  const connection = connectionQuery.data
  const adoptableQuery = useQuery({
    queryKey: ['plugin-jira', 'adoptable-schemas', sourceId],
    queryFn: async () =>
      (await api.get(`plugins/jira/schemas/source/${sourceId}/adoptable`)).data?.schemas || [],
    enabled: !!sourceId && canTrigger && !!connection,
    retry: false,
  })
  const adoptable = (adoptableQuery.data || []).some((entry) => entry.id === schemaId)
  const { data: accountsData } = useServiceAccounts(sourceId, 0, 50, null, {
    enabled: !!sourceId && adoptable,
  })
  const accounts = filterEligibleAccounts(accountsData || [], Date.now())
  const [accountId, setAccountId] = useState('')
  const [jiraSchemaId, setJiraSchemaId] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (accountId) return
    const preset = defaultAccountSelection(connection?.default_service_account_id, accounts)
    if (preset) setAccountId(preset)
  }, [connection, accounts, accountId])
  const jiraSchemasQuery = useQuery({
    queryKey: ['plugin-jira', 'source-schemas', sourceId, accountId],
    queryFn: async () =>
      (
        await api.get(`plugins/jira/schemas/source/${sourceId}`, {
          params: { service_account_id: accountId },
        })
      ).data?.schemas || [],
    enabled: adoptable && !!accountId,
    retry: false,
  })
  const jiraOptions = (jiraSchemasQuery.data || [])
    .filter((entry) => !entry.core_schema_id)
    .map((entry) => ({
      value: String(entry.jira_schema_id),
      label: entry.key ? `${entry.name} (${entry.key})` : entry.name,
    }))

  if (!canTrigger || !connection || connection.binding !== 'live') return null
  if (adoptableQuery.isLoading) return null
  if (!adoptable) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="plugin-jira-schema-link-not-empty">
        {t(
          'section.link_requires_empty',
          'Only an empty schema (without object types) can be linked to an existing Jira schema here.'
        )}
      </p>
    )
  }

  const link = async () => {
    setBusy(true)
    try {
      await api.post(`plugins/jira/schemas/${schemaId}/link`, {
        jira_schema_id: jiraSchemaId,
        service_account_id: accountId,
      })
      showToast(
        t('section.link_toast', 'Linked to Jira. The next pull for this Source fills this schema.')
      )
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'links'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'schema-status', schemaId] })
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'adoptable-schemas', sourceId] })
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'source-schemas', sourceId] })
    } catch (err) {
      showToast(
        interpolateDefault(t('section.link_error', "Couldn't link this schema: {{message}}."), {
          message: extractErrorMessage(err),
        }),
        { type: 'error' }
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2" data-testid="plugin-jira-schema-link">
      <p className="text-xs text-muted-foreground">
        {t(
          'section.link_help',
          'Link this empty schema to a Jira schema. Nothing is imported yet: the next pull for this Source fills it and keeps this name and key prefix.'
        )}
      </p>
      <SelectInput
        id="plugin-jira-schema-link-account"
        data-testid="plugin-jira-schema-link-account"
        value={accountId}
        placeholder={t('task.service_account_placeholder', 'Select a service account…')}
        options={accounts.map((account) => ({ value: account.id, label: account.name }))}
        disabled={busy}
        onChange={(event) => {
          setAccountId(event.target.value)
          setJiraSchemaId('')
        }}
      />
      <SelectInput
        id="plugin-jira-schema-link-jira"
        data-testid="plugin-jira-schema-link-jira"
        value={jiraSchemaId}
        placeholder={
          jiraSchemasQuery.isLoading
            ? t('task.jira_schema_loading', 'Loading Jira schemas…')
            : t('section.link_jira_placeholder', 'Select a Jira schema…')
        }
        options={jiraOptions}
        disabled={busy || !accountId}
        onChange={(event) => setJiraSchemaId(event.target.value)}
      />
      {jiraSchemasQuery.isError && (
        <p className="text-xs text-destructive" data-testid="plugin-jira-schema-link-load-error">
          {interpolateDefault(
            t('section.link_load_failed', "Couldn't load the Jira schemas: {{message}}."),
            { message: extractErrorMessage(jiraSchemasQuery.error) }
          )}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        leftIcon="link"
        loading={busy}
        disabled={!accountId || !jiraSchemaId || busy}
        data-testid="plugin-jira-schema-link-button"
        onClick={link}
      >
        {t('section.link_button', 'Link to Jira schema')}
      </Button>
    </div>
  )
}

export function JiraSchemaSection({ schema }) {
  const { t } = usePluginTranslation('jira')
  const schemaId = schema?.id
  const statusQuery = useSchemaStatusQuery(schemaId)
  const linkQuery = useLinksQuery('schema', schemaId ? [schemaId] : [])
  const item = linkQuery.data?.[0] || null
  const linked = !!item?.linked
  // D-09: the schema carries its own persisted source_id; the connection lookup
  // runs only for a linked, readable schema, otherwise the ID stays plain.
  const path = linked ? buildSchemaJiraPath(item.jira_id) : null
  const href = useJiraEntityHref(path, schema?.source_id, linked)

  if (!schemaId) return null
  if (statusQuery.isLoading || linkQuery.isLoading) return <LoadingLine />
  if (statusQuery.isError || linkQuery.isError) {
    return (
      <ErrorLine
        error={statusQuery.error || linkQuery.error}
        onRetry={() => {
          statusQuery.refetch()
          linkQuery.refetch()
        }}
      />
    )
  }

  const status = statusQuery.data || {}
  const link = status.link
  const checkpoints = status.open_checkpoints || []

  return (
    <div className="space-y-6" data-testid="plugin-jira-schema-section">
      <div className="space-y-3">
        <h3 className="text-sm font-semibold">{t('section.heading_link_direction', 'Link and direction')}</h3>
        <DefinitionList>
          <DefinitionPair label={t('section.schema_id_label', 'Jira schema')}>
            {linked ? (
              <JiraIdLink jiraId={item.jira_id} href={href} />
            ) : (
              <div className="space-y-2">
                <span className="text-muted-foreground">
                  {t(
                    'section.unlinked_schema',
                    "This schema isn't linked to Jira. Run a Jira pull for its Source to link it."
                  )}
                </span>
                <JiraSchemaLinkControl schema={schema} />
              </div>
            )}
          </DefinitionPair>
          {linked && (
            <DefinitionPair label={t('direction.label', 'Sync direction')}>
              <JiraDirectionControl
                entity="schema"
                entityId={schemaId}
                item={item}
                schemaLevel
              />
            </DefinitionPair>
          )}
        </DefinitionList>
        {link?.binding === 'stale' && (
          <p
            className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-500"
            data-testid="plugin-jira-schema-stale"
          >
            <LucideIcon name="triangle-alert" size={12} aria-hidden="true" />
            {t(
              'section.stale',
              'Out of date: the Source now points to a different Jira address. Run a pull to relink this schema.'
            )}
          </p>
        )}
      </div>

      <div className="space-y-3">
        <h3 className="text-sm font-semibold">{t('section.heading_status', 'Status')}</h3>
        <DefinitionList>
          <DefinitionPair label={t('section.last_pull', 'Last pull')}>
            <RunStatusLine
              run={status.last_pull}
              neverKey="section.never_pulled"
              neverFallback="Never pulled"
            />
          </DefinitionPair>
          <DefinitionPair label={t('section.last_push', 'Last push')}>
            <RunStatusLine
              run={status.last_push}
              neverKey="section.never_pushed"
              neverFallback="Never pushed"
            />
          </DefinitionPair>
          <DefinitionPair label={t('section.unfinished_pulls', 'Unfinished pulls')}>
            {checkpoints.length === 0 ? (
              <span className="text-muted-foreground">{t('section.unfinished_none', 'None')}</span>
            ) : (
              <span className="flex items-center gap-2">
                <LucideIcon name="triangle-alert" size={12} aria-hidden="true" />
                {t(
                  checkpoints.length === 1
                    ? 'section.unfinished_one'
                    : 'section.unfinished_other',
                  checkpoints.length === 1
                    ? '{{count}} object type resumes on the next pull'
                    : '{{count}} object types resume on the next pull',
                  { count: checkpoints.length }
                )}
              </span>
            )}
          </DefinitionPair>
          <DefinitionPair label={t('section.open_conflicts', 'Open conflicts')}>
            <ConflictsLine conflicts={status.open_conflicts} capped={status.open_conflicts_capped} />
          </DefinitionPair>
        </DefinitionList>
      </div>
    </div>
  )
}

function useAttributeLinks(attributeIds) {
  const ids = attributeIds || []
  const chunks = chunkIds(ids)
  return useQuery({
    queryKey: ['plugin-jira', 'links', 'attribute', ids.join(',')],
    queryFn: async () => {
      const items = []
      for (const chunk of chunks) {
        const qs = serializeRepeatedParams({ ids: chunk })
        const response = await api.get(`plugins/jira/links/attribute?${qs}`)
        items.push(...(response.data?.items || []))
      }
      return items
    },
    enabled: ids.length > 0,
    retry: false,
  })
}

export function JiraObjectTypeSection({ objectType }) {
  const { t } = usePluginTranslation('jira')
  const objectTypeId = objectType?.id
  const schemaId = objectType?.schema_id
  const typeQuery = useLinksQuery('object_type', objectTypeId ? [objectTypeId] : [])
  const attributesQuery = useObjectTypeAttributes(objectTypeId)
  const attributes = attributesQuery.data || []
  const attributeLinksQuery = useAttributeLinks(attributes.map((attribute) => attribute.id))
  // D-09: the type's Source comes from the persisted schema ancestry, never from
  // an attribute ID. The schema link supplies the numeric Jira schema id the
  // type URL needs; a denied or unlinked schema keeps the type ID plain.
  const schemaQuery = useSchema(schemaId)
  const schemaLinkQuery = useLinksQuery('schema', schemaId ? [schemaId] : [])
  const typeItem = typeQuery.data?.[0] || null
  const typeLinked = !!typeItem?.linked
  const schemaItem = schemaLinkQuery.data?.[0] || null
  const schemaJiraId = schemaItem?.linked ? schemaItem.jira_id : null
  const typePath = typeLinked
    ? buildObjectTypeJiraPath(schemaJiraId, typeItem.jira_id)
    : null
  const typeHref = useJiraEntityHref(typePath, schemaQuery.data?.source_id, typeLinked)

  if (!objectTypeId) return null
  if (typeQuery.isLoading || attributesQuery.isLoading || attributeLinksQuery.isLoading) {
    return <LoadingLine />
  }
  if (typeQuery.isError || attributesQuery.isError || attributeLinksQuery.isError) {
    return (
      <ErrorLine
        error={typeQuery.error || attributesQuery.error || attributeLinksQuery.error}
        onRetry={() => {
          typeQuery.refetch()
          attributesQuery.refetch()
          attributeLinksQuery.refetch()
        }}
      />
    )
  }

  if (!typeLinked) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plugin-jira-object-type-unlinked">
        {t('section.unlinked_object_type', "This object type isn't linked to Jira.")}
      </p>
    )
  }

  const linksById = new Map((attributeLinksQuery.data || []).map((item) => [item.id, item]))

  return (
    <div className="space-y-4" data-testid="plugin-jira-object-type-section">
      <DefinitionList>
        <DefinitionPair label={t('section.object_type_id_label', 'Jira object type ID')}>
          <JiraIdLink jiraId={typeItem.jira_id} href={typeHref} />
        </DefinitionPair>
        <DefinitionPair label={t('direction.label', 'Sync direction')}>
          <JiraDirectionControl entity="object_type" entityId={objectTypeId} item={typeItem} />
        </DefinitionPair>
      </DefinitionList>

      <div className="space-y-3">
        <h3 className="text-sm font-semibold">{t('section.heading_attributes', 'Attributes')}</h3>
        <div className="rounded-lg border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="px-4 py-3 font-medium">{t('section.attribute_name', 'Attribute')}</th>
                <th className="px-4 py-3 font-medium">{t('section.attribute_jira_id', 'Jira ID')}</th>
                <th className="px-4 py-3 font-medium">{t('section.sync_direction_column', 'Sync direction')}</th>
                <th className="px-4 py-3 font-medium">{t('section.effective_direction_column', 'Effective direction')}</th>
              </tr>
            </thead>
            <tbody>
              {attributes.map((attribute) => {
                const item = linksById.get(attribute.id)
                return (
                  <tr key={attribute.id} className="border-b last:border-0" data-testid={`plugin-jira-attribute-${attribute.id}`}>
                    <td className="px-4 py-3">{attribute.name}</td>
                    <td className="px-4 py-3 tabular-nums">
                      {item?.linked ? (
                        item.jira_id
                      ) : (
                        <span className="text-muted-foreground">{t('section.not_in_jira', 'Not in Jira')}</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {item?.linked ? (
                        <JiraDirectionControl entity="attribute" entityId={attribute.id} item={item} />
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">
                      {item?.linked
                        ? interpolateDefault(t('direction.effective', 'Effective: {{direction}}.'), {
                            direction: t(`direction.${item.effective_direction}`, item.effective_direction || ''),
                          })
                        : ''}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

// S3 (JUI-03, D-09): the object detail section. It reads the object's Jira
// link, states attached/detached, explains the pull-only lock and offers a
// confirm-gated detach/reattach. The button label is deliberately distinct from
// the transitional Core "Detach/Re-sync" control (R3-04), which Phase 296
// removes. The server record gate is authoritative; a stale binding keeps the
// previous state and explains why.
export function JiraObjectSection({ object }) {
  const { t } = usePluginTranslation('jira')
  const canTrigger = useCan('sync', 'trigger')
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const objectId = object?.id
  const linkQuery = useLinksQuery('object', objectId ? [objectId] : [])
  const schemaQuery = useSchema(object?.schema_id)
  const item = linkQuery.data?.[0] || null
  const linked = !!item?.linked
  // D-09: the Source is reached through the object's persisted schema_id.
  const path = linked ? buildObjectJiraPath(item.jira_id) : null
  const href = useJiraEntityHref(path, schemaQuery.data?.source_id, linked)

  if (!objectId) return null
  if (linkQuery.isLoading) return <LoadingLine />
  if (linkQuery.isError) {
    return <ErrorLine error={linkQuery.error} onRetry={() => linkQuery.refetch()} />
  }
  if (!linked) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="plugin-jira-object-unlinked">
        {t('section.unlinked_object', "This object isn't linked to Jira.")}
      </p>
    )
  }

  const attached = !item.is_detached
  const objectName = object?.name ?? ''

  const onToggle = async () => {
    const nextDetached = attached
    const confirmed = await confirmDialog({
      message: interpolateDefault(
        nextDetached
          ? t(
              'section.detach_confirm',
              "Detach “{{name}}” from Jira? Its values stay as they are and become editable here. Pulls and pushes skip this object until you reattach it."
            )
          : t(
              'section.reattach_confirm',
              "Reattach “{{name}}” to Jira? Pull-only values lock again, and the next pull replaces them with the values from Jira."
            ),
        { name: objectName }
      ),
      danger: false,
    })
    if (!confirmed) return
    setBusy(true)
    try {
      await api.put(`plugins/jira/objects/${objectId}/detach`, { detached: nextDetached })
      showToast(
        interpolateDefault(
          nextDetached
            ? t('section.detach_toast', 'Detached “{{name}}” from Jira.')
            : t('section.reattach_toast', 'Reattached “{{name}}” to Jira.'),
          { name: objectName }
        )
      )
      // Invalidate the link query, the object query and the item-badge queries,
      // so the detach state is recomputed everywhere it is shown.
      queryClient.invalidateQueries({ queryKey: ['plugin-jira', 'links'] })
      queryClient.invalidateQueries({ queryKey: ['object', objectId] })
      queryClient.invalidateQueries({ queryKey: ['plugin-item-badges'] })
    } catch (err) {
      const message =
        err?.response?.status === 409
          ? t(
              'section.detach_stale',
              "This object's Jira link is out of date. Run a pull for its Source, then try again."
            )
          : interpolateDefault(
              nextDetached
                ? t('section.detach_error', "Couldn't detach “{{name}}”: {{message}}.")
                : t('section.reattach_error', "Couldn't reattach “{{name}}”: {{message}}."),
              { name: objectName, message: extractErrorMessage(err) }
            )
      showToast(message, { type: 'error' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4" data-testid="plugin-jira-object-section">
      <DefinitionList>
        {item.jira_key ? (
          <DefinitionPair label={t('section.object_label', 'Jira object')}>
            <span className="flex flex-wrap items-baseline gap-2">
              <JiraIdLink jiraId={item.jira_key} href={href} />
              <span className="text-xs text-muted-foreground tabular-nums">
                {interpolateDefault(t('section.object_id_hint', 'ID {{id}}'), { id: item.jira_id })}
              </span>
            </span>
          </DefinitionPair>
        ) : (
          <DefinitionPair label={t('section.object_id_label', 'Jira object ID')}>
            <JiraIdLink jiraId={item.jira_id} href={href} />
          </DefinitionPair>
        )}
        <DefinitionPair label={t('section.sync_label', 'Sync')}>
          <span className="flex items-center gap-2 text-sm">
            <LucideIcon
              name={attached ? 'refresh-cw' : 'unlink'}
              size={14}
              aria-hidden="true"
            />
            {attached
              ? t('section.object_syncing', 'Syncing with Jira')
              : t('section.object_detached', 'Detached from Jira')}
          </span>
        </DefinitionPair>
      </DefinitionList>

      {attached && (
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <LucideIcon name="lock" size={12} aria-hidden="true" className="mt-0.5" />
          {t(
            'section.object_lock_hint',
            'Values of pull-only attributes are locked here and change only through a Jira pull. Detach the object to edit them.'
          )}
        </p>
      )}

      {canTrigger && (
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            leftIcon={attached ? 'unlink' : 'link'}
            loading={busy}
            onClick={onToggle}
            data-testid="plugin-jira-object-toggle"
          >
            {attached
              ? t('section.detach_button', 'Detach from Jira')
              : t('section.reattach_button', 'Reattach to Jira')}
          </Button>
        </div>
      )}
    </div>
  )
}

export default JiraSchemaSection
