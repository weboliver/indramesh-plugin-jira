"""DB-free Jira Assets payload builders (294-04, D-01/D-02, bug 8).

The legacy push built one ``attributes[]`` entry per ObjectValue, so a multi-value
scalar attribute was silently reduced to its last value under Jira's
replace-whole-attribute PUT semantics. This module groups every value of one
attribute into exactly one entry and ports the per-data-type formatting ladder
from ``app/services/connectors/jira_connector._build_attribute_payload``.

It is pure: no DB, no ``app.*`` import, no I/O. It only imports the pure
``value_merge`` sibling for the remote-side normalization.
"""

import json
import re

from .value_merge import canonical_repr, decode_repr, normalize_jira_value

# Jira reports a rejected attribute as ``rlabs-insight-attribute-<id>`` in the
# 400 error body; stripping exactly those and retrying once lets the rest land.
_REJECTED_ATTR_RE = re.compile(r"rlabs-insight-attribute-(\d+)")


def parse_rejected_attribute_ids(message) -> set[int]:
    """The Jira ObjectTypeAttribute ids a 400 body named as rejected."""
    if not message:
        return set()
    return {int(match) for match in _REJECTED_ATTR_RE.findall(str(message))}


def strip_rejected(payload: dict, rejected_ids) -> dict:
    """A payload copy without the rejected attribute entries (never the conflicted one)."""
    rejected = {int(value) for value in rejected_ids}
    if not rejected:
        return payload
    return {
        **payload,
        "attributes": [
            entry
            for entry in payload.get("attributes", [])
            if int(entry.get("objectTypeAttributeId")) not in rejected
        ],
    }


def _to_jira_scalar(data_type, raw, structure, *, status_jira_ids=None):
    """One canonical local value rendered as its Jira scalar (bug 8 port)."""
    if raw is None:
        return None
    if data_type == "integer":
        try:
            return int(raw)
        except (ValueError, TypeError):
            return raw
    if data_type in ("double", "float", "number"):
        try:
            return float(raw)
        except (ValueError, TypeError):
            return raw
    if data_type == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        return raw
    if data_type == "select":
        # Local SELECT values are canonical option ids; Jira only understands
        # the option's display string.
        text = str(raw)
        for option in (structure or {}).get("options") or []:
            if (
                isinstance(option, dict)
                and option.get("id") == text
                and option.get("value") is not None
            ):
                return option["value"]
        return text
    if data_type == "status":
        # The Core struct stores the CORE status UUID; Jira needs its own id.
        if not isinstance(raw, dict):
            return None
        core_id = raw.get("id")
        if core_id is None:
            return raw.get("jira_status_id")
        if status_jira_ids is not None:
            return status_jira_ids.get(str(core_id))
        return raw.get("jira_status_id")
    if data_type == "json" and isinstance(raw, (dict, list)):
        return json.dumps(raw, sort_keys=True)
    return raw


def jira_scalar_values(
    data_type, items, *, structure=None, status_jira_ids=None
) -> list[dict]:
    """Canonical scalar reps -> Jira ``objectAttributeValues`` entries (bug 8)."""
    values: list[dict] = []
    for item in items:
        rep = (
            item
            if isinstance(item, str)
            else json.dumps(item, sort_keys=True, default=str)
        )
        typed_column, raw = decode_repr(rep, data_type)
        if typed_column is None or raw is None:
            continue
        scalar = _to_jira_scalar(
            data_type, raw, structure, status_jira_ids=status_jira_ids
        )
        if scalar is None:
            continue
        values.append({"value": scalar})
    return values


def jira_reference_values(jira_object_ids) -> list[dict]:
    """Jira reference values use ``{\"value\": \"<id>\"}`` on PUT (Pitfall 6)."""
    return [{"value": str(jira_object_id)} for jira_object_id in jira_object_ids]


def attribute_entry(jira_attribute_id, values) -> dict:
    """Exactly one ``attributes[]`` entry for one Jira ObjectTypeAttribute."""
    return {
        "objectTypeAttributeId": int(jira_attribute_id),
        "objectAttributeValues": values,
    }


def _remote_attr_id(attribute):
    raw_id = attribute.get("objectTypeAttributeId")
    if raw_id is None:
        raw_id = (attribute.get("objectTypeAttribute") or {}).get("id")
    return None if raw_id is None else int(raw_id)


def _passthrough_values(attribute) -> list[dict]:
    """A remote attribute's values in PUT shape: references collapse to ids."""
    values: list[dict] = []
    for value in attribute.get("objectAttributeValues") or []:
        if not isinstance(value, dict):
            continue
        referenced = value.get("referencedObject")
        if isinstance(referenced, dict) and referenced.get("id") is not None:
            values.append({"value": str(referenced["id"])})
        else:
            values.append({"value": value.get("value")})
    return values


def build_update_payload(*, jira_type_id, remote_attributes, replaced_entries) -> dict:
    """The PUT body: one entry per attribute, replaced entries winning (bug 6/8).

    A Jira ``PUT /object/{id}`` replaces the whole attribute set, so every remote
    attribute the resolution does not touch is passed through unchanged (with the
    GET-only ``referencedObject`` shape collapsed back to ``{\"value\": id}``).
    """
    entries: dict[int, dict] = {}
    order: list[int] = []

    for attribute in remote_attributes or []:
        if not isinstance(attribute, dict):
            continue
        attribute_id = _remote_attr_id(attribute)
        if attribute_id is None:
            continue
        if attribute_id not in entries:
            order.append(attribute_id)
        entries[attribute_id] = attribute_entry(
            attribute_id, _passthrough_values(attribute)
        )

    for entry in replaced_entries or []:
        attribute_id = int(entry["objectTypeAttributeId"])
        if attribute_id not in entries:
            order.append(attribute_id)
        entries[attribute_id] = {
            "objectTypeAttributeId": attribute_id,
            "objectAttributeValues": list(entry.get("objectAttributeValues") or []),
        }

    return {
        "objectTypeId": int(jira_type_id),
        "attributes": [entries[attribute_id] for attribute_id in order],
    }


def remote_scalar_items(
    data_type, jira_attribute, *, structure=None, status_resolver=None
) -> list[str]:
    """A remote attribute's scalar values normalized to canonical reprs.

    A Jira status value lives under ``objectAttributeValues[].status`` (not
    ``value``); it is mapped back to the Core status struct through
    ``status_resolver`` so the repr matches the local ``value_struct`` repr.
    """
    items: list[str] = []
    for value in (jira_attribute or {}).get("objectAttributeValues") or []:
        if not isinstance(value, dict) or "referencedObject" in value:
            continue
        if data_type == "status":
            status = value.get("status")
            if not isinstance(status, dict) or status.get("id") is None:
                continue
            if status_resolver is None:
                continue
            core_id = status_resolver(str(status["id"]))
            if core_id is None:
                continue
            struct = {"id": str(core_id), "name": status.get("name", "")}
            items.append(canonical_repr("value_struct", struct))
            continue
        _typed, _raw, rep = normalize_jira_value(
            data_type, value.get("value"), structure=structure
        )
        if rep is not None:
            items.append(rep)
    return items


def remote_reference_ids(jira_attribute) -> list[str]:
    """The referenced Jira object ids of a remote reference attribute."""
    ids: list[str] = []
    for value in (jira_attribute or {}).get("objectAttributeValues") or []:
        if not isinstance(value, dict):
            continue
        referenced = value.get("referencedObject")
        if isinstance(referenced, dict) and referenced.get("id") is not None:
            ids.append(str(referenced["id"]))
        elif value.get("value") is not None:
            ids.append(str(value["value"]))
    return ids


# --- Create payload builders (294-05, JSYNC-04) ------------------------------
#
# The plugin may import only ``indramesh.sdk`` from Core, so the live-verified
# Core mapping is COPIED here, not imported (294-RESEARCH "Don't Hand-Roll").
# Values are (type_index, defaultTypeId_or_None); ``None`` omits defaultTypeId.
INDRAMESH_TO_JIRA_TYPE: dict[str, tuple[int, int | None]] = {
    "text": (0, 0),
    "integer": (0, 1),
    "boolean": (0, 2),
    "double": (0, 3),
    "datetime": (0, 6),
    "url": (0, 7),
    "email": (0, 8),
    "select": (0, 10),
    "status": (7, None),
    "reference": (1, None),
    "json": (0, 0),
    "version": (5, None),
    "group": (4, None),
    "user": (2, None),
    "jira": (0, 0),
    "confluence": (0, 0),
    "textarea": (0, 9),
    "custom_unknown_type": (0, 0),
}

# Core Status.status_category -> Jira status category int (0/1/2).
STATUS_CATEGORY_TO_JIRA = {"Inactive": 0, "Active": 1, "Pending": 2}


def status_create_payload(name, status_category) -> dict:
    """POST /rest/assets/1.0/config/statustype body."""
    return {
        "name": name,
        "category": STATUS_CATEGORY_TO_JIRA.get(status_category, 1),
    }


def reference_type_create_payload(name, description=None) -> dict:
    """POST /rest/insight/1.0/config/referencetype body."""
    return {"name": name, "description": description or name}


def object_type_create_payload(
    name, *, parent_jira_type_id=None, is_abstract=False
) -> dict:
    """POST /rest/insight/1.0/objecttype/create body (parent-first hierarchy)."""
    payload: dict = {"name": name, "abstractObjectType": bool(is_abstract)}
    if parent_jira_type_id is not None:
        payload["parentObjectTypeId"] = int(parent_jira_type_id)
    return payload


def _select_options_csv(structure) -> str | None:
    options = (structure or {}).get("options")
    if not options:
        return None
    if all(isinstance(option, str) for option in options):
        values = [option for option in options if option]
        return ",".join(values) if values else None
    ordered = sorted(
        options,
        key=lambda option: (
            option.get("position", 0) if isinstance(option, dict) else 0
        ),
    )
    values = [
        option["value"]
        for option in ordered
        if isinstance(option, dict) and option.get("value")
    ]
    return ",".join(values) if values else None


def attribute_create_payload(
    *,
    name,
    data_type,
    structure=None,
    reference_jira_type_id=None,
    reference_type_jira_id=None,
    status_jira_ids=None,
    is_label=False,
) -> dict:
    """POST /rest/insight/1.0/objecttypeattribute/{id} body.

    Reference attributes carry ``typeValue`` (the target ObjectType) and an
    optional ``additionalValue`` (the Jira reference type); select attributes
    carry their comma-separated ``options`` (live-verified: the key is
    ``options``, not ``additionalValue``).
    """
    type_idx, default_type_idx = INDRAMESH_TO_JIRA_TYPE.get(data_type, (0, 0))
    payload: dict = {"name": name, "type": type_idx}
    if default_type_idx is not None:
        payload["defaultTypeId"] = default_type_idx
    if data_type == "reference":
        payload = {"name": name, "type": 1}
        if reference_jira_type_id is not None:
            payload["typeValue"] = int(reference_jira_type_id)
        if reference_type_jira_id is not None:
            payload["additionalValue"] = int(reference_type_jira_id)
    elif data_type == "select":
        options_csv = _select_options_csv(structure)
        if options_csv:
            payload["options"] = options_csv
    elif data_type == "status" and status_jira_ids:
        payload["typeValueMulti"] = ",".join(
            sorted(str(value) for value in status_jira_ids)
        )
    if is_label:
        payload["label"] = True
    return payload


def object_create_payload(
    *, jira_type_id, label_jira_attribute_id, label_value, extra_entries=None
) -> dict:
    """POST /rest/insight/1.0/object/create body.

    Assets requires at least one attribute value; the object type's label
    attribute is the natural carrier (live-verified: the value is addressed by
    the label attribute id, never a flat ``{"name": ...}`` body).
    """
    attributes = []
    if label_jira_attribute_id is not None:
        attributes.append(
            attribute_entry(
                int(label_jira_attribute_id),
                [{"value": "" if label_value is None else label_value}],
            )
        )
    for entry in extra_entries or []:
        attribute_id = int(entry["objectTypeAttributeId"])
        if attribute_id in {
            int(existing["objectTypeAttributeId"]) for existing in attributes
        }:
            continue
        attributes.append(entry)
    return {"objectTypeId": int(jira_type_id), "attributes": attributes}

