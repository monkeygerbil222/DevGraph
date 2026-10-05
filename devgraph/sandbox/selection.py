"""Hardened tracked-file selection (spec §3.2).

Candidates are tracked files only, from `git ls-files`. A plain `git ls-files`
reads the repository's own `.git/config` and can run programs it names: a
`core.fsmonitor` hook, or, in a partial clone whose sparse index must be
expanded, a lazy fetch through `core.sshCommand`, `remote.<name>.uploadpack`
or an `ext::` transport. So the invocation is fixed: command-line `-c`
overrides (which outrank every config file, includes too); `--cached` (index
only, so no `filter.*` or `textconv` program); `GIT_NO_LAZY_FETCH=1`, the
lazy-fetch guard (git 2.45+, so older git is refused); `--sparse`, defence in
depth that keeps a cone-mode sparse index collapsed (non-cone patterns still
expand it); `--no-pager` with `GIT_PAGER=cat` and a piped stdout; and an
environment built from nothing rather than filtered from the caller's.

`-t --stage` adds each entry's tag and mode, so skip-worktree entries (not on
disk) and gitlinks (submodules) are dropped whether or not the index is sparse.
"""

from __future__ import annotations

import logging
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from devgraph.indexer.dispatch import is_ignored_path
from devgraph.sandbox.limits import (
    DENYLIST,
    FIXED_PATH,
    GIT_TIMEOUT_SECONDS,
    INDEX_MAX_ENTRIES,
    INPUT_MAX_FILES,
)
from devgraph.sandbox.reader import InputError

logger = logging.getLogger(__name__)

#: `GIT_NO_LAZY_FETCH` first appears in git 2.45.
GIT_MIN_VERSION = (2, 45)
#: A pending, unterminated name longer than this ends the read (PATH_MAX is 4 KiB).
_MAX_NAME_BYTES = 64 * 1024


@dataclass(frozen=True)
class Selection:
    matched: tuple[str, ...]  # sorted repo-relative paths, as git names them
    denied: int  # glob matches the secret-name denylist excluded


def git_binary() -> str | None:
    return shutil.which("git", path=FIXED_PATH)


def git_env(home: str, ceiling: str) -> dict[str, str]:
    """The whole environment `git` runs with: constructed, never copied."""
    return {
        "PATH": FIXED_PATH,
        "HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CEILING_DIRECTORIES": ceiling,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }


def _check_git_version(
    git: str, env: dict[str, str], cwd: str, deadline: float
) -> None:
    try:
        proc = subprocess.run(
            [git, "--version"],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(deadline - time.monotonic(), 0.001),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise InputError("input_unavailable", "git --version could not run") from None
    found = re.match(rb"git version (\d+)\.(\d+)", proc.stdout)
    if (
        proc.returncode != 0
        or not found
        or (int(found[1]), int(found[2])) < GIT_MIN_VERSION
    ):
        raise InputError(
            "input_unavailable",
            "git 2.45 or later is required (it can refuse lazy fetches)",
        )


def check_git(git: str | None) -> None:
    """Raise `InputError("input_unavailable")` unless `git` exists and is new enough for
    selection, checked as selection checks it (for `doctor`)."""
    if git is None:
        raise InputError("input_unavailable", "git is not installed")
    with tempfile.TemporaryDirectory(prefix="devgraph-git-home-") as home:
        _check_git_version(
            git, git_env(home, home), home, time.monotonic() + GIT_TIMEOUT_SECONDS
        )


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def _iter_tracked(root: Path, git: str | None) -> Iterator[str]:
    """Stream tracked names. Closing the iterator early kills git. Raises
    `InputError("input_unavailable")` on any failure, after the names read so far."""
    if git is None:
        raise InputError("input_unavailable", "git is not installed")
    try:
        # The real path, not the NFC trust-key form: an NFD directory must still open.
        real = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise InputError(
            "input_unavailable", "the repository path cannot be resolved"
        ) from None
    command = [
        git,
        "--no-pager",
        "-C",
        str(real),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "ls-files",
        "-z",
        "--cached",
        "--sparse",
        "-t",
        "--stage",
    ]
    with tempfile.TemporaryDirectory(prefix="devgraph-git-home-") as home:
        env = git_env(home, str(real.parent))
        # One deadline covers the version check, the read and the final wait.
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        _check_git_version(git, env, home, deadline)
        try:
            proc = subprocess.Popen(
                command,
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise InputError(
                "input_unavailable", "git ls-files could not run"
            ) from None
        try:
            yield from _read_names(proc, deadline)
            returncode = proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
            if returncode != 0:
                raise InputError(
                    "input_unavailable", f"git ls-files exited with status {returncode}"
                )
        except subprocess.TimeoutExpired:
            raise InputError("input_unavailable", "git ls-files timed out") from None
        finally:
            _kill(proc)
            proc.stdout.close()


# Modes that name a file in the work tree; a gitlink (160000) or a sparse
# directory (040000) does not.
_FILE_MODES = (b"100644", b"100755", b"120000")


def _read_names(proc: subprocess.Popen, deadline: float) -> Iterator[str]:
    """Parse `ls-files -z -t --stage` entries (`<tag> <mode> <sha> <stage>\\t<path>`)
    into the paths of files on disk, stopping past `INDEX_MAX_ENTRIES`."""
    fd = proc.stdout.fileno()
    pending, skipped, entries_seen, previous = b"", 0, 0, None
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InputError("input_unavailable", "git ls-files timed out")
            if not selector.select(remaining):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            *entries, pending = (pending + chunk).split(b"\x00")
            if len(pending) > _MAX_NAME_BYTES:
                raise InputError(
                    "input_unavailable", "git ls-files printed an overlong name"
                )
            for raw in entries:
                entries_seen += 1
                if entries_seen > INDEX_MAX_ENTRIES:
                    raise InputError(
                        "input_cap", f"index has more than {INDEX_MAX_ENTRIES} entries"
                    )
                header, tab, path = raw.partition(b"\t")
                fields = header.split(b" ")
                if not tab or not path or len(fields) != 4:
                    raise InputError(
                        "input_unavailable", "git ls-files printed an unexpected entry"
                    )
                tag, mode = fields[0], fields[1]
                if tag == b"S" or mode not in _FILE_MODES or path == previous:
                    continue  # skip-worktree, gitlink, sparse directory, or another stage
                previous = path
                try:
                    yield path.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
    if pending:
        raise InputError("input_unavailable", "git ls-files output was truncated")
    if skipped:
        logger.warning(
            "skipped %d tracked file name(s) that are not valid UTF-8", skipped
        )


def _tracked_files(root: Path, *, git: str | None) -> list[str]:
    """Test helper: every selectable tracked path, as `select_inputs` sees them."""
    return list(_iter_tracked(root, git))


def _denied(name: str) -> bool:
    """A component matches the denylist after NFKC and case folding, so compatibility
    forms (fullwidth letters, say) and any case are caught at any depth."""
    folded = unicodedata.normalize(
        "NFKC", unicodedata.normalize("NFKC", name).casefold()
    )
    return any(
        fnmatchcase(part, pattern) for part in folded.split("/") for pattern in DENYLIST
    )


def select_inputs(
    root: Path, globs: tuple[str, ...] | list[str], *, git: str | None
) -> Selection:
    """The tracked files matching a declared glob, less ignored directories and the
    secret-name denylist. More than `INPUT_MAX_FILES` matches is `input_cap`."""
    matched, denied = [], 0
    names = _iter_tracked(root, git)
    try:
        for name in names:
            path = PurePosixPath(unicodedata.normalize("NFC", name))
            if not any(path.full_match(glob) for glob in globs) or is_ignored_path(
                path
            ):
                continue
            if _denied(name):
                denied += 1
                continue
            matched.append(name)
            if len(matched) > INPUT_MAX_FILES:
                raise InputError(
                    "input_cap", f"more than {INPUT_MAX_FILES} input files"
                )
    finally:
        names.close()  # kills git if the read stopped early
    return Selection(matched=tuple(sorted(matched)), denied=denied)
