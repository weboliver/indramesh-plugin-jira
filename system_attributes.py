"""Plugin-owned Jira metadata; Core's system fields keep their native meaning."""

JIRA_METADATA = {
    "Key": ("Jira Key", "text"),
    "Created": ("Jira Created", "datetime"),
    "Updated": ("Jira Updated", "datetime"),
}
METADATA_MARKER = "jira_managed_field"


def is_jira_metadata(structure) -> bool:
    """Distinguish imported metadata from genuine attributes with similar names."""
    return isinstance(structure, dict) and structure.get(METADATA_MARKER) in JIRA_METADATA
