"""Hardened tracked-file selection (spec §3.2, §10.2).

Run against real `git` in `tmp_path`. The marker programs below are fixtures
these tests write; the assertion is that selection never runs them.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devgraph.indexer.dispatch import IGNORED_DIR_NAMES
from devgraph.sandbox import selection
from devgraph.sandbox.limits import DENYLIST
from devgraph.sandbox.reader import InputError
from devgraph.sandbox.selection import git_binary, select_inputs, tracked_files

GIT = git_binary()
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

# Setup runs git without the user's global or system config, so a global
# excludes file or signing setting cannot change what the fixture tracks.
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [GIT, "-C", str(repo), *args], env=_SETUP_ENV, check=True, capture_output=True
    )


def _repo(path: Path, files: dict[str, bytes]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for rel, data in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if files:
        _git(path, "add", "-f", "--", *files)
        _git(path, "commit", "-q", "-m", "fixture")
    return path


def _marker_program(tmp_path: Path, name: str) -> tuple[Path, Path]:
    marker = tmp_path / f"{name}.ran"
    program = tmp_path / f"{name}.sh"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    program.chmod(0o755)
    return program, marker


def _denylisted_names() -> list[str]:
    names = []
    for pattern in DENYLIST:
        base = pattern.replace("*", "zz")
        names += [base, f"d1/d2/{base}", base.upper(), f"D1/{base.upper()}/inner.txt"]
    names.append("secrets/readme.md")
    names.append("Config/Prod.Credentials.json")
    return names


@needs_git
def test_selection_excludes_untracked_and_ignored_files(tmp_path):
    kept = ["src/app.py", "README.md", "docs/guide.md"]
    ignored = [f"{name}/f.txt" for name in sorted(IGNORED_DIR_NAMES) if name != ".git"]
    ignored.append("pkg.egg-info/PKG-INFO")
    denied = _denylisted_names()
    root = _repo(tmp_path / "repo", {rel: b"x" for rel in kept + ignored + denied})
    (root / ".gitignore").write_text("generated.py\n")
    (root / "generated.py").write_text("gitignored and untracked")
    (root / "untracked.py").write_text("untracked")

    for globs in (["**/*"], ["*", "**/*", "**"]):
        result = select_inputs(root, globs, git=GIT)
        assert set(result.matched) == set(kept)
        assert list(result.matched) == sorted(result.matched)
        assert result.denied == len(denied)


@needs_git
def test_selection_globs_are_case_sensitive_and_nfc(tmp_path):
    decomposed = "café.md"  # NFD on disk; NFC is "café.md"
    root = _repo(tmp_path / "repo", {"a.PY": b"x", "b.py": b"x", decomposed: b"x"})
    assert select_inputs(root, ["*.py"], git=GIT).matched == ("b.py",)
    assert select_inputs(root, ["café.md"], git=GIT).matched == (decomposed,)


@needs_git
def test_selection_input_cap(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo", {"a.py": b"x", "b.py": b"x", "c.py": b"x"})
    monkeypatch.setattr(selection, "INPUT_MAX_FILES", 3)
    assert len(select_inputs(root, ["*.py"], git=GIT).matched) == 3
    monkeypatch.setattr(selection, "INPUT_MAX_FILES", 2)
    with pytest.raises(InputError) as info:
        select_inputs(root, ["*.py"], git=GIT)
    assert info.value.code == "input_cap"


@needs_git
def test_tracked_files_skips_names_that_are_not_utf8(tmp_path):
    root = _repo(tmp_path / "repo", {"ok.py": b"x"})
    fd = os.open(os.fsencode(root) + b"/bad\xff.py", os.O_WRONLY | os.O_CREAT, 0o644)
    os.close(fd)
    _git(root, "add", "-A")
    assert tracked_files(root, git=GIT) == ["ok.py"]


def test_no_git_work_tree_is_input_unavailable(tmp_path):
    # No git binary: no selection, whatever the directory.
    with pytest.raises(InputError) as info:
        tracked_files(tmp_path, git=None)
    assert info.value.code == "input_unavailable"
    if GIT is None:
        pytest.skip("git is not installed")

    # A plain directory inside another repository is not that repository.
    outer = _repo(tmp_path / "outer", {"plain/inner.py": b"x"})
    with pytest.raises(InputError) as info:
        tracked_files(outer / "plain", git=GIT)
    assert info.value.code == "input_unavailable"

    # A corrupt index: git exits non-zero.
    broken = _repo(tmp_path / "broken", {"a.py": b"x"})
    (broken / ".git" / "index").write_bytes(b"not an index")
    with pytest.raises(InputError) as info:
        tracked_files(broken, git=GIT)
    assert info.value.code == "input_unavailable"

    # A path that does not exist.
    with pytest.raises(InputError) as info:
        tracked_files(tmp_path / "missing", git=GIT)
    assert info.value.code == "input_unavailable"


def _fsmonitor(repo: Path, program: Path) -> None:
    _git(repo, "config", "core.fsmonitor", str(program))


def _fsmonitor_include(repo: Path, program: Path) -> None:
    included = repo.parent / "included.gitconfig"
    included.write_text(f"[core]\n\tfsmonitor = {program}\n")
    _git(repo, "config", "include.path", str(included))


def _pager(repo: Path, program: Path) -> None:
    _git(repo, "config", "pager.ls-files", str(program))


def _filter(repo: Path, program: Path) -> None:
    _git(repo, "config", "filter.x.clean", str(program))
    _git(repo, "config", "filter.x.smudge", str(program))
    _git(repo, "config", "filter.x.required", "true")
    (repo / ".gitattributes").write_text("* filter=x\n")


def _textconv(repo: Path, program: Path) -> None:
    _git(repo, "config", "diff.x.textconv", str(program))
    (repo / ".gitattributes").write_text("* diff=x\n")


@needs_git
@pytest.mark.parametrize(
    "configure, control",
    [
        pytest.param(_fsmonitor, True, id="core.fsmonitor"),
        pytest.param(_fsmonitor_include, True, id="include.path-fsmonitor"),
        pytest.param(_pager, False, id="pager.ls-files"),
        pytest.param(_filter, False, id="filter.x"),
        pytest.param(_textconv, False, id="diff.x.textconv"),
    ],
)
def test_ls_files_runs_no_repository_program(tmp_path, configure, control):
    root = _repo(tmp_path / "repo", {"a.py": b"x", "sub/b.py": b"y"})
    program, marker = _marker_program(tmp_path, "hook")
    configure(root, program)
    # Touch the work tree so a stat-dirty index tempts a refresh.
    (root / "a.py").write_bytes(b"changed")

    assert select_inputs(root, ["**/*.py"], git=GIT).matched == ("a.py", "sub/b.py")
    assert not marker.exists()

    if control:
        # The premise: a plain `git ls-files` with the caller's environment runs it.
        subprocess.run(
            [GIT, "ls-files", "-z"],
            cwd=root,
            env=dict(os.environ),
            check=False,
            capture_output=True,
        )
        assert marker.exists()


@needs_git
def test_caller_git_environment_has_no_effect(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo", {"mine.py": b"x"})
    other = _repo(tmp_path / "other", {"theirs.py": b"x"})
    program, marker = _marker_program(tmp_path, "env-hook")
    hostile_global = tmp_path / "global.gitconfig"
    hostile_global.write_text(f"[core]\n\tfsmonitor = {program}\n")

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "no-such-index"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(program))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor'='{program}'")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert tracked_files(root, git=GIT) == ["mine.py"]
    assert not marker.exists()


def test_git_binary_comes_from_the_fixed_path(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    assert git_binary() == shutil.which("git", path="/usr/bin:/bin")
