# jira plugin

The **Jira Assets connector** for IndraMesh Core: bidirectional pull, push and export between Core
objects and Jira Assets, with per-level sync direction, conflict handling, resumable checkpoints
and an audit trail.

## Install

The plugin is distributed as its own repository and installed into a running IndraMesh Core stack.
**Requires IndraMesh Core 1.0.0 or later** (plugin API `1.0.0`). It is a read-only mirror of the
maintainer's develop repository, so it is **not standalone buildable** today — the plugin SDK ships
in a later release; the committed frontend bundle in this repository is the supported distribution
format.

**Compose stack** — from the Core checkout root:

```bash
./install_plugin_docker.sh https://github.com/weboliver/indramesh-plugin-jira
```

The script clones the repository, copies the plugin to `<core>/plugins/jira` and restarts `backend`
and `worker` so the plugin registry re-scans. The destination directory name comes from the plugin's
own manifest; a second argument overrides it only if you deliberately want a different name. The
same command accepts a local directory instead of the URL.

**Local k3s stack** — from the Core checkout root, with a kubeconfig for the cluster
(`KUBECONFIG=$HOME/.kube/config` when it is not already at the default location):

```bash
./install_plugin_k8s.sh https://github.com/weboliver/indramesh-plugin-jira
```

The script copies the plugin directory onto the plugins PVC and restarts `backend` and `worker`.

A URL source accepts an optional ref (tag or branch) as a further argument; without one the default
branch is installed.

**Manual equivalent:** copy the plugin directory to `<core>/plugins/jira` yourself and restart
`backend` **and** `worker`. Plugin discovery runs exactly once at process startup — there is no
live filesystem watch — so both processes must be restarted before the plugin is registered.

**Replacing an installed plugin:** both installers refuse an existing destination and neither offers
a way around that refusal. An installed plugin is replaced by removing its directory deliberately
first — `plugins/jira` in a Compose checkout, the plugin's directory on the plugins PVC on
Kubernetes — and installing again. See **Installing and Updating Plugins** in the Core README for the removal commands, including the writable helper pod for the k3s case.

## Purpose

All Jira-specific protocol, orchestration and state live here — Core is Jira-free. Core stores only
neutral `managed_by` provenance and write-protect markers; Jira links, sync directions, cloud
configuration, conflict strategy and reconciliation state live in the plugin's own `plugin_jira_*`
tables on a private Alembic chain. Pull is resumable through plugin-owned checkpoints, push honors
link direction and same-instance checks, and export creates into an empty remote destination
without persisting links.

- Manifest: `plugin.toml` (`capabilities = ["api_routes", "task_type", "connector"]`,
  `requires_worker_update = true`).
- `active_by_default` is not declared — the manifest schema default (`true`) applies, and an
  administrator can disable the plugin.
- Backend: `module.py` (declarations) + `pull.py` / `push.py` / `export.py` (engines) +
  `connections.py`, `conflicts.py`, `checkpoints.py`, `client.py`, `admission.py`, `database.py`.
- Frontend: `frontend/`; committed bundle `static/jira.js`.

## Permissions

The plugin declares **no plugin-owned permission** (`plugin.toml` has no `[[permissions]]` entry);
everything gates on existing Core permissions:

| Core permission | Used for |
|---|---|
| `sync:read` | Reading connections, sync status and conflicts through the plugin's routes. |
| `sync:trigger` | Connection test, pull/push/export starts, and all three task types. |
| `tasks:create` | Creating a Jira task through the Tasks API. |
| `plugins:read` / `plugins:manage` | The Core plugin administration surfaces. |

Credentials are never plugin data: the task-bound service account's Jira bearer token is the only
credential the run sees.

## Configuration

- **No `[config_schema]`:** connection configuration is plugin-owned data
  (`plugin_jira_connections`, one connection bound to exactly one Source or Target), edited through
  the plugin's own routes and UI. `JIRA_URL` is not the plugin's connection configuration.
- **`[database]`:** `migrations_dir = "migrations"`, `version_table = "alembic_version_jira"`,
  `require_restricted_role = true` — the plugin's migrations run under the dedicated non-owner
  `indramesh_plugin` role.
- **`requires_worker_update = true`:** the worker must have loaded the same plugin version to
  dispatch its task types; restart `backend` and `worker` after changing the plugin directory.
- No LLM and no key requirement — "no key = off" does not apply to this plugin.

## Task types

| Task type | Required permission | Scope / schedulability |
|---|---|---|
| `jira.sync-pull` | `sync:trigger` | Source-scoped; schedulable. |
| `jira.sync-push` | `sync:trigger` | Schema + Target-scoped; schedulable. |
| `jira.sync-export` | `sync:trigger` | Schema + Target-scoped; **not** schedulable (requires an empty remote destination). |

## Connectors

One connector type, `jira` (label `Jira`), accepting the `source` and `target` roles;
Source/Target URLs and service-account credentials stay Core-owned.

## Frontend

Bundle `static/jira.js`; eight extension points: `pages`, `navItems`, `taskForms`,
`connectorConfigEditors`, `schemaSettingsSections`, `objectTypeSettingsSections`,
`objectDetailSections`, `itemBadges`. Delivery uses the standard plugin mount over the read-only
`./plugins` mount (Compose) or the plugins PVC (k8s); the plugin's migration chain runs as the
restricted role before the workloads serve it.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).

The committed bundle `static/jira.js` is built from `plugins/jira/frontend/` (JSX sources) by the
**Core** repository's Vite toolchain — this repository carries no build tooling of its own:

```bash
# In the Core checkout, with this plugin directory at plugins/jira:
npm --prefix frontend run build:plugin-jira
```

To rebuild the shipped bundle from its source, check out the Core repository at the tag that
matches the Core release this plugin was published with — for the 1.0.0 release line,
<https://github.com/weboliver/indramesh-core/tree/v1.0.0> — and run that script; it writes
`plugins/jira/static/jira.js` in place.

## Status

This repository is a **read-only mirror** of the private develop repository: it is regenerated
from the Core source tree at release time, so issues and pull requests are applied upstream by
hand rather than merged here.
