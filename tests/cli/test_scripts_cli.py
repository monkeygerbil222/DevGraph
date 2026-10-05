"""`devgraph config scripts`, consent, the `add` notice, `remove`, the `config enable`
resume list and doctor's "Script providers" section (spec §5.4-§5.7).

Nothing here runs a script: E1 only reads, scans and records approvals.
`CliRunner` is never a TTY, so interactive cases patch `consent.require_tty`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config.settings import Settings
from devgraph.registry.store import RepoRegistry
from devgraph.sandbox import consent, paths, selection
from devgraph.sandbox.selection import git_binary
from devgraph.sandbox.snapshot import provider_snapshot
from devgraph.sandbox.trust import TrustStore

GIT = git_binary()
REAL_SCHEMA_STATUS = getattr(cli_main, "_applied_schema_status", None)  # bound before the autouse patch
pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")

_SETUP_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

PROVIDER = "runbook_links"
SCHEMA = """\
version: 1
custom_providers:
  - name: runbook_links
    inputs: ["docs/**/*.md"]
    params: {note: "plain"}
node_types:
  - label: Runbook
    key: [slug]
    metadata: [{name: slug, required: true}]
    source: {provider: custom, name: runbook_links}
"""
SCRIPT = "import re\n\n\ndef derive(ctx):\n    return []\n"
DOCS = {f"docs/r{i:02d}.md": "# runbook\n" for i in range(3)}
APPROVE_DIGEST = re.compile(r"approve.*\b[0-9a-fA-F]{64}\b", re.IGNORECASE)
SHA256_DIGEST = re.compile(r"--sha256[\s=]+[0-9a-fA-F]{64}")
BARE_DIGEST = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _git(repo: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(repo), *args], env=_SETUP_ENV, check=True, capture_output=True)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", "-f", "--", *files)
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def files(schema: str = SCHEMA, script: str = SCRIPT, **extra: str) -> dict[str, str]:
    return {"devgraph.schema.yaml": schema, f".devgraph/providers/{PROVIDER}.py": script, **DOCS, **extra}


@pytest.fixture
def runner():
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def home():
    home = paths.sandbox_home()
    (home / ".devgraph").mkdir(parents=True, mode=0o700)
    return home


@pytest.fixture(autouse=True)
def settings(monkeypatch, home):
    """The CLI's registry is the fixed-path registry gate 1 reads, inside the tmp sandbox home."""
    fake = Settings(_env_file=None, neo4j_password="x", registry_db_path=paths.fixed_registry_path(home))
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def not_pending(monkeypatch):
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "applied")


@pytest.fixture(autouse=True)
def linux(monkeypatch):
    monkeypatch.setattr(consent, "current_platform", lambda: "linux")


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(consent, "require_tty", lambda *a, **k: None)


def register(settings, root: Path, repo_id: str = "demo") -> None:
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(root, repo_id=repo_id)
    finally:
        registry.close()
    os.chmod(settings.registry_db_path, 0o600)


@pytest.fixture
def repo(tmp_path, settings):
    root = make_repo(tmp_path / "work" / "acme", files())
    register(settings, root)
    return root


def digest(root: Path) -> str:
    return provider_snapshot(root, PROVIDER, git=GIT).digest


def store_path(home: Path) -> Path:
    return paths.trust_store_path(home)


def approvals(home: Path, root: Path):
    store = TrustStore.open_read(store_path(home))
    if store is None:
        return []
    with store:
        return store.approvals("demo", paths.canonical_repo_path(root), PROVIDER)


def write_script(root: Path, text: str) -> None:
    (root / ".devgraph" / "providers" / f"{PROVIDER}.py").write_text(text)


def approve(runner, *args: str, typed: str = PROVIDER):
    return runner.invoke(app, ["config", "scripts", "approve", "demo", *args], input=f"{typed}\n")


def enable(runner, typed: str = "demo"):
    return runner.invoke(app, ["config", "scripts", "enable", "demo"], input=f"{typed}\n")


def state(runner) -> str:
    result = runner.invoke(app, ["config", "scripts", "list"])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if PROVIDER in line)
    return row.split("│")[3].strip()  # the State cell


def no_approve_digest(output: str) -> None:
    """No approve command carrying a digest: not on one line, not as `--sha256 <hex>` even
    across a line break, and no bare digest on a line next to one that says approve."""
    lines = output.splitlines()
    assert not SHA256_DIGEST.search(output), output
    for index, line in enumerate(lines):
        assert not APPROVE_DIGEST.search(line), line
        if BARE_DIGEST.search(line):
            for near in lines[max(index - 1, 0):index + 2]:
                assert "approve" not in near.lower(), (line, near)


# --- consent helpers --------------------------------------------------------------


class _Stream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_require_tty_needs_both_streams():
    consent.require_tty(stdin=_Stream(True), stdout=_Stream(True))
    for stdin, stdout in ((False, True), (True, False), (False, False)):
        with pytest.raises(consent.ConsentError):
            consent.require_tty(stdin=_Stream(stdin), stdout=_Stream(stdout))


def test_digest_matches_exact_hex_only():
    current = "ab" * 32
    assert consent.digest_matches(current, current)
    assert consent.digest_matches(current.upper(), current)
    assert not consent.digest_matches("ab" * 31 + "ac", current)
    assert not consent.digest_matches(current[:-1], current)
    assert not consent.digest_matches(current + "a", current)
    assert not consent.digest_matches("zz" * 32, current)
    assert not consent.digest_matches(f" {current}", current)
    assert not consent.digest_matches("", current)


# --- TTY and platform ------------------------------------------------------------------


@pytest.mark.parametrize("stdin_tty, stdout_tty", [(False, True), (True, False), (False, False)])
def test_approve_and_enable_refuse_without_tty(runner, repo, home, monkeypatch, stdin_tty, stdout_tty):
    real = consent.require_tty
    monkeypatch.setattr(consent, "require_tty",
                        lambda *a, **k: real(stdin=_Stream(stdin_tty), stdout=_Stream(stdout_tty)))
    for result in (approve(runner), approve(runner, PROVIDER), enable(runner)):
        assert result.exit_code == 1, result.output
        assert "in a terminal" in " ".join(result.output.split())
    assert not store_path(home).exists()


def test_refusal_without_tty_leaves_an_existing_store_unchanged(runner, repo, home, tty, monkeypatch):
    assert approve(runner, PROVIDER, "--sha256", digest(repo)).exit_code == 0
    before = store_path(home).read_bytes()
    monkeypatch.setattr(consent, "require_tty",
                        lambda *a, **k: (_ for _ in ()).throw(consent.ConsentError("no terminal")))
    write_script(repo, SCRIPT + "# changed\n")
    assert approve(runner, PROVIDER).exit_code == 1
    assert enable(runner).exit_code == 1
    assert store_path(home).read_bytes() == before


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_scripts_unavailable_off_linux(runner, repo, home, tty, monkeypatch, platform):
    monkeypatch.setattr(consent, "current_platform", lambda: platform)
    for result in (approve(runner, PROVIDER), enable(runner),
                   runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256", digest(repo)])):
        assert result.exit_code == 1
        assert "unavailable (platform)" in result.output
    assert state(runner) == "unavailable"
    assert not store_path(home).exists()


# --- approve ------------------------------------------------------------------------


def test_sha256_exact_match_approves_without_a_tty(runner, repo, home):
    result = runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256", digest(repo).upper()])
    assert result.exit_code == 0, result.output
    assert [a.digest for a in approvals(home, repo)] == [digest(repo)]
    no_approve_digest(result.output)


@pytest.mark.parametrize("given", ["0" * 64, "ab" * 31, "ab" * 33, "zz" * 32])
def test_sha256_mismatch_steers_an_agent_to_the_user(runner, repo, home, given):
    result = runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256", given])
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "does not match" in out
    ci = out.index("--sha256 is for CI with a digest kept in a protected secret")
    ask = out.index("ask the user to run `devgraph config scripts approve demo runbook_links` in a terminal")
    assert ci < ask
    assert "config scripts show" not in out[:ci]
    assert given not in result.output
    assert not store_path(home).exists()
    no_approve_digest(result.output)


def test_sha256_needs_a_provider_name(runner, repo, home):
    result = runner.invoke(app, ["config", "scripts", "approve", "demo", "--sha256", digest(repo)])
    assert result.exit_code == 1
    assert not store_path(home).exists()


def test_there_is_no_yes_option(runner, repo):
    result = runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--yes"])
    assert result.exit_code != 0
    assert "--yes" not in runner.invoke(app, ["config", "scripts", "approve", "--help"]).output


def test_typing_the_name_approves_and_anything_else_aborts(runner, repo, home, tty):
    wrong = approve(runner, PROVIDER, typed="yes")
    assert wrong.exit_code == 1 and "Not approved" in wrong.output
    assert not store_path(home).exists()
    right = approve(runner, PROVIDER)
    assert right.exit_code == 0, right.output
    assert [a.digest for a in approvals(home, repo)] == [digest(repo)]


def test_approval_prompt_shows_what_is_approved(runner, repo, tty):
    out = approve(runner, PROVIDER, typed="no").output
    canon = paths.canonical_repo_path(repo)
    for expected in ("demo", canon, PROVIDER, f".devgraph/providers/{PROVIDER}.py", f"{len(SCRIPT)} bytes",
                     "docs/**/*.md", "3 matched", "docs/r00.md", '"node_types"', '"plain"', "def derive(ctx):",
                     "no findings"):
        assert expected in out, expected
    assert out.index("def derive(ctx):") < out.index("Type the provider name")


def test_approval_prompt_wraps_long_lines_and_flags_them(tmp_path, settings, tty, monkeypatch):
    monkeypatch.setattr(cli_main.console, "_width", 80)  # the autouse wide console is 1000
    long_line = "    x = '" + "a" * 300 + "'  # HIDDEN_TAIL\n"
    root = make_repo(tmp_path / "wide", files(script="def derive(ctx):\n" + long_line + "    return []\n"))
    register(settings, root)
    out = CliRunner(env={"COLUMNS": "80"}).invoke(
        app, ["config", "scripts", "approve", "demo", PROVIDER], input="no\n").output
    assert "HIDDEN_TAIL" in out
    assert "…" not in out
    assert "wider than the terminal" in out
    assert all(len(line) <= 80 for line in out.splitlines())


def test_reapproval_shows_declaration_and_script_diffs(runner, repo, home, tty):
    assert approve(runner, PROVIDER).exit_code == 0
    (repo / "devgraph.schema.yaml").write_text(SCHEMA.replace('"plain"', '"changed"'))
    write_script(repo, SCRIPT + "# second version\n")
    out = approve(runner, PROVIDER, typed="no").output
    lines = [line.strip() for line in out.splitlines()]
    assert '-      "note": "plain"' in lines and '+      "note": "changed"' in lines
    assert "+# second version" in lines
    assert "since the approval at" in out


def test_script_with_findings_cannot_be_approved(tmp_path, settings, home, tty, runner):
    root = make_repo(tmp_path / "bad", files(script="import socket\n\ndef derive(ctx):\n    return []\n"))
    register(settings, root)
    for result in (approve(runner, PROVIDER),
                   runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER,
                                       "--sha256", provider_snapshot(root, PROVIDER, git=GIT).digest])):
        assert result.exit_code == 1
        assert "findings" in result.output and "socket" in result.output
    assert not store_path(home).exists()


def test_rejected_script_cannot_be_approved(tmp_path, settings, home, tty, runner):
    root = make_repo(tmp_path / "rej", files(script="def derive(ctx):\n  return (\n"))
    register(settings, root)
    result = approve(runner, PROVIDER)
    assert result.exit_code == 1 and "rejected" in result.output
    assert state(runner) == "rejected"
    assert not store_path(home).exists()


def test_approve_without_a_name_walks_providers_awaiting_approval(tmp_path, settings, home, tty, runner):
    schema = SCHEMA + "  - label: Other\n    key: [slug]\n    metadata: [{name: slug, required: true}]\n" \
                      "    source: {provider: custom, name: other}\n"
    schema = schema.replace("node_types:", "  - name: other\n    inputs: [\"docs/*.md\"]\nnode_types:")
    root = make_repo(tmp_path / "two", files(schema=schema, **{".devgraph/providers/other.py": SCRIPT}))
    register(settings, root)
    result = runner.invoke(app, ["config", "scripts", "approve", "demo"], input=f"{PROVIDER}\nother\n")
    assert result.exit_code == 0, result.output
    store = TrustStore.open_read(store_path(home))
    with store:
        canon = paths.canonical_repo_path(root)
        assert store.active_digests("demo", canon, PROVIDER) and store.active_digests("demo", canon, "other")
    again = runner.invoke(app, ["config", "scripts", "approve", "demo"])
    assert again.exit_code == 0 and "nothing awaiting approval" in again.output.lower()


def test_approve_walk_skips_a_provider_with_findings_and_exits_1(tmp_path, settings, home, tty, runner):
    schema = SCHEMA.replace("node_types:", "  - name: bad\n    inputs: [\"docs/*.md\"]\nnode_types:")
    root = make_repo(tmp_path / "walk", files(schema=schema, **{".devgraph/providers/bad.py": "import socket\n"}))
    register(settings, root)
    result = runner.invoke(app, ["config", "scripts", "approve", "demo"], input=f"{PROVIDER}\n")
    assert result.exit_code == 1, result.output
    out = " ".join(result.output.split())
    assert "Skipping bad" in out and "socket" in out
    store = TrustStore.open_read(store_path(home))
    with store:
        canon = paths.canonical_repo_path(root)
        assert store.active_digests("demo", canon, PROVIDER) and not store.active_digests("demo", canon, "bad")
    rest = runner.invoke(app, ["config", "scripts", "approve", "demo"])
    assert rest.exit_code == 1 and "nothing awaiting approval" in rest.output.lower()


def test_unreadable_inputs_cannot_be_approved(tmp_path, settings, home, tty, runner):
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    root = make_repo(tmp_path / "links", files())
    (root / "docs" / "link.md").symlink_to(outside)
    _git(root, "add", "docs/link.md")
    _git(root, "commit", "-q", "-m", "link")
    register(settings, root)
    for result in (approve(runner, PROVIDER),
                   runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256",
                                       provider_snapshot(root, PROVIDER, git=GIT).digest])):
        assert result.exit_code == 1, result.output
        assert "inputs cannot be read" in result.output and "link.md" in result.output
    assert not store_path(home).exists()


# --- §5.6 multiple digests -------------------------------------------------------------


def test_approve_retires_previous_digests(runner, repo, home, tty):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    first = digest(repo)
    assert state(runner) == "approved"
    write_script(repo, SCRIPT + "# v2\n")
    assert state(runner) == "awaiting_approval"
    assert approve(runner, PROVIDER).exit_code == 0
    assert {a.digest: a.state for a in approvals(home, repo)} == {first: "retired", digest(repo): "active"}
    assert state(runner) == "approved"


def test_keep_previous_caps_at_five(runner, repo, home):
    approved = []
    for n in range(6):
        write_script(repo, SCRIPT + f"# v{n}\n")
        approved.append(digest(repo))
        result = runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER,
                                     "--sha256", approved[-1], "--keep-previous"])
        assert result.exit_code == 0, result.output
    states = {a.digest: a.state for a in approvals(home, repo)}
    assert [states[d] for d in approved] == ["retired"] + ["active"] * 5


def test_retired_digest_reprompts(runner, repo, home, tty):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    write_script(repo, SCRIPT + "# v2\n")
    assert approve(runner, PROVIDER).exit_code == 0
    write_script(repo, SCRIPT)  # back to the retired digest
    assert state(runner) == "awaiting_approval"
    result = runner.invoke(app, ["config", "scripts", "approve", "demo"], input="no\n")
    assert "Type the provider name" in result.output and result.exit_code == 1


def test_kept_older_digest_is_marked_with_its_date(runner, repo, home, tty):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    write_script(repo, SCRIPT + "# v2\n")
    assert approve(runner, PROVIDER, "--keep-previous").exit_code == 0
    write_script(repo, SCRIPT)
    out = runner.invoke(app, ["config", "scripts", "list"]).output
    first = next(a for a in approvals(home, repo) if a.digest == digest(repo))
    row = next(line for line in out.splitlines() if PROVIDER in line)
    assert "approved" in row and "kept" in row and first.approved_at[:10] in row and "UTC" in row


def test_revoke_stops_active_digest(runner, repo, home, tty):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    assert state(runner) == "approved"
    result = runner.invoke(app, ["config", "scripts", "revoke", "demo", PROVIDER])
    assert result.exit_code == 0, result.output
    assert "no longer runs" in " ".join(result.output.split())
    assert approvals(home, repo) == []
    assert state(runner) == "awaiting_approval"


def test_revoke_one_digest(runner, repo, home):
    first = digest(repo)
    assert runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256", first]).exit_code == 0
    write_script(repo, SCRIPT + "# v2\n")
    second = digest(repo)
    assert runner.invoke(app, ["config", "scripts", "approve", "demo", PROVIDER, "--sha256", second,
                               "--keep-previous"]).exit_code == 0
    result = runner.invoke(app, ["config", "scripts", "revoke", "demo", PROVIDER, "--digest", first[:12]])
    assert result.exit_code == 0, result.output
    assert [a.digest for a in approvals(home, repo)] == [second]
    out = " ".join(result.output.split())
    assert "no longer runs" not in out
    assert first[:12] in out and "current digest" in out and second[:12] in out and "still approved" in out
    last = runner.invoke(app, ["config", "scripts", "revoke", "demo", PROVIDER, "--digest", second])
    assert "no longer runs" in " ".join(last.output.split())
    unknown = runner.invoke(app, ["config", "scripts", "revoke", "demo", PROVIDER, "--digest", "f" * 64])
    assert unknown.exit_code == 1


def test_list_shows_growth_marker(runner, repo, home, tty):
    assert approve(runner, PROVIDER).exit_code == 0
    for i in range(4):
        (repo / f"docs/extra{i}.md").write_text("x\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "more")
    row = next(line for line in runner.invoke(app, ["config", "scripts", "list"]).output.splitlines()
               if PROVIDER in line)
    assert "7" in row and "grew" in row


# --- enable / disable -------------------------------------------------------------------


def test_enable_requires_typing_the_repo_id(runner, repo, home, tty):
    wrong = enable(runner, typed="yes")
    assert wrong.exit_code == 1 and not store_path(home).exists()
    right = enable(runner)
    assert right.exit_code == 0, right.output
    assert paths.canonical_repo_path(repo) in right.output and PROVIDER in right.output
    store = TrustStore.open_read(store_path(home))
    with store:
        assert store.scripts_enabled("demo", paths.canonical_repo_path(repo))
    off = runner.invoke(app, ["config", "scripts", "disable", "demo"])
    assert off.exit_code == 0, off.output
    store = TrustStore.open_read(store_path(home))
    with store:
        assert not store.scripts_enabled("demo", paths.canonical_repo_path(repo))


def test_project_config_resume_cli(runner, repo, home, tty):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    before = approvals(home, repo)
    assert state(runner) == "approved"

    assert runner.invoke(app, ["config", "disable", "demo"]).exit_code == 0
    assert state(runner) == "disabled"
    from devgraph.sandbox import gates

    canon = paths.canonical_repo_path(repo)
    assert not gates.gate1_project_config("demo", canon, registry_path=paths.fixed_registry_path(home))

    result = runner.invoke(app, ["config", "enable", "demo"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert PROVIDER in out and "resume" in out and "without re-approval" in out
    assert state(runner) == "approved"
    assert approvals(home, repo) == before


# --- show -------------------------------------------------------------------------------


def test_show_prints_the_digest_alone_on_its_line(runner, repo, home, tty):
    assert approve(runner, PROVIDER).exit_code == 0
    result = runner.invoke(app, ["config", "scripts", "show", "demo", PROVIDER])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert digest(repo) in lines
    for line in lines:
        assert digest(repo) not in line or line == digest(repo)
    for expected in ("state: disabled", paths.canonical_repo_path(repo), f".devgraph/providers/{PROVIDER}.py",
                     "3 matched", "docs/r00.md", "denylist", "Approval history", "UTC"):
        assert expected.lower() in result.output.lower(), expected
    at = lines.index(digest(repo))
    assert lines[at - 1] == "Digest (for review):"
    assert "not to be passed to --sha256 by an automated agent" in " ".join(lines[at + 1:at + 3])
    no_approve_digest(result.output)


@pytest.mark.parametrize("script, expected", [
    ("def derive(ctx):\n  return (\n", "rejected"),
    ("import socket\n\ndef derive(ctx):\n    return []\n", "disabled"),
])
def test_show_offers_no_approve_hint_when_approval_would_be_refused(tmp_path, settings, runner, script, expected):
    root = make_repo(tmp_path / "nohint", files(script=script))
    register(settings, root)
    result = runner.invoke(app, ["config", "scripts", "show", "demo", PROVIDER])
    assert f"State: {expected}" in result.output
    assert "config scripts approve" not in result.output


def test_hostile_characters_are_shown_escaped(tmp_path, settings, runner, tty):
    schema = SCHEMA.replace('"plain"', '"\\e[2J\\e]0;owned\\a"')
    root = make_repo(tmp_path / "hostile", files(schema=schema, **{"docs/a\x1b[31mred.md": "x\n"}))
    register(settings, root)
    shown = runner.invoke(app, ["config", "scripts", "show", "demo", PROVIDER])
    prompt = approve(runner, PROVIDER, typed="no")
    listed = runner.invoke(app, ["config", "scripts", "list"])
    for result in (shown, prompt, listed):
        assert "\x1b" not in result.output and "\x07" not in result.output
    assert "\\x1b[31mred.md" in shown.output and "\\x1b[31mred.md" in prompt.output
    assert "\\u001b[2J" in prompt.output


# --- add, remove, doctor ----------------------------------------------------------------


class _NoGraph:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Neo4j is not reachable in this test")


class _DownGraph:
    def __init__(self, *args, **kwargs):
        pass

    def verify_connectivity(self):
        raise RuntimeError("Neo4j is not reachable in this test")

    def close(self):
        pass


class _StubGraph:
    def __init__(self, *args, **kwargs):
        pass

    def read_applied_schema(self, repo_id):
        return None

    def delete_repository(self, repo_id):
        pass

    def close(self):
        pass


def test_add_prints_the_interactive_commands_only(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "GraphEngine", _NoGraph)
    root = make_repo(tmp_path / "fresh", files())
    result = runner.invoke(app, ["add", str(root)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    repo_id = re.search(r"Registered: (\S+)", out)[1]
    assert "will not run" in out
    assert f"devgraph config scripts enable {repo_id}" in out
    assert f"devgraph config scripts approve {repo_id}" in out
    assert not re.search(r"\b[0-9a-f]{64}\b", result.output)


def test_add_without_custom_providers_prints_no_notice(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "GraphEngine", _NoGraph)
    root = make_repo(tmp_path / "plain", {"README.md": "x\n"})
    result = runner.invoke(app, ["add", str(root)])
    assert result.exit_code == 0 and "config scripts" not in result.output


def test_remove_deletes_trust_rows(runner, repo, home, tty, monkeypatch):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    monkeypatch.setattr(cli_main, "GraphEngine", _StubGraph)
    result = runner.invoke(app, ["remove", "demo"])
    assert result.exit_code == 0, result.output
    assert approvals(home, repo) == []
    store = TrustStore.open_read(store_path(home))
    with store:
        assert not store.scripts_enabled("demo", paths.canonical_repo_path(repo))


def test_remove_warns_when_the_trust_store_cannot_be_written(runner, repo, home, tty, monkeypatch):
    assert approve(runner, PROVIDER).exit_code == 0
    os.chmod(store_path(home), 0o666)
    monkeypatch.setattr(cli_main, "GraphEngine", _StubGraph)
    result = runner.invoke(app, ["remove", "demo"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Warning" in out and "trust" in out
    assert "grant nothing" not in out and "cleared when" in out


def test_re_adding_a_repository_never_inherits_old_trust_rows(runner, tmp_path, settings, home, tty, monkeypatch):
    monkeypatch.setattr(cli_main, "GraphEngine", _NoGraph)
    root = make_repo(tmp_path / "again", files())
    first = runner.invoke(app, ["add", str(root)])
    repo_id = re.search(r"Registered: (\S+)", " ".join(first.output.split()))[1]
    assert runner.invoke(app, ["config", "scripts", "enable", repo_id], input=f"{repo_id}\n").exit_code == 0
    assert runner.invoke(app, ["config", "scripts", "approve", repo_id, PROVIDER], input=f"{PROVIDER}\n").exit_code == 0
    registry = RepoRegistry(settings.registry_db_path)  # a remove whose trust cleanup failed
    try:
        registry.remove_repo(repo_id)
    finally:
        registry.close()
    again = runner.invoke(app, ["add", str(root)])
    assert again.exit_code == 0, again.output
    assert re.search(r"Registered: (\S+)", " ".join(again.output.split()))[1] == repo_id
    store = TrustStore.open_read(store_path(home))
    with store:
        canon = paths.canonical_repo_path(root)
        assert not store.scripts_enabled(repo_id, canon)
        assert store.approvals(repo_id, canon, PROVIDER) == []


def doctor(runner, monkeypatch):
    monkeypatch.setattr(cli_main, "GraphEngine", _DownGraph)
    monkeypatch.setattr(cli_main, "resolve_podman", lambda: None)
    result = runner.invoke(app, ["doctor"])
    section = result.output.split("Script providers", 1)[1].split("Live Watcher", 1)[0]
    return result, " ".join(section.split())


def test_doctor_reports_platform_and_provider_states(runner, repo, monkeypatch):
    result, section = doctor(runner, monkeypatch)
    assert "linux" in section
    assert "runbook_links" in section and "disabled" in section
    assert "devgraph config scripts enable demo" in section
    no_approve_digest(result.output)


def test_doctor_reports_awaiting_approval_with_the_interactive_hint(runner, repo, tty, monkeypatch):
    assert enable(runner).exit_code == 0
    result, section = doctor(runner, monkeypatch)
    assert "awaiting_approval" in section
    assert "ask the user to review and run `devgraph config scripts approve demo runbook_links` in a terminal" in section
    no_approve_digest(result.output)


def test_doctor_off_linux(runner, repo, monkeypatch):
    monkeypatch.setattr(consent, "current_platform", lambda: "darwin")
    _, section = doctor(runner, monkeypatch)
    assert "unavailable (platform)" in section


def test_doctor_reports_group_writable_devgraph_dir(runner, repo, home, monkeypatch):
    os.chmod(home / ".devgraph", 0o775)
    _, section = doctor(runner, monkeypatch)
    assert "writable by group or others" in section and "chmod go-w" in section


def test_doctor_reports_old_git(runner, repo, monkeypatch):
    from devgraph.sandbox.reader import InputError

    def old(git):
        raise InputError("input_unavailable", "git 2.45 or later is required (it can refuse lazy fetches)")

    monkeypatch.setattr(selection, "check_git", old)
    _, section = doctor(runner, monkeypatch)
    assert "git 2.45 or later" in section


def test_doctor_reports_a_corrupt_trust_store(runner, repo, home, monkeypatch):
    store_path(home).write_bytes(b"not a database")
    os.chmod(store_path(home), 0o600)
    result, section = doctor(runner, monkeypatch)
    assert "trust store" in section and "not a readable SQLite database" in section
    assert "doctor found one or more failing checks" in result.output


def test_doctor_names_a_permission_problem_on_the_trust_store(runner, repo, home, tty, monkeypatch):
    assert approve(runner, PROVIDER).exit_code == 0
    os.chmod(store_path(home), 0o666)
    _, section = doctor(runner, monkeypatch)
    assert "writable by group or others" in section and "chmod 600" in section
    assert "corrupt" not in section


def test_doctor_tells_project_config_off_from_a_gate_read_off(runner, repo, home, monkeypatch):
    assert runner.invoke(app, ["config", "disable", "demo"]).exit_code == 0
    _, off = doctor(runner, monkeypatch)
    assert "project config is off" in off and "`devgraph config enable demo`" in off
    assert runner.invoke(app, ["config", "enable", "demo"]).exit_code == 0
    os.chmod(paths.fixed_registry_path(home), 0o664)
    _, gate = doctor(runner, monkeypatch)
    assert "project config is on, but the script gate reads it as off" in gate
    assert "`devgraph config enable demo`" not in gate


def test_doctor_reports_registry_location_mismatch(runner, tmp_path, monkeypatch):
    elsewhere = Settings(_env_file=None, neo4j_password="x", registry_db_path=tmp_path / "other" / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: elsewhere)
    root = make_repo(tmp_path / "work" / "acme", files())
    register(elsewhere, root)
    _, section = doctor(runner, monkeypatch)
    assert "default registry location" in section



def test_doctor_follows_a_symlinked_devgraph_dir_as_the_gates_do(runner, repo, home, tmp_path, monkeypatch):
    real = tmp_path / "real-devgraph"
    (home / ".devgraph").rename(real)
    (home / ".devgraph").symlink_to(real)
    _, section = doctor(runner, monkeypatch)
    assert "writable by group or others" not in section


# --- an unreachable graph, and gate log lines -------------------------------------------------


@pytest.fixture
def unreachable(monkeypatch):
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "unreachable")


def test_unreachable_graph_is_pending_and_says_so(runner, repo, home, tty, monkeypatch):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "unreachable")
    assert state(runner) == "pending"
    row = next(line for line in runner.invoke(app, ["config", "scripts", "list"]).output.splitlines()
               if PROVIDER in line)
    note = row.split("│")[7]
    assert "graph unreachable" in note and "start Neo4j" in note and "approved" in note
    result, section = doctor(runner, monkeypatch)
    assert "pending" in section and "graph unreachable" in section and "start Neo4j" in section
    no_approve_digest(result.output)


def test_pending_with_a_reachable_graph_says_rescan(runner, repo, tty, monkeypatch):
    assert enable(runner).exit_code == 0
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "pending")
    assert state(runner) == "pending"
    _, section = doctor(runner, monkeypatch)
    assert "devgraph rescan demo --now" in section and "graph unreachable" not in section


def test_pending_schema_with_scripts_off_is_disabled(runner, repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "pending")
    assert state(runner) == "disabled"
    _, section = doctor(runner, monkeypatch)
    assert "devgraph config scripts enable demo" in section and "rescan" not in section


def test_config_disable_then_rescan_is_disabled_not_pending(runner, repo, tty, monkeypatch):
    assert enable(runner).exit_code == 0
    assert approve(runner, PROVIDER).exit_code == 0
    assert runner.invoke(app, ["config", "disable", "demo"]).exit_code == 0
    # A rescan with project config off leaves the custom schema unapplied.
    monkeypatch.setattr(cli_main, "_applied_schema_status", lambda repo_id, schema_hash: "pending")
    assert state(runner) == "disabled"
    _, section = doctor(runner, monkeypatch)
    assert "project config is off" in section and "rescan" not in section


def test_applied_schema_status_reports_an_unreachable_graph(monkeypatch):
    real = REAL_SCHEMA_STATUS
    monkeypatch.setattr(cli_main, "GraphEngine", _NoGraph)
    assert real("demo", "sha256:x") == "unreachable"

    class Applied(_StubGraph):
        def read_applied_schema(self, repo_id):
            return {"hash": "sha256:x"}

    monkeypatch.setattr(cli_main, "GraphEngine", Applied)
    assert real("demo", "sha256:x") == "applied"
    assert real("demo", "sha256:y") == "pending"


def test_gate_warnings_are_not_repeated(tmp_path, settings, home, runner, monkeypatch, caplog):
    schema = SCHEMA.replace("node_types:", "  - name: other\n    inputs: [\"docs/*.md\"]\nnode_types:")
    root = make_repo(tmp_path / "logs", files(schema=schema, **{".devgraph/providers/other.py": SCRIPT}))
    register(settings, root)
    os.chmod(paths.fixed_registry_path(home), 0o664)  # gate 1 logs a warning on every read

    def gate1_records():
        return [r for r in caplog.records if "script gate 1" in r.getMessage()]

    result = runner.invoke(app, ["config", "scripts", "list"])
    assert result.exit_code == 0, result.output
    assert len(gate1_records()) == 1  # two providers, one line
    caplog.clear()
    doctor(runner, monkeypatch)
    assert len(gate1_records()) == 1


@pytest.mark.parametrize("output", [
    "devgraph config scripts approve demo x --sha256 " + "ab" * 32,
    "Approve with:\n" + "AB" * 32,
    "ab" * 32 + "\nthen approve it",
    "run it with --sha256\n" + "ab" * 32,
    "pass --sha256=" + "ab" * 32,
])
def test_digest_scanner_catches_split_and_adjacent_forms(output):
    with pytest.raises(AssertionError):
        no_approve_digest(output)
