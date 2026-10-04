"""The single entry point for running git against a registered repository.

DevGraph only ever reads a repository's history and working-tree state, but
a repository's own configuration (`.git/config`, any file it pulls in with
`include.path`, and `.gitattributes`) can name programs for git to run while
doing those reads: an fsmonitor hook, a `core.hooksPath` hook, a `textconv`
diff driver, or a transport helper reached through a partial-clone lazy
fetch. `open_repo` returns a GitPython `Repo` whose every git invocation runs
with those mechanisms switched off:

- `-c` overrides on every command. Command-line config takes precedence over
  every config file, including files reached through `include.path`, so a
  repository cannot re-enable anything pinned here.
- A fixed environment: no lazy fetching of missing objects, no transport
  protocols at all, no prompts, no system/global config.
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
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from git import Git, Repo

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
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_PAGER": "cat",
}

# GitPython's `Repo()` also reads these from `os.environ` to locate the
# repository, so they are removed from the process environment rather than
# only from the child's.
_INHERITED_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")

LAZY_FETCH_GUARD_MIN_VERSION = (2, 45)


@lru_cache(maxsize=1)
def _trusted_safe_directories() -> tuple[str, ...]:
    """`safe.directory` entries from the user's system and global config.

    System and global config are turned off for repository commands, but a
    user may rely on `safe.directory` there to open repositories owned by
    another account. Those entries are carried over as `-c` options (a scope
    git also trusts for this setting).
    """
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_LOCATION_VARS}
    entries: list[str] = []
    for scope in ("--system", "--global"):
        result = subprocess.run(
            ["git", "config", scope, "--get-all", "safe.directory"],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            entries.extend(result.stdout.splitlines())
    return tuple(entries)


class _HardenedGit(Git):
    def __init__(self, working_dir: Any = None) -> None:
        super().__init__(working_dir)
        self.set_persistent_git_options(
            c=[*CONFIG_OVERRIDES, *(f"safe.directory={d}" for d in _trusted_safe_directories())]
        )
        self.update_environment(**GIT_ENV)


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


def open_repo(path: str | Path) -> HardenedRepo:
    """Open the repository at `path` for hardened git access."""
    for var in _INHERITED_LOCATION_VARS:
        os.environ.pop(var, None)
    _check_git_version()
    return HardenedRepo(str(path))
