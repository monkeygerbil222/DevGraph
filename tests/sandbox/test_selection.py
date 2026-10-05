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
from devgraph.sandbox.selection import _tracked_files, git_binary, select_inputs

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
def test__tracked_files_skips_names_that_are_not_utf8(tmp_path):
    root = _repo(tmp_path / "repo", {"ok.py": b"x"})
    fd = os.open(os.fsencode(root) + b"/bad\xff.py", os.O_WRONLY | os.O_CREAT, 0o644)
    os.close(fd)
    _git(root, "add", "-A")
    assert _tracked_files(root, git=GIT) == ["ok.py"]


def test_no_git_work_tree_is_input_unavailable(tmp_path):
    # No git binary: no selection, whatever the directory.
    with pytest.raises(InputError) as info:
        _tracked_files(tmp_path, git=None)
    assert info.value.code == "input_unavailable"
    if GIT is None:
        pytest.skip("git is not installed")

    # A plain directory inside another repository is not that repository.
    outer = _repo(tmp_path / "outer", {"plain/inner.py": b"x"})
    with pytest.raises(InputError) as info:
        _tracked_files(outer / "plain", git=GIT)
    assert info.value.code == "input_unavailable"

    # A corrupt index: git exits non-zero.
    broken = _repo(tmp_path / "broken", {"a.py": b"x"})
    (broken / ".git" / "index").write_bytes(b"not an index")
    with pytest.raises(InputError) as info:
        _tracked_files(broken, git=GIT)
    assert info.value.code == "input_unavailable"

    # A path that does not exist.
    with pytest.raises(InputError) as info:
        _tracked_files(tmp_path / "missing", git=GIT)
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

    assert _tracked_files(root, git=GIT) == ["mine.py"]
    assert not marker.exists()


def test_git_binary_comes_from_the_fixed_path(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    assert git_binary() == shutil.which("git", path="/usr/bin:/bin")


def _exit_marker(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A marker program that records it ran and then fails, so no transport waits on it."""
    marker = tmp_path / f"{name}.ran"
    program = tmp_path / f"{name}.sh"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    program.chmod(0o755)
    return program, marker


def _promisor_sparse_repo(path: Path, *, cone: bool = True) -> Path:
    """A partial clone with a sparse index whose `far` trees are missing, so a
    full-index read would lazily fetch them from the promisor remote. With
    `cone=False` the patterns are non-cone, so even `--sparse` expands the index."""
    root = _repo(path, {"keep/a": b"a", "far/b": b"b", "far/sub/c": b"c"})
    _git(root, "sparse-checkout", "set", "--cone", "--sparse-index", "keep")
    trees = []
    for tree in ("far", "far/sub"):
        out = subprocess.run(
            [GIT, "-C", str(root), "rev-parse", f"HEAD:{tree}"],
            env=_SETUP_ENV,
            check=True,
            capture_output=True,
            text=True,
        )
        trees.append(out.stdout.strip())
    for sha in trees:
        (root / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    _git(root, "config", "core.repositoryformatversion", "1")
    _git(root, "config", "extensions.partialClone", "origin")
    _git(root, "config", "remote.origin.promisor", "true")
    if not cone:
        _git(root, "config", "core.sparseCheckoutCone", "false")
        (root / ".git" / "info" / "sparse-checkout").write_text("/keep/a\n!/far/\n")
    return root


def _ssh_command(root: Path, program: Path) -> None:
    _git(root, "config", "remote.origin.url", "ssh://example.invalid/repo")
    _git(root, "config", "core.sshCommand", str(program))


def _upload_pack(root: Path, program: Path) -> None:
    _git(root, "config", "remote.origin.url", str(root.parent / "elsewhere"))
    _git(root, "config", "remote.origin.uploadpack", str(program))


def _ext_transport(root: Path, program: Path) -> None:
    _git(root, "config", "remote.origin.url", f"ext::{program}")
    _git(root, "config", "protocol.ext.allow", "always")


@needs_git
@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(_ssh_command, id="core.sshCommand"),
        pytest.param(_upload_pack, id="remote.uploadpack"),
        pytest.param(_ext_transport, id="ext-transport"),
    ],
)
@pytest.mark.parametrize(
    "cone", [pytest.param(True, id="cone"), pytest.param(False, id="non_cone_patterns")]
)
def test_lazy_fetch_runs_no_repository_program(tmp_path, configure, cone):
    root = _promisor_sparse_repo(tmp_path / "repo", cone=cone)
    (tmp_path / "elsewhere").mkdir()
    program, marker = _exit_marker(tmp_path, "fetch")
    configure(root, program)

    result = select_inputs(root, ["**/*"], git=GIT)
    assert result.matched == ("keep/a",)
    assert not marker.exists()

    # The premise: the pre-fix hardened command, without the lazy-fetch guard
    # or `--sparse`, expands the index and runs the configured program.
    with __import__("tempfile").TemporaryDirectory() as home:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CEILING_DIRECTORIES": str(root.parent),
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
        subprocess.run(
            [
                GIT,
                "--no-pager",
                "-C",
                str(root),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "ls-files",
                "-z",
                "--cached",
            ],
            env=env,
            check=False,
            capture_output=True,
            timeout=30,
        )
    assert marker.exists()


def _fake_git(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "fake-git"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)
    return str(fake)


@pytest.mark.parametrize(
    "version", ["git version 2.44.2", "git version 1.9", "not git"]
)
def test_git_older_than_2_45_is_refused(tmp_path, version):
    root = tmp_path / "repo"
    root.mkdir()
    fake = _fake_git(tmp_path, f"echo '{version}'")
    with pytest.raises(InputError) as info:
        _tracked_files(root, git=fake)
    assert info.value.code == "input_unavailable"
    assert "2.45" in info.value.reason


@needs_git
def test_nfd_repository_directory(tmp_path):
    root = _repo(tmp_path / "cafe\u0301", {"a.py": b"x"})
    assert _tracked_files(root, git=GIT) == ["a.py"]
    assert select_inputs(root, ["*.py"], git=GIT).matched == ("a.py",)


@needs_git
def test_denylist_catches_compatibility_forms(tmp_path):
    root = _repo(
        tmp_path / "repo",
        {
            "\uff53\uff45\uff43\uff52\uff45\uff54.txt": b"x",
            "\uff0eenv": b"x",
            "ok.txt": b"x",
        },
    )
    result = select_inputs(root, ["*"], git=GIT)
    assert result.matched == ("ok.txt",)
    assert result.denied == 2


def test_selection_stops_reading_past_the_cap(tmp_path, monkeypatch):
    """Matches past the cap end the read and kill git, rather than buffering everything."""
    root = tmp_path / "repo"
    root.mkdir()
    done = tmp_path / "finished"
    fake = _fake_git(
        tmp_path,
        'case "$1" in --version) echo "git version 2.55.0"; exit 0;; esac\n'
        "seq 2000000 | sed 's/.*/H 100644 0 0\\tf&.py/' | tr '\\n' '\\0'\n"
        f"touch '{done}'",
    )
    monkeypatch.setattr(selection, "INPUT_MAX_FILES", 3)
    with pytest.raises(InputError) as info:
        select_inputs(root, ["*.py"], git=fake)
    assert info.value.code == "input_cap"
    assert not done.exists()


def test_git_environment_is_constructed():
    env = selection.git_env("/tmp/home", "/srv")
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/home",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CEILING_DIRECTORIES": "/srv",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }


@needs_git
def test_sparse_index_is_not_expanded(tmp_path):
    """Sparse directories stay collapsed (`--sparse`): their files are outside the
    work tree, and expanding them is what reads tree objects at all."""
    root = _repo(tmp_path / "repo", {"keep/a": b"a", "far/b": b"b", "far/sub/c": b"c"})
    _git(root, "sparse-checkout", "set", "--cone", "--sparse-index", "keep")
    assert select_inputs(root, ["**/*"], git=GIT).matched == ("keep/a",)


def _blob(root: Path, data: bytes) -> str:
    out = subprocess.run(
        [GIT, "-C", str(root), "hash-object", "-w", "--stdin"],
        env=_SETUP_ENV,
        input=data,
        check=True,
        capture_output=True,
    )
    return out.stdout.decode().strip()


@needs_git
def test_index_entry_cap(tmp_path, monkeypatch):
    """A huge index ends the read with `input_cap`, whatever the globs match."""
    root = _repo(tmp_path / "repo", {"seed": b"x"})
    sha = _blob(root, b"x")
    lines = "".join(f"100644 {sha}\tgen/f{i:03}.txt\n" for i in range(30))
    subprocess.run(
        [GIT, "-C", str(root), "update-index", "--index-info"],
        env=_SETUP_ENV,
        input=lines.encode(),
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(selection, "INDEX_MAX_ENTRIES", 31)
    assert select_inputs(root, ["seed"], git=GIT).matched == ("seed",)
    monkeypatch.setattr(selection, "INDEX_MAX_ENTRIES", 30)
    with pytest.raises(InputError) as info:
        select_inputs(root, ["seed"], git=GIT)
    assert info.value.code == "input_cap"
    assert "index" in info.value.reason


def test_git_wall_time_is_bounded(tmp_path, monkeypatch):
    """The read deadline and the final wait share one deadline: a git that closes
    its output late and then hangs is killed at the timeout, not twice it."""
    import time

    root = tmp_path / "repo"
    root.mkdir()
    fake = _fake_git(
        tmp_path,
        'case "$1" in --version) echo "git version 2.55.0"; exit 0;; esac\n'
        "sleep 1.5\nexec 1>&-\nsleep 30",
    )
    monkeypatch.setattr(selection, "GIT_TIMEOUT_SECONDS", 2)
    started = time.monotonic()
    with pytest.raises(InputError) as info:
        select_inputs(root, ["*"], git=fake)
    assert info.value.code == "input_unavailable"
    assert time.monotonic() - started < 3


@needs_git
def test_skip_worktree_and_gitlinks_are_not_inputs(tmp_path):
    root = _repo(
        tmp_path / "repo",
        {"keep/a": b"a", "far/b": b"b", "top.txt": b"t", "hidden.txt": b"h"},
    )
    sub = _repo(tmp_path / "sub", {"s": b"s"})
    head = subprocess.run(
        [GIT, "-C", str(sub), "rev-parse", "HEAD"],
        env=_SETUP_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
    _git(root, "update-index", "--skip-worktree", "hidden.txt")
    assert select_inputs(root, ["**/*", "*"], git=GIT).matched == (
        "far/b",
        "keep/a",
        "top.txt",
    )

    # A sparse checkout without a sparse index marks out-of-cone files skip-worktree
    # (cone mode always includes top-level files, so hidden.txt is back on disk).
    _git(root, "sparse-checkout", "set", "--cone", "--no-sparse-index", "keep")
    assert select_inputs(root, ["**/*", "*"], git=GIT).matched == ("hidden.txt", "keep/a", "top.txt")
    # With the sparse index the collapsed directory is dropped the same way.
    _git(root, "sparse-checkout", "set", "--cone", "--sparse-index", "keep")
    assert select_inputs(root, ["**/*", "*"], git=GIT).matched == ("hidden.txt", "keep/a", "top.txt")
