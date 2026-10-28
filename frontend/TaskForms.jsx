// Jira plugin task forms (Phase 295, JUI-01, D-01..D-04).
//
// The host renders one of these components in the Create Task modal (FEEXT-05).
// Each is CONTROLLED: it owns `scope` and `plugin_parameters`, reports
// `{ scope, plugin_parameters, valid }` on every change and once on mount, and
// never posts a task itself — the host keeps the type select, permanent/name
// fields, banners, footer and the single POST /tasks/ (with plugin_parameters
// and current_step:null). The persisted account lives in the task SCOPE
// (`service_account_id`), never in plugin_parameters.
//
// Every scope/filter/preflight decision lives in the host-free task-form-logic
// module so it is unit-testable without a bundler (R-07).
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Button,
  CheckboxField,
  FormField,
  LucideIcon,
  SelectInput,
  api,
  interpolateDefault,
  usePluginTranslation,
  useSchemas,
  useServiceAccounts,
  useSources,
  useTargets,
} from '@indramesh/plugin-api'
import {
  buildExportFormState,
  buildPullFormState,
  buildPushFormState,
  exportPreflightKey,
  exportPreflightState,
  filterEligibleAccounts,
  filterJiraSources,
  filterJiraTargets,
} from './task-form-logic.js'
import { defaultAccountSelection } from './connection-logic.js'

function useEligibleAccounts(sourceId, targetId) {
  const { data, isLoading } = useServiceAccounts(
    sourceId || null,
    0,
    200,
    targetId || null,
    { enabled: !!sourceId || !!targetId }
  )
  const accounts = useMemo(
    () => filterEligibleAccounts(data || [], Date.now()),
    [data]
  )
  return { accounts, isLoading }
}

// 261004: once per selected endpoint, preselect the connection's stored default
// account while the picker is still empty and the account is still eligible.
function usePresetAccount(endpointId, connectionData, accounts, serviceAccountId, setServiceAccountId) {
  const [presetFor, setPresetFor] = useState(null)
  useEffect(() => {
    if (!endpointId || presetFor === endpointId || serviceAccountId) return
    const preset = defaultAccountSelection(
      connectionData?.connection?.default_service_account_id,
      accounts
    )
    if (!preset) return
    setServiceAccountId(preset)
    setPresetFor(endpointId)
  }, [endpointId, connectionData, accounts, serviceAccountId, presetFor, setServiceAccountId])
}

function normalizeList(data) {
  if (Array.isArray(data)) return data
  return data?.items || []
}

function NoJiraSourcesLink({ t }) {
  return (
    <p className="text-xs text-muted-foreground" data-testid="plugin-jira-no-sources">
      {t('task.no_sources', 'There are no Jira sources yet. Add one under Sources.')}{' '}
      <a href="/configuration/sources" className="text-primary hover:underline underline-offset-4">
        {t('task.action_open_sources', 'Open Sources')}
      </a>
    </p>
  )
}

function NoJiraTargetsLink({ t }) {
  return (
    <p className="text-xs text-muted-foreground" data-testid="plugin-jira-no-targets">
      {t('task.no_targets', 'There are no Jira targets yet. Add one under Targets.')}{' '}
      <a href="/configuration/targets" className="text-primary hover:underline underline-offset-4">
        {t('task.action_open_targets', 'Open Targets')}
      </a>
    </p>
  )
}

function NoServiceAccountsLink({ t, kind }) {
  return (
    <p className="text-xs text-muted-foreground" data-testid="plugin-jira-no-service-accounts">
      {interpolateDefault(
        t(
          'task.no_service_accounts',
          'This {{kind}} has no eligible service account yet. Add one under Service accounts.'
        ),
        { kind }
      )}{' '}
      <a
        href="/configuration/service-accounts"
        className="text-primary hover:underline underline-offset-4"
      >
        {t('task.action_open_service_accounts', 'Open service accounts')}
      </a>
    </p>
  )
}

// S1 Pull (D-01/D-04): Source -> Service account -> optional Jira schema ->
// unchecked "Include comments and attachments". `valid` needs Source AND
// account; the Jira schema is optional (blank = whole Source).
export function PullForm({ onChange, disabled }) {
  const { t } = usePluginTranslation('jira')
  const [sourceId, setSourceId] = useState('')
  const [serviceAccountId, setServiceAccountId] = useState('')
  const [jiraSchemaId, setJiraSchemaId] = useState('')
  const [targetSchemaId, setTargetSchemaId] = useState('')
  const [includeCommentsFiles, setIncludeCommentsFiles] = useState(false)

  // R-08: the host starts with pluginFormValid=true, so the form reports the
  // initial (empty -> invalid) state on mount before any picker is loaded.
  useEffect(() => {
    onChange(buildPullFormState({}))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const { data: sources = [] } = useSources(0, 200)
  const jiraSources = useMemo(() => filterJiraSources(sources), [sources])

  const { accounts, isLoading: accountsLoading } = useEligibleAccounts(sourceId, null)
  const { data: connectionData, isError: connectionError, error: connectionErrorObj } = useQuery({
    // Distinct key: JiraSections caches the unwrapped connection under
    // ['plugin-jira', 'source-connection', id]; this query keeps the envelope.
    queryKey: ['plugin-jira', 'source-connection-envelope', sourceId],
    queryFn: () => api.get(`plugins/jira/connections/source/${sourceId}`).then((response) => response.data),
    enabled: !!sourceId,
    retry: false,
  })
  const connectionReady = !!connectionData?.connection && !connectionError
  usePresetAccount(sourceId, connectionData, accounts, serviceAccountId, setServiceAccountId)

  useEffect(() => {
    onChange(
      buildPullFormState({ sourceId, serviceAccountId, jiraSchemaId, targetSchemaId, includeCommentsFiles, connectionReady })
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId, serviceAccountId, jiraSchemaId, targetSchemaId, includeCommentsFiles, connectionReady])

  const { data: schemaData, isLoading: schemasLoading, isError: schemasError, error: schemasErrorObj } =
    useQuery({
      queryKey: ['plugin-jira', 'source-schemas', sourceId, serviceAccountId],
      queryFn: () =>
        api
          .get(`plugins/jira/schemas/source/${sourceId}`, {
            params: { service_account_id: serviceAccountId },
          })
          .then((response) => response.data),
      enabled: !!sourceId && !!serviceAccountId && connectionReady,
    })
  const schemaOptions = (schemaData?.schemas || []).map((schema) => ({
    value: schema.jira_schema_id,
    label: schema.key ? `${schema.name} (${schema.key})` : schema.name,
  }))
  const selectedSchema = (schemaData?.schemas || []).find((schema) => String(schema.jira_schema_id) === jiraSchemaId)
  const linkedSchemaNames = (schemaData?.schemas || []).filter((schema) => schema.core_schema_id).map((schema) => schema.core_schema_name)
  // 261004: a first pull of an unlinked Jira schema may adopt an existing empty,
  // unlinked schema of the Source instead of creating one named after Jira.
  const adoptionPossible = !!selectedSchema && !selectedSchema.core_schema_id
  const { data: adoptableData, isSuccess: adoptableLoaded } = useQuery({
    queryKey: ['plugin-jira', 'adoptable-schemas', sourceId],
    queryFn: () => api.get(`plugins/jira/schemas/source/${sourceId}/adoptable`).then((response) => response.data),
    enabled: !!sourceId && adoptionPossible,
  })
  const adoptableOptions = (adoptableData?.schemas || []).map((schema) => ({
    value: schema.id,
    label: schema.keygen ? `${schema.name} (${schema.keygen})` : schema.name,
  }))

  return (
    <div className="space-y-4" data-testid="plugin-jira-task-form-pull">
      <div className="grid gap-4 sm:grid-cols-2">
      {jiraSources.length === 0 ? (
        <NoJiraSourcesLink t={t} />
      ) : (
        <FormField id="plugin-jira-pull-source" label={t('task.source', 'Source')} required>
          <SelectInput
            id="plugin-jira-pull-source"
            data-testid="plugin-jira-pull-source"
            value={sourceId}
            placeholder={t('task.source_placeholder', 'Select a Jira source…')}
            options={jiraSources.map((source) => ({ value: source.id, label: source.name }))}
            disabled={disabled}
            onChange={(event) => {
              setSourceId(event.target.value)
              setServiceAccountId('')
              setJiraSchemaId('')
              setTargetSchemaId('')
            }}
          />
        </FormField>
      )}
      {sourceId && accounts.length === 0 && !accountsLoading ? (
        <NoServiceAccountsLink t={t} kind="source" />
      ) : (
        <FormField
          id="plugin-jira-pull-account"
          label={t('task.service_account', 'Service account')}
          required
        >
          <SelectInput
            id="plugin-jira-pull-account"
            data-testid="plugin-jira-pull-account"
            value={serviceAccountId}
            placeholder={t('task.service_account_placeholder', 'Select a service account…')}
            options={accounts.map((account) => ({ value: account.id, label: account.name }))}
            disabled={disabled || !sourceId || !connectionReady}
            onChange={(event) => {
              setServiceAccountId(event.target.value)
              setJiraSchemaId('')
            }}
          />
        </FormField>
      )}
      </div>

      {sourceId && connectionError && (
        <p role="alert" className="text-xs text-destructive" data-testid="plugin-jira-pull-connection-error">
          {connectionErrorObj?.response?.status === 404
            ? t('task.connection_missing', 'This Source has no Jira plugin connection. Configure it under Sources before starting a pull.')
            : t('task.connection_check_failed', 'The Jira plugin connection could not be verified. Pull stays blocked until the connection can be checked.')}
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
      <FormField
        id="plugin-jira-pull-schema"
        label={t('task.jira_schema', 'Jira schema')}
        help={t('task.jira_schema_help', 'Pick one schema to pull only that schema.')}
      >
        <SelectInput
          id="plugin-jira-pull-schema"
          data-testid="plugin-jira-pull-schema"
          value={jiraSchemaId}
          placeholder={t('task.jira_schema_all', 'All linked Jira schemas in this Source')}
          options={schemaOptions}
          disabled={disabled || !serviceAccountId || !connectionReady}
          onChange={(event) => {
            setJiraSchemaId(event.target.value)
            setTargetSchemaId('')
          }}
        />
      </FormField>
      {adoptionPossible && (
        <FormField
          id="plugin-jira-pull-target-schema"
          label={t('task.target_schema', 'Core schema')}
          help={t(
            'task.target_schema_help',
            'Pull into an existing empty schema of this Source to keep its name and key prefix, or create a new schema named after the Jira schema.'
          )}
        >
          <SelectInput
            id="plugin-jira-pull-target-schema"
            data-testid="plugin-jira-pull-target-schema"
            value={targetSchemaId}
            placeholder={t('task.target_schema_new', 'Create a new schema')}
            options={adoptableOptions}
            disabled={disabled}
            onChange={(event) => setTargetSchemaId(event.target.value)}
          />
        </FormField>
      )}
      </div>
      {adoptionPossible && adoptableLoaded && adoptableOptions.length === 0 && (
        <p className="text-xs text-muted-foreground" data-testid="plugin-jira-pull-no-adoptable">
          {t(
            'task.target_schema_none',
            'This Source has no empty schema to pull into. To choose the name and key prefix yourself, create an empty schema for this Source first.'
          )}
        </p>
      )}
      {schemaData && !schemasLoading && !schemasError && (
        <p role="status" className="text-sm font-medium text-foreground" data-testid="plugin-jira-pull-core-target">
          {selectedSchema
            ? selectedSchema.core_schema_id
              ? interpolateDefault(t('task.core_target_linked', 'Core target — linked: {{schema}}'), { schema: selectedSchema.core_schema_name })
              : targetSchemaId
                ? interpolateDefault(t('task.core_target_adopt', 'Core target — the existing schema {{schema}} will be linked.'), {
                    schema: adoptableOptions.find((option) => option.value === targetSchemaId)?.label || '',
                  })
                : t('task.core_target_new', 'Core target — a new schema will be created.')
            : linkedSchemaNames.length > 0
              ? interpolateDefault(t('task.core_targets_all', 'Core targets — linked: {{schemas}}'), { schemas: linkedSchemaNames.join(', ') })
              : t('task.core_target_none', 'No linked Core schema. Select a Jira schema to create a new Core schema.')}
        </p>
      )}
      {serviceAccountId && schemasLoading && (
        <p className="text-xs text-muted-foreground" data-testid="plugin-jira-schema-loading">
          {t('task.jira_schema_loading', 'Loading Jira schemas…')}
        </p>
      )}
      {serviceAccountId && schemasError && (
        <p className="text-xs text-destructive" data-testid="plugin-jira-schema-error">
          {interpolateDefault(
            t(
              'task.jira_schema_load_failed',
              "Couldn't load the Jira schemas: {{message}}. The pull can still run for all schemas."
            ),
            { message: schemasErrorObj?.message || '' }
          )}
        </p>
      )}

      <CheckboxField
        id="plugin-jira-pull-comments"
        data-testid="plugin-jira-pull-comments"
        label={t('task.include_comments', 'Include comments and attachments')}
        help={t(
          'task.include_comments_help',
          'Also imports Jira comments and attachments. The pull takes longer because each object needs extra Jira requests.'
        )}
        checked={includeCommentsFiles}
        disabled={disabled}
        onChange={(event) => setIncludeCommentsFiles(event.target.checked)}
      />
    </div>
  )
}

// S1 Push (D-02): whole selected schema, no ObjectType filter — Schema ->
// Target -> target-bound Service account. The account is required scope.
export function PushForm({ onChange, disabled }) {
  const { t } = usePluginTranslation('jira')
  const [schemaId, setSchemaId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [serviceAccountId, setServiceAccountId] = useState('')

  useEffect(() => {
    onChange(buildPushFormState({}))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    onChange(buildPushFormState({ schemaId, targetId, serviceAccountId }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schemaId, targetId, serviceAccountId])

  const { data: schemasData = [] } = useSchemas(null, 0, 200, { enabled: true })
  const schemas = normalizeList(schemasData)
  const { data: targetsData } = useTargets(0, 200)
  const jiraTargets = useMemo(
    () => filterJiraTargets(normalizeList(targetsData), schemaId),
    [targetsData, schemaId]
  )
  const { accounts, isLoading: accountsLoading } = useEligibleAccounts(null, targetId)
  const { data: targetConnectionData } = useQuery({
    queryKey: ['plugin-jira', 'target-connection', targetId],
    queryFn: () => api.get(`plugins/jira/connections/target/${targetId}`).then((response) => response.data),
    enabled: !!targetId,
    retry: false,
  })
  usePresetAccount(targetId, targetConnectionData, accounts, serviceAccountId, setServiceAccountId)

  return (
    <div className="space-y-4" data-testid="plugin-jira-task-form-push">
      <FormField id="plugin-jira-push-schema" label={t('task.schema', 'Schema')} required>
        <SelectInput
          id="plugin-jira-push-schema"
          data-testid="plugin-jira-push-schema"
          value={schemaId}
          placeholder={t('task.schema_placeholder', 'Select a schema…')}
          options={schemas.map((schema) => ({ value: schema.id, label: schema.name }))}
          disabled={disabled}
          onChange={(event) => {
            setSchemaId(event.target.value)
            setTargetId('')
            setServiceAccountId('')
          }}
        />
      </FormField>

      {jiraTargets.length === 0 ? (
        <NoJiraTargetsLink t={t} />
      ) : (
        <FormField id="plugin-jira-push-target" label={t('task.target', 'Target')} required>
          <SelectInput
            id="plugin-jira-push-target"
            data-testid="plugin-jira-push-target"
            value={targetId}
            placeholder={t('task.target_placeholder', 'Select a Target…')}
            options={jiraTargets.map((target) => ({ value: target.id, label: target.name }))}
            disabled={disabled}
            onChange={(event) => {
              setTargetId(event.target.value)
              setServiceAccountId('')
            }}
          />
        </FormField>
      )}

      {targetId && accounts.length === 0 && !accountsLoading ? (
        <NoServiceAccountsLink t={t} kind="target" />
      ) : (
        <FormField
          id="plugin-jira-push-account"
          label={t('task.service_account', 'Service account')}
          required
        >
          <SelectInput
            id="plugin-jira-push-account"
            data-testid="plugin-jira-push-account"
            value={serviceAccountId}
            placeholder={t('task.service_account_placeholder', 'Select a service account…')}
            options={accounts.map((account) => ({ value: account.id, label: account.name }))}
            disabled={disabled || !targetId}
            onChange={(event) => setServiceAccountId(event.target.value)}
          />
        </FormField>
      )}
    </div>
  )
}

function preflightMessage(t, preflight) {
  switch (preflight.status) {
    case 'loading':
      return t('task.precheck_checking', 'Checking whether the destination is empty…')
    case 'empty':
      return t('task.precheck_empty', 'The destination is empty. Ready to export.')
    case 'not_empty':
      return interpolateDefault(
        t(
          preflight.count === 1 ? 'task.precheck_not_empty_one' : 'task.precheck_not_empty_other',
          preflight.count === 1
            ? 'The destination already has {{count}} object type. Export needs an empty Jira schema, so pick an empty one or push instead.'
            : 'The destination already has {{count}} object types. Export needs an empty Jira schema, so pick an empty one or push instead.'
        ),
        { count: preflight.count ?? 0 }
      )
    case 'failed':
      return null
    default:
      return null
  }
}

// S1 Export (D-03): Schema -> Target -> Service account -> Destination Jira
// schema -> pre-check. Submission stays blocked until the destination is
// proven empty; the server re-checks at run time.
export function ExportForm({ onChange, disabled }) {
  const { t } = usePluginTranslation('jira')
  const [schemaId, setSchemaId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [serviceAccountId, setServiceAccountId] = useState('')
  const [jiraSchemaId, setJiraSchemaId] = useState('')
  const [precheckAttempt, setPrecheckAttempt] = useState(0)

  const { data: schemasData = [] } = useSchemas(null, 0, 200, { enabled: true })
  const schemas = normalizeList(schemasData)
  const { data: targetsData } = useTargets(0, 200)
  const jiraTargets = useMemo(
    () => filterJiraTargets(normalizeList(targetsData), schemaId),
    [targetsData, schemaId]
  )
  const { accounts, isLoading: accountsLoading } = useEligibleAccounts(null, targetId)

  const {
    data: destData,
    isLoading: destinationsLoading,
    isError: destinationsError,
    error: destinationsErrorObj,
  } = useQuery({
    queryKey: ['plugin-jira', 'target-schemas', targetId, serviceAccountId],
    queryFn: () =>
      api
        .get(`plugins/jira/schemas/target/${targetId}`, {
          params: { service_account_id: serviceAccountId },
        })
        .then((response) => response.data),
    enabled: !!targetId && !!serviceAccountId,
  })
  const destinations = destData?.schemas || []

  const preflightKey = exportPreflightKey({ targetId, jiraSchemaId, serviceAccountId })
  const {
    data: countReply,
    isLoading: countLoading,
    isError: countError,
    error: countErrorObj,
    refetch: refetchCount,
  } = useQuery({
    queryKey: ['plugin-jira', 'destination-count', targetId, jiraSchemaId, serviceAccountId, precheckAttempt],
    queryFn: ({ queryKey: [, , replyTargetId, replyJiraSchemaId, replyAccountId] }) => {
      const requestKey = exportPreflightKey({
        targetId: replyTargetId,
        jiraSchemaId: replyJiraSchemaId,
        serviceAccountId: replyAccountId,
      })
      return api
        .get(`plugins/jira/schemas/target/${replyTargetId}/${replyJiraSchemaId}/object-type-count`, {
          params: { service_account_id: replyAccountId },
        })
        .then(
          (response) => ({ requestKey, data: response.data }),
          (error) => {
            throw Object.assign(error || new Error('preflight failed'), { preflightRequestKey: requestKey })
          }
        )
    },
    enabled: !!preflightKey,
  })
  const preflight = exportPreflightState({
    currentKey: preflightKey,
    requestKey: (countError ? countErrorObj?.preflightRequestKey : countReply?.requestKey) || '',
    loading: countLoading,
    data: countReply?.data ?? null,
    error: countError ? countErrorObj || new Error('preflight failed') : null,
  })

  useEffect(() => {
    onChange(
      buildExportFormState({
        schemaId,
        targetId,
        serviceAccountId,
        jiraSchemaId,
        destinationEmpty: preflight.empty,
      })
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schemaId, targetId, serviceAccountId, jiraSchemaId, preflight.empty])

  const preflightText = preflightMessage(t, preflight)

  return (
    <div className="space-y-4" data-testid="plugin-jira-task-form-export">
      <FormField id="plugin-jira-export-schema" label={t('task.schema', 'Schema')} required>
        <SelectInput
          id="plugin-jira-export-schema"
          data-testid="plugin-jira-export-schema"
          value={schemaId}
          placeholder={t('task.schema_placeholder', 'Select a schema…')}
          options={schemas.map((schema) => ({ value: schema.id, label: schema.name }))}
          disabled={disabled}
          onChange={(event) => {
            setSchemaId(event.target.value)
            setTargetId('')
            setServiceAccountId('')
            setJiraSchemaId('')
          }}
        />
      </FormField>

      {jiraTargets.length === 0 ? (
        <NoJiraTargetsLink t={t} />
      ) : (
        <FormField id="plugin-jira-export-target" label={t('task.target', 'Target')} required>
          <SelectInput
            id="plugin-jira-export-target"
            data-testid="plugin-jira-export-target"
            value={targetId}
            placeholder={t('task.target_placeholder', 'Select a Target…')}
            options={jiraTargets.map((target) => ({ value: target.id, label: target.name }))}
            disabled={disabled}
            onChange={(event) => {
              setTargetId(event.target.value)
              setServiceAccountId('')
              setJiraSchemaId('')
            }}
          />
        </FormField>
      )}

      {targetId && accounts.length === 0 && !accountsLoading ? (
        <NoServiceAccountsLink t={t} kind="target" />
      ) : (
        <FormField
          id="plugin-jira-export-account"
          label={t('task.service_account', 'Service account')}
          required
        >
          <SelectInput
            id="plugin-jira-export-account"
            data-testid="plugin-jira-export-account"
            value={serviceAccountId}
            placeholder={t('task.service_account_placeholder', 'Select a service account…')}
            options={accounts.map((account) => ({ value: account.id, label: account.name }))}
            disabled={disabled || !targetId}
            onChange={(event) => {
              setServiceAccountId(event.target.value)
              setJiraSchemaId('')
            }}
          />
        </FormField>
      )}

      <FormField
        id="plugin-jira-export-destination"
        label={t('task.destination', 'Destination Jira schema')}
        required
      >
        <SelectInput
          id="plugin-jira-export-destination"
          data-testid="plugin-jira-export-destination"
          value={jiraSchemaId}
          placeholder={t('task.destination_placeholder', 'Select a destination…')}
          options={destinations.map((schema) => ({
            value: schema.jira_schema_id,
            label: schema.key ? `${schema.name} (${schema.key})` : schema.name,
          }))}
          disabled={disabled || !serviceAccountId}
          onChange={(event) => setJiraSchemaId(event.target.value)}
        />
      </FormField>
      {serviceAccountId && destinationsLoading && (
        <p className="text-xs text-muted-foreground" data-testid="plugin-jira-destination-loading">
          {t('task.jira_schema_loading', 'Loading Jira schemas…')}
        </p>
      )}
      {serviceAccountId && destinationsError && (
        <p className="text-xs text-destructive" data-testid="plugin-jira-destination-error">
          {interpolateDefault(
            t(
              'task.destination_load_failed',
              "Couldn't load the Jira schemas for this Target: {{message}}. Check the service account, then choose the Target again."
            ),
            { message: destinationsErrorObj?.message || '' }
          )}
        </p>
      )}

      {preflight.status !== 'idle' && (
        <div className="flex items-center gap-2 text-xs" data-testid="plugin-jira-export-precheck">
          {preflight.status === 'empty' ? (
            <LucideIcon name="circle-check" size={12} className="text-green-700 dark:text-green-400" aria-hidden="true" />
          ) : preflight.status === 'loading' ? (
            <LucideIcon name="loader-circle" size={12} className="text-muted-foreground motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <LucideIcon name="circle-x" size={12} className="text-destructive" aria-hidden="true" />
          )}
          <span
            role="status"
            aria-live="polite"
            className={
              preflight.status === 'empty'
                ? 'text-green-700 dark:text-green-400'
                : preflight.status === 'loading'
                  ? 'text-muted-foreground'
                  : 'text-destructive'
            }
          >
            {preflight.status === 'failed'
              ? interpolateDefault(
                  t(
                    'task.precheck_failed',
                    "Couldn't check the destination: {{message}}. Export stays blocked until the check succeeds."
                  ),
                  { message: countErrorObj?.message || '' }
                )
              : preflightText}
          </span>
          {preflight.status === 'failed' && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="plugin-jira-export-recheck"
              onClick={() => {
                setPrecheckAttempt((value) => value + 1)
                refetchCount()
              }}
            >
              {t('task.precheck_retry', 'Check again')}
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
