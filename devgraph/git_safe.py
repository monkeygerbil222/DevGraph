"""The single entry point for running git against a registered repository.

DevGraph only ever reads a repository's history and working-tree state, but
a repository's own configuration (`.git/config`, any file it pulls in with
`include.path`, and `.gitattributes`) can name programs for git to run while
doing those reads: an fsmonitor hook, a `core.hooksPath` hook, a `textconv`
diff driver, a clean/smudge/process filter driver, or a transport helper
reached through a partial-clone lazy fetch. `open_repo` returns a GitPython
`Repo` whose every git invocation runs with those mechanisms switched off:

- `-c` overrides on every command. Command-line config takes precedence over
  every config file, including files reached through `include.path`, so a
  repository cannot re-enable anything pinned here.
- Filter drivers defined in the repository's own config (`local` and
  `worktree` scope) are listed when the repository is opened and blanked
  with `-c`. Filters from the user's global/system config (e.g. Git LFS)
  are left alone; the user's global and system config apply as usual.
- A fixed environment: no lazy fetching of missing objects, no transport
  protocols at all, no prompts.
- A timeout on every command (`GIT_TIMEOUT_SECONDS`), so a hung git never
  blocks the dashboard or the indexer.
- Location variables inherited from DevGraph's own environment (e.g. when it
  is launched from inside a git hook) are dropped, so git operates on the
  repository DevGraph opened and nothing else.

`include.path` itself cannot be disabled: git rejects an `include.path` given
with `-c` ("relative config includes must come from files"). Included files
are still read, but every setting above wins over them.

The lazy-fetch block needs git >= 2.45 (`GIT_NO_LAZY_FETCH`). On older git
the fetch is still refused because no transport protocol is allowed
(`GIT_ALLOW_PROTOCOL` is empty, and the program-running protocols are pinned
to `never` with `-c`, which beats a repository's own `protocol.*.allow`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from subprocess import Popen
from typing import Any

from git import Git, GitCommandError, Repo

logger = logging.getLogger(__name__)

CONFIG_OVERRIDES: tuple[str, ...] = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.untrackedCache=false",
    "protocol.allow=never",
    # A repository's own `protocol.<name>.allow` beats `protocol.allow`, so
    # the protocols that run a local program are pinned individually.
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "diff.external=",
    "core.pager=cat",
    "core.sshCommand=false",
)

GIT_ENV: dict[str, str] = {
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
}

# Upper bound on how long any one git command may run before it is killed.
GIT_TIMEOUT_SECONDS: float = 30.0

_FILTER_DRIVER_KEYS = r"^filter\..*\.(clean|smudge|process)$"
_REPO_SCOPES = ("local", "worktree")

# GitPython's `Repo()` also reads these from `os.environ` to locate the
# repository, so they are removed from the process environment rather than
# only from the child's.
_INHERITED_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")

LAZY_FETCH_GUARD_MIN_VERSION = (2, 45)


class _StreamWatchdog:
    """Kills commands GitPython streams (`as_process=True`) once they pass
    `GIT_TIMEOUT_SECONDS`; GitPython's own timeout only covers commands whose
    output it collects itself. One shared thread polls them all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: list[tuple[float, Popen, list[str]]] = []
        self._thread: threading.Thread | None = None

    def watch(self, proc: Popen, command: list[str]) -> None:
        with self._lock:
            self._procs.append((time.monotonic() + GIT_TIMEOUT_SECONDS, proc, command))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="git-timeout", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            with self._lock:
                alive = []
                for deadline, proc, command in self._procs:
                    if proc.poll() is not None:
                        continue
                    if now >= deadline:
                        logger.warning(
                            "git command %r did not complete in %g secs; killed", " ".join(command), GIT_TIMEOUT_SECONDS
                        )
                        proc.kill()
                        continue
                    alive.append((deadline, proc, command))
                self._procs = alive


_stream_watchdog = _StreamWatchdog()


class _HardenedGit(Git):
    def __init__(self, working_dir: Any = None) -> None:
        super().__init__(working_dir)
        self.set_persistent_git_options(c=list(CONFIG_OVERRIDES))
        self.update_environment(**GIT_ENV)

    def disable_filter_drivers(self, names: list[str]) -> None:
        overrides = [
            f"filter.{name}.{key}"
            for name in names
            # `required=false` too: a required filter with no command makes
            # git abort instead of reading the file unfiltered.
            for key in ("clean=", "smudge=", "process=", "required=false")
        ]
        self.set_persistent_git_options(c=[*CONFIG_OVERRIDES, *overrides])

    def execute(self, command, *args, **kwargs):  # type: ignore[override]
        if kwargs.get("as_process"):
            proc = super().execute(command, *args, **kwargs)
            # GitPython's persistent `cat-file --batch` readers live as long
            # as the Repo and only read local objects.
            if "cat-file" not in command:
                _stream_watchdog.watch(proc.proc, command)
            return proc
        kwargs.setdefault("kill_after_timeout", GIT_TIMEOUT_SECONDS)
        return super().execute(command, *args, **kwargs)


class HardenedRepo(Repo):
    GitCommandWrapperType = _HardenedGit

    def blame(self, rev, file, incremental=False, rev_opts=None, **kwargs):  # type: ignore[override]
        # A `.gitattributes` diff driver's textconv program runs on every
        # blob blame reads; blame the raw content instead.
        kwargs["no_textconv"] = True
        return super().blame(rev, file, incremental, rev_opts, **kwargs)


@lru_cache(maxsize=1)
def _check_git_version() -> None:
    version = Git().version_info[:2]
    if version < LAZY_FETCH_GUARD_MIN_VERSION:
        logger.warning(
            "git %s does not support GIT_NO_LAZY_FETCH (needs %s); missing objects "
            "in a partial clone may still trigger a fetch attempt, which is refused "
            "because no transport protocol is allowed",
            ".".join(map(str, version)),
            ".".join(map(str, LAZY_FETCH_GUARD_MIN_VERSION)),
        )


_env_scrub_lock = threading.Lock()
_env_scrubbed = False


def _scrub_inherited_location_vars() -> None:
    global _env_scrubbed
    with _env_scrub_lock:
        if _env_scrubbed:
            return
        for var in _INHERITED_LOCATION_VARS:
            os.environ.pop(var, None)
        _env_scrubbed = True


def _repo_filter_drivers(repo: HardenedRepo) -> list[str]:
    """Names of filter drivers defined in the repository's own config."""
    try:
        listing = repo.git.config("--show-scope", "--get-regexp", _FILTER_DRIVER_KEYS)
    except GitCommandError:
        return []  # exit 1: no matching keys
    names: list[str] = []
    for line in listing.splitlines():
        scope, _, rest = line.partition("\t")
        if scope not in _REPO_SCOPES:
            continue
        key = rest.split(" ", 1)[0]
        name = key[len("filter.") :].rsplit(".", 1)[0]
        if name not in names:
            names.append(name)
    return names


def open_repo(path: str | Path) -> HardenedRepo:
    """Open the repository at `path` for hardened git access."""
    _scrub_inherited_location_vars()
    _check_git_version()
    repo = HardenedRepo(str(path))
    names = _repo_filter_drivers(repo)
    if names:
        repo.git.disable_filter_drivers(names)
    return repo
