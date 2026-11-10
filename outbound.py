"""The plugin's single outbound-target gate (SESS-07, D-06, 321-05).

Every request the plugin makes with a decrypted PAT crosses the boundary between
a Source/Target row and a Jira host, and that host is attacker-influenced through
the endpoint URL. Core applies the same predicate at save time (321-04), but a
save-time DNS answer can be stale and Core is not the layer that dials the
socket, so the check runs again here, on every request.

:func:`assert_target_allowed` reads the endpoint's **own** row — the current
``sources.source_url`` / ``targets.target_url`` plus that row's ``config`` — and
never the bind snapshot. That is what covers a first bind, where no
``plugin_jira_connections`` row exists yet, and a repeat PUT where only the
strategy changes. A missing endpoint row is a refusal, never a pass.

This module is the plugin's single outbound gate: ``grep -rl
"assert_outbound_url" plugins/jira/`` prints only this file. The rule itself is
not re-implemented here — ``indramesh.sdk.assert_outbound_url`` delegates to
Core's ``check_resolved_target`` with ``fail_closed=True``, so an empty answer
set, a timeout and a resolver error are all refusals, and the endpoint's
``allow_internal_target`` flag (read with identity by Core) short-circuits it.

The guard is deliberately NOT inside ``JiraPullClient.__init__``: that would make
every client test resolver-dependent and invite a bypass. Callers guard before
they construct the client.

Only ``indramesh.sdk``, stdlib and sibling plugin modules are imported (the
plugin AST gate in ``tests/test_plugin_discovery.py``).
"""

from indramesh.sdk import (
    OUTBOUND_RESOLVE_TIMEOUT_SECONDS,
    PluginTaskError,
    assert_outbound_url,
)

from .connections import load_endpoint_target

#: The endpoint kinds this gate accepts; also the ``sources``/``targets`` split.
SOURCE = "source"
TARGET = "target"


async def assert_target_allowed(
    session,
    *,
    kind: str,
    endpoint_id,
    resolver=None,
    timeout: float = OUTBOUND_RESOLVE_TIMEOUT_SECONDS,
) -> None:
    """Refuse unless the endpoint row's current URL may be dialled right now.

    ``kind`` is ``"source"`` or ``"target"``. The URL and the waiver flag come
    from the endpoint row itself (see ``load_endpoint_target``), so this answers
    on a first bind too.

    Raises the bounded, Core-authored ``PluginTaskError`` on a refusal — a
    non-global literal, a host answering with any non-global address, an
    unparseable URL, a resolution yielding no answer at all, or a missing
    endpoint row. The message names the reason and never the URL.

    ``resolver``/``timeout`` are test seams forwarded unchanged, exactly like the
    Core seam; production callers pass neither. ``timeout`` defaults to the
    imported bound — never a literal and never ``None``, because
    ``asyncio.wait_for(..., None)`` would remove the bound entirely.
    """
    endpoint = await load_endpoint_target(
        session, kind=kind, endpoint_id=endpoint_id
    )
    if endpoint is None:
        raise PluginTaskError(
            "JIRA source not found" if kind == SOURCE else "JIRA target not found"
        )
    await assert_outbound_url(
        endpoint["url"],
        config=endpoint["config"],
        resolver=resolver,
        timeout=timeout,
    )
