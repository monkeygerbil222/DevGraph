"""The sandbox home and the fixed paths under it (spec §5.1).

`<home>` comes from the password database, never `HOME`, `USERPROFILE`,
`Path.home()` or `Settings`, so no environment variable or `.env` file can move
the trust store, the gate-1 registry or the machine lock. Callers reach the home
through this module's attribute (`paths.sandbox_home()`), which tests patch.
"""

from __future__ import annotations

import os
import stat
import sys
import unicodedata
from pathlib import Path

from devgraph.sandbox.limits import SUPPORTED_PLATFORMS


class SandboxPathError(Exception):
    """A repository path that cannot be canonicalised: every decision on it is off."""


def _windows_profile_dir() -> Path:
    """The user profile known folder (`FOLDERID_Profile`), not `USERPROFILE`."""
    import ctypes
    from ctypes import wintypes

    class _Guid(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    folder_id = _Guid(0x5E6C858F, 0x0E22, 0x4760, (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73))
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    try:
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) != 0 or not out.value:
            raise SandboxPathError("cannot resolve the user profile folder")
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)  # type: ignore[attr-defined]


def sandbox_home() -> Path:
    if sys.platform == "win32":
        return _windows_profile_dir()
    import pwd  # Unix-only: imported here so the module loads on Windows

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def trust_store_path(home: Path) -> Path:
    return home / ".devgraph" / "script_trust.sqlite3"


def fixed_registry_path(home: Path) -> Path:
    """The default `registry_db_path`, which gate 1 reads whatever `Settings` says."""
    return home / ".devgraph" / "registry.sqlite3"


def sandbox_lock_path(home: Path) -> Path:
    return home / ".devgraph" / "sandbox.lock"


def canonical_repo_path(path: Path | str) -> str:
    """The resolved real path, NFC-normalised: half of every trust key."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxPathError(f"cannot resolve {path!s}: {exc}") from exc
    try:
        str(resolved).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SandboxPathError(f"{path!r} is not valid UTF-8") from exc
    return unicodedata.normalize("NFC", str(resolved))


def _check_private(st: os.stat_result, what: Path) -> None:
    if not hasattr(os, "getuid"):
        raise SandboxPathError(f"{what}: ownership cannot be checked on this platform")
    if st.st_uid != os.getuid():
        raise SandboxPathError(f"{what} is not owned by the current user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SandboxPathError(f"{what} is writable by group or others")


def private_file_stat(path: Path) -> os.stat_result:
    """`lstat` of a sandbox state file (trust store, fixed registry), checked like ssh's
    strict modes: the file must be a regular file, not a symlink, and the file and its
    parent directory must be owned by the current user and not group- or world-writable.
    Raises `SandboxPathError` (or `OSError`, e.g. when missing)."""
    path = Path(path)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise SandboxPathError(f"{path} is not a regular file")
    _check_private(st, path)
    _check_private(os.stat(path.parent), path.parent)
    return st


def check_private_dir(path: Path) -> None:
    """The ownership and mode check of `private_file_stat`, for a directory."""
    _check_private(os.stat(path), Path(path))


def same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def inside_repo(path: Path, canon: str) -> bool:
    """True if `path` (lexically or once its directory is resolved) lies in the repository
    `canon`; a repository that holds DevGraph's own state cannot vouch for itself."""
    path = Path(path)
    repo = Path(canon)
    candidates = (path, path.parent.resolve() / path.name)
    return any(Path(unicodedata.normalize("NFC", str(c))).is_relative_to(repo) for c in candidates)


def platform_supported(platform: str = sys.platform) -> bool:
    return platform in SUPPORTED_PLATFORMS
