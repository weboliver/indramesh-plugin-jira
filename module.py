"""Jira directory plugin (Phase 292, JDATA-01; cut-over Phase 296/CLEAN-01).

The plugin package that owns the Jira integration end to end. As of the Phase
296 cut-over it is the sole owner of the ``jira`` connector identity, the
``jira.*`` task types, the Jira API routes and the plugin frontend bundle:

* ``connector_types()`` publishes the one ``jira`` connector (source + target)
  through the public SDK; Core no longer seeds a built-in descriptor for it and
  the transitional ``TRANSITIONAL_UNPUBLISHED_OWNERS`` seam is deleted;
* ``task_types()`` publishes the pull/push/export task types;
* ``database_metadata`` exposes the plugin's OWN ``MetaData`` so the Core
  migration runner can migrate ``plugin_jira_*`` tables under a verified
  restricted role.

Imports only the public ``indramesh.sdk`` surface. It must never import a Core
``app.*`` module (the AST gate in ``tests/test_plugin_discovery.py`` enforces
this), and it must never import a Core Jira module (ROADMAP SC1).
"""

from pathlib import Path

from pydantic import BaseModel

from indramesh.sdk import (
    ApiRoutesPlugin,
    ConnectorPlugin,
    ConnectorTypeDeclaration,
    TaskTypeDeclaration,
    TaskTypePlugin,
)

from .admission import admit_export, admit_pull, admit_push
from .export import run_export
from .pull import run_pull
from .push import run_push
from .routes import register_jira_routes

# Phase 293 (JSYNC-01/02): the first dispatchable Jira task type. It is
# permanent (D-03): a rename would be a new type, never an alias.
JIRA_PULL_TASK_TYPE = "jira.sync-pull"

# Phase 294 (JSYNC-04): the push type. Permanent like the pull (293 D-03); a
# rename would be a new type, never an alias.
JIRA_PUSH_TASK_TYPE = "jira.sync-push"

# Phase 294 (JSYNC-05): the export type. Permanent like the pull (293 D-03). It
# is deliberately NOT schedulable (JSYNC-12): an export copies into an empty
# disposable schema, so a recurring run makes no sense.
JIRA_EXPORT_TASK_TYPE = "jira.sync-export"

# Phase 296 (CLEAN-01/D-03): the plugin is now the sole owner of the `jira`
# connector identity through the public SDK. The declaration mirrors the
# descriptor Core seeded before the cut-over (source + target, a target requires
# a URL and a schema id) so persisted `jira` rows keep resolving to an active
# owner. Connector identifiers are not plugin-qualified, so the persisted string
# stays `jira` unchanged.
JIRA_CONNECTOR_TYPE = "jira"
JIRA_CONNECTOR_LABEL = "Jira"


class JiraConnectorConfig(BaseModel):
    """The plugin-owned Jira connector config contract (D-03).

    ``jira_object_type_id`` is resolved dynamically during SYNC_PUSH and is not
    required at write time; unknown keys are tolerated (the historical
    contract), so this model mainly pins the field's type. The descriptor does
    not normalize the submitted config, matching the pre-cut-over behavior.
    """

    jira_object_type_id: str | None = None

# D-04/D-05: inspectable runtime parameters, never secrets. ``object_type_id``
# optionally narrows the push to one Core ObjectType of the selected schema
# (carried as a parameter, never as a scope column — 294-RESEARCH Pattern 9).
JIRA_PUSH_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {"object_type_id": {"type": "string", "maxLength": 36}},
    "additionalProperties": False,
}

# D-04/D-05/T-294-27: the export's destination Jira schema id is user input sent
# into a Jira URL path — digits only, bounded length. Parameters are inspectable
# runtime data, never secrets.
JIRA_EXPORT_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "jira_schema_id": {
            "type": "string",
            "maxLength": 20,
            "pattern": "^[0-9]+$",
        }
    },
    "required": ["jira_schema_id"],
    "additionalProperties": False,
}

# D-04/D-05: the plugin-owned parameter schema. Parameters are ordinary
# inspectable runtime data — never secrets. `jira_schema_id` selects one Jira
# schema within the task's Source; `include_comments_files` opts into the
# (later-plan) comment/attachment import.
JIRA_PULL_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "jira_schema_id": {"type": "string", "maxLength": 100},
        # Quick 261004: an existing empty, unlinked schema of the Source a first
        # pull adopts instead of creating a new one.
        "target_schema_id": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        },
        "include_comments_files": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}


def _load_database_metadata():
    """Load ``database.py``'s private ``MetaData`` (package-relative import).

    Mirrors ``plugins/example/module.py``: returning ``None`` when the file is
    absent lets a copied plugin be neutralised (for example in tests) without
    editing Python; a present but broken ``database.py`` still fails loudly.
    """
    database_py = Path(__file__).with_name("database.py")
    if not database_py.is_file():
        return None

    from .database import Base

    return Base.metadata


class JiraPlugin(ApiRoutesPlugin, TaskTypePlugin, ConnectorPlugin):
    """The Jira directory plugin (Phase 292).

    Implements the three capabilities declared by its manifest. The API-routes
    capability is intentionally inert in this phase: the Core mount chain already
    forces authentication + ``plugin-runtime:use`` on every plugin route, and
    Phase 292 exposes no Jira HTTP surface.
    """

    database_metadata = _load_database_metadata()

    def register_routes(self, ctx):
        # Phase 293 (JSYNC-10/11/13): permission- and Source-scoped batch link
        # reads, the Source/Target Jira schema picker, the per-level direction
        # PUT and the object detach PUT. The Core mount chain still forces auth
        # and `plugin-runtime:use`; every route here is ADDITIVE beneath it.
        return register_jira_routes(ctx)

    def connector_types(self):
        # Phase 296 (CLEAN-01/D-03): the plugin declares the sole `jira` connector
        # through the public SDK. Core no longer seeds a built-in descriptor, so
        # this is the one owner of the identity across source and target writes.
        return [
            ConnectorTypeDeclaration(
                name=JIRA_CONNECTOR_TYPE,
                applies_to=frozenset({"source", "target"}),
                label=JIRA_CONNECTOR_LABEL,
                requires_url=frozenset({"target"}),
                requires_public_url=frozenset({"source", "target"}),
                requires_schema_id=frozenset({"target"}),
                config_model=JiraConnectorConfig,
            )
        ]

    def task_types(self):
        # Phase 293 (JSYNC-01/02): the Jira pull runs on disposable, plugin-owned
        # links and refuses any Source that Core still binds (see pull.py).
        # Phase 294 (JSYNC-04): the push is schedulable and scoped to a schema +
        # Target; admit_push refuses a wrong-instance or stale binding (T-294-14).
        return [
            TaskTypeDeclaration(
                name=JIRA_PULL_TASK_TYPE,
                run=run_pull,
                steps=(),
                parameter_schema=JIRA_PULL_PARAMETER_SCHEMA,
                label="Jira Sync Pull",
                schedulable=True,
                scope_requirements=("source_id",),
                required_permission="sync:trigger",
                admit=admit_pull,
                invalidates=("object",),
            ),
            TaskTypeDeclaration(
                name=JIRA_PUSH_TASK_TYPE,
                run=run_push,
                steps=(),
                parameter_schema=JIRA_PUSH_PARAMETER_SCHEMA,
                label="Jira Sync Push",
                schedulable=True,
                scope_requirements=("schema_id", "target_id"),
                required_permission="sync:trigger",
                admit=admit_push,
                invalidates=("object",),
            ),
            TaskTypeDeclaration(
                name=JIRA_EXPORT_TASK_TYPE,
                run=run_export,
                steps=(),
                parameter_schema=JIRA_EXPORT_PARAMETER_SCHEMA,
                label="Jira Sync Export",
                schedulable=False,
                scope_requirements=("schema_id", "target_id"),
                required_permission="sync:trigger",
                admit=admit_export,
            ),
        ]
