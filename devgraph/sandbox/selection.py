"""Hardened tracked-file selection (spec §3.2).

Candidates are tracked files only, from `git ls-files`. A plain `git ls-files`
reads the repository's own `.git/config` and runs a repository-local
`core.fsmonitor` program, so the invocation is fixed: command-line `-c`
overrides (which outrank every config file, includes too), `--cached` (index
only, so no `filter.*` or `textconv` program), `--no-pager` with `GIT_PAGER=cat`
and a piped stdout, and an environment built from nothing rather than filtered
from the caller's.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from devgraph.indexer.dispatch import is_ignored_path
from devgraph.sandbox.limits import (
    DENYLIST,
    FIXED_PATH,
    GIT_TIMEOUT_SECONDS,
    INPUT_MAX_FILES,
)
from devgraph.sandbox.paths import SandboxPathError, canonical_repo_path
from devgraph.sandbox.reader import InputError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Selection:
    matched: tuple[str, ...]  # sorted repo-relative paths, as git names them
    denied: int  # glob matches the secret-name denylist excluded


def git_binary() -> str | None:
    return shutil.which("git", path=FIXED_PATH)


def tracked_files(root: Path, *, git: str | None) -> list[str]:
    """The repository's tracked paths, from the index only. Names that are not valid
    UTF-8 are skipped. Raises `InputError("input_unavailable")` on any failure."""
    if git is None:
        raise InputError("input_unavailable", "git is not installed")
    try:
        canon = canonical_repo_path(root)
    except SandboxPathError:
        raise InputError(
            "input_unavailable", "the repository path cannot be resolved"
        ) from None
    command = [
        git,
        "--no-pager",
        "-C",
        canon,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "ls-files",
        "-z",
        "--cached",
    ]
    with tempfile.TemporaryDirectory(prefix="devgraph-git-home-") as home:
        env = {
            "PATH": FIXED_PATH,
            "HOME": home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CEILING_DIRECTORIES": str(Path(canon).parent),
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
        try:
            proc = subprocess.run(
                command,
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise InputError(
                "input_unavailable", "git ls-files could not run"
            ) from None
    if proc.returncode != 0:
        raise InputError(
            "input_unavailable", f"git ls-files exited with status {proc.returncode}"
        )

    names, skipped = [], 0
    for raw in proc.stdout.split(b"\x00"):
        if not raw:
            continue
        try:
            names.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            skipped += 1
    if skipped:
        logger.warning(
            "skipped %d tracked file name(s) that are not valid UTF-8", skipped
        )
    return names


def _denied(nfc: str) -> bool:
    return any(
        fnmatchcase(part, pattern)
        for part in nfc.casefold().split("/")
        for pattern in DENYLIST
    )


def select_inputs(
    root: Path, globs: tuple[str, ...] | list[str], *, git: str | None
) -> Selection:
    """The tracked files matching a declared glob, less ignored directories and the
    secret-name denylist. More than `INPUT_MAX_FILES` matches is `input_cap`."""
    matched, denied = [], 0
    for name in tracked_files(root, git=git):
        nfc = unicodedata.normalize("NFC", name)
        path = PurePosixPath(nfc)
        if not any(path.full_match(glob) for glob in globs) or is_ignored_path(path):
            continue
        if _denied(nfc):
            denied += 1
            continue
        matched.append(name)
    if len(matched) > INPUT_MAX_FILES:
        raise InputError(
            "input_cap", f"{len(matched)} input files, more than {INPUT_MAX_FILES}"
        )
    return Selection(matched=tuple(sorted(matched)), denied=denied)
