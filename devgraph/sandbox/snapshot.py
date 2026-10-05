"""One provider snapshot per run, and the provider state (spec §3.2, §5.2, §5.5).

The schema file and the script are each read once, through the no-follow
reader, from the repository's real path. The digest, the static scan and the
approval figures all come from those same bytes, and the caller hands the same
snapshot on, so nothing is re-read between check and use.

Executes no repository code: the script is normalised and scanned as data.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devgraph.config.project_schema import (
    SCHEMA_FILENAME,
    ProjectSchema,
    ProjectSchemaError,
    parse_project_schema,
    resolve_declaration,
)
from devgraph.sandbox.digest import canonical_json, provider_digest
from devgraph.sandbox.gates import evaluate_gates
from devgraph.sandbox.limits import (
    APPROVAL_SAMPLE_SIZE,
    INPUT_MAX_FILE_BYTES,
    INPUT_MAX_RUN_BYTES,
)
from devgraph.sandbox.paths import platform_supported
from devgraph.sandbox.reader import (
    InputError,
    read_provider_script,
    read_repo_file,
    read_schema_file,
)
from devgraph.sandbox.scan import Finding, static_scan
from devgraph.sandbox.selection import select_inputs
from devgraph.sandbox.source import normalise_script


@dataclass(frozen=True)
class ProviderSnapshot:
    name: str
    schema_hash: (
        str  # `sha256:<hex>` of the schema bytes read, as `schema_file_hash` gives
    )
    declaration_set: dict[str, Any]
    declaration_json: str
    script_text: str
    digest: str
    findings: tuple[Finding, ...]
    literal_spans: tuple[tuple[int, int], ...]
    matched: tuple[str, ...]
    matched_count: int
    sample: tuple[str, ...]  # the first APPROVAL_SAMPLE_SIZE sorted matched paths
    denied: int  # glob matches the secret-name denylist excluded
    total_bytes: int  # bytes of the inputs that were read
    errors: tuple[
        InputError, ...
    ]  # per-file refusals (oversize, unreadable); those files are skipped


def _read_schema(root: Path) -> tuple[str, ProjectSchema]:
    raw = read_schema_file(root)
    try:
        schema = parse_project_schema(raw.decode("utf-8"), Path(SCHEMA_FILENAME))
        resolve_declaration(schema)
    except (UnicodeDecodeError, ProjectSchemaError):
        raise InputError(
            "static_reject", f"{SCHEMA_FILENAME} is not a valid schema"
        ) from None
    return "sha256:" + hashlib.sha256(raw).hexdigest(), schema


def _snapshot(
    root: Path, name: str, schema_hash: str, schema: ProjectSchema, git: str | None
) -> ProviderSnapshot:
    try:
        declaration = schema.custom_declaration_set(name)
    except KeyError:
        raise InputError(
            "input_unavailable", f"no custom provider named {name!r} is declared"
        ) from None
    declaration_json = canonical_json(declaration).decode("ascii")

    try:
        raw = read_provider_script(root, name)
    except InputError as exc:
        # A script that cannot be read safely (missing, a symlink) cannot be approved.
        raise InputError("static_reject", exc.reason) from None
    text = normalise_script(raw)
    scan = static_scan(text)

    selection = select_inputs(root, tuple(declaration["provider"]["inputs"]), git=git)
    errors, total = [], 0
    for rel in selection.matched:
        try:
            total += len(read_repo_file(root, rel, cap=INPUT_MAX_FILE_BYTES))
        except InputError as exc:
            errors.append(exc)
            continue
        if total > INPUT_MAX_RUN_BYTES:
            raise InputError("input_cap", f"inputs exceed {INPUT_MAX_RUN_BYTES} bytes")

    return ProviderSnapshot(
        name=name,
        schema_hash=schema_hash,
        declaration_set=declaration,
        declaration_json=declaration_json,
        script_text=text,
        digest=provider_digest(name, declaration, text),
        findings=scan.findings,
        literal_spans=scan.literal_spans,
        matched=selection.matched,
        matched_count=len(selection.matched),
        sample=selection.matched[:APPROVAL_SAMPLE_SIZE],
        denied=selection.denied,
        total_bytes=total,
        errors=tuple(errors),
    )


def provider_snapshot(root: Path, name: str, *, git: str | None) -> ProviderSnapshot:
    """The snapshot of one declared provider. Raises `InputError`: `static_reject`
    for an invalid schema or script, `input_unavailable`, or `input_cap`."""
    real = Path(os.path.realpath(root))
    schema_hash, schema = _read_schema(real)
    return _snapshot(real, name, schema_hash, schema, git)


def repo_snapshots(
    root: Path, *, git: str | None
) -> dict[str, ProviderSnapshot | InputError]:
    """A snapshot, or the reason there is none, for every declared provider.

    The schema is read once for all of them. No schema file means no providers;
    a schema that cannot be read or is invalid raises `InputError`.
    """
    real = Path(os.path.realpath(root))
    if not os.path.lexists(real / SCHEMA_FILENAME):
        return {}
    schema_hash, schema = _read_schema(real)
    result: dict[str, ProviderSnapshot | InputError] = {}
    for provider in schema.custom_providers:
        try:
            result[provider.name] = _snapshot(
                real, provider.name, schema_hash, schema, git
            )
        except InputError as exc:
            result[provider.name] = exc
    return result


def provider_state(
    repo_id: str,
    canon: str,
    snap: ProviderSnapshot | InputError,
    *,
    platform: str,
    pending: bool,
    registry_path: Path,
    store_path: Path,
) -> str:
    """`unavailable`, `rejected`, `disabled`, `pending`, `awaiting_approval` or
    `approved` (§5.5), decided in that order.

    `rejected` is a `static_reject` snapshot (an invalid script or declaration),
    which no approval can fix until the file changes; any other `InputError` is
    `unavailable`. Gates 1 and 2 come before `pending`, so a provider that would
    not run after a rescan shows as `disabled`. `failing` needs run records,
    which arrive in E3.
    """
    if not platform_supported(platform):
        return "unavailable"
    if isinstance(snap, InputError):
        return "rejected" if snap.code == "static_reject" else "unavailable"
    gates = evaluate_gates(
        repo_id,
        canon,
        snap.name,
        snap.digest,
        registry_path=registry_path,
        store_path=store_path,
    )
    if not (gates.project_config and gates.scripts_enabled):
        return "disabled"
    if pending:
        return "pending"
    if not gates.digest_active:
        return "awaiting_approval"
    return "approved"
