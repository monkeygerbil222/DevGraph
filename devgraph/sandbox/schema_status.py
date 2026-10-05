"""Whether the graph holds the schema a provider snapshot was read from (spec §5.5).

Shared by the CLI and, later, the tray. Reads the graph only; executes no
repository code.
"""

from __future__ import annotations

from typing import Any

from devgraph.graph.engine import GraphEngine


def applied_schema_status(repo_id: str, schema_hash: str, settings: Any) -> str:
    """`applied` when the graph's applied schema hash equals `schema_hash`, else
    `pending`; `unreachable` when the graph cannot be read, which callers count
    as pending (fail closed)."""
    try:
        engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            applied = engine.read_applied_schema(repo_id)
        finally:
            engine.close()
    except Exception:
        return "unreachable"
    return "applied" if applied is not None and applied.get("hash") == schema_hash else "pending"
