"""Typed Jira value normalization and the content-based three-way merge (293-04).

This is the plugin-side port of the Core importer's typed-value matrix
(``app/services/sync_service.py``: ``_jira_value_to_typed``,
``_select_option_id_by_value``, ``_find_matching_ov``, ``_apply_three_way_merge``)
with one deliberate change (293-PLAN-REVIEW.md F-02): the merge compares CONTENT
against the plugin baseline instead of the legacy ``updated_at > last_synced_at``
timestamp rule. ``last_synced_at`` is never written by this engine.

Why content (D-01/D-02/D-03):

* The plugin never sees the Core commit timestamp atomically through the SDK, so
  the timestamp rule would report a false local edit after a crash between the
  SDK value commit and the plugin page commit.
* Comparing ``local != baseline`` and ``remote != baseline`` removes that case:
  if the SDK write committed but the page commit did not, the replay sees
  ``local == remote`` and converges without a second write and without a
  conflict.
* The only remaining edge — a native edit inside that crash window — becomes a
  pending conflict, which is the safe outcome.

The module is pure: it never touches the database or ``app.*``.
"""

import json
from datetime import datetime

from indramesh.sdk import PluginTaskError

# Core ObjectValue typed column per Jira/Core data_type. "reference" has no
# ObjectValue column (it is an ObjectRef, owned by plan 05) and is deliberately
# absent here; callers must skip it.
TYPED_COLUMN_MAP = {
    "text": "value_simple",
    "textarea": "value_simple",
    "url": "value_simple",
    "email": "value_simple",
    "select": "value_simple",
    "user": "value_simple",
    "group": "value_simple",
    "version": "value_simple",
    "jira": "value_simple",
    "confluence": "value_simple",
    "integer": "value_integer",
    "double": "value_float",
    "float": "value_float",
    "number": "value_float",
    "boolean": "value_boolean",
    "date": "value_datetime",
    "datetime": "value_datetime",
    "json": "value_struct",
    "status": "value_struct",
}

# The six typed payload columns, in the precedence order this engine reads them.
TYPED_COLUMNS = (
    "value_simple",
    "value_integer",
    "value_float",
    "value_boolean",
    "value_datetime",
    "value_struct",
)

# Merge decisions (the closed vocabulary returned by ``merge_value``).
NO_OP = "no_op"
APPLY = "apply"
KEEP_LOCAL = "keep_local"
CONVERGE = "converge"
CONFLICT = "conflict"


def typed_column_for_data_type(data_type: str | None) -> str:
    """The Core ObjectValue column for ``data_type`` (fallback ``value_simple``)."""
    return TYPED_COLUMN_MAP.get(data_type or "", "value_simple")


def parse_jira_datetime(value) -> datetime:
    """Parse a Jira ISO timestamp into the naive-UTC shape Core stores."""
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def canonical_repr(typed_column: str, value) -> str:
    """A stable JSON string for one typed value (``sort_keys=True``, D-01).

    The representation is what the baseline and the merge compare: it is the
    canonical JSON of ``{typed_column: value}`` (or of the raw struct for
    ``value_struct``), so a DB round-trip of the same value always yields the
    same string.
    """
    if typed_column == "value_struct":
        return json.dumps(value, sort_keys=True, default=str)
    if typed_column == "value_datetime" and isinstance(value, datetime):
        return json.dumps({typed_column: value.isoformat()}, sort_keys=True)
    return json.dumps({typed_column: value}, sort_keys=True, default=str)


def canonical_set(reprs) -> str:
    """A stable JSON string for a whole attribute's value set (D-02).

    Duplicates are KEPT (a scalar attribute is a multiset) and the items are
    sorted, so two attribute groups holding the same values serialize
    byte-identically and equality is string equality. ``merge_value`` works
    unchanged on these set strings.
    """
    return json.dumps(sorted(str(item) for item in reprs))


def decode_repr(rep: str, data_type: str | None = None):
    """The ``(typed_column, value)`` a canonical repr encodes.

    ``canonical_repr`` wraps a scalar as ``{typed_column: value}`` but stores a
    ``value_struct`` (status/json) as the BARE struct, so a naive
    ``next(iter(decoded))`` reads a struct field name as the column. When
    ``data_type`` is known the column is derived from it, which disambiguates a
    one-key scalar wrapper from a bare struct and survives a json LIST value.
    """
    decoded = json.loads(rep)
    if data_type is not None:
        typed_column = typed_column_for_data_type(data_type)
        if typed_column == "value_struct":
            return "value_struct", decoded
        if isinstance(decoded, dict):
            return typed_column, decoded.get(typed_column)
        return typed_column, decoded
    if isinstance(decoded, dict):
        if len(decoded) == 1:
            key = next(iter(decoded))
            if key in TYPED_COLUMNS:
                return key, decoded[key]
        return "value_struct", decoded
    return None, decoded


def repr_from_columns(row) -> tuple[str | None, object]:
    """Extract ``(typed_column, value)`` from a Core ObjectValue-shaped mapping.

    ``row`` may be a mapping or an object exposing the six typed columns. The
    first non-``None`` column wins (the same precedence the Core model uses).
    Returns ``(None, None)`` when every column is null.
    """
    for column in TYPED_COLUMNS:
        value = row[column] if hasattr(row, "__getitem__") else getattr(row, column)
        if value is not None:
            return column, value
    return None, None


def find_option_id(structure, raw_value) -> str | None:
    """Resolve a Jira SELECT display string to the local option id (D-03/D-04).

    Exact match over the attribute's canonical ``structure.options``; the first
    match wins. Returns ``None`` when the value matches no option — the caller
    then stores the raw string rather than fabricating an option.
    """
    text = str(raw_value)
    for option in (structure or {}).get("options") or []:
        if (
            isinstance(option, dict)
            and option.get("value") == text
            and option.get("id")
        ):
            return str(option["id"])
    return None


def normalize_jira_value(data_type: str | None, raw_value, *, structure=None):
    """Coerce one raw Jira value to ``(typed_column, value, canonical_repr)``.

    Port of the legacy per-datatype ladder, including the
    fall-back-to-``value_simple``-on-coercion-failure behavior and the SELECT
    display-string → option-id resolution. ``raw_value`` of ``None`` returns
    ``(None, None, None)`` so callers can skip it.
    """
    if raw_value is None:
        return None, None, None

    typed_column = typed_column_for_data_type(data_type)
    actual = None

    if data_type == "integer":
        try:
            actual = int(raw_value)
        except (ValueError, TypeError):
            actual, typed_column = str(raw_value), "value_simple"
    elif data_type in ("double", "float", "number"):
        try:
            actual = float(raw_value)
        except (ValueError, TypeError):
            actual, typed_column = str(raw_value), "value_simple"
    elif data_type == "boolean":
        if isinstance(raw_value, bool):
            actual = raw_value
        elif isinstance(raw_value, str) and raw_value.strip().lower() in (
            "true",
            "false",
        ):
            actual = raw_value.strip().lower() == "true"
        else:
            actual, typed_column = str(raw_value), "value_simple"
    elif data_type in ("date", "datetime"):
        try:
            actual = parse_jira_datetime(raw_value)
        except (ValueError, TypeError):
            actual, typed_column = str(raw_value), "value_simple"
    elif data_type == "json":
        if isinstance(raw_value, (dict, list)):
            actual = raw_value
        else:
            actual, typed_column = str(raw_value), "value_simple"
    elif data_type == "select":
        actual = str(raw_value)
        matched = find_option_id(structure, actual)
        if matched is not None:
            actual = matched
    else:
        actual = str(raw_value)

    return typed_column, actual, canonical_repr(typed_column, actual)


def merge_value(local: str | None, remote: str | None, baseline: str | None) -> str:
    """Content-based three-way merge decision (D-01/D-02).

    ``local``/``remote``/``baseline`` are canonical value reprs (or ``None``).
    Returns exactly one of ``no_op``, ``apply``, ``keep_local``, ``converge`` or
    ``conflict``:

    * unchanged on both sides → ``no_op`` (no write);
    * remote-only change → ``apply`` (SDK write of the remote value);
    * local-only change → ``keep_local`` (never overwrite a native edit);
    * both changed to the SAME value → ``converge`` (baseline advances, no
      write — this is the crash-replay case);
    * both changed differently → ``conflict`` (one pending pull conflict, local
      value untouched).
    """
    local_changed = local != baseline
    remote_changed = remote != baseline

    if not local_changed and not remote_changed:
        return NO_OP
    if not local_changed:
        return APPLY
    if not remote_changed:
        return KEEP_LOCAL
    if local == remote:
        return CONVERGE
    return CONFLICT


def require_mapping(raw, label: str) -> dict:
    """A Jira object attribute entry, or a fatal malformed-envelope error."""
    if not isinstance(raw, dict):
        raise PluginTaskError(f"The Jira {label} entry is malformed.")
    return raw
