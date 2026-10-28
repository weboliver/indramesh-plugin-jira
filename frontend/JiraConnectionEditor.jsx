// Jira plugin connector config editor (Phase 295, JUI-02, D-05..D-08).
//
// The host renders this through PluginConnectorEditorGroup for
// connector_type 'jira' and role source/target. It owns NO Core field and never
// collects credentials: the connection test uses a bound Core ServiceAccount id
// only, and that account is never written into Core config or the plugin
// connection.
//
// Two-store save (R2-01/R2-03): the host modal saves the Core Source/Target;
// this module's `afterCoreSave` (registered on the descriptor) then binds or
// updates the plugin-owned plugin_jira_connections row through
// PUT /connections/{kind}/{id}, and only when one of the R2-03 conditions holds.
// The editor reports its own persistence payload back to the host via the
// separate `draft`/`onDraftChange` connector contribution state — never through
// Core `config`.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Button,
  FormField,
  LucideIcon,
  SelectInput,
  api,
  interpolateDefault,
  pluginText,
  usePluginTranslation,
  useServiceAccounts,
} from '@indramesh/plugin-api'
import {
  canRunConnectionTest,
  defaultAccountSelection,
  connectionTestKey,
  decideConnectionWrite,
  hasUnsavedUrlChange,
  selectValueToStrategy,
  strategyToSelectValue,
} from './connection-logic.js'
import { filterEligibleAccounts } from './task-form-logic.js'

const KIND_BY_ROLE = { source: 'source', target: 'target' }
const URL_KEY_BY_KIND = { source: 'source_url', target: 'target_url' }

// D-06: the English fallback per server category (UI-SPEC S2 copywriting). Only
// `ok` gets success styling; every other category is an error line.
const TEST_DEFAULTS = {
  ok: 'Connected. Jira accepted the service account.',
  auth: "Jira rejected the service account's credentials. Update its token under Service accounts, then test again.",
  permission:
    "Jira accepted the sign-in but the account can't read Assets schemas. Grant it Assets access in Jira, then test again.",
  network:
    "Jira didn't respond at {{url}}. Check the address and that Jira is reachable from IndraMesh.",
  not_found: 'Jira has no schema with this ID. Check the schema, then test again.',
  error:
    'The connection test failed. Check the address and service account, then test again.',
}

// The descriptor-level `afterCoreSave` is a plain function the host calls outside
// the React tree, so it resolves its rejection copy through the host-assigned
// plugin namespace (`pluginText`) instead of a translator captured from a render.
function localize(key, fallback, vars) {
  const text = pluginText('jira', key, { defaultValue: fallback })
  return vars ? interpolateDefault(text, vars) : text
}

// R2-01/R2-03: the generic post-Core-save hook the descriptor registers. A
// rejection carries an already localized plugin message; the host shows it
// inside its connector-neutral partial-save copy and keeps the saved id.
export async function afterCoreSaveJiraConnection({ role, record, previousRecord, draft } = {}) {
  const kind = KIND_BY_ROLE[role]
  const endpointId = record?.id
  if (!kind || !endpointId) return

  const urlKey = URL_KEY_BY_KIND[kind]
  const urlChanged = previousRecord
    ? hasUnsavedUrlChange({ draftUrl: record?.[urlKey], savedUrl: previousRecord?.[urlKey] })
    : false

  const decision = decideConnectionWrite({
    loaded: draft?.loaded === true,
    fetchFailed: draft?.fetchFailed === true,
    connection: draft?.connection ?? null,
    strategy: draft?.strategy ?? null,
    serviceAccountId: draft?.serviceAccountId,
    urlChanged,
  })
  if (decision.action !== 'write') return

  try {
    await api.put(`plugins/jira/connections/${kind}/${endpointId}`, decision.body)
  } catch (err) {
    const status = err?.response?.status
    if (status === 422) {
      throw new Error(
        localize(
          'connector.bind_invalid_url',
          'The {{kind}} needs a valid http(s) address before the Jira connection can be saved.',
          { kind }
        )
      )
    }
    if (status === 404) {
      throw new Error(
        localize(
          'connector.bind_not_jira',
          'This {{kind}} is no longer a Jira {{kind}}, so no Jira connection can be saved.',
          { kind }
        )
      )
    }
    throw new Error(
      localize(
        'connector.bind_failed',
        'The Jira connection could not be saved. Check the address and try again.'
      )
    )
  }
}

export default function JiraConnectionEditor({
  role,
  record,
  draft = {},
  onDraftChange,
  draftFields = {},
}) {
  const { t } = usePluginTranslation('jira')

  const kind = KIND_BY_ROLE[role] || 'source'
  const recordId = record?.id || null
  // A ref keeps the latest draft without making the query effect re-run on every
  // parent state change (which would loop through onDraftChange).
  const draftRef = useRef(draft)
  draftRef.current = draft

  const { data, isLoading, isError } = useQuery({
    queryKey: ['plugin-jira', 'connection', kind, recordId],
    queryFn: async () => {
      try {
        const response = await api.get(`plugins/jira/connections/${kind}/${recordId}`)
        return response.data?.connection ?? null
      } catch (err) {
        // R2-03: 404 means "not bound yet", not an error.
        if (err?.response?.status === 404) return null
        throw err
      }
    },
    enabled: !!recordId,
    retry: false,
  })

  useEffect(() => {
    const base = draftRef.current || {}
    if (!recordId) {
      // A new record has no id yet, so it is implicitly unbound and afterCoreSave
      // must bind it once the host has created it.
      onDraftChange({ ...base, connection: null, loaded: true, fetchFailed: false })
      return
    }
    if (isLoading) return
    if (isError) {
      // R2-03: 403/network -> load error, and no write.
      onDraftChange({ ...base, connection: null, loaded: false, fetchFailed: true })
      return
    }
    onDraftChange({
      ...base,
      strategy: data ? data.default_conflict_strategy ?? null : base.strategy ?? null,
      connection: data ?? null,
      loaded: true,
      fetchFailed: false,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordId, isLoading, isError, data])

  const connection = draft.connection ?? null
  const loaded = draft.loaded === true
  const fetchFailed = draft.fetchFailed === true
  const stale = connection?.binding === 'stale'
  const strategyValue = strategyToSelectValue(draft.strategy ?? null)
  const savedUrl = record?.[URL_KEY_BY_KIND[kind]] ?? ''
  const urlChanged = !!recordId && hasUnsavedUrlChange({ draftUrl: draftFields.url, savedUrl })
  const selectDisabled = !loaded || fetchFailed || stale

  const roleHelp =
    kind === 'source'
      ? t('connector.role_help_source', 'Used when pulling from Jira.')
      : t('connector.role_help_target', 'Used when pushing to Jira.')

  // D-05/D-07: the test lists only accounts bound to this record and filters the
  // obviously unusable ones; the server `_load_account` check stays authoritative.
  const [serviceAccountId, setServiceAccountId] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)

  const { data: accountsData, isLoading: accountsLoading } = useServiceAccounts(
    kind === 'source' ? recordId : null,
    0,
    50,
    kind === 'target' ? recordId : null,
    { enabled: !!recordId }
  )
  const accounts = useMemo(
    () => filterEligibleAccounts(accountsData || [], Date.now()),
    [accountsData]
  )

  // 261004: the connection's stored default account preselects the picker until
  // the user picks one; the choice is saved with the connector settings.
  const [accountTouched, setAccountTouched] = useState(false)
  useEffect(() => {
    if (accountTouched) return
    const preset = defaultAccountSelection(data?.default_service_account_id, accounts)
    if (preset) setServiceAccountId(preset)
  }, [data, accounts, accountTouched])

  // A new account or record must not keep showing a stale result.
  useEffect(() => {
    setTestResult(null)
  }, [serviceAccountId, recordId])

  const hasRecord = !!recordId
  const noAccounts = hasRecord && !accountsLoading && accounts.length === 0
  const testDisabled = !canRunConnectionTest({ recordId, serviceAccountId, testing })

  const runTest = async () => {
    if (!canRunConnectionTest({ recordId, serviceAccountId, testing })) return
    setTesting(true)
    setTestResult(null)
    try {
      const response = await api.post(`plugins/jira/connections/${kind}/${recordId}/test`, {
        service_account_id: serviceAccountId,
      })
      setTestResult({ category: response.data?.category ?? 'error' })
    } catch {
      setTestResult({ category: 'error' })
    } finally {
      setTesting(false)
    }
  }

  const manageLink = (
    <a
      href="/configuration/service-accounts"
      className="text-primary hover:underline underline-offset-4"
    >
      {t('connector.manage_link', 'Manage service accounts')}
    </a>
  )

  const resultCategory = testResult?.category ?? 'error'
  const resultOk = resultCategory === 'ok'
  const resultText = t(
    connectionTestKey(resultCategory),
    TEST_DEFAULTS[resultCategory] || TEST_DEFAULTS.error,
    { url: savedUrl || '—' }
  )

  return (
    <div className="space-y-4" data-testid="plugin-jira-connection-editor">
      <FormField
        id="plugin-jira-connector-strategy"
        label={t('connector.strategy_label', 'Default conflict handling')}
        help={roleHelp}
      >
        <SelectInput
          id="plugin-jira-connector-strategy"
          data-testid="plugin-jira-strategy"
          value={strategyValue}
          disabled={selectDisabled}
          options={[
            { value: '', label: t('connector.strategy_manual', 'Review manually') },
            {
              value: 'prefer_local',
              label: t('connector.strategy_prefer_local', 'Prefer IndraMesh values'),
            },
            {
              value: 'prefer_remote',
              label: t('connector.strategy_prefer_remote', 'Prefer Jira values'),
            },
          ]}
          onChange={(event) => {
            onDraftChange({
              ...(draftRef.current || {}),
              strategy: selectValueToStrategy(event.target.value),
            })
          }}
        />
      </FormField>
      <p className="text-xs text-muted-foreground" data-testid="plugin-jira-strategy-scope">
        {t(
          'connector.scope_help',
          'Applies to conflicts found from now on. Conflicts that are already open keep their state.'
        )}
      </p>

      {fetchFailed && (
        <p className="text-xs text-destructive" data-testid="plugin-jira-connection-load-error">
          {t(
            'connector.load_failed',
            "Couldn't load the Jira connection settings. Reopen the dialog to try again."
          )}
        </p>
      )}

      {stale && (
        <p className="text-xs text-amber-600 dark:text-amber-500" data-testid="plugin-jira-stale">
          {t(
            'connector.stale_state',
            'The address changed after this connection was bound, so Jira synchronisation is locked for this record. Re-binding is not available yet.'
          )}
        </p>
      )}

      {urlChanged && !stale && loaded && !fetchFailed && (
        <p
          className="text-xs text-amber-600 dark:text-amber-500"
          data-testid="plugin-jira-url-changed"
        >
          {t(
            'connector.url_changed_warning',
            'You changed the address. Saving it locks Jira synchronisation for this record because re-binding is not available yet.'
          )}
        </p>
      )}

      <div className="space-y-2" data-testid="plugin-jira-connection-test">
        {hasRecord && (
          <FormField
            id="plugin-jira-test-account"
            label={t('task.service_account', 'Service account')}
            help={t(
              'connector.account_help',
              'Saved with the connector settings and preselected for the connection test and Jira tasks.'
            )}
          >
            <SelectInput
              id="plugin-jira-test-account"
              data-testid="plugin-jira-test-account"
              value={serviceAccountId}
              placeholder={t('task.service_account_placeholder', 'Select a service account…')}
              options={accounts.map((account) => ({ value: account.id, label: account.name }))}
              disabled={noAccounts || testing}
              onChange={(event) => {
                setServiceAccountId(event.target.value)
                setAccountTouched(true)
                onDraftChange({
                  ...(draftRef.current || {}),
                  serviceAccountId: event.target.value,
                })
              }}
            />
          </FormField>
        )}

        {hasRecord && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            leftIcon="plug-zap"
            loading={testing}
            disabled={testDisabled}
            data-testid="plugin-jira-test-button"
            onClick={runTest}
          >
            {t('connector.test_button', 'Test connection')}
          </Button>
        )}

        {hasRecord && (
          <p className="text-xs text-muted-foreground" data-testid="plugin-jira-tested-url">
            {urlChanged
              ? t('connector.unsaved_url_hint', 'Save first to test the address you just entered.')
              : interpolateDefault(
                  t('connector.tested_url_hint', 'Tests the saved address {{url}}.'),
                  { url: savedUrl || '—' }
                )}
          </p>
        )}

        {!hasRecord && (
          <p className="text-xs text-muted-foreground" data-testid="plugin-jira-test-new-record">
            {interpolateDefault(
              t(
                'connector.new_record_test_disabled',
                'Save this {{kind}} first. The connection test is available afterwards: assign a service account to this {{kind}} and test here.'
              ),
              { kind }
            )}{' '}
            {manageLink}
          </p>
        )}

        {noAccounts && (
          <p className="text-xs text-muted-foreground" data-testid="plugin-jira-test-no-accounts">
            {interpolateDefault(
              t(
                'connector.no_account_test_disabled',
                'Add a service account for this {{kind}} to test the connection.'
              ),
              { kind }
            )}{' '}
            {manageLink}
          </p>
        )}

        {testResult && (
          <p
            role="status"
            aria-live="polite"
            data-testid="plugin-jira-test-result"
            className={
              resultOk
                ? 'flex items-center gap-2 text-sm text-green-700 dark:text-green-400'
                : 'flex items-center gap-2 text-sm text-destructive'
            }
          >
            <LucideIcon
              name={resultOk ? 'circle-check' : 'circle-x'}
              size={12}
              aria-hidden="true"
            />
            {resultText}
          </p>
        )}
      </div>
    </div>
  )
}
