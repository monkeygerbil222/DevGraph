"""DevGraph CLI: register repositories, manage watch settings, check status."""

import contextlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from devgraph.agent import lifecycle
from devgraph.cli._env import resolve_podman, resolve_repo_root, resolve_venv_python
from devgraph.cli.exporters import export_cypher, export_dot, export_json
from devgraph.config import get_settings
from devgraph.config.edits import GLOBAL_TOOLS_NOTE as _GLOBAL_TOOLS_NOTE
from devgraph.config.edits import SCHEMA_SECTIONS as _SCHEMA_SECTIONS
from devgraph.config.edits import project_config_notes as _project_config_notes
from devgraph.config.edits import removed_types as _removed_types  # noqa: F401  (kept importable from here)
from devgraph.config.schema_findings import project_schema_findings as _project_schema_findings
from devgraph.dashboard import queries as dashboard_queries
from devgraph.dashboard.url import dashboard_url
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.docs.extractor import index_file as index_doc_file
from devgraph.indexer.git_history.extractor import sync_git_history
from devgraph.paths import is_within, read_bounded
from devgraph.registry.store import RepoRegistry
from devgraph.sandbox import consent, selection
from devgraph.sandbox import paths as sandbox_paths
from devgraph.sandbox.display import visible
from devgraph.sandbox.selection import git_binary

app = typer.Typer(help="DevGraph: local-first developer knowledge graph")

# Arguments for launching the MCP server with the venv python. -P keeps the
# working directory off sys.path, so a `devgraph/` directory in the repo the
# client starts in cannot shadow the installed package.
MCP_SERVER_ARGS = ("-P", "-m", "devgraph.mcp.server")
tray_app = typer.Typer(help="Manage the DevGraph tray app (live watcher + incremental indexer) as a background process.")
app.add_typer(tray_app, name="tray")
console = Console()


def _get_registry() -> RepoRegistry:
    """Get or create the registry from configured path."""
    settings = get_settings()
    return RepoRegistry(settings.registry_db_path)


@app.command()
@app.command(name="register")
def add(
    path: str = typer.Argument(".", help="Path to a git repository (defaults to the current directory)."),
    full: bool = typer.Option(
        False, "--full", help="Also run incremental git-history indexing after the file scan (local-only, no network)."
    ),
) -> None:
    """Register a repository and run its initial full scan.

    No-op (with a status message) if this path is already registered.

    Args:
        path: Absolute or relative path to a git repository.
    """
    try:
        registry = _get_registry()
        try:
            resolved = Path(path).resolve()
            existing = next((r for r in registry.list_repos() if r.path == resolved), None)
            if existing is not None:
                console.print(f"[green][OK][/green] Already registered: {existing.repo_id} at {existing.path}")
                return
            record = registry.add_repo(path)
            console.print(
                f"[green][OK][/green] Registered: {record.repo_id} at {record.path}"
            )
            _forget_script_trust(record.repo_id, registering=True)
            _scripts_notice(record.repo_id, Path(record.path))

            try:
                settings = get_settings()
                engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
                try:
                    provision_repository_schema(engine, record.path)
                    engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
                    count = full_scan(engine, record.repo_id, record.path, docs_path=record.docs_path, mentions_enabled=record.mentions_enabled)
                    registry.mark_indexed(record.repo_id)
                    console.print(f"[green][OK][/green] Indexed {count} file(s)")

                    if full:
                        try:
                            result = sync_git_history(engine, registry, record.repo_id)
                            console.print(f"[green][OK][/green] Indexed {result['commits_indexed']} commit(s)")
                        except Exception as e:
                            console.print(
                                f"[yellow]Full scan complete but history indexing failed:[/yellow] {e}\n"
                                f"  Run 'devgraph index-history {record.repo_id}' to retry."
                            )
                finally:
                    engine.close()
            except Exception as e:
                # Registration already succeeded (SQLite committed above) — an
                # indexing failure (e.g. Neo4j unreachable) shouldn't undo that.
                # `devgraph rescan <repo_id>` retries the scan once Neo4j is up.
                console.print(
                    f"[yellow]Registered but initial scan failed:[/yellow] {e}\n"
                    f"  Run 'devgraph rescan {record.repo_id}' once Neo4j is reachable."
                )
        finally:
            registry.close()
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def remove(repo_id: str) -> None:
    """Unregister a repository.

    Args:
        repo_id: The repository ID (shown by 'devgraph list').
    """
    try:
        registry = _get_registry()
        try:
            if registry.get(repo_id) is None:
                raise ValueError(f"no such repo_id: {repo_id}")

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                recorded = engine.read_applied_schema(repo_id) or {}
                engine.delete_repository(repo_id)
                _release_labels(engine, recorded.get("labels") or [])
            finally:
                engine.close()

            registry.remove_repo(repo_id)
            console.print(f"[green][OK][/green] Removed: {repo_id} (registry entry and graph data)")
            _forget_script_trust(repo_id)
        finally:
            registry.close()
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


def _release_labels(engine: GraphEngine, labels: list[str]) -> None:
    """After deleting a repository's graph data: drop the generated constraints/indexes
    of its labels no other repository uses. A failure is a warning; the data is gone either way."""
    from devgraph.indexer.schema_constraints import release_labels

    try:
        for name in release_labels(engine, labels):
            console.print(f"[green][OK][/green] Dropped {name} (no repository declares it any more)")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] could not drop unused schema constraints: {e}")


@app.command(name="list")
def list_repos() -> None:
    """List all registered repositories and their status."""
    try:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
            if not repos:
                console.print("No repositories registered.")
                return

            table = Table(title="Registered Repositories")
            table.add_column("Repo ID", style="cyan")
            table.add_column("Path", style="magenta")
            table.add_column("Active", style="green")
            table.add_column("Watch", style="blue")
            table.add_column("Project config", style="blue")
            table.add_column("Last Indexed", style="yellow")

            # Load repo issues for display
            settings = get_settings()
            issues_path = settings.registry_db_path.parent / "repo_issues.json"
            repo_issues = {}
            if issues_path.exists():
                try:
                    import json
                    repo_issues = json.loads(issues_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            for repo in repos:
                active_str = "[OK]" if repo.active else "[X]"
                watch_str = "[OK]" if repo.watch_enabled else "[X]"
                last_indexed = repo.last_indexed or "-"
                table.add_row(
                    repo.repo_id,
                    str(repo.path),
                    active_str,
                    watch_str,
                    "on" if repo.project_config_enabled else "off",
                    last_indexed,
                )

            console.print(table)
            
            # Show any issues
            if repo_issues:
                console.print("\n[bold]⚠️  Repository Issues[/bold]")
                for repo_id, error_msg in repo_issues.items():
                    console.print(f"  [yellow]{repo_id}:[/yellow] {error_msg}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def rescan(
    repo_id: str,
    full: bool = typer.Option(
        False, "--full", help="Also run git-history indexing after the file scan, forcing a full re-sync even if HEAD hasn't moved (local-only, no network)."
    ),
    now: bool = typer.Option(
        False, "--now",
        help="Apply a changed devgraph.schema.yaml right away instead of waiting for the agent's "
        "5-minute quiet period. A CLI rescan always applies immediately; this states it explicitly.",
    ),
) -> None:
    """Run a full re-index of a registered repository.

    Walks every file under the repo's root and re-runs every extractor that
    recognizes it (Python, docs, containers, APIs, datastores). Idempotent —
    safe to run repeatedly; existing nodes are updated in place via MERGE.

    Reconciles the graph to disk: file nodes whose source file no longer
    exists are pruned, and git history is always re-synced (cheap no-op when
    HEAD hasn't moved; full reconcile when history was rewritten, e.g. a
    force-push or pruned branch). With --full, git history is force
    re-synced even when HEAD hasn't moved — the escape hatch for repairing
    a graph whose MODIFIES edges were destroyed by a bug.

    A changed devgraph.schema.yaml is applied by this command immediately;
    --now says so explicitly (the agent applies it only after a quiet period).

    Args:
        repo_id: The repository ID to rescan.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                provision_repository_schema(engine, repo.path)
                engine.upsert_repository(repo_id, repo_id, str(repo.path))
                count = full_scan(engine, repo_id, repo.path, docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled)
                registry.mark_indexed(repo_id)
                console.print(f"[green][OK][/green] Rescanned {repo_id}: {count} file(s) indexed")

                # Always reconcile git history on a rescan — not just with
                # --full. sync_git_history is a cheap no-op when HEAD hasn't
                # moved, and it's the only path that heals a rewritten
                # history (force-push, pruned branch): without it, orphaned
                # Commit nodes linger forever. --full additionally forces a
                # full re-sync even when HEAD hasn't moved, repairing
                # MODIFIES edges destroyed by the old replace_file_nodes
                # blanket-delete bug.
                try:
                    result = sync_git_history(engine, registry, repo_id, force=full)
                    if result["commits_indexed"] or result["commits_deleted"]:
                        console.print(
                            f"[green][OK][/green] History: {result['commits_indexed']} commit(s) indexed, "
                            f"{result['commits_deleted']} pruned"
                        )
                except Exception as e:
                    console.print(
                        f"[yellow]Rescan complete but history indexing failed:[/yellow] {e}\n"
                        f"  Run 'devgraph index-history {repo_id}' to retry."
                    )
            finally:
                engine.close()
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command()
def watch(action: str, repo_id: str) -> None:
    """Enable or disable file watching for a repository.

    Args:
        action: 'enable' or 'disable'.
        repo_id: The repository ID.
    """
    try:
        if action not in ("enable", "disable"):
            console.print(f"[red][X] Error:[/red] action must be 'enable' or 'disable'")
            raise typer.Exit(code=1)

        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            if action == "enable":
                registry.enable_watch(repo_id)
                console.print(f"[green][OK][/green] Watch enabled for {repo_id}")
            else:
                registry.disable_watch(repo_id)
                console.print(f"[green][OK][/green] Watch disabled for {repo_id}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


@tray_app.command("start")
def tray_start() -> None:
    """Launch the tray app (watcher + incremental indexer) as a detached background process.

    No-op if a tray process (per the PID file) is already running. This does
    not register the process to survive reboots/logout — rerun after each
    login, or wire it into your own Startup-folder/Task Scheduler entry.

    Note: connecting an MCP client (e.g. Claude Code) also auto-starts this
    the same way, so most workflows never need to run this by hand — it's
    here for manual control (checking in on it, or running it without an
    MCP client attached).
    """
    started_pid = lifecycle.start_tray_if_not_running()
    if started_pid is None:
        existing_pid = lifecycle.read_tray_pid()
        console.print(f"[yellow]Tray app already running[/yellow] (pid {existing_pid})")
        return

    console.print(f"[green][OK][/green] Tray app started (pid {started_pid})")
    console.print("  Run 'devgraph status' to confirm the heartbeat once it's up.")


@tray_app.command("stop")
def tray_stop() -> None:
    """Force-stop the background tray process, regardless of any connected MCP clients.

    Every connected MCP client's server process holds a "hold" on the tray
    (see devgraph/agent/lifecycle.py) so a single client disconnecting
    doesn't stop indexing for the others — this command overrides that and
    stops it outright, clearing all recorded holders in the process.
    """
    pid = lifecycle.read_tray_pid()
    lifecycle.clear_tray_holders()
    if pid is None or not lifecycle.pid_is_running(pid):
        console.print("[yellow]Tray app is not running[/yellow] (no live PID on record)")
        lifecycle.tray_pid_path().unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green][OK][/green] Sent stop signal to tray app (pid {pid})")
    except OSError as e:
        console.print(f"[red][X] Error:[/red] failed to stop pid {pid}: {e}")
        raise typer.Exit(code=1)
    finally:
        lifecycle.tray_pid_path().unlink(missing_ok=True)


@tray_app.command("status")
def tray_status() -> None:
    """Report whether the background tray process (per the PID file) is alive."""
    pid = lifecycle.read_tray_pid()
    if pid is not None and lifecycle.pid_is_running(pid):
        console.print(f"[green][OK] running[/green] (pid {pid})")
    else:
        console.print("[yellow]not running[/yellow] (start with 'devgraph tray start')")


@app.command()
def annotate(
    repo_id: str,
    docs_path: Optional[str] = typer.Option(
        None, "--docs-path", help="Repo-relative path to the docs folder (e.g. 'devgraph/docs')."
    ),
    note: Optional[str] = typer.Option(
        None, "--note", help="Index a single Markdown note file immediately (path relative to the repo root)."
    ),
) -> None:
    """Configure or use Phase 2 doc annotations for a repository.

    With --docs-path: register/update the repo's docs folder (registry-scoped
    only — never a path outside the repo).
    With --note: parse and upsert one Markdown note (Requirement /
    DesignDecision / ArchitectureNote front-matter) into the graph immediately.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            if docs_path is not None:
                registry.set_docs_path(repo_id, docs_path)
                console.print(f"[green][OK][/green] Docs path set for {repo_id}: {docs_path}")

            if note is not None:
                note_path = repo.path / note
                if not is_within(note_path.resolve(), repo.path):
                    console.print("[red][X] Error:[/red] note path must be inside the repository")
                    raise typer.Exit(code=1)

                settings = get_settings()
                engine = GraphEngine(
                    settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
                )
                try:
                    engine.init_schema()
                    index_doc_file(engine, repo_id, note_path)
                    console.print(f"[green][OK][/green] Indexed note: {note}")
                finally:
                    engine.close()

            if docs_path is None and note is None:
                console.print(f"docs_path: {repo.docs_path or '(not set)'}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command(name="index-history")
def index_history(
    repo_id: str,
    max_count: Optional[int] = typer.Option(
        None, "--max-count", help="Cap the number of commits walked (useful for a first scan of a large repo)."
    ),
) -> None:
    """Incrementally index a repo's git commit history (Phase 3).

    Purely local — reads the repo's own .git directory, no network calls.
    Walks only commits newer than the last indexed one for this repo_id.

    Args:
        repo_id: The repository ID to index history for.
    """
    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                engine.init_schema()
                result = sync_git_history(engine, registry, repo_id, max_count=max_count)
                console.print(f"[green][OK][/green] Indexed {result['commits_indexed']} new commit(s) for {repo_id}")
            finally:
                engine.close()
        finally:
            registry.close()
    except typer.Exit:
        raise
    except ValueError as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red][X] Unexpected error:[/red] {e}")
        raise typer.Exit(code=1)


@app.command(name="pr-source")
def pr_source(repo_id: str, action: str) -> None:
    """Enable or disable PR ingestion opt-in for a repository (Phase 3).

    Off by default (Design Brief Principle 2). This command only flips the
    registry flag — it does not itself contact GitHub/GitLab/etc.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "pr_source_enabled", "PR")


@app.command(name="issue-source")
def issue_source(repo_id: str, action: str) -> None:
    """Enable or disable issue ingestion opt-in for a repository (Phase 3).

    Off by default (Design Brief Principle 2). This command only flips the
    registry flag — it does not itself contact GitHub/GitLab/etc.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "issue_source_enabled", "Issue")


@app.command(name="mentions")
def mentions(repo_id: str, action: str) -> None:
    """Enable or disable mentions indexing for a repository.

    Off by default (Design Brief Principle 2). When enabled, Markdown files
    are scanned for references to entities in the graph.

    Args:
        repo_id: The repository ID.
        action: 'enable' or 'disable'.
    """
    _set_external_source_flag(repo_id, action, "mentions_enabled", "Mentions")


def _set_external_source_flag(repo_id: str, action: str, setter_flag: str, label: str) -> None:
    if action not in ("enable", "disable"):
        console.print("[red][X] Error:[/red] action must be 'enable' or 'disable'")
        raise typer.Exit(code=1)

    try:
        registry = _get_registry()
        try:
            repo = registry.get(repo_id)
            if not repo:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)

            enabled = action == "enable"
            if setter_flag == "pr_source_enabled":
                registry.set_pr_source_enabled(repo_id, enabled)
            elif setter_flag == "issue_source_enabled":
                registry.set_issue_source_enabled(repo_id, enabled)
            elif setter_flag == "mentions_enabled":
                registry.set_mentions_enabled(repo_id, enabled)

            verb = "enabled" if enabled else "disabled"
            console.print(f"[green][OK][/green] {label} {verb} for {repo_id}")
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)


def _tray_liveness_text(settings) -> str:
    """Read the tray heartbeat file (item 7) and classify liveness.

    Returns one of "running" / "stale (process may have crashed)" / "not running".
    Shared between `status` and `doctor` so the read/compare logic isn't duplicated.
    """
    heartbeat_path = settings.registry_db_path.parent / "tray_heartbeat.txt"
    if not heartbeat_path.exists():
        return "not running"
    try:
        raw = heartbeat_path.read_text(encoding="utf-8").strip()
        last_beat = datetime.fromisoformat(raw)
    except (ValueError, OSError):
        return "not running"
    age_s = (datetime.now(timezone.utc) - last_beat).total_seconds()
    if age_s <= 2 * settings.health_check_interval_s:
        return "running"
    return "stale (process may have crashed)"


@app.command()
def status() -> None:
    """Check DevGraph status: Neo4j connectivity and repository counts."""
    settings = get_settings()
    console.print()

    # Check Neo4j connectivity
    console.print("[bold]Neo4j Connection[/bold]")
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
        console.print(f"  [green][OK] Reachable[/green] at {settings.neo4j_uri}")
    except Exception as e:
        console.print(f"  [red][X] Not reachable:[/red] {e}")
    finally:
        engine.close()

    # Repository counts
    console.print("[bold]Registered Repositories[/bold]")
    try:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
            active_repos = [r for r in repos if r.active]
            console.print(f"  Total: {len(repos)}")
            console.print(f"  Active: {len(active_repos)}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"  [red]Error:[/red] {e}")

    # Live watcher (tray app) liveness
    console.print("[bold]Live Watcher[/bold]")
    liveness = _tray_liveness_text(settings)
    if liveness == "running":
        console.print("  [green][OK] running[/green]")
    elif liveness == "not running":
        console.print("  [yellow]not running[/yellow] (no heartbeat file — start with 'devgraph tray start')")
    else:
        console.print(f"  [red]{liveness}[/red]")

    # Repo issues (missing paths, etc.)
    issues_path = settings.registry_db_path.parent / "repo_issues.json"
    if issues_path.exists():
        try:
            import json
            issues = json.loads(issues_path.read_text(encoding="utf-8"))
            if issues:
                console.print("[bold]⚠️  Repository Issues[/bold]")
                for repo_id, error_msg in issues.items():
                    console.print(f"  [yellow]{repo_id}:[/yellow] {error_msg}")
        except Exception:
            pass

    console.print()


def _registered_repo_id(root: Path) -> str:
    """The registry id of the repository at `root`, or the `<repo_id>` placeholder."""
    target = root.resolve()
    registry = _get_registry()
    try:
        for repo in registry.list_repos():
            if Path(repo.path).expanduser().resolve() == target:
                return repo.repo_id
    finally:
        registry.close()
    return "<repo_id>"


def _project_tools_findings(repos: list[Any]) -> list[dict[str, Any]]:
    """Per-repository `devgraph.tools.yaml` state, in the same shape as
    `_project_schema_findings`. A tool named like a built-in is a non-failing
    warning: the built-in is always used, as the tool plane will report."""
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError, load_project_tools
    from devgraph.config.project_trust import untrusted_reason
    from devgraph.mcp.catalog import builtin_tool_names

    builtin = builtin_tool_names()
    findings: list[dict[str, Any]] = []
    for repo in sorted(repos, key=lambda r: r.repo_id):
        try:
            declared = load_project_tools(repo.path)
        except ProjectToolsError as exc:
            findings.append({"repo_id": repo.repo_id, "status": "invalid", "detail": str(exc), "failed": True})
            continue
        if declared is None:
            findings.append({"repo_id": repo.repo_id, "status": "absent", "detail": f"no {TOOLS_FILENAME}", "failed": False})
            continue
        names = ", ".join(tool.name for tool in declared.tools)
        trust = _trust_state(repo.path)
        if project_config_enabled(repo.path) and trust == "trusted":
            findings.append({"repo_id": repo.repo_id, "status": "valid", "detail": f"tools: {names or 'none'}", "failed": False})
        elif project_config_enabled(repo.path):
            findings.append({
                "repo_id": repo.repo_id,
                "status": "warning",
                "detail": f"tools: {names or 'none'} (not served: {untrusted_reason(repo.repo_id, trust or 'untrusted')})",
                "failed": False,
            })
        else:
            # The file is checked, but the MCP tool plane serves none of it.
            findings.append({
                "repo_id": repo.repo_id,
                "status": "disabled",
                "detail": f"tools: {names or 'none'} (not served: project config disabled)",
                "failed": False,
            })
        for tool in declared.tools:
            if tool.name in builtin:
                findings.append({
                    "repo_id": repo.repo_id,
                    "status": "warning",
                    "detail": f"{TOOLS_FILENAME}: tool {tool.name!r} shadows a locked tool; the fixed implementation is used",
                    "failed": False,
                })
    return findings


def _global_tools_findings(repos: list[Any]) -> list[dict[str, Any]]:
    """The global tools store's state, plus a non-failing notice for each project
    tool that overrides a global one. No finding when there is no store."""
    from devgraph.config.global_tools import GLOBAL_TOOLS_FILENAME, load_global_tools
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.config.project_tools import ProjectToolsError, load_project_tools
    from devgraph.mcp.catalog import builtin_tool_names

    try:
        declared = load_global_tools()
    except ProjectToolsError as exc:
        return [{"repo_id": "global", "status": "invalid", "detail": str(exc), "failed": True}]
    if declared is None:
        return []
    names = {tool.name for tool in declared.tools}
    findings: list[dict[str, Any]] = [{
        "repo_id": "global",
        "status": "valid",
        "detail": f"tools: {', '.join(tool.name for tool in declared.tools) or 'none'}",
        "failed": False,
    }]
    builtin = builtin_tool_names()
    for tool in declared.tools:
        if tool.name in builtin:
            findings.append({
                "repo_id": "global",
                "status": "warning",
                "detail": f"{GLOBAL_TOOLS_FILENAME}: tool {tool.name!r} shadows a locked tool; the fixed implementation is used",
                "failed": False,
            })
    for repo in sorted(repos, key=lambda r: r.repo_id):
        try:
            project = load_project_tools(repo.path) if project_config_enabled(repo.path) else None
        except ProjectToolsError:
            continue  # reported by the project tools check
        for tool in project.tools if project else ():
            if tool.name in names and tool.name not in builtin:
                findings.append({
                    "repo_id": repo.repo_id,
                    "status": "notice",
                    "detail": f"tool {tool.name!r} overrides the global tool of the same name",
                    "failed": False,
                })
    return findings


def _stale_schema_objects(engine: Any, repos: list[Any]) -> list[Any]:
    """Generated constraints/indexes no repository uses (schema_constraints.stale_generated_objects),
    also counting the labels registered repositories' files declare but have not applied yet."""
    from devgraph.config.project_schema import ProjectSchemaError, resolve_effective_schema
    from devgraph.indexer.schema_constraints import stale_generated_objects

    declared: set[str] = set()
    for repo in repos:
        try:
            effective = resolve_effective_schema(repo.path)
        except ProjectSchemaError:
            continue  # what it applied is in the graph's recorded state
        declared.update(node_type.label.casefold() for node_type in effective.node_types)
    return stale_generated_objects(engine, declared)


def _schema_drift_findings(engine: Any, repos: list[Any]) -> list[dict[str, Any]]:
    """Per active repository: is the graph built with the schema the file hashes to?

    `applied`, `pending` (a warning, fixed by the next rescan) or `never
    applied` (a schema exists but no rescan recorded one). Mirrors
    `schema_pending`: no recorded state and no schema is in sync.
    """
    from devgraph.config.project_schema import ABSENT_SCHEMA_HASH, schema_file_hash
    from devgraph.config.project_switch import project_config_enabled

    findings: list[dict[str, Any]] = []
    for repo in sorted(repos, key=lambda r: r.repo_id):
        if not repo.active:
            continue
        current = schema_file_hash(repo.path)
        try:
            recorded = engine.read_applied_schema(repo.repo_id)
        except Exception as exc:
            findings.append({"repo_id": repo.repo_id, "status": "error", "detail": f"could not read the applied schema: {exc}"})
            continue
        disabled = not project_config_enabled(repo.path)
        if current.startswith("unreadable:"):
            status, detail = "unreadable", "schema file unreadable"
        elif recorded is None and current == ABSENT_SCHEMA_HASH:
            status, detail = "applied", "no schema; graph is in sync"
        elif recorded is None:
            status, detail = "never applied", f"a schema exists but no rescan has applied one; run `devgraph rescan {repo.repo_id} --now`"
        elif disabled and current == recorded["hash"]:
            status, detail = "applied", "project config disabled; built-in schema applied"
        elif disabled:
            status, detail = "pending", (
                f"project config disabled; built-in schema applied at the next rescan "
                f"(devgraph rescan {repo.repo_id} --now)"
            )
        elif current == recorded["hash"]:
            status, detail = "applied", "graph is in sync with the schema file"
        else:
            status, detail = "pending", (
                f"the schema changed since the graph was built; applied at the next rescan, "
                f"or `devgraph rescan {repo.repo_id} --now`"
            )
        findings.append({"repo_id": repo.repo_id, "status": status, "detail": detail})
    return findings


@app.command()
def doctor() -> None:
    """Run a heavier environment-drift diagnostic than `status`.

    Checks Python version, the installed `mcp` package, MCP server
    importability, Neo4j reachability + schema, Podman container state, the
    repo registry, each repository's optional `devgraph.schema.yaml`, and tray
    liveness — continuing past non-fatal failures so one run surfaces
    everything at once. Intended for bootstrap/troubleshooting moments;
    `status` stays the fast/lightweight command for quick glances.
    """
    settings = get_settings()
    any_failed = False
    console.print()

    # 1. Python version
    console.print("[bold]Python[/bold]")
    py_ok = sys.version_info >= (3, 13)
    marker = "[green][OK][/green]" if py_ok else "[red][X][/red]"
    console.print(f"  {marker} {sys.version.split()[0]} ({sys.executable})")
    any_failed = any_failed or not py_ok

    # 2. mcp package version
    console.print("[bold]mcp package[/bold]")
    try:
        mcp_version = importlib.metadata.version("mcp")
        console.print(f"  [green][OK][/green] mcp {mcp_version} (pyproject.toml requires >=2.0)")
    except importlib.metadata.PackageNotFoundError:
        console.print("  [red][X] Not installed[/red]")
        any_failed = True

    # 3. MCP server importability smoke check
    console.print("[bold]MCP server import[/bold]")
    try:
        from devgraph.mcp.server import build_server  # noqa: F401

        console.print("  [green][OK][/green] devgraph.mcp.server.build_server imports cleanly")
    except Exception as e:
        console.print(f"  [red][X] Import failed:[/red] {e}")
        any_failed = True

    # 3b. Indexer extractors importability smoke check
    console.print("[bold]Indexer extractors[/bold]")
    try:
        import devgraph.indexer.dispatch  # noqa: F401  # eagerly imports every language extractor

        console.print("  [green][OK][/green] devgraph.indexer.dispatch imports cleanly (all language extractors)")
    except ModuleNotFoundError as e:
        console.print(f"  [red][X] Missing dependency:[/red] {e}")
        console.print("       Run `pip install -e '.[dev]'` to install all declared grammars.")
        any_failed = True
    except Exception as e:
        console.print(f"  [red][X] Import failed:[/red] {e}")
        any_failed = True

    # 4 & 5. Neo4j reachability + schema
    console.print("[bold]Neo4j[/bold]")
    neo4j_reachable = False
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
        neo4j_reachable = True
        console.print(f"  [green][OK] Reachable[/green] at {settings.neo4j_uri}")
        try:
            engine.init_schema()
            console.print("  [green][OK][/green] Schema present (init_schema is idempotent)")
        except Exception as e:
            console.print(f"  [red][X] Schema init failed:[/red] {e}")
            any_failed = True
    except Exception as e:
        console.print(f"  [red][X] Not reachable:[/red] {e}")
        any_failed = True
    finally:
        engine.close()

    # 6. Podman container state
    console.print("[bold]Podman[/bold]")
    podman_path = resolve_podman()
    if podman_path is None:
        console.print("  [red][X] podman not found[/red] on PATH or %LOCALAPPDATA%\\Programs\\Podman")
        any_failed = True
    else:
        try:
            result = subprocess.run(
                [str(podman_path), "ps", "-a", "--filter", "name=devgraph-neo4j", "--format", "{{.Names}}\t{{.State}}"],
                capture_output=True, text=True, timeout=15,
            )
            output = result.stdout.strip()
            if not output:
                console.print("  [yellow]devgraph-neo4j container not found[/yellow]")
            else:
                console.print(f"  [green][OK][/green] {output}")
        except Exception as e:
            console.print(f"  [red][X] podman ps failed:[/red] {e}")
            any_failed = True

    # 7. Registry reachability
    console.print("[bold]Registry[/bold]")
    registered_repos: list[Any] = []
    try:
        registry = _get_registry()
        try:
            registered_repos = registry.list_repos()
            console.print(f"  [green][OK][/green] {len(registered_repos)} repo(s) registered at {settings.registry_db_path}")
        finally:
            registry.close()
    except Exception as e:
        console.print(f"  [red][X] Registry error:[/red] {e}")
        any_failed = True

    # 7b. Per-repository project schemas. Filesystem-only, and reuses the list
    # section 7 already read: an unreadable registry is reported once, there,
    # and leaves this section with nothing to check rather than crashing.
    console.print("[bold]Project schemas[/bold]")
    schema_findings = _project_schema_findings(registered_repos)
    if not schema_findings:
        console.print("  [green][OK][/green] no registered repositories to check")
    for finding in schema_findings:
        subject = finding["repo_id"] or "conflict"
        if finding["status"] == "disabled":
            console.print(f"  [yellow][!] {escape(str(subject))}:[/yellow] {escape(finding['detail'])}")
        elif finding["failed"]:
            console.print(f"  [red][X] {escape(str(subject))}:[/red] {escape(finding['detail'])}")
            any_failed = True
        else:
            console.print(f"  [green][OK][/green] {escape(str(subject))}: {escape(finding['detail'])}")

    console.print("[bold]Project tools[/bold]")
    tools_findings = _project_tools_findings(registered_repos)
    if not tools_findings:
        console.print("  [green][OK][/green] no registered repositories to check")
    for finding in tools_findings:
        subject = escape(str(finding["repo_id"]))
        if finding["failed"]:
            console.print(f"  [red][X] {subject}:[/red] {escape(finding['detail'])}")
            any_failed = True
        elif finding["status"] in ("warning", "disabled"):
            console.print(f"  [yellow][!] {subject}:[/yellow] {escape(finding['detail'])}")
        else:
            console.print(f"  [green][OK][/green] {subject}: {escape(finding['detail'])}")

    console.print("[bold]Global tools[/bold]")
    global_findings = _global_tools_findings(registered_repos)
    if not global_findings:
        console.print("  [green][OK][/green] no global tools")
    for finding in global_findings:
        subject = escape(str(finding["repo_id"]))
        if finding["failed"]:
            console.print(f"  [red][X] {subject}:[/red] {escape(finding['detail'])}")
            any_failed = True
        elif finding["status"] in ("warning", "notice"):
            console.print(f"  [yellow][!] {subject}:[/yellow] {escape(finding['detail'])}")
        else:
            console.print(f"  [green][OK][/green] {subject}: {escape(finding['detail'])}")

    # 7c. Schema drift: needs the graph, so it is skipped when Neo4j is down.
    console.print("[bold]Schema drift[/bold]")
    if not neo4j_reachable:
        console.print("  [yellow]skipped[/yellow]: Neo4j is not reachable")
    else:
        drift_engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            drift = _schema_drift_findings(drift_engine, registered_repos)
        finally:
            drift_engine.close()
        if not drift:
            console.print("  [green][OK][/green] no active repositories to check")
        for finding in drift:
            subject = escape(str(finding["repo_id"]))
            if finding["status"] == "applied":
                console.print(f"  [green][OK][/green] {subject}: applied ({escape(finding['detail'])})")
            else:
                console.print(f"  [yellow][!] {subject}:[/yellow] {finding['status']}: {escape(finding['detail'])}")

    # 7d. Stale generated constraints/indexes: also needs the graph.
    console.print("[bold]Schema constraints[/bold]")
    if not neo4j_reachable:
        console.print("  [yellow]skipped[/yellow]: Neo4j is not reachable")
    else:
        from devgraph.indexer.schema_constraints import constraint_drift

        constraint_engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            stale = _stale_schema_objects(constraint_engine, registered_repos)
            drift = constraint_drift(constraint_engine)
        except Exception as e:
            stale = drift = None
            console.print(f"  [yellow][!][/yellow] could not check: {escape(str(e))}")
        finally:
            constraint_engine.close()
        if stale == [] and drift == []:
            console.print("  [green][OK][/green] generated constraints and indexes match the applied schemas")
        for obj in stale or []:
            console.print(
                f"  [yellow][!] {escape(obj.name)}:[/yellow] stale {obj.kind} on {escape(obj.label)} "
                f"(no repository declares it); remove with `devgraph config schema prune-constraints`",
                soft_wrap=True,
            )
        for finding in drift or []:
            subject, label = escape(finding["repo_id"]), escape(finding["label"])
            key = escape(", ".join(finding["key"]))
            if finding["status"] == "missing":
                detail = (
                    f"applied {label} has no uniqueness constraint; re-provision with "
                    f"`devgraph rescan {finding['repo_id']} --now`"
                )
            else:
                detail = (
                    f"key change blocked by duplicate nodes: {label} nodes share a (repo_id, {key}) value, "
                    f"so the constraint keeps its old key; remove the duplicates, then "
                    f"`devgraph rescan {finding['repo_id']} --now`"
                )
            console.print(f"  [yellow][!] {subject}:[/yellow] {detail}", soft_wrap=True)

    # 7e. Custom provider scripts (§5.7). Runs no script; the §4.3 Podman readiness checks arrive with the runner.
    console.print("[bold]Script providers[/bold]")
    with _gate_warnings_once():
        script_findings = _script_provider_findings(registered_repos)
    for level, subject, detail in script_findings:
        marker = {"ok": "[green][OK]", "warning": "[yellow][!]", "failed": "[red][X]"}[level]
        closing = {"ok": "[/green]", "warning": "[/yellow]", "failed": "[/red]"}[level]
        console.print(f"  {marker} {escape(subject)}:{closing} {escape(detail)}", soft_wrap=True, emoji=False,
                      highlight=False)
        any_failed = any_failed or level == "failed"

    # 8. Tray/watcher liveness
    console.print("[bold]Live Watcher[/bold]")
    liveness = _tray_liveness_text(settings)
    if liveness == "running":
        console.print("  [green][OK] running[/green]")
    elif liveness == "not running":
        console.print("  [yellow]not running[/yellow] (expected if the tray app isn't started)")
    else:
        console.print(f"  [red]{liveness}[/red]")

    console.print()
    if any_failed:
        console.print("[red]doctor found one or more failing checks above.[/red]")
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")


def _vscode_mcp_config_path() -> Path:
    """Default user-level VS Code MCP config path for the current platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("%APPDATA% is not set; cannot locate VS Code's user config directory")
        config_home = Path(appdata)
    elif sys.platform == "darwin":
        config_home = Path.home() / "Library" / "Application Support"
    elif sys.platform == "linux":
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    else:
        raise RuntimeError(f"VS Code config location is unsupported on {sys.platform}")
    return config_home / "Code" / "User" / "mcp.json"


def _register_vscode(python_path: Path, repo_root: Path) -> bool:
    """Upsert a 'devgraph' entry into VS Code's user-level mcp.json.

    Merges into any existing file rather than overwriting it — other
    registered servers must survive this call untouched. Returns True on
    success; prints its own error and returns False on failure so callers
    (e.g. a multi-target loop) can report per-target status instead of
    aborting the whole run.
    """
    config_path = _vscode_mcp_config_path()
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            data = {}
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red][X] Error:[/red] could not read {config_path}: {exc}")
        return False

    data.setdefault("servers", {})
    data["servers"]["devgraph"] = {
        "type": "stdio",
        "command": str(python_path),
        "args": list(MCP_SERVER_ARGS),
        "cwd": str(repo_root),
    }

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        console.print(f"[red][X] Error:[/red] could not write {config_path}: {exc}")
        return False

    console.print(f"[green][OK][/green] VS Code: registered 'devgraph' in {config_path}")
    return True


def _run_claude_mcp_add(claude_path: str, python_path: Path, repo_root: Path) -> bool:
    """Register 'devgraph' with Claude Code via `claude mcp add`, skipping if already registered."""
    already_registered = subprocess.run(
        [claude_path, "mcp", "get", "devgraph"],
        cwd=str(repo_root),
        capture_output=True,
    ).returncode == 0
    if already_registered:
        console.print("[green][OK][/green] Claude Code: 'devgraph' already registered")
        return True
    mcp_add_line = f'claude mcp add devgraph -- "{python_path}" {" ".join(MCP_SERVER_ARGS)}'
    console.print(f"\n[bold]Running:[/bold] {mcp_add_line}")
    result = subprocess.run(
        [claude_path, "mcp", "add", "devgraph", "--", str(python_path), *MCP_SERVER_ARGS],
        cwd=str(repo_root),
    )
    return result.returncode == 0


mcp_app = typer.Typer(help="Manage DevGraph's MCP registration with Claude Code.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command(name="add")
def mcp_add() -> None:
    """Register DevGraph as an MCP server with Claude Code (runs 'claude mcp add')."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X] Error:[/red] 'claude' not found on PATH")
        raise typer.Exit(code=1)
    if not _run_claude_mcp_add(claude_path, resolve_venv_python(), resolve_repo_root()):
        raise typer.Exit(code=1)


@mcp_app.command(name="remove")
def mcp_remove() -> None:
    """Unregister DevGraph's MCP server from Claude Code (runs 'claude mcp remove')."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X] Error:[/red] 'claude' not found on PATH")
        raise typer.Exit(code=1)
    result = subprocess.run([claude_path, "mcp", "remove", "devgraph"])
    if result.returncode != 0:
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] Removed 'devgraph' from Claude Code")


@mcp_app.command(name="doctor")
def mcp_doctor() -> None:
    """Diagnose the DevGraph <-> Claude Code MCP registration."""
    any_failed = False

    claude_path = shutil.which("claude")
    if claude_path is None:
        console.print("[red][X][/red] 'claude' CLI not found on PATH")
        any_failed = True
    else:
        console.print(f"[green][OK][/green] claude CLI at {claude_path}")
        result = subprocess.run(
            [claude_path, "mcp", "get", "devgraph"], capture_output=True, text=True
        )
        if result.returncode == 0:
            console.print("[green][OK][/green] 'devgraph' registered")
            console.print(result.stdout.strip())
        else:
            console.print("[red][X][/red] 'devgraph' not registered (run 'devgraph mcp add')")
            any_failed = True

    try:
        from devgraph.mcp.server import build_server  # noqa: F401

        console.print("[green][OK][/green] devgraph.mcp.server imports cleanly")
    except Exception as e:
        console.print(f"[red][X] Import failed:[/red] {e}")
        any_failed = True

    if any_failed:
        raise typer.Exit(code=1)
    console.print("[green]All checks passed.[/green]")


@app.command(name="client-config")
def client_config(
    claude_mcp_add_only: bool = typer.Option(
        False, "--claude-mcp-add-only", help="Print just the 'claude mcp add' one-liner."
    ),
    run: bool = typer.Option(
        False, "--run", help="Execute registration for the selected --target(s) (opt-in; print-only is the default)."
    ),
    target: str = typer.Option(
        "both", "--target", help="Which client(s) to print/run registration for: 'claude', 'vscode', or 'both'."
    ),
) -> None:
    """Print (or optionally run) the MCP registration command for this machine's checkout.

    Resolves sys.executable and the repo root so the output is portable across
    machines instead of hardcoding a literal path — copy/paste this into any
    client repo's docs instead of a fixed path that only works on one machine.
    """
    if target not in ("claude", "vscode", "both"):
        console.print(f"[red][X] Error:[/red] --target must be 'claude', 'vscode', or 'both' (got '{target}')")
        raise typer.Exit(code=1)

    python_path = resolve_venv_python()
    repo_root = resolve_repo_root()
    mcp_add_line = f'claude mcp add devgraph -- "{python_path}" {" ".join(MCP_SERVER_ARGS)}'
    want_claude = target in ("claude", "both")
    want_vscode = target in ("vscode", "both")

    if claude_mcp_add_only:
        console.print(mcp_add_line, soft_wrap=True)
    else:
        console.print("## Connect DevGraph as an MCP server\n")
        console.print(f"- **command**: {python_path}")
        console.print(f"- **args**: {' '.join(MCP_SERVER_ARGS)}")
        console.print(f"- **cwd**: {repo_root}\n")
        if want_claude:
            console.print("```bash")
            console.print(mcp_add_line, soft_wrap=True)
            console.print("```")
        if want_vscode:
            console.print(f"\nVS Code (user mcp.json at {_vscode_mcp_config_path()}):")
            console.print("```json")
            console.print(json.dumps(
                {"servers": {"devgraph": {
                    "type": "stdio",
                    "command": str(python_path),
                    "args": list(MCP_SERVER_ARGS),
                    "cwd": str(repo_root),
                }}},
                indent=2,
            ))
            console.print("```")

    if run:
        any_failed = False
        if want_claude:
            claude_path = shutil.which("claude")
            if claude_path is None:
                console.print("[red][X] Error:[/red] 'claude' not found on PATH; skipping Claude Code registration")
                any_failed = True
            elif not _run_claude_mcp_add(claude_path, python_path, repo_root):
                any_failed = True
        if want_vscode:
            if not _register_vscode(python_path, repo_root):
                any_failed = True
        if any_failed:
            raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the installed DevGraph version."""
    try:
        ver = importlib.metadata.version("devgraph")
    except importlib.metadata.PackageNotFoundError:
        # Fallback: read pyproject.toml
        try:
            repo_root = resolve_repo_root()
            pyproject = repo_root / "pyproject.toml"
            if pyproject.exists():
                import tomllib
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                ver = data.get("project", {}).get("version", "unknown")
            else:
                ver = "unknown"
        except Exception:
            ver = "unknown"
    console.print(ver)


@app.command()
def dashboard(
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the dashboard in the default browser."
    ),
    url_only: bool = typer.Option(
        False, "--url-only", help="Just print the dashboard URL, don't open."
    ),
) -> None:
    """Open the DevGraph dashboard in the default browser, or print its URL."""
    settings = get_settings()
    url = dashboard_url(settings)

    # Check tray liveness
    liveness = _tray_liveness_text(settings)
    if liveness != "running":
        console.print("[yellow]Dashboard may not be running[/yellow] (tray app is not alive)")
        console.print("  Start it with: devgraph tray start")

    if url_only:
        console.print(url)
    elif open_browser:
        console.print(f"Opening {url} ...")
        webbrowser.open(url)
    else:
        console.print(url)


@app.command()
def stats(
    repo_id: Optional[str] = typer.Argument(
        None, help="Repository ID. Omit for aggregate stats across all repos."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON instead of a table."
    ),
) -> None:
    """Print summary statistics for one or all registered repositories."""
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        if repo_id:
            registry = _get_registry()
            try:
                if registry.get(repo_id) is None:
                    console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                    raise typer.Exit(code=1)
            finally:
                registry.close()

            data = dashboard_queries.summary_counts(engine, repo_id)
            total_nodes = sum(data["nodes_by_label"].values())
            total_rels = sum(data["relationships_by_type"].values())
        else:
            data = dashboard_queries.total_counts(engine)
            total_nodes = sum(data["nodes_by_label"].values())
            total_rels = sum(data["relationships_by_type"].values())

        if as_json:
            console.print_json(json.dumps({
                "total_nodes": total_nodes,
                "total_relationships": total_rels,
                **data,
            }))
        else:
            console.print(f"\n[bold]Stats{' for ' + repo_id if repo_id else ''}[/bold]")
            console.print(f"  Total nodes: {total_nodes}")
            console.print(f"  Total relationships: {total_rels}")

            if data["nodes_by_label"]:
                node_table = Table(title="Nodes by Label")
                node_table.add_column("Label", style="cyan")
                node_table.add_column("Count", style="green")
                for label, count in sorted(data["nodes_by_label"].items()):
                    node_table.add_row(label, str(count))
                console.print(node_table)

            if data["relationships_by_type"]:
                rel_table = Table(title="Relationships by Type")
                rel_table.add_column("Type", style="cyan")
                rel_table.add_column("Count", style="green")
                for rtype, count in sorted(data["relationships_by_type"].items()):
                    rel_table.add_row(rtype, str(count))
                console.print(rel_table)
    finally:
        engine.close()


@app.command()
def info(
    repo_id: str,
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON instead of a table."
    ),
) -> None:
    """Show detailed information about a registered repository."""
    settings = get_settings()
    registry = _get_registry()
    try:
        repo = registry.get(repo_id)
        if not repo:
            console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
            raise typer.Exit(code=1)

        # Node count from Neo4j
        node_count = 0
        engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            node_count = dashboard_queries.count_nodes(engine, repo_id)
        except Exception:
            pass
        finally:
            engine.close()

        # Git status
        git_status: dict[str, Any] = {}
        try:
            from devgraph.dashboard.git_info import get_git_status
            git_status = get_git_status(repo.path)
        except Exception:
            pass

        # Issues
        issues_path = settings.registry_db_path.parent / "repo_issues.json"
        repo_issues: dict[str, str] = {}
        if issues_path.exists():
            try:
                repo_issues = json.loads(issues_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if as_json:
            console.print_json(json.dumps({
                "repo_id": repo.repo_id,
                "path": str(repo.path),
                "active": repo.active,
                "watch_enabled": repo.watch_enabled,
                "last_indexed": repo.last_indexed,
                "last_indexed_commit": repo.last_indexed_commit,
                "docs_path": repo.docs_path,
                "mentions_enabled": repo.mentions_enabled,
                "pr_source_enabled": repo.pr_source_enabled,
                "issue_source_enabled": repo.issue_source_enabled,
                "node_count": node_count,
                "git_branch": git_status.get("branch"),
                "uncommitted_changes": len(git_status.get("uncommitted", [])),
                "issue": repo_issues.get(repo_id),
            }, default=str))
        else:
            console.print(f"\n[bold]Repository: {repo.repo_id}[/bold]")
            console.print(f"  Path: {repo.path}")
            console.print(f"  Active: {'[OK]' if repo.active else '[X]'}")
            console.print(f"  Watch: {'[OK]' if repo.watch_enabled else '[X]'}")
            console.print(f"  Last indexed: {repo.last_indexed or '-'}")
            console.print(f"  Last indexed commit: {repo.last_indexed_commit or '-'}")
            console.print(f"  Docs path: {repo.docs_path or '(not set)'}")
            console.print(f"  Mentions: {'[OK]' if repo.mentions_enabled else '[X]'}")
            console.print(f"  PR source: {'[OK]' if repo.pr_source_enabled else '[X]'}")
            console.print(f"  Issue source: {'[OK]' if repo.issue_source_enabled else '[X]'}")
            console.print(f"  Nodes in graph: {node_count}")
            console.print(f"  Git branch: {git_status.get('branch', '-')}")
            uncommitted = git_status.get("uncommitted", [])
            console.print(f"  Uncommitted changes: {len(uncommitted)}")
            if uncommitted:
                for entry in uncommitted[:10]:
                    console.print(f"    {entry['state']:>10}  {entry['path']}")
                if len(uncommitted) > 10:
                    console.print(f"    ... and {len(uncommitted) - 10} more")
            issue = repo_issues.get(repo_id)
            if issue:
                console.print(f"  [yellow]Issue:[/yellow] {issue}")
    finally:
        registry.close()


def _git_pull_command(remote_branch: str) -> list[str]:
    """Build a pull command from the CLI's REMOTE/BRANCH value."""
    remote, separator, branch = remote_branch.partition("/")
    if not separator or not remote or not branch:
        raise ValueError("update branch must use REMOTE/BRANCH format")
    return ["git", "pull", "--ff-only", remote, branch]


@app.command()
def update(
    force: bool = typer.Option(
        False, "--force", help="Allow update with a dirty working tree (stash first)."
    ),
    branch: str = typer.Option(
        "origin/master", "--branch", help="Remote branch to pull from."
    ),
) -> None:
    """Pull latest from git, reinstall dependencies, and verify.

    Port of scripts/update.ps1 into Python. Runs git pull --ff-only,
    reinstalls the editable package, runs doctor, and restarts the tray
    if it was running.
    """
    repo_root = resolve_repo_root()
    python_path = resolve_venv_python()

    # 1. Check working tree
    if not force:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if result.stdout.strip():
            console.print("[red][X] Local changes present[/red] — commit, stash, or use --force.")
            console.print(result.stdout)
            raise typer.Exit(code=1)

    # 2. Check tray liveness
    settings = get_settings()
    was_running = _tray_liveness_text(settings) == "running"
    if was_running:
        console.print("[yellow]Tray app is running — will restart after update.[/yellow]")

    # 3. Pull
    console.print("[bold]Pulling latest...[/bold]")
    try:
        pull_command = _git_pull_command(branch)
    except ValueError as exc:
        console.print(f"[red][X] {exc}[/red]")
        raise typer.Exit(code=2) from exc
    result = subprocess.run(
        pull_command, cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red][X] git pull failed:[/red] {result.stderr.strip()}")
        raise typer.Exit(code=1)
    console.print(f"[green][OK][/green] {result.stdout.strip()}")

    # 4. Stop tray if running
    if was_running:
        console.print("[bold]Stopping tray app...[/bold]")
        subprocess.run(
            [str(python_path), "-P", "-m", "devgraph.cli.main", "tray", "stop"],
            cwd=str(repo_root),
        )

    # 5. Reinstall
    console.print("[bold]Reinstalling dependencies...[/bold]")
    result = subprocess.run(
        [str(python_path), "-m", "pip", "install", "-e", ".[dev]"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red][X] pip install failed:[/red] {result.stderr.strip()}")
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] Dependencies reinstalled.")

    # 6. Doctor
    console.print("[bold]Verifying environment...[/bold]")
    result = subprocess.run(
        [str(python_path), "-P", "-m", "devgraph.cli.main", "doctor"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print("[red]doctor reported issues:[/red]")
        console.print(result.stdout)
        raise typer.Exit(code=1)
    console.print("[green][OK][/green] All checks passed.")

    # 7. Restart tray
    if was_running:
        console.print("[bold]Restarting tray app...[/bold]")
        subprocess.run(
            [str(python_path), "-P", "-m", "devgraph.cli.main", "tray", "start"],
            cwd=str(repo_root),
        )

    console.print("[green]Update complete.[/green]")


class _ConfigGroup(TyperGroup):
    """`devgraph config` once took a setting name positionally; point that
    old form at `config settings` instead of a bare "No such command"."""

    def resolve_command(self, ctx: typer.Context, args: list[str]):  # type: ignore[override]
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            from devgraph.config.settings import Settings

            if args[0] in Settings.model_fields:
                ctx.fail(
                    f"'{args[0]}' is a setting, not a subcommand: use "
                    f"`devgraph config settings {args[0]}`"
                )
        return super().resolve_command(ctx, args)


config_app = typer.Typer(
    cls=_ConfigGroup,
    invoke_without_command=True,
    help="View DevGraph settings, inspect and scaffold a repository's devgraph.schema.yaml, or enable/disable a repository's project config.",
)
app.add_typer(config_app, name="config")

# Substrings that mark a setting as secret: masked wherever settings are shown.
_SECRET_MARKERS = ("password", "secret", "token")


def _is_secret_setting(name: str) -> bool:
    return any(marker in name.lower() for marker in _SECRET_MARKERS)


def _shown_value(name: str, value: Any) -> Any:
    if _is_secret_setting(name):
        return "****" if value else "(empty)"
    return value


def _show_settings(key: str | None, show_defaults: bool, as_json: bool) -> None:
    """The settings view behind `devgraph config` and `devgraph config settings`."""
    settings = get_settings()

    fields: list[tuple[str, Any, Any]] = []
    model_fields = type(settings).model_fields
    for field_name in model_fields:
        field_info = model_fields[field_name]
        fields.append((field_name, getattr(settings, field_name), field_info.default))

    if key:
        fields = [(n, v, d) for n, v, d in fields if n == key]
        if not fields:
            console.print(f"[red][X] Unknown setting:[/red] {key}")
            raise typer.Exit(code=1)

    if as_json:
        data = {n: _shown_value(n, v) for n, v, _ in fields}
        console.print_json(json.dumps(data, default=str))
        return

    table = Table(title="DevGraph Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="green")
    if show_defaults:
        table.add_column("Default", style="yellow")
    for field_name, value, default in fields:
        row = [field_name, str(_shown_value(field_name, value))]
        if show_defaults:
            row.append(str(_shown_value(field_name, default)))
        table.add_row(*row)
    console.print(table)


@config_app.callback()
def config(
    ctx: typer.Context,
    show_defaults: bool = typer.Option(False, "--show-defaults", help="Also show the default value for each setting."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """With no subcommand, show DevGraph's settings (same as `config settings`)."""
    if ctx.invoked_subcommand is not None:
        if as_json or show_defaults:
            ctx.fail("--json/--show-defaults go after the subcommand, e.g. `devgraph config show --json`")
        return
    _show_settings(None, show_defaults, as_json)


@config_app.command("settings")
def config_settings(
    key: Optional[str] = typer.Argument(None, help="Setting key to show (e.g. 'neo4j_uri'). Omit to show all."),
    show_defaults: bool = typer.Option(False, "--show-defaults", help="Also show the default value for each setting."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show DevGraph's settings, or one setting. Secrets are masked."""
    _show_settings(key, show_defaults, as_json)


def _repo_dir(repo: Path) -> Path:
    """`--repo` as an absolute directory, or exit 1 with a plain message."""
    root = repo.expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red][X] Error:[/red] {escape(str(root))} is not a directory")
        raise typer.Exit(code=1)
    return root


def _registered_repos() -> list[Any]:
    """Every registered repository; [] when there is no registry yet (it is not created)."""
    if not get_settings().registry_db_path.exists():
        return []
    registry = _get_registry()
    try:
        return registry.list_repos()
    finally:
        registry.close()


def _containing_repo(repos: list[Any], target: Path) -> Any | None:
    """The deepest of `repos` whose root contains `target` (as the MCP session scope is chosen)."""
    best = None
    for repo in repos:
        root = Path(repo.path).expanduser().resolve()
        if target == root or target.is_relative_to(root):
            if best is None or len(root.parts) > len(Path(best.path).expanduser().resolve().parts):
                best = repo
    return best


def _default_repo_root() -> tuple[Path, bool]:
    """The scope when no `--repo` is given: the deepest active registered repository
    containing the current directory, else the current directory. Also: was one found."""
    cwd = _repo_dir(Path("."))
    match = _containing_repo([r for r in _registered_repos() if r.active], cwd)
    return (_repo_dir(Path(match.path)), True) if match is not None else (cwd, False)


@config_app.command("eject")
def config_eject(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root (default: current directory)."),
) -> None:
    """Write a commented starter devgraph.schema.yaml. Never overwrites an existing file."""
    from devgraph.config.project_schema import project_schema_path, starter_schema_text

    path = project_schema_path(_repo_dir(repo))
    try:
        # Exclusive create: also refuses a symlink or a file that appeared
        # after any check we could have made.
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(starter_schema_text())
    except FileExistsError:
        console.print(
            f"[red][X] Error:[/red] {escape(str(path))} already exists; eject never overwrites a "
            f"project schema. Edit it, or move it aside and eject again."
        )
        raise typer.Exit(code=1)
    console.print(f"[green][OK][/green] Wrote {escape(str(path))}")
    console.print("  Edit it, then run `devgraph config validate` and `devgraph rescan <repo_id>`.")


def _set_project_config(repo_id: str, enabled: bool) -> None:
    word = "enabled" if enabled else "disabled"
    registry = _get_registry()
    try:
        repo = registry.get(repo_id)
        if repo is None:
            console.print(f"[red][X] Error:[/red] no such repo_id: {escape(repo_id)}")
            raise typer.Exit(code=1)
        if repo.project_config_enabled == enabled:
            console.print(f"Project config for {escape(repo_id)} is already {word}.")
            return
        registry.set_project_config_enabled(repo_id, enabled)
    finally:
        registry.close()
    console.print(f"[green][OK][/green] Project config {word} for {escape(repo_id)}.")
    for note in _project_config_notes(repo_id):
        console.print(f"  {escape(note)}")
    if enabled:
        for line in _resuming_providers(repo):
            _plain(f"  {line}")


@config_app.command("enable")
def config_enable(repo_id: str = typer.Argument(..., help="Registered repo id.")) -> None:
    """Use the repository's devgraph.schema.yaml and devgraph.tools.yaml (the default)."""
    _set_project_config(repo_id, True)


@config_app.command("disable")
def config_disable(repo_id: str = typer.Argument(..., help="Registered repo id.")) -> None:
    """Ignore the repository's devgraph.schema.yaml and devgraph.tools.yaml, as if they did not exist.

    The files stay on disk. Use it to compare behaviour with and without them.
    """
    _set_project_config(repo_id, False)


def _schema_report(repo_root: Path | None) -> dict[str, Any]:
    """The effective schema and where each entry comes from.

    `repo_root=None` reports the built-in schema alone. Raises
    ProjectSchemaError for an unreadable or invalid project file.
    """
    from devgraph.config.project_schema import (
        SCHEMA_FILENAME,
        load_project_schema,
        project_schema_path,
        resolve_declaration,
    )
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES

    enabled = True if repo_root is None else project_config_enabled(repo_root)
    declaration = None if repo_root is None else load_project_schema(repo_root)
    origin = None if repo_root is None else str(project_schema_path(repo_root))
    effective = resolve_declaration(declaration, origin=origin or SCHEMA_FILENAME)
    inherits = effective.extends == "default"

    node_types: list[dict[str, Any]] = [
        {"label": label, "origin": "built-in", "key": None, "color": None} for label in (NODE_LABELS if inherits else ())
    ]
    node_types += [
        {"label": n.label, "origin": SCHEMA_FILENAME, "key": list(n.key), "color": n.color} for n in effective.node_types
    ]
    relationships: list[dict[str, Any]] = [
        {"type": rel, "origin": "built-in", "from": None, "to": None, "provider": "builtin", "color": None}
        for rel in (RELATIONSHIP_TYPES if inherits else ())
    ]
    relationships += [
        {"type": r.type, "origin": SCHEMA_FILENAME, "from": r.from_, "to": r.to, "provider": r.provider, "color": r.color}
        for r in effective.relationships
    ]
    if repo_root is None:
        status = "global"
    elif not enabled:
        status = "disabled"
    else:
        status = "absent" if declaration is None else "valid"
    return {
        "repo": None if repo_root is None else str(repo_root),
        "project_config": None if repo_root is None else ("enabled" if enabled else "disabled"),
        "schema_file": origin if declaration is not None else None,
        "status": status,
        "extends": effective.extends,
        "node_types": node_types,
        "relationships": relationships,
    }


def _global_tools_report(repo_root: Path | None) -> dict[str, Any]:
    """The global tools store for `config show`, with the ones a project tool overrides. Raises ProjectToolsError."""
    from devgraph.config.global_tools import global_tools_path, load_global_tools
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.config.project_tools import load_project_tools

    declared = load_global_tools()
    if declared is None:
        return {"status": "absent", "tools_file": None, "tools": []}
    project = None
    if repo_root is not None and project_config_enabled(repo_root):
        project = load_project_tools(repo_root)
    overriding = {tool.name for tool in project.tools} if project else set()
    return {
        "status": "valid",
        "tools_file": str(global_tools_path()),
        "tools": [
            {"name": tool.name, "description": tool.description, "overridden": tool.name in overriding}
            for tool in declared.tools
        ],
    }


def _tools_report(repo_root: Path) -> dict[str, Any]:
    """A repository's project tools for `config show`. Raises ProjectToolsError."""
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.config.project_tools import load_project_tools, tools_file_path

    if not project_config_enabled(repo_root):
        return {"status": "disabled", "tools_file": None, "tools": []}
    declared = load_project_tools(repo_root)
    if declared is None:
        return {"status": "absent", "tools_file": None, "tools": []}
    return {
        "status": "valid",
        "tools_file": str(tools_file_path(repo_root)),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": [
                    {"name": p.name, "type": p.type, "required": p.required, "default": p.default}
                    for p in tool.parameters
                ],
                "max_rows": tool.max_rows,
                "timeout_s": tool.timeout_s,
            }
            for tool in declared.tools
        ],
    }


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_only: bool = typer.Option(False, "--global", help="Show only DevGraph's built-in schema."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show the effective graph schema for a repository and where each entry comes from.

    Built-in node types are keyed by DevGraph's own identity rules (JSON `key: null`).
    Also shows the repository's project tools.
    """
    from devgraph.config.project_schema import SCHEMA_FILENAME, ProjectSchemaError
    from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError

    if global_only and repo is not None:
        ctx.fail("use either --global or --repo, not both")
    root = None if global_only else (_repo_dir(repo) if repo is not None else _default_repo_root()[0])
    try:
        report = _schema_report(root)
    except ProjectSchemaError as exc:
        console.print(f"[red][X] Invalid project schema:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)

    if root is None:
        report["tools"] = None
    else:
        try:
            report["tools"] = _tools_report(root)
        except ProjectToolsError as exc:
            console.print(f"[red][X] Invalid project tools:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1)

    try:
        report["global_tools"] = _global_tools_report(root)
    except ProjectToolsError as exc:
        console.print(f"[red][X] Invalid global tools:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return

    if report["status"] == "global":
        console.print("Built-in schema (no global config file yet)")
    elif report["status"] == "disabled":
        console.print(
            f"{escape(report['repo'])}: project config disabled — built-in schema "
            f"(`devgraph config enable {escape(_registered_repo_id(root))}` to use {SCHEMA_FILENAME})"
        )
    elif report["status"] == "absent":
        console.print(f"{escape(report['repo'])}: no {SCHEMA_FILENAME} — built-in schema")
    else:
        console.print(f"{escape(report['repo'])}: {escape(report['schema_file'])} (valid, extends: {report['extends']})")

    nodes = Table(title="Node types")
    nodes.add_column("Label", style="cyan")
    nodes.add_column("Origin")
    nodes.add_column("Key")
    nodes.add_column("Colour")
    for node in report["node_types"]:
        nodes.add_row(
            node["label"], node["origin"], ", ".join(node["key"]) if node["key"] else "built-in identity",
            node["color"] or "\u2014",
        )
    console.print(nodes)

    rels = Table(title="Relationships")
    rels.add_column("Type", style="cyan")
    rels.add_column("From")
    rels.add_column("To")
    rels.add_column("Provider")
    rels.add_column("Origin")
    rels.add_column("Colour")
    for rel in report["relationships"]:
        rels.add_row(
            rel["type"], rel["from"] or "any", rel["to"] or "any", rel["provider"], rel["origin"],
            rel["color"] or "\u2014",
        )
    console.print(rels)

    tools = report["tools"]
    if tools is not None:
        if tools["status"] == "disabled":
            console.print("Project config disabled — no project tools")
        elif tools["status"] == "absent":
            console.print(f"No {TOOLS_FILENAME} — no project tools")
        else:
            table = Table(title=f"Project tools ({escape(tools['tools_file'])})")
            table.add_column("Name", style="cyan")
            table.add_column("Parameters")
            table.add_column("Max rows")
            table.add_column("Timeout")
            for tool in tools["tools"]:
                params = ", ".join(p["name"] + ("" if p["required"] else "?") for p in tool["parameters"]) or "—"
                table.add_row(tool["name"], params, str(tool["max_rows"]), f"{tool['timeout_s']}s")
            console.print(table)

    global_tools = report["global_tools"]
    if global_tools["status"] == "absent":
        console.print("No global tools")
    else:
        table = Table(title=f"Global tools ({escape(global_tools['tools_file'])})")
        table.add_column("Name", style="cyan")
        table.add_column("Description")
        table.add_column("Overridden by project")
        for tool in global_tools["tools"]:
            table.add_row(tool["name"], escape(tool["description"]), "yes" if tool["overridden"] else "")
        console.print(table)


@config_app.command("validate")
def config_validate(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    all_repos: bool = typer.Option(False, "--all", help="Check every registered repository, and conflicts between them."),
) -> None:
    """Fail-closed check of devgraph.schema.yaml and devgraph.tools.yaml. Exits 1 if anything is invalid or conflicting."""
    from types import SimpleNamespace

    from devgraph.config.project_switch import project_config_enabled

    if all_repos and repo is not None:
        ctx.fail("use either --repo or --all, not both")
    if all_repos:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
        finally:
            registry.close()
        if not repos:
            console.print("No registered repositories.")
            return
    else:
        root = _repo_dir(repo) if repo is not None else _default_repo_root()[0]
        # `hint_id` is only needed (and the registry only opened) for a disabled repo,
        # which is necessarily registered.
        hint_id = "<repo_id>" if project_config_enabled(root) else _registered_repo_id(root)
        repos = [SimpleNamespace(repo_id=str(root), path=root, hint_id=hint_id)]

    findings = _project_schema_findings(repos) + _project_tools_findings(repos) + _global_tools_findings(repos)
    for finding in findings:
        colour = "red" if finding["failed"] else ("yellow" if finding["status"] in ("warning", "disabled", "notice") else "green")
        subject = finding["repo_id"] or "cross-repository"
        console.print(f"[{colour}]{finding['status']}[/{colour}] {escape(str(subject))}: {escape(finding['detail'])}")
    if any(finding["failed"] for finding in findings):
        raise typer.Exit(code=1)


tools_app = typer.Typer(
    help="List, add, edit, delete or reset tools: a repository's devgraph.tools.yaml, or with --global the user's global tools.",
    no_args_is_help=True,
)
config_app.add_typer(tools_app, name="tools")


def _tools_scope(
    ctx: typer.Context,
    repo: Optional[Path],
    global_: bool,
    consequence: str = "MCP sessions won't serve its tools",
) -> Path | None:
    """The repository root for a `config tools` command, or None for the global store.

    Without `--repo`: the registered repository containing the current directory, else
    the current directory itself (with a warning on stderr).
    """
    if global_ and repo is not None:
        ctx.fail("use either --global or --repo, not both")
    if global_:
        return None
    if repo is not None:
        return _repo_dir(repo)
    root, registered = _default_repo_root()
    if not registered:
        Console(stderr=True).print(
            f"[yellow]Warning:[/yellow] {escape(str(root))} is not a registered repository (nor inside one), "
            f"so {consequence}; register it with `devgraph add`.",
            soft_wrap=True,
        )
    return root


def _scope_record(root: Path):
    """The registry record for the repository at `root` (active or not), else None."""
    return next((r for r in _registered_repos() if Path(r.path).expanduser().resolve() == root), None)


def _tools_scope_note(root: Path | None) -> str:
    """Whether running MCP sessions will serve what is in this scope's file."""
    from devgraph.config.edits import tools_effect_note

    return tools_effect_note(root, None if root is None else _scope_record(root))


@contextlib.contextmanager
def _edit_errors(edit_command: str | None = None):
    """Report a refused config edit as the CLI's error line and exit 1.

    `edit_command` (e.g. "devgraph config tools edit x") replaces a taken-name
    refusal's generic "edit it instead" with the command that does it.
    """
    from devgraph.config.edits import EDIT_INSTEAD, ConfigEditError

    try:
        yield
    except ConfigEditError as exc:
        message = exc.message
        if edit_command and message.endswith(EDIT_INSTEAD):
            message = message[: -len(EDIT_INSTEAD)] + f"; use `{edit_command}`"
        raise _tools_fail(message) from None


def _tools_fail(message: str) -> typer.Exit:
    console.print(f"[red][X] Error:[/red] {escape(message)}")
    return typer.Exit(code=1)


def _read_tool_source(source: str) -> dict:
    """One tool mapping from a YAML/JSON file, or stdin for `-`."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _tools_fail(f"cannot read {source}: {exc}")
    return _parse_tool_text(text, "stdin" if source == "-" else source)


def _parse_tool_text(text: str, label: str, what: str = "one tool") -> dict:
    from devgraph.config.project_tools import YAML_LOAD_ERRORS
    from devgraph.config.yaml_bound import bounded_safe_load

    try:
        tool = bounded_safe_load(text)
    except YAML_LOAD_ERRORS as exc:
        raise _tools_fail(f"{label}: malformed YAML: {exc}")
    if not isinstance(tool, dict):
        raise _tools_fail(f"{label}: expected {what} as a YAML mapping")
    return tool


def _tools_done(verb: str, name: str, path: Path, root: Path | None) -> None:
    console.print(f"[green]{verb}[/green] tool {escape(repr(name))}: {escape(str(path))}", soft_wrap=True)
    console.print(escape(_tools_scope_note(root)), soft_wrap=True)


@tools_app.command("list")
def config_tools_list(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_: bool = typer.Option(False, "--global", help="List only the global tools store."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List the tools in effect: built-in (locked), global, and project, with overrides marked."""
    from devgraph.config.global_tools import load_global_tools
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.config.project_tools import ProjectToolsError, load_project_tools
    from devgraph.mcp.catalog import builtin_tool_names

    root = _tools_scope(ctx, repo, global_)
    builtin = builtin_tool_names()
    try:
        declared_global = load_global_tools()
        declared_project = None if root is None or not project_config_enabled(root) else load_project_tools(root)
    except ProjectToolsError as exc:
        raise _tools_fail(str(exc))
    global_tools = {t.name: t for t in (declared_global.tools if declared_global else ())}
    project_names = [t.name for t in (declared_project.tools if declared_project else ())]
    ignored = {"ignored": "shadows a locked tool"}  # a built-in name in a tools file: MCP serves the built-in

    rows: list[dict[str, Any]] = []
    if root is None:
        rows = [{"name": name, "origin": "global", "locked": False, **(ignored if name in builtin else {})}
                for name in global_tools]
    else:
        # run_cypher is served (and so in effect) only with enable_run_cypher.
        served_builtin = builtin if get_settings().enable_run_cypher else builtin - {"run_cypher"}
        rows = [{"name": name, "origin": "built-in", "locked": True} for name in sorted(served_builtin)]
        rows += [{"name": n, "origin": "global", "locked": False} for n in global_tools if n not in project_names and n not in builtin]
        for name in project_names:
            if name in builtin:
                continue
            origin = "project (overrides global)" if name in global_tools else "project"
            rows.append({"name": name, "origin": origin, "locked": False})
        rows += [{"name": n, "origin": "global", "locked": False, **ignored} for n in global_tools if n in builtin]
        rows += [{"name": n, "origin": "project", "locked": False, **ignored} for n in project_names if n in builtin]

    record = None if root is None else _scope_record(root)
    serves = record is not None and record.active and record.project_config_enabled
    trust = _trust_state(root) if serves else None
    if as_json:
        typer.echo(json.dumps({"tools": rows, **({"trust": trust} if root is not None else {})}, indent=2))
        return
    table = Table(title="Global tools" if root is None else escape(f"Tools for {root}"))
    table.add_column("Name", style="cyan")
    table.add_column("Origin")
    for row in rows:
        origin = row["origin"] + (" (locked)" if row["locked"] else "")
        if row.get("ignored"):
            origin += f" — ignored: {row['ignored']}"
        table.add_row(escape(row["name"]), origin)
    console.print(table)
    if not serves:  # unregistered or disabled; or the global note for --global
        console.print(escape(_tools_scope_note(root)), soft_wrap=True)
    elif not project_config_enabled(root):
        console.print("Project config disabled: project tools are not served")
    elif trust == "trusted":
        console.print("Project tools: trusted (devgraph.tools.yaml matches the approved sha256)", soft_wrap=True)
    elif trust is not None:
        from devgraph.config.project_trust import untrusted_reason

        console.print(escape(f"Project tools are not served: {untrusted_reason(record.repo_id, trust)}"), soft_wrap=True)
    if root is not None:
        console.print(_GLOBAL_TOOLS_NOTE, soft_wrap=True)


def _trust_state(root: Path) -> str | None:
    """The trust state of the repository's tools file as it is now (see `project_trust`); None without a regular file."""
    from devgraph.config import project_trust
    from devgraph.config.project_tools import tools_file_outside, tools_file_path

    path = tools_file_path(Path(root))
    try:
        if tools_file_outside(Path(root)) or not path.is_file():
            return None
        data = read_bounded(path)
    except OSError:
        return None
    return project_trust.project_tools_trust(root, data)


def _global_tool_names() -> set[str]:
    """Names in the global tools store; empty when it is absent or invalid."""
    from devgraph.config.global_tools import load_global_tools
    from devgraph.config.project_tools import ProjectToolsError

    try:
        store = load_global_tools()
    except ProjectToolsError:
        return set()
    return {t.name for t in store.tools} if store is not None else set()


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _trust_target(repo: Optional[str]) -> Any:
    """The registered repository `repo` names (a repo id, or a path inside one), else the one containing the current directory."""
    repos = _registered_repos()
    if repo is not None:
        match = next((r for r in repos if r.repo_id == repo), None)
        target = Path(repo).expanduser()
        if match is None and target.is_dir():
            match = _containing_repo(repos, target.resolve())
    else:
        match = _containing_repo([r for r in repos if r.active], _repo_dir(Path(".")))
    if match is None:
        raise _tools_fail(f"{repo or 'the current directory'} is not a registered repository (nor inside one); "
                          "register it with `devgraph add`")
    return match


@tools_app.command("trust")
def config_tools_trust(
    repo: Optional[str] = typer.Argument(None, help="Registered repo id or path (default: the repository containing the current directory)."),
    sha256: Optional[str] = typer.Option(None, "--sha256", help="For CI: approve without asking, only if this is the file's sha256 (hex)."),
) -> None:
    """Serve a repository's devgraph.tools.yaml: shows its tools and sha256, then approves exactly those bytes.

    Project tools are off until trusted, and any change to the file needs trusting again.
    """
    from devgraph.config.project_tools import ProjectToolsError, parse_project_tools, tools_file_outside, tools_file_path
    from devgraph.config.project_trust import tools_sha256

    record = _trust_target(repo)
    path = tools_file_path(Path(record.path))
    if tools_file_outside(Path(record.path)):
        raise _tools_fail(f"{path.name} in {record.path} resolves outside the repository; nothing was trusted")
    try:
        if not path.is_file():
            raise _tools_fail(f"no {path.name} in {record.path}; nothing to trust")
        data = read_bounded(path)
    except OSError as exc:
        raise _tools_fail(f"cannot read {path}: {exc}")
    try:
        declared = parse_project_tools(data.decode("utf-8"), path)
    except (UnicodeDecodeError, ProjectToolsError) as exc:
        raise _tools_fail(f"{path.name} is invalid; fix it before trusting it: {str(exc).splitlines()[0]}")
    digest = tools_sha256(data)
    global_names = _global_tool_names()
    console.print(f"Tools in {escape(str(path))}:", soft_wrap=True)
    for tool in declared.tools:
        console.print(f"\n[cyan]{escape(tool.name)}[/cyan]")
        if tool.name in global_names:
            console.print(f"  overrides the global tool {escape(repr(tool.name))} in this repository", soft_wrap=True)
        console.print(f"  description: {escape(tool.description)}", highlight=False, soft_wrap=True)
        for p in tool.parameters:
            detail = f"{p.type}, " + ("required" if p.required else f"optional, default {p.default!r}")
            line = f"  parameter {p.name} ({detail})" + (f": {p.description}" if p.description else "")
            console.print(escape(line), highlight=False, soft_wrap=True)
        console.print(escape(tool.cypher.rstrip()), highlight=False, soft_wrap=True)
    if not declared.tools:
        console.print("(no tools)")
    console.print(f"\nsha256: {digest}")
    console.print(
        "An enabled project tool can read the whole graph, every registered repository's data and not only this "
        "one's: the $repo_id rule is a convention, not a sandbox.", soft_wrap=True,
    )
    if sha256 is not None:
        if sha256.strip().lower() != digest:
            raise _tools_fail(f"--sha256 does not match {path.name}; nothing was trusted")
    elif not _stdin_is_tty():
        raise _tools_fail("no terminal to confirm on: approving project tools needs the user at a terminal "
                          f"to review them and run `devgraph config tools trust {record.repo_id}`")
    elif not typer.confirm(f"Trust these tools for {record.repo_id}?", default=False):
        console.print("Not trusted.")
        raise typer.Exit(code=1)
    registry = _get_registry()
    try:
        registry.set_project_tools_sha256(record.repo_id, digest)
    finally:
        registry.close()
    console.print(f"[green][OK][/green] Trusted {escape(path.name)} for {escape(record.repo_id)}; "
                  "running MCP sessions serve its tools within 2 seconds.", soft_wrap=True)
    if not record.project_config_enabled:
        console.print(f"Project config is disabled for {escape(record.repo_id)}, so its tools are still not served; "
                      f"enable it with `devgraph config enable {escape(record.repo_id)}`.", soft_wrap=True)


@tools_app.command("untrust")
def config_tools_untrust(
    repo: Optional[str] = typer.Argument(None, help="Registered repo id or path (default: the repository containing the current directory)."),
) -> None:
    """Stop serving a repository's project tools until they are trusted again."""
    record = _trust_target(repo)
    if record.project_tools_sha256 is None:
        console.print(f"Project tools for {escape(record.repo_id)} are not trusted; nothing to do.")
        return
    registry = _get_registry()
    try:
        registry.set_project_tools_sha256(record.repo_id, None)
    finally:
        registry.close()
    console.print(f"[green][OK][/green] Revoked trust in {escape(record.repo_id)}'s project tools; "
                  "running MCP sessions stop serving them within 2 seconds.", soft_wrap=True)


@tools_app.command("add")
def config_tools_add(
    ctx: typer.Context,
    source: str = typer.Option(..., "--from", help="YAML or JSON file holding one tool, or - for stdin."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_: bool = typer.Option(False, "--global", help="Add to the global tools store."),
) -> None:
    """Add one tool. Fails if the name exists (use `edit`) or is a built-in name."""
    from devgraph.config.edits import add_tool

    root = _tools_scope(ctx, repo, global_)
    tool = _read_tool_source(source)
    with _edit_errors(f"devgraph config tools edit {tool.get('name')}"):
        result = add_tool(root, tool)
    _tools_done("Added", str(tool.get("name")), result.path, root)


@tools_app.command("edit")
def config_tools_edit(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Tool to replace."),
    source: Optional[str] = typer.Option(None, "--from", help="YAML or JSON file holding the new tool, or - for stdin. Default: open $EDITOR."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_: bool = typer.Option(False, "--global", help="Edit a global tool."),
) -> None:
    """Replace one tool, from a file or in $EDITOR. Nothing is written if the result is unchanged or invalid."""
    from devgraph.config.edits import find_tool, replace_tool
    from devgraph.config.tools_edit import dump_tool

    root = _tools_scope(ctx, repo, global_)
    with _edit_errors():
        current = find_tool(root, name)
    if source is not None:
        tool = _read_tool_source(source)
    else:
        original = dump_tool(current)
        edited = click.edit(original, extension=".yaml")
        if edited is None or edited == original:
            console.print("No changes.")
            return
        tool = _parse_tool_text(edited, "edited tool")
        if tool == current:
            console.print("No changes.")
            return
    with _edit_errors():
        result = replace_tool(root, name, tool)
    _tools_done("Updated", name, result.path, root)


@tools_app.command("delete")
def config_tools_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Tool to remove."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_: bool = typer.Option(False, "--global", help="Delete a global tool."),
) -> None:
    """Remove one tool. Unknown names exit 1."""
    from devgraph.config.edits import delete_tool

    root = _tools_scope(ctx, repo, global_)
    with _edit_errors():
        result = delete_tool(root, name)
    _tools_done("Deleted", name, result.path, root)


@tools_app.command("reset")
def config_tools_reset(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    global_: bool = typer.Option(False, "--global", help="Empty the global tools store."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Remove every tool in the scope: delete devgraph.tools.yaml, or empty the global store."""
    from devgraph.config.edits import reset_tools, tools_path

    root = _tools_scope(ctx, repo, global_)
    path = tools_path(root)
    if not os.path.lexists(path):
        console.print(f"Nothing to reset: {escape(str(path))} does not exist.", soft_wrap=True)
        return
    if not yes:
        typer.confirm(f"Remove every tool in {path}?", abort=True)
    with _edit_errors():
        reset_tools(root)
    console.print(f"[green]Reset[/green] {escape(str(path))}", soft_wrap=True)
    console.print(escape(_tools_scope_note(root)), soft_wrap=True)


scripts_app = typer.Typer(
    help="Custom provider scripts: list, show, approve, revoke, enable or disable. This version runs no scripts.",
    no_args_is_help=True,
)
config_app.add_typer(scripts_app, name="scripts")

# A message for whoever reads the output, possibly an agent: approval is the user's, at a terminal.
_ASK_THE_USER = "ask the user to review and run `{command}` in a terminal"


def _plain(text: str, prefix: str = "") -> None:
    """Print `text` as plain text after DevGraph's own markup `prefix`: never Rich markup or
    emoji, and wrapped onto further rows rather than cropped, so nothing hides off to the right.
    Repository-sourced parts of `text` must already have gone through `display.visible`."""
    line = Text.from_markup(prefix) + Text(text) if prefix else Text(text)
    console.print(line, overflow="fold", no_wrap=False, highlight=False)


def _scripts_fail(message: str) -> typer.Exit:
    _plain(message, "[red][X] Error:[/red] ")
    return typer.Exit(code=1)


def _sandbox_files() -> tuple[Path, Path]:
    """(trust store, fixed-path registry), under the sandbox home (never `Settings`)."""
    home = sandbox_paths.sandbox_home()
    return sandbox_paths.trust_store_path(home), sandbox_paths.fixed_registry_path(home)


def _require_scripts_platform(action: str) -> None:
    platform = consent.current_platform()
    if not sandbox_paths.platform_supported(platform):
        raise _scripts_fail(f"custom providers are unavailable (platform): {action} needs Linux, and this is {visible(platform)}")


def _scripts_repo(repo_id: str) -> tuple[Any, str, Path]:
    """The registered repository, its canonical path (the trust key) and its real path (for the reader)."""
    record = next((r for r in _registered_repos() if r.repo_id == repo_id), None)
    if record is None:
        raise _scripts_fail(f"no such repo_id: {visible(repo_id)}")
    try:
        canon = sandbox_paths.canonical_repo_path(record.path)
    except sandbox_paths.SandboxPathError as exc:
        raise _scripts_fail(f"{visible(str(exc))}; custom providers are unavailable for {visible(repo_id)}")
    return record, canon, Path(os.path.realpath(record.path))


def _declared_providers(root: Path) -> list[str]:
    """The custom provider names `root`'s schema declares; [] if none, or if it cannot be read."""
    from devgraph.config.project_schema import parse_project_schema
    from devgraph.sandbox.reader import SCHEMA_FILE, read_schema_file

    try:
        real = Path(os.path.realpath(root))
        schema = parse_project_schema(read_schema_file(real).decode("utf-8"), Path(SCHEMA_FILE))
    except Exception:
        return []
    return [provider.name for provider in schema.custom_providers]


def _applied_schema_status(repo_id: str, schema_hash: str) -> str:
    """`applied` when the graph's applied schema hash equals the snapshot's, else `pending` (§5.5);
    `unreachable` when the graph cannot be read, which also counts as pending (fail closed)."""
    try:
        settings = get_settings()
        engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        try:
            applied = engine.read_applied_schema(repo_id)
        finally:
            engine.close()
    except Exception:
        return "unreachable"
    return "applied" if applied is not None and applied.get("hash") == schema_hash else "pending"


_GRAPH_UNREACHABLE = "graph unreachable (start Neo4j)"


@contextlib.contextmanager
def _gate_warnings_once() -> Any:
    """Within one command, log each distinct script-gate warning once: the gates are evaluated
    per provider, and with no logging configured every record would reach stderr."""
    seen: set[str] = set()

    def once(record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message in seen:
            return False
        seen.add(message)
        return True

    gate_logger = logging.getLogger("devgraph.sandbox.gates")
    gate_logger.addFilter(once)
    try:
        yield
    finally:
        gate_logger.removeFilter(once)


def _provider_states(record: Any) -> list[tuple[str, Any, str, Optional[str]]]:
    """(provider name, snapshot or InputError, state, would-be state) for each provider `record`
    declares. The state is `pending` while the graph is unreachable; the would-be state is then
    what the gates and digest alone give, else None. A schema that cannot be read gives one row named "-"."""
    from devgraph.sandbox.reader import InputError
    from devgraph.sandbox.snapshot import provider_state, repo_snapshots

    store_path, registry_path = _sandbox_files()
    platform = consent.current_platform()
    try:
        canon = sandbox_paths.canonical_repo_path(record.path)
    except sandbox_paths.SandboxPathError as exc:
        error = InputError("input_unavailable", str(exc))
        return [(name, error, "unavailable", None) for name in _declared_providers(Path(record.path)) or ["-"]]
    try:
        snaps: dict[str, Any] = repo_snapshots(Path(os.path.realpath(record.path)), git=git_binary())
    except InputError as exc:
        snaps = {"-": exc}
    hashes = {s.schema_hash for s in snaps.values() if not isinstance(s, InputError)}
    status = "applied"
    if hashes and sandbox_paths.platform_supported(platform):
        status = _applied_schema_status(record.repo_id, hashes.pop())

    def state(snap: Any, pending: bool) -> str:
        return provider_state(record.repo_id, canon, snap, platform=platform, pending=pending,
                              registry_path=registry_path, store_path=store_path)

    rows = []
    for name, snap in snaps.items():
        current = state(snap, status != "applied")
        would_be = state(snap, False) if status == "unreachable" and current == "pending" else None
        rows.append((name, snap, current, would_be))
    return rows


def _open_store_read() -> Any:
    from devgraph.sandbox.trust import TrustStore

    return TrustStore.open_read(_sandbox_files()[0])


def _approval_notes(snap: Any, history: list[Any]) -> tuple[str, str]:
    """(approved-at, note) for a list row: the approval of the current digest, and whether
    it is an older kept digest or the matched-file count has grown (§5.6)."""
    active = [a for a in history if a.state == "active"]
    current = next((a for a in active if a.digest == snap.digest), None)
    reference = current or (active[-1] if active else (history[-1] if history else None))
    notes = []
    if current is not None and current is not active[-1]:
        notes.append(f"kept older digest is active (approved {consent.utc_time(current.approved_at)})")
    if reference is not None and consent.grew(snap.matched_count, reference.matched_count):
        notes.append(f"inputs grew: {snap.matched_count} matched, approved with {reference.matched_count}")
    return (consent.utc_time(current.approved_at) if current else "-"), "; ".join(notes)


@scripts_app.command("list")
def scripts_list(
    repo: Optional[str] = typer.Option(None, "--repo", help="Only this registered repo id."),
) -> None:
    """Each custom provider's state, digest, approval date and matched-file count."""
    from devgraph.sandbox.digest import short_digest
    from devgraph.sandbox.reader import InputError

    records = _registered_repos()
    if repo is not None:
        records = [r for r in records if r.repo_id == repo]
        if not records:
            raise _scripts_fail(f"no such repo_id: {visible(repo)}")
    table = Table(title="Custom providers")
    for column in ("Repository", "Provider", "State", "Digest", "Approved at", "Matched", "Note"):
        table.add_column(column, overflow="fold")
    store = _open_store_read()
    try:
        with _gate_warnings_once():
            for record in records:
                for name, snap, state, would_be in _provider_states(record):
                    if isinstance(snap, InputError):
                        table.add_row(Text(visible(record.repo_id)), Text(visible(name)), state, "-", "-", "-",
                                      Text(visible(snap.reason or snap.code)))
                        continue
                    history = store.approvals(record.repo_id, sandbox_paths.canonical_repo_path(record.path), name) if store else []
                    approved_at, note = _approval_notes(snap, history)
                    if would_be is not None:
                        note = "; ".join(filter(None, (f"{_GRAPH_UNREACHABLE}; otherwise {would_be}", note)))
                    table.add_row(Text(visible(record.repo_id)), Text(visible(name)), state, short_digest(snap.digest),
                                  approved_at, str(snap.matched_count), note)
    finally:
        if store is not None:
            store.close()
    if not table.rows:
        console.print("No custom providers declared.")
        return
    console.print(table)


@scripts_app.command("show")
def scripts_show(
    repo_id: str = typer.Argument(..., help="Registered repo id."),
    name: str = typer.Argument(..., help="Custom provider name."),
) -> None:
    """One provider in full: state, script, inputs, findings, the digest (alone on its line) and approval history."""
    from devgraph.sandbox.digest import short_digest
    from devgraph.sandbox.reader import InputError

    record, canon, _ = _scripts_repo(repo_id)
    with _gate_warnings_once():
        rows = {row_name: (snap, state, would_be) for row_name, snap, state, would_be in _provider_states(record)}
    if name not in rows:
        snap, state, _ = rows.get("-", (None, "unavailable", None))
        reason = snap.reason if isinstance(snap, InputError) else f"no custom provider named {name!r} is declared"
        raise _scripts_fail(f"{visible(name)}: {state}: {visible(reason)}")
    snap, state, would_be = rows[name]
    _plain(f"State: {state}" + (f" ({_GRAPH_UNREACHABLE}; otherwise {would_be})" if would_be else ""))
    _plain(f"Repository: {visible(record.repo_id)}")
    _plain(f"Canonical path: {visible(canon)}")
    if isinstance(snap, InputError):
        _plain(f"Reason: {visible(snap.reason or snap.code)}")
        raise typer.Exit(code=1)
    _plain(f"Script: .devgraph/providers/{name}.py ({len(snap.script_text.encode('utf-8'))} bytes)")
    _plain("Inputs: " + ", ".join(visible(g) for g in snap.declaration_set["provider"]["inputs"]))
    _plain(f"  {snap.matched_count} matched file(s); {snap.denied} excluded by the secret-name denylist; "
           f"{snap.total_bytes} input bytes")
    _plain(f"  sample (first {len(snap.sample)}):")
    for path in snap.sample:
        _plain(f"    {visible(path)}")
    for error in snap.errors:
        _plain(f"  unreadable: {visible(error.reason or error.code)}")
    if snap.findings:
        _plain("Static scan findings:")
        for finding in snap.findings:
            _plain(f"  line {finding.line}: {finding.rule}: {visible(finding.message)}")
    else:
        _plain("Static scan: no findings")
    _plain("Digest (for review):")
    _plain(snap.digest)
    _plain("(This digest is for review: it is not to be passed to --sha256 by an automated agent. That option is "
           "for CI, with the digest kept in a protected secret.)")
    store = _open_store_read()
    history = []
    if store is not None:
        with store:
            history = store.approvals(record.repo_id, canon, name)
    _plain("Approval history:" if history else "Approval history: none")
    for approval in history:
        _plain(f"  {short_digest(approval.digest)}  {approval.state}  approved {consent.utc_time(approval.approved_at)}, "
               f"{approval.matched_count} matched")
    # No hint where approval would be refused (§5.4): rejected, unavailable, findings or unreadable inputs.
    if state not in ("approved", "rejected", "unavailable") and not snap.findings and not snap.errors:
        command = f"devgraph config scripts approve {visible(record.repo_id)} {name}"
        _plain("To approve it, " + _ASK_THE_USER.format(command=command) + ".")


def _approval_refusal(snap: Any) -> Optional[str]:
    """Why `snap` cannot be approved (printing its findings or unreadable inputs first), or None."""
    from devgraph.sandbox.reader import InputError

    if isinstance(snap, InputError):
        state = "rejected" if snap.code == "static_reject" else "unavailable"
        return f"this provider is {state}: {visible(snap.reason or snap.code)}"
    if snap.findings:
        _plain(f"Static scan findings for {snap.name}:")
        for finding in snap.findings:
            _plain(f"  line {finding.line}: {finding.rule}: {visible(finding.message)}")
        return (f"{snap.name} cannot be approved while the static scan reports findings; "
                "change the script, then approve it again")
    if snap.errors:
        for error in snap.errors:
            _plain(f"  unreadable input: {visible(error.reason or error.code)}")
        return f"{snap.name} cannot be approved while some of its inputs cannot be read"
    return None


def _approve_one(record: Any, canon: str, snap: Any, sha256: Optional[str], keep_previous: bool) -> None:
    from devgraph.sandbox.digest import short_digest
    from devgraph.sandbox.trust import TrustStore, TrustStoreError

    repo_id = visible(record.repo_id)
    refusal = _approval_refusal(snap)
    if refusal is not None:
        raise _scripts_fail(f"{refusal}. Nothing was approved.")
    name = snap.name
    store_path, _ = _sandbox_files()
    store = TrustStore.open_read(store_path)
    history = []
    if store is not None:
        with store:
            history = store.approvals(record.repo_id, canon, name)
    if sha256 is not None:
        if not consent.digest_matches(sha256, snap.digest):
            raise _scripts_fail(
                f"--sha256 does not match the current digest of {name}. Nothing was approved. --sha256 is for CI "
                "with a digest kept in a protected secret. To approve, ask the user to run "
                f"`devgraph config scripts approve {repo_id} {name}` in a terminal, where they review it first.")
    else:
        for line in consent.review_lines(record.repo_id, canon, snap, history[-1] if history else None,
                                         width=console.width):
            _plain(line)
        typed = click.prompt(f"Type the provider name ({name}) to approve it", default="", show_default=False)
        if typed.strip() != name:
            console.print("Not approved.")
            raise typer.Exit(code=1)
    try:
        with TrustStore.open_write(store_path) as writer:
            writer.approve(record.repo_id, canon, name, snap.digest, declaration_json=snap.declaration_json,
                           script_text=snap.script_text, matched_count=snap.matched_count,
                           keep_previous=keep_previous)
            enabled = writer.scripts_enabled(record.repo_id, canon)
    except TrustStoreError as exc:
        raise _scripts_fail(f"{visible(str(exc))}; nothing was approved")
    _plain(f"Approved {name} for {repo_id} (digest {short_digest(snap.digest)}).", "[green][OK][/green] ")
    if not enabled:
        _plain(f"  Scripts are off for {repo_id}; enable them with `devgraph config scripts enable {repo_id}`.")
    _plain("  This version does not run scripts yet.")


@scripts_app.command("approve")
def scripts_approve(
    repo_id: str = typer.Argument(..., help="Registered repo id."),
    name: Optional[str] = typer.Argument(None, help="Custom provider (default: each one awaiting approval, in turn)."),
    sha256: Optional[str] = typer.Option(
        None, "--sha256", help="For CI: approve without asking, only if this equals the provider's current digest."
    ),
    keep_previous: bool = typer.Option(
        False, "--keep-previous", help="Keep earlier approved digests active (up to five) instead of retiring them."
    ),
) -> None:
    """Approve a provider's script and declaration after reviewing them at a terminal: type its name to confirm."""
    from devgraph.sandbox.reader import InputError
    from devgraph.sandbox.snapshot import provider_snapshot, repo_snapshots

    _require_scripts_platform("approving a script")
    if sha256 is not None and name is None:
        raise _scripts_fail("--sha256 approves one provider: name it")
    if sha256 is None:
        try:
            consent.require_tty()
        except consent.ConsentError:
            command = f"devgraph config scripts approve {visible(repo_id)}" + (f" {visible(name)}" if name else "")
            raise _scripts_fail("no terminal to confirm on: approving a script needs the user at a terminal; "
                                + _ASK_THE_USER.format(command=command))
    record, canon, real = _scripts_repo(repo_id)
    git = git_binary()
    if name is not None:
        try:
            snaps: list[Any] = [provider_snapshot(real, name, git=git)]
        except InputError as exc:
            snaps = [exc]
    else:
        try:
            found = repo_snapshots(real, git=git)
        except InputError as exc:
            raise _scripts_fail(f"{visible(exc.reason or exc.code)}; nothing was approved")
        store = _open_store_read()
        try:
            active = {n: {a.digest for a in store.active_digests(record.repo_id, canon, n)} if store else set()
                      for n in found}
        finally:
            if store is not None:
                store.close()
        snaps, skipped = [], 0
        for found_name, snap in found.items():
            if not isinstance(snap, InputError) and snap.digest in active[found_name]:
                continue
            refusal = _approval_refusal(snap)
            if refusal is not None:
                _plain(f"Skipping {visible(found_name)}: {refusal}.")
                skipped += 1
            else:
                snaps.append(snap)
        if not snaps:
            _plain(f"Nothing awaiting approval for {visible(record.repo_id)}.")
        for snap in snaps:
            _approve_one(record, canon, snap, sha256, keep_previous)
        if skipped:
            raise _scripts_fail(f"{skipped} provider(s) could not be approved; see above")
        return
    for snap in snaps:
        _approve_one(record, canon, snap, sha256, keep_previous)


@scripts_app.command("revoke")
def scripts_revoke(
    repo_id: str = typer.Argument(..., help="Registered repo id."),
    name: str = typer.Argument(..., help="Custom provider name."),
    digest: Optional[str] = typer.Option(None, "--digest", help="Revoke only this approved digest (or a unique prefix of 12+ hex characters)."),
) -> None:
    """Remove a provider's approvals (all, or one digest). Stops future runs; keeps graph data."""
    from devgraph.sandbox.digest import short_digest
    from devgraph.sandbox.reader import InputError
    from devgraph.sandbox.snapshot import provider_snapshot
    from devgraph.sandbox.trust import TrustStore, TrustStoreError

    record, canon, real = _scripts_repo(repo_id)
    store_path, _ = _sandbox_files()
    if not os.path.lexists(store_path):
        _plain(f"{name} has no approvals for {visible(record.repo_id)}; nothing to revoke.")
        return
    try:
        with TrustStore.open_write(store_path) as store:
            chosen = None
            if digest is not None:
                wanted = digest.lower()
                matches = [a.digest for a in store.approvals(record.repo_id, canon, name) if a.digest.startswith(wanted)]
                if not re.fullmatch(r"[0-9a-f]{12,64}", wanted) or len(matches) != 1:
                    raise _scripts_fail(f"--digest names no single approval of {name}; "
                                        f"see `devgraph config scripts show {visible(record.repo_id)} {name}`")
                chosen = matches[0]
            removed = store.revoke(record.repo_id, canon, name, chosen)
            remaining = {a.digest for a in store.active_digests(record.repo_id, canon, name)}
    except TrustStoreError as exc:
        raise _scripts_fail(f"{visible(str(exc))}; nothing was revoked")
    if not removed:
        _plain(f"{name} has no approvals for {visible(record.repo_id)}; nothing to revoke.")
        return
    what = f"digest {short_digest(chosen)}" if chosen else f"{removed} approval(s)"
    if not remaining:
        _plain(f"Revoked {what} of {name} for {visible(record.repo_id)}; no approved digest remains, so it no "
               "longer runs. Graph data is kept.", "[green][OK][/green] ")
        return
    try:
        current = provider_snapshot(real, name, git=git_binary()).digest
    except InputError:
        current = None
    if current in remaining:
        rest = f"the current digest {short_digest(current)} is still approved, so it still runs"
    else:
        rest = (f"{len(remaining)} other approved digest(s) remain active, but the current script matches none, "
                "so it does not run as it stands")
    _plain(f"Revoked {what} of {name} for {visible(record.repo_id)}; {rest}.", "[green][OK][/green] ")


@scripts_app.command("enable")
def scripts_enable(repo_id: str = typer.Argument(..., help="Registered repo id.")) -> None:
    """Allow a repository's approved custom provider scripts to run: type the repo id to confirm."""
    from devgraph.sandbox.trust import TrustStore, TrustStoreError

    _require_scripts_platform("enabling scripts")
    try:
        consent.require_tty()
    except consent.ConsentError:
        raise _scripts_fail("no terminal to confirm on: enabling scripts needs the user at a terminal; "
                            + _ASK_THE_USER.format(command=f"devgraph config scripts enable {visible(repo_id)}"))
    record, canon, real = _scripts_repo(repo_id)
    shown = visible(record.repo_id)
    store = _open_store_read()
    if store is not None:
        with store:
            if store.scripts_enabled(record.repo_id, canon):
                _plain(f"Scripts are already enabled for {shown}.")
                return
    names = _declared_providers(real)
    _plain(f"Repository: {shown}")
    _plain(f"Canonical path: {visible(canon)}")
    _plain("Declared custom providers: " + (", ".join(visible(n) for n in names) or "none"))
    _plain("Enabling scripts lets this repository's custom provider scripts run in the sandbox once each "
           "provider is approved. Each provider still needs its own approval.")
    typed = click.prompt(f"Type the repository id ({shown}) to enable scripts", default="", show_default=False)
    if typed.strip() != record.repo_id:
        console.print("Not enabled.")
        raise typer.Exit(code=1)
    try:
        with TrustStore.open_write(_sandbox_files()[0]) as writer:
            writer.set_scripts_enabled(record.repo_id, canon, True)
    except TrustStoreError as exc:
        raise _scripts_fail(f"{visible(str(exc))}; scripts were not enabled")
    _plain(f"Scripts enabled for {shown}.", "[green][OK][/green] ")
    if names:
        _plain(f"  Approve providers with `devgraph config scripts approve {shown}`.")
    if not record.project_config_enabled:
        _plain(f"  Project config is off for {shown}, so nothing runs until `devgraph config enable {shown}`.")


@scripts_app.command("disable")
def scripts_disable(repo_id: str = typer.Argument(..., help="Registered repo id.")) -> None:
    """Stop a repository's scripts. Approvals and graph data are kept."""
    from devgraph.sandbox.trust import TrustStore, TrustStoreError

    record, canon, _ = _scripts_repo(repo_id)
    shown = visible(record.repo_id)
    store = _open_store_read()
    enabled = False
    if store is not None:
        with store:
            enabled = store.scripts_enabled(record.repo_id, canon)
    if not enabled and not os.path.lexists(_sandbox_files()[0]):
        _plain(f"Scripts are already disabled for {shown}.")
        return
    try:
        with TrustStore.open_write(_sandbox_files()[0]) as writer:
            writer.set_scripts_enabled(record.repo_id, canon, False)
    except TrustStoreError as exc:
        raise _scripts_fail(f"{visible(str(exc))}; scripts were not disabled")
    _plain(f"Scripts disabled for {shown}; approvals and graph data are kept.", "[green][OK][/green] ")


def _scripts_notice(repo_id: str, root: Path) -> None:
    """After `add`: a schema's custom providers do not run until enabled and approved (§5.7)."""
    names = _declared_providers(root)
    if not names:
        return
    shown = visible(repo_id)
    _plain(f"This repository declares custom providers ({', '.join(visible(n) for n in names)}); they will not run "
           "until scripts are enabled and each provider is approved. To use them, ask the user to review and run "
           "these in a terminal:")
    _plain(f"  devgraph config scripts enable {shown}")
    _plain(f"  devgraph config scripts approve {shown}")


def _forget_script_trust(repo_id: str, *, registering: bool = False) -> None:
    """Delete every trust row for `repo_id`, under any path (NFC keys can collapse): on `remove`,
    and on `add`, so a re-registered id never inherits approvals a failed cleanup left behind."""
    from devgraph.sandbox.trust import TrustStore

    store_path, _ = _sandbox_files()
    if not os.path.lexists(store_path):
        return
    try:
        with TrustStore.open_write(store_path) as store:
            store.forget_repo(repo_id)
    except Exception as exc:
        if registering:
            consequence = (f"old approvals for {visible(repo_id)} may still apply; run "
                           f"`devgraph config scripts disable {visible(repo_id)}` and revoke them once the trust "
                           "store can be written")
        else:
            consequence = ("while no repository is registered under this id they match nothing; they are cleared "
                           "when the id is registered again")
        _plain(f"could not delete {visible(repo_id)}'s script trust rows: {visible(str(exc))}. {consequence}.",
               "[yellow]Warning:[/yellow] ")


def _resuming_providers(record: Any) -> list[str]:
    """After project config is switched on: the approved providers that resume without re-approval (§5.7)."""
    from devgraph.sandbox.reader import InputError
    from devgraph.sandbox.snapshot import repo_snapshots

    if not sandbox_paths.platform_supported(consent.current_platform()) or not _declared_providers(Path(record.path)):
        return []
    try:
        canon = sandbox_paths.canonical_repo_path(record.path)
        snaps = repo_snapshots(Path(os.path.realpath(record.path)), git=git_binary())
    except Exception:
        return []
    store = _open_store_read()
    if store is None:
        return []
    with store:
        if not store.scripts_enabled(record.repo_id, canon):
            return []
        lines = []
        for name, snap in snaps.items():
            if isinstance(snap, InputError):
                continue
            current = next((a for a in store.active_digests(record.repo_id, canon, name) if a.digest == snap.digest), None)
            if current is not None:
                lines.append(f"custom provider {visible(name)}: approved {consent.utc_time(current.approved_at)}; "
                             "resumes without re-approval")
    return lines


def _script_provider_findings(repos: list[Any]) -> list[tuple[str, str, str]]:
    """doctor's "Script providers" section: (level, subject, detail), level ok/warning/failed.
    Hints are interactive commands only, never one carrying a digest."""
    from devgraph.sandbox import gates
    from devgraph.sandbox.reader import InputError
    from devgraph.sandbox.trust import TrustStore

    findings: list[tuple[str, str, str]] = []
    platform = consent.current_platform()
    if sandbox_paths.platform_supported(platform):
        findings.append(("ok", "platform", f"{visible(platform)}: custom providers are supported"))
    else:
        findings.append(("warning", "platform", f"{visible(platform)}: custom providers are unavailable (platform); "
                                                "this version supports them on Linux only"))
    try:
        selection.check_git(git_binary())
        findings.append(("ok", "git", "new enough for input selection"))
    except InputError as exc:
        findings.append(("warning", "git", f"{visible(exc.reason)}; custom provider inputs cannot be selected, "
                                           "so every provider is unavailable"))

    home = sandbox_paths.sandbox_home()
    store_path, registry_path = _sandbox_files()
    # The directory is followed, as the gates follow it; the files are not (a symlinked one is refused).
    for path, look in ((home / ".devgraph", os.stat), (registry_path, os.lstat), (store_path, os.lstat)):
        try:
            st = look(path)
        except OSError:
            continue
        if st.st_uid != os.getuid():
            findings.append(("warning", visible(str(path)), "is not owned by you, so every script gate reads off; "
                                                          "fix its ownership"))
        elif st.st_mode & 0o022:
            findings.append(("warning", visible(str(path)),
                             "is writable by group or others, so every script gate reads off (scripts never run); "
                             f"a umask such as 002 creates it so. Fix: chmod go-w {visible(str(path))}"))
    if not os.path.lexists(store_path):
        findings.append(("ok", "trust store", "none yet (no repository has scripts enabled)"))
    else:
        store = TrustStore.open_read(store_path)
        if store is None:
            problem = TrustStore.read_problem(store_path) or f"{store_path} could not be opened (it may be locked)"
            fix = f" Fix: chmod 600 {visible(str(store_path))}" if "writable by group or others" in problem else ""
            findings.append(("failed", "trust store",
                             f"{visible(problem)}; every script reads as off and unapproved.{fix}"))
        else:
            store.close()
            findings.append(("ok", "trust store", visible(str(store_path))))

    declaring = [r for r in repos if _declared_providers(Path(r.path))]
    configured = Path(get_settings().registry_db_path).expanduser()
    if declaring and configured.resolve() != registry_path.resolve():
        findings.append(("warning", "registry",
                         f"DevGraph uses the registry at {visible(str(configured))}, but custom providers read "
                         f"project config only from the default registry location {visible(str(registry_path))}, "
                         f"so scripts stay off for {', '.join(visible(r.repo_id) for r in declaring)}. "
                         "Move the registry there (unset DEVGRAPH_REGISTRY_DB_PATH) to use them."))
    for record in declaring:
        repo_id = visible(record.repo_id)
        for name, snap, state, would_be in _provider_states(record):
            subject = f"{repo_id} {visible(name)}"
            if state == "approved":
                findings.append(("ok", subject, "approved"))
            elif state == "awaiting_approval":
                hint = _ASK_THE_USER.format(command=f"devgraph config scripts approve {repo_id} {visible(name)}")
                findings.append(("warning", subject, f"awaiting_approval: {hint}"))
            elif state == "disabled":
                canon = sandbox_paths.canonical_repo_path(record.path)
                if not record.project_config_enabled:
                    hint = f"project config is off: `devgraph config enable {repo_id}`"
                elif not gates.gate1_project_config(record.repo_id, canon, registry_path=registry_path):
                    hint = ("project config is on, but the script gate reads it as off: the registry at the default "
                            "location is missing this repository, is not private to you, or DevGraph uses another "
                            "registry (see the registry and permission lines above)")
                else:
                    hint = "scripts are off: " + _ASK_THE_USER.format(command=f"devgraph config scripts enable {repo_id}")
                findings.append(("warning", subject, f"disabled: {hint}"))
            elif state == "pending" and would_be is not None:
                findings.append(("warning", subject, f"pending: {_GRAPH_UNREACHABLE}, so the applied schema cannot "
                                                     f"be checked; otherwise {would_be}"))
            elif state == "pending":
                findings.append(("warning", subject, "pending: the schema is not applied yet; "
                                                     f"`devgraph rescan {repo_id} --now` applies it"))
            else:
                reason = (snap.reason or snap.code) if isinstance(snap, InputError) else ""
                detail = f"{state}: {visible(reason)}" if reason else state
                findings.append(("warning", subject, detail))
    return findings


schema_app = typer.Typer(
    help="List, add, edit, delete or reset node types and relationships in a repository's devgraph.schema.yaml.",
    no_args_is_help=True,
)
config_app.add_typer(schema_app, name="schema")

def _schema_scope(ctx: typer.Context, repo: Optional[Path]) -> Path:
    return _tools_scope(ctx, repo, False, "DevGraph does not index it")


def _schema_record(root: Path):
    """The registered, active repository record for `root`, else None."""
    record = _scope_record(root)
    return record if record is not None and record.active else None


def _schema_effect_note(root: Path) -> str:
    """When a schema change takes effect for this repository."""
    from devgraph.config.edits import schema_effect_note

    return schema_effect_note(root, _schema_record(root))


def _schema_text(path: Path) -> str:
    from devgraph.config.edits import read_text

    with _edit_errors():
        return read_text(path)


def _read_schema_entry(source: str) -> dict:
    if source == "-":
        return _parse_tool_text(sys.stdin.read(), "stdin", "one schema entry")
    try:
        text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _tools_fail(f"cannot read {source}: {exc}")
    return _parse_tool_text(text, source, "one schema entry")


def _schema_done(verb: str, section: str, name: str, path: Path, root: Path) -> None:
    noun = _SCHEMA_SECTIONS[section][1]
    console.print(f"[green]{verb}[/green] {noun} {escape(repr(name))}: {escape(str(path))}", soft_wrap=True)
    console.print(escape(_schema_effect_note(root)), soft_wrap=True)


def _schema_follow_up(root: Path, result) -> None:
    """Warnings and notes after a successful write: lost nodes, unpopulated types, key changes, conflicts."""
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(warning)}", soft_wrap=True)
    for note in result.notes:
        console.print(escape(note), soft_wrap=True)
    record = _schema_record(root)
    if record is None or result.after is None:
        return
    for finding in _project_schema_findings(_registered_repos()):
        if finding["status"] == "conflict" and record.repo_id in finding.get("repo_ids", ()):
            console.print(f"[yellow]Warning:[/yellow] {escape(finding['detail'])}", soft_wrap=True)


@schema_app.command("prune-constraints")
def config_schema_prune_constraints(
    labels: Optional[list[str]] = typer.Option(None, "--label", help="Only this label (repeatable)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be dropped without dropping it."),
) -> None:
    """Drop DevGraph-generated constraints/indexes no repository declares any more.

    Database-wide. Stale means: no repository's applied schema records the
    label, no registered repository's schema file declares it, and no node
    carries it. Built-in constraints are never touched.
    """
    from devgraph.indexer.schema_constraints import release_labels

    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
        stale = _stale_schema_objects(engine, _registered_repos())
        if labels:
            wanted = {label.casefold() for label in labels}
            stale = [obj for obj in stale if obj.label.casefold() in wanted]
        if not stale:
            console.print("[green]No stale generated constraints or indexes.[/green]")
            return
        for obj in stale:
            console.print(f"  {obj.kind} {escape(obj.name)} on {escape(obj.label)}")
        if dry_run:
            console.print("[yellow]Dry run — nothing dropped.[/yellow]")
            return
        dropped = release_labels(engine, [obj.label for obj in stale])
        for name in dropped:
            console.print(f"[green][OK][/green] Dropped {escape(name)}")
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {escape(str(e))}")
        raise typer.Exit(code=1)
    finally:
        engine.close()


@schema_app.command("list")
def config_schema_list(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List the effective node types and relationships, marked built-in or project."""
    from devgraph.config.project_schema import ProjectSchemaError, load_project_schema, resolve_declaration
    from devgraph.config.project_switch import project_config_enabled
    from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES

    root = _schema_scope(ctx, repo)
    try:
        declaration = load_project_schema(root)
        effective = resolve_declaration(declaration)
    except ProjectSchemaError as exc:
        raise _tools_fail(str(exc))

    node_types: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    if declaration is None or declaration.extends == "default":
        node_types += [{"label": n, "origin": "built-in", "key": None, "source": None, "color": None} for n in NODE_LABELS]
        relationships += [
            {"type": t, "from": None, "to": None, "provider": "builtin", "origin": "built-in", "color": None}
            for t in RELATIONSHIP_TYPES
        ]
    node_types += [
        {
            "label": n.label,
            "origin": "project",
            "key": list(n.key),
            # kind for a filesystem source, name for a custom one
            "source": n.source.model_dump(exclude_none=True) if n.source else None,
            "color": n.color,
        }
        for n in effective.node_types
    ]
    relationships += [
        {"type": r.type, "from": list(r.from_labels), "to": r.to, "provider": r.provider, "origin": "project", "color": r.color}
        for r in effective.relationships
    ]

    if as_json:
        typer.echo(json.dumps({"node_types": node_types, "relationships": relationships}, indent=2))
        return
    nodes = Table(title=escape(f"Node types for {root}"))
    nodes.add_column("Label", style="cyan")
    nodes.add_column("Origin")
    nodes.add_column("Key")
    nodes.add_column("Source")
    nodes.add_column("Colour")
    for row in node_types:
        source = row["source"]
        nodes.add_row(
            escape(row["label"]), row["origin"], escape(", ".join(row["key"] or ())),
            escape(f"{source['provider']} ({source.get('kind') or source.get('name')})") if source else "\u2014",
            row["color"] or "\u2014",
        )
    console.print(nodes)
    rels = Table(title="Relationships")
    for column in ("Type", "Origin", "From", "To", "Provider", "Colour"):
        rels.add_column(column, style="cyan" if column == "Type" else None)
    for row in relationships:
        rels.add_row(
            escape(row["type"]), row["origin"], escape(", ".join(row["from"] or ())), escape(row["to"] or ""), row["provider"], row["color"] or "\u2014"
        )
    console.print(rels)
    if not project_config_enabled(root):
        console.print("Project config is disabled: only the built-in schema is in effect.")


@schema_app.command("add")
def config_schema_add(
    ctx: typer.Context,
    source: str = typer.Option(..., "--from", help="YAML or JSON file holding one node type (`label`) or relationship (`type`), or - for stdin."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
) -> None:
    """Add one node type or relationship. Fails if the label exists (use `edit`)."""
    from devgraph.config.edits import add_schema_entry, entry_section

    root = _schema_scope(ctx, repo)
    entry = _read_schema_entry(source)
    with _edit_errors(f"devgraph config schema edit {entry.get('label')}"):
        section = entry_section(entry)
        result = add_schema_entry(root, entry, record=_schema_record(root))
    _schema_done("Added", section, str(entry[_SCHEMA_SECTIONS[section][0]]), result.path, root)
    _schema_follow_up(root, result)


@schema_app.command("edit")
def config_schema_edit(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node type label or relationship type to replace."),
    source: Optional[str] = typer.Option(None, "--from", help="YAML or JSON file holding the new entry, or - for stdin. Default: open $EDITOR."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    node_type: bool = typer.Option(False, "--node-type", help="NAME is a node type label."),
    relationship: bool = typer.Option(False, "--relationship", help="NAME is a relationship type."),
) -> None:
    """Replace one entry, from a file or in $EDITOR. Nothing is written if the result is unchanged or invalid."""
    from devgraph.config.edits import find_schema_entry, locate_entry, replace_schema_entry
    from devgraph.config.list_edit import dump_entry
    from devgraph.config.project_schema import project_schema_path

    root = _schema_scope(ctx, repo)
    path = project_schema_path(root)
    text = _schema_text(path)
    with _edit_errors():
        section = locate_entry(text, name, node_type, relationship)
        current = find_schema_entry(text, name, section)
    if source is not None:
        entry = _read_schema_entry(source)
    else:
        original = dump_entry(current)
        edited = click.edit(original, extension=".yaml")
        if edited is None or edited == original:
            console.print("No changes.")
            return
        entry = _parse_tool_text(edited, "edited entry", "one schema entry")
        if entry == current:
            console.print("No changes.")
            return
    with _edit_errors():
        result = replace_schema_entry(
            root, name, entry, node_type=node_type, relationship=relationship, record=_schema_record(root)
        )
    _schema_done("Updated", section, name, result.path, root)
    _schema_follow_up(root, result)


@schema_app.command("delete")
def config_schema_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Node type label or relationship type to remove."),
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    node_type: bool = typer.Option(False, "--node-type", help="NAME is a node type label."),
    relationship: bool = typer.Option(False, "--relationship", help="NAME is a relationship type."),
) -> None:
    """Remove one node type or relationship. Unknown names exit 1."""
    from devgraph.config.edits import delete_schema_entry, locate_entry
    from devgraph.config.project_schema import project_schema_path

    root = _schema_scope(ctx, repo)
    with _edit_errors():
        section = locate_entry(_schema_text(project_schema_path(root)), name, node_type, relationship)
        result = delete_schema_entry(
            root, name, node_type=node_type, relationship=relationship, record=_schema_record(root)
        )
    _schema_done("Deleted", section, name, result.path, root)
    _schema_follow_up(root, result)


@schema_app.command("reset")
def config_schema_reset(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Delete devgraph.schema.yaml, returning the repository to the built-in schema."""
    from devgraph.config.edits import reset_schema
    from devgraph.config.project_schema import project_schema_path

    root = _schema_scope(ctx, repo)
    path = project_schema_path(root)
    if not os.path.lexists(path):
        console.print(f"Nothing to reset: {escape(str(path))} does not exist.", soft_wrap=True)
        return
    if not yes:
        typer.confirm(f"Delete {path} and return to the built-in schema?", abort=True)
    with _edit_errors():
        result = reset_schema(root, record=_schema_record(root))
    console.print(f"[green]Reset[/green] {escape(str(path))}", soft_wrap=True)
    console.print(escape(_schema_effect_note(root)), soft_wrap=True)
    _schema_follow_up(root, result)


@app.command()
def logs(
    lines: int = typer.Option(
        50, "--lines", "-n", help="Number of recent lines to show."
    ),
    level: str = typer.Option(
        "INFO", "--level", "-l", help="Minimum log level filter (DEBUG, INFO, WARNING, ERROR)."
    ),
) -> None:
    """Show recent log output from the DevGraph tray/dashboard process."""
    settings = get_settings()
    log_path = settings.log_file
    if not log_path or not log_path.exists():
        console.print("[yellow]No log file found.[/yellow]")
        console.print(f"  Expected at: {log_path}")
        console.print("  The tray app must be started with file logging enabled.")
        return

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    min_level = level_map.get(level.upper(), logging.INFO)

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError as e:
        console.print(f"[red][X] Error reading log file:[/red] {e}")
        raise typer.Exit(code=1)

    # Parse log lines, filter by level, take last N
    filtered: list[str] = []
    for line in text.splitlines():
        # Simple level detection from common log formats
        line_upper = line.upper()
        line_level = logging.INFO
        if "ERROR" in line_upper:
            line_level = logging.ERROR
        elif "WARNING" in line_upper or "WARN" in line_upper:
            line_level = logging.WARNING
        elif "DEBUG" in line_upper:
            line_level = logging.DEBUG
        if line_level >= min_level:
            filtered.append(line)

    tail = filtered[-lines:] if lines > 0 else filtered
    for line in tail:
        console.print(line)


@app.command()
def prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without actually deleting."
    ),
) -> None:
    """Remove orphaned graph data for repos no longer in the registry."""
    settings = get_settings()
    registry = _get_registry()
    try:
        registered_ids = {r.repo_id for r in registry.list_repos()}
    finally:
        registry.close()

    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        # Find all Repository nodes in Neo4j
        repo_rows = engine.run_cypher(
            "MATCH (n:Repository) RETURN n.repo_id AS repo_id"
        )
        neo4j_ids = {row["repo_id"] for row in repo_rows if row.get("repo_id")}
        orphaned = neo4j_ids - registered_ids

        if not orphaned:
            console.print("[green]No orphaned repos found.[/green]")
            return

        console.print(f"Found {len(orphaned)} orphaned repo(s):")
        for rid in sorted(orphaned):
            console.print(f"  {rid}")

        if dry_run:
            console.print("[yellow]Dry run — no data deleted.[/yellow]")
            return

        for rid in sorted(orphaned):
            recorded = engine.read_applied_schema(rid) or {}
            engine.delete_repository(rid)
            console.print(f"[green][OK][/green] Deleted: {rid}")
            _release_labels(engine, recorded.get("labels") or [])
    finally:
        engine.close()


@app.command(name="self-test")
def self_test(
    repo_id: Optional[str] = typer.Argument(
        None, help="Repository ID to test. Omit to test all registered repos."
    ),
) -> None:
    """Run internal consistency checks against the graph data."""
    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    registry = _get_registry()
    try:
        if repo_id:
            if registry.get(repo_id) is None:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)
            repo_ids = [repo_id]
        else:
            repo_ids = [r.repo_id for r in registry.list_repos()]
    finally:
        registry.close()

    all_passed = True

    for rid in repo_ids:
        console.print(f"\n[bold]Checking: {rid}[/bold]")

        # 1. Every Module node has a file property
        try:
            bad = engine.run_cypher(
                "MATCH (n:Module {repo_id: $rid}) WHERE n.file IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} Module(s) missing 'file' property[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All Module nodes have 'file'")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 2. No dangling CONTAINS edges
        try:
            bad = engine.run_cypher(
                "MATCH (a {repo_id: $rid})-[r:CONTAINS]->(b) "
                "WHERE b.repo_id <> $rid OR b IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} dangling CONTAINS edge(s)[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All CONTAINS edges valid")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 3. No dangling CALLS edges
        try:
            bad = engine.run_cypher(
                "MATCH (a {repo_id: $rid})-[r:CALLS]->(b) "
                "WHERE b.repo_id <> $rid OR b IS NULL "
                "RETURN count(*) AS c", {"rid": rid}
            )
            count = bad[0]["c"] if bad else 0
            if count:
                console.print(f"  [red][X] {count} dangling CALLS edge(s)[/red]")
                all_passed = False
            else:
                console.print(f"  [green][OK][/green] All CALLS edges valid")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

        # 4. Registry ↔ Neo4j consistency
        try:
            repo_nodes = engine.run_cypher(
                "MATCH (n:Repository) RETURN n.repo_id AS rid"
            )
            neo4j_ids = {r["rid"] for r in repo_nodes if r.get("rid")}
            registry = _get_registry()
            try:
                reg_ids = {r.repo_id for r in registry.list_repos()}
            finally:
                registry.close()
            orphaned = neo4j_ids - reg_ids
            missing = reg_ids - neo4j_ids
            if orphaned:
                console.print(f"  [red][X] {len(orphaned)} orphaned repo(s) in Neo4j: {', '.join(sorted(orphaned))}[/red]")
                all_passed = False
            if missing:
                console.print(f"  [yellow]{len(missing)} repo(s) in registry but not in Neo4j: {', '.join(sorted(missing))}[/yellow]")
            if not orphaned and not missing:
                console.print(f"  [green][OK][/green] Registry ↔ Neo4j consistent")
        except Exception as e:
            console.print(f"  [red][X] Check failed:[/red] {e}")
            all_passed = False

    console.print()
    if all_passed:
        console.print("[green]All checks passed.[/green]")
    else:
        console.print("[red]One or more checks failed.[/red]")
        raise typer.Exit(code=1)


@app.command()
def export(
    repo_id: str,
    fmt: str = typer.Option(
        "json", "--format", "-f", help="Output format: json, cypher, or dot."
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="File path to write to (default: stdout)."
    ),
    limit: int = typer.Option(
        500, "--limit", "-n", help="Maximum number of nodes to export."
    ),
) -> None:
    """Export graph data for a repository in a portable format."""
    if fmt not in ("json", "cypher", "dot"):
        console.print("[red][X] Error:[/red] --format must be 'json', 'cypher', or 'dot'")
        raise typer.Exit(code=1)

    settings = get_settings()
    registry = _get_registry()
    try:
        if registry.get(repo_id) is None:
            console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
            raise typer.Exit(code=1)
    finally:
        registry.close()

    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        engine.verify_connectivity()
    except Exception as e:
        console.print(f"[red][X] Neo4j not reachable:[/red] {e}")
        raise typer.Exit(code=1)

    try:
        nodes, edges = dashboard_queries.graph_slice(engine, repo_id, None, limit)

        if fmt == "json":
            result = export_json(nodes, edges)
        elif fmt == "cypher":
            result = export_cypher(nodes, edges)
        else:
            result = export_dot(nodes, edges)

        if output:
            Path(output).write_text(result, encoding="utf-8")
            console.print(f"[green][OK][/green] Exported {len(nodes)} nodes, {len(edges)} edges to {output}")
        else:
            console.print(result)
    finally:
        engine.close()


if __name__ == "__main__":
    app()
