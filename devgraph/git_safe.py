"""The single entry point for running git against a registered repository.

DevGraph only ever reads a repository's history and working-tree state, but
a repository's own configuration (`.git/config`, any file it pulls in with
`include.path`, and `.gitattributes`) can name programs for git to run while
doing those reads: an fsmonitor hook, a `core.hooksPath` hook, a `textconv`
diff driver, a clean/smudge/process filter driver, or a transport helper
reached through a partial-clone lazy fetch. `open_repo` returns a GitPython
`Repo` whose every git invocation runs with those mechanisms switched off:

- Config overrides on every command, passed as `GIT_CONFIG_COUNT` /
  `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` environment pairs. These have
  the same precedence as `-c` and beat every config file, including files
  reached through `include.path`, so a repository cannot re-enable anything
  pinned here. Unlike `-c`, a pair keeps its key intact even when it
  contains `=`.
- Filter drivers defined in the repository's own config (`local` and
  `worktree` scope) are listed again before each command that may read the
  working tree, and blanked with further pairs. Filters from the user's
  global/system config (e.g. Git LFS) are left alone; the user's global and
  system config apply as usual.
- A fixed environment: no lazy fetching of missing objects, no transport
  protocols at all, no prompts.
- Timeouts, so a hung git never blocks the dashboard or the indexer. A
  one-shot command is killed after `GIT_TIMEOUT_SECONDS` of wall-clock time.
  A streamed command (`as_process=True`, e.g. the commit walk) is killed
  only when a read of its output has waited `GIT_STREAM_IDLE_TIMEOUT_SECONDS`
  with no data, so a long walk that keeps producing is never cut short.
  Every git process starts in its own session and the kill goes to the
  whole process group, so helpers git started die with it.
- Location variables inherited from DevGraph's own environment (e.g. when it
  is launched from inside a git hook) are dropped, so git operates on the
  repository DevGraph opened and nothing else.

`include.path` itself cannot be disabled: git rejects an `include.path` that
does not come from a file ("relative config includes must come from files").
Included files are still read, but every setting above wins over them.

The lazy-fetch block needs git >= 2.45 (`GIT_NO_LAZY_FETCH`). On older git
the fetch is still refused because no transport protocol is allowed
(`GIT_ALLOW_PROTOCOL` is empty, and the program-running protocols are pinned
to `never` by the overrides, which beat a repository's own
`protocol.*.allow`).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from git import Git, GitCommandError, Repo
from git.compat import safe_decode
from git.util import remove_password_if_present

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

# Wall-clock limit for a one-shot git command.
GIT_TIMEOUT_SECONDS: float = 30.0
# How long a read of a streamed command's output may wait with no data.
GIT_STREAM_IDLE_TIMEOUT_SECONDS: float = 30.0

_FILTER_DRIVER_KEYS = r"^filter\..*\.(clean|smudge|process)$"
_REPO_SCOPES = ("local", "worktree")

# Subcommands that only read objects and refs, never the working tree, so
# no filter driver can run; they skip the per-command filter listing.
_OBJECT_ONLY_COMMANDS = frozenset({"cat-file", "config", "diff-tree", "rev-list", "rev-parse"})

# GitPython's `Repo()` also reads these from `os.environ` to locate the
# repository, so they are removed from the process environment rather than
# only from the child's.
_INHERITED_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")

LAZY_FETCH_GUARD_MIN_VERSION = (2, 45)


def _config_env(pairs: list[tuple[str, str]]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _fixed_pairs() -> list[tuple[str, str]]:
    return [(key, value) for key, _, value in (o.partition("=") for o in CONFIG_OVERRIDES)]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _timeout_message(command: list[str], seconds: float, what: str) -> str:
    return f'Timeout: the command "{" ".join(command)}" did not complete in {seconds:g} secs ({what}).'


class _IdleTrackingReader:
    """Wraps a streamed command's stdout and records when a read started
    waiting; the clock restarts each time a read returns."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.waiting_since: float | None = None

    def _timed(self, method: str, *args: Any) -> Any:
        self.waiting_since = time.monotonic()
        try:
            return getattr(self._stream, method)(*args)
        finally:
            self.waiting_since = None

    def read(self, *args: Any) -> Any:
        return self._timed("read", *args)

    def read1(self, *args: Any) -> Any:
        return self._timed("read1", *args)

    def readline(self, *args: Any) -> Any:
        return self._timed("readline", *args)

    def __iter__(self) -> _IdleTrackingReader:
        return self

    def __next__(self) -> Any:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _StreamWatchdog:
    """Kills the process group of a streamed command whose output read has
    waited `GIT_STREAM_IDLE_TIMEOUT_SECONDS` with no data. One shared thread
    polls them all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: list[tuple[subprocess.Popen, _IdleTrackingReader, list[str]]] = []
        self._thread: threading.Thread | None = None

    def watch(self, proc: subprocess.Popen, reader: _IdleTrackingReader, command: list[str]) -> None:
        with self._lock:
            self._procs.append((proc, reader, command))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="git-timeout", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            time.sleep(0.25)
            now = time.monotonic()
            with self._lock:
                alive = []
                for proc, reader, command in self._procs:
                    if proc.poll() is not None:
                        continue
                    waiting_since = reader.waiting_since
                    if waiting_since is not None and now - waiting_since >= GIT_STREAM_IDLE_TIMEOUT_SECONDS:
                        logger.warning(
                            "%s; killed", _timeout_message(command, GIT_STREAM_IDLE_TIMEOUT_SECONDS, "no output")
                        )
                        _kill_group(proc)
                        continue
                    alive.append((proc, reader, command))
                self._procs = alive


_stream_watchdog = _StreamWatchdog()


def _subcommand(command: list[str]) -> str | None:
    return next((arg for arg in command[1:] if not str(arg).startswith("-")), None)


class _HardenedGit(Git):
    def __init__(self, working_dir: Any = None) -> None:
        super().__init__(working_dir)
        self.update_environment(**GIT_ENV)

    def _repo_filter_drivers(self) -> list[str]:
        """Names of filter drivers defined in the repository's own config."""
        status, listing, stderr = self.config(
            "-z", "--show-scope", "--get-regexp", _FILTER_DRIVER_KEYS, with_extended_output=True, with_exceptions=False
        )
        if status == 1:
            return []  # no matching keys
        if status != 0:
            raise GitCommandError(["git", "config", "--get-regexp", _FILTER_DRIVER_KEYS], status, stderr)
        # -z: `<scope>\0<key>\n<value>\0` per entry.
        fields = listing.split("\0")
        names: list[str] = []
        for scope, entry in zip(fields[0::2], fields[1::2]):
            if scope not in _REPO_SCOPES:
                continue
            key = entry.split("\n", 1)[0]
            name = key[len("filter.") :].rsplit(".", 1)[0]
            if name not in names:
                names.append(name)
        return names

    def _config_pairs(self, command: list[str]) -> list[tuple[str, str]]:
        pairs = _fixed_pairs()
        if _subcommand(command) in _OBJECT_ONLY_COMMANDS:
            return pairs
        for name in self._repo_filter_drivers():
            pairs += [(f"filter.{name}.{key}", "") for key in ("clean", "smudge", "process")]
            # A required filter with no command makes git abort instead of
            # reading the file unfiltered.
            pairs.append((f"filter.{name}.required", "false"))
        return pairs

    def execute(self, command, **kwargs):  # type: ignore[override]
        kwargs.pop("kill_after_timeout", None)
        kwargs["start_new_session"] = True
        kwargs["env"] = {**(kwargs.get("env") or {}), **_config_env(self._config_pairs(command))}
        if kwargs.get("as_process"):
            handle = super().execute(command, **kwargs)
            reader = _IdleTrackingReader(handle.proc.stdout)
            handle.proc.stdout = reader
            _stream_watchdog.watch(handle.proc, reader, command)
            return handle
        return self._run_one_shot(command, **kwargs)

    def _run_one_shot(
        self,
        command: list[str],
        with_extended_output: bool = False,
        with_exceptions: bool = True,
        output_stream: Any = None,
        stdout_as_string: bool = True,
        strip_newline_in_stdout: bool = True,
        max_chunk_size: Any = None,
        **kwargs: Any,
    ) -> Any:
        """GitPython's `execute` result handling, with a wall-clock timeout
        that kills the command's whole process group."""
        redacted = remove_password_if_present(command)
        handle = super().execute(command, as_process=True, **kwargs)
        proc = handle.proc
        try:
            stdout, stderr = proc.communicate(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            raise GitCommandError(
                redacted, proc.returncode, _timeout_message(redacted, GIT_TIMEOUT_SECONDS, "wall clock")
            ) from None
        status = proc.returncode

        newline = "\n" if kwargs.get("universal_newlines") else b"\n"
        if output_stream is not None:
            if stdout:
                output_stream.write(stdout)
            stdout = stdout[:0] if stdout is not None else None
        elif stdout is not None and strip_newline_in_stdout and stdout.endswith(newline):
            stdout = stdout[:-1]
        if stderr.endswith(newline):
            stderr = stderr[:-1]

        if with_exceptions and status != 0:
            raise GitCommandError(redacted, status, stderr, stdout)
        if isinstance(stdout, bytes) and stdout_as_string:
            stdout = safe_decode(stdout)
        if with_extended_output:
            return (status, stdout, safe_decode(stderr))
        return stdout


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


def open_repo(path: str | Path) -> HardenedRepo:
    """Open the repository at `path` for hardened git access."""
    _scrub_inherited_location_vars()
    _check_git_version()
    return HardenedRepo(str(path))
