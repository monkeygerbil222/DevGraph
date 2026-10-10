"""The repository's `.gitignore` files, as the walk and the watcher apply them.

Every `.gitignore` in the tree counts, each for the folder it sits in and
below, with the rules of gitignore(5): `#` comments, `!` negations (the last
matching rule wins, and a deeper file's rules come after its parents'), a
trailing `/` for folders only, a leading or middle `/` anchoring the pattern
to the file's folder, and `*`, `?`, `[...]` and `**` wildcards. A file inside
an ignored folder stays ignored whatever a later rule says, as in git.

Applied to any registered folder, git checkout or not. `.git/info/exclude`
and the user's global excludes file are not read: they are per-clone, so two
clones of one repository would index differently.

Parsed files are cached by path and re-read when their stat changes, so an
edited `.gitignore` takes effect on the next check.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

GITIGNORE = ".gitignore"


class Rule(NamedTuple):
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


#: (repo-relative POSIX prefix of the folder holding a .gitignore -- "" for
#: the root, else ending in "/" -- and its rules), outermost first.
Chain = Sequence[tuple[str, Sequence[Rule]]]

_cache: dict[str, tuple[tuple[int, int, int], list[Rule]]] = {}


def parse(text: str) -> list[Rule]:
    """The rules of one .gitignore file's text."""
    rules = []
    for line in text.splitlines():
        rule = _rule(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _rule(line: str) -> Rule | None:
    # Trailing spaces are dropped unless escaped with a backslash.
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    dir_only = line.endswith("/") and not line.endswith("\\/")
    if dir_only:
        line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.removeprefix("/")
    body = _translate(line)
    return Rule(re.compile(body if anchored else f"(?:.*/)?{body}", re.DOTALL), negated, dir_only)


def _translate(pattern: str) -> str:
    """The regex for a pattern matched against a path relative to its .gitignore's folder."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and pattern.startswith("**", i):
            at_start = i == 0 or pattern[i - 1] == "/"
            at_end = i + 2 == n
            if at_start and at_end:
                out.append(".*")
                i += 2
                continue
            if at_start and pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append("[^/]*")  # not a whole segment: an ordinary star
            i += 2
            while i < n and pattern[i] == "*":
                i += 1
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = _class_end(pattern, i)
            if end is None:
                out.append(re.escape(c))
            else:
                out.append(_char_class(pattern[i + 1:end]))
                i = end
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _class_end(pattern: str, start: int) -> int | None:
    """Index of the `]` closing the class opened at `start`, or None."""
    i = start + 1
    if i < len(pattern) and pattern[i] in "!^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1  # a leading ] is literal
    while i < len(pattern):
        if pattern[i] == "\\":
            i += 2
            continue
        if pattern[i] == "]":
            return i
        i += 1
    return None


def _char_class(inner: str) -> str:
    negated = inner[:1] in ("!", "^")
    if negated:
        inner = inner[1:]
    parts = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            i += 1
            parts.append(re.escape(inner[i]))
        else:
            parts.append("-" if c == "-" else re.escape(c))
        i += 1
    body = "".join(parts)
    return f"[^/{body}]" if negated else f"(?!/)[{body}]"


def matches(chain: Chain, rel: str, is_dir: bool) -> bool:
    """Whether the rules in `chain` ignore `rel` (repo-relative POSIX). Only
    `rel` itself is judged, not the folders above it."""
    ignored = False
    for prefix, rules in chain:
        if not rel.startswith(prefix):
            continue
        sub = rel[len(prefix):]
        for rule in rules:
            if (not rule.dir_only or is_dir) and rule.regex.fullmatch(sub):
                ignored = not rule.negated
    return ignored


def rules_in(folder: Path) -> list[Rule]:
    """The rules of `folder`'s .gitignore: none when it has none or it can't be read."""
    path = os.path.join(folder, GITIGNORE)
    try:
        st = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return []
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as f:
            rules = parse(f.read().decode("utf-8", errors="replace"))
    except OSError:  # a folder, or unreadable
        rules = []
    _cache[path] = (stamp, rules)
    return rules


def chain_to(repo_root: Path, rel_dir: str) -> list[tuple[str, list[Rule]]] | None:
    """The rules that apply inside the folder `rel_dir` ("" for the root),
    or None when it, or a folder above it, is ignored."""
    chain: list[tuple[str, list[Rule]]] = []
    prefix = ""
    parts = [part for part in rel_dir.split("/") if part]
    for depth in range(len(parts) + 1):
        rules = rules_in(repo_root / prefix if prefix else repo_root)
        if rules:
            chain.append((prefix, rules))
        if depth == len(parts):
            break
        folder = prefix + parts[depth]
        if matches(chain, folder, True):
            return None
        prefix = folder + "/"
    return chain


def is_gitignored(repo_root: Path, rel: str, is_dir: bool = False) -> bool:
    """Whether a .gitignore ignores the repo-relative POSIX path `rel`, or a folder above it."""
    parent, _, _name = rel.rpartition("/")
    chain = chain_to(Path(repo_root), parent)
    return chain is None or matches(chain, rel, is_dir)
