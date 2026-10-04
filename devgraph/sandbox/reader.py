"""The no-follow reader (spec §3.2): the only code that reads a sandbox input, a
provider script, or the schema file whose declaration feeds a digest.

For each path, in order:

1. containment on path components (`is_relative_to`), never a string prefix;
2. `lstat` of every component from the root, refusing any symlink or any
   non-directory intermediate;
3. an open that follows nothing: `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS
   | RESOLVE_NO_MAGICLINKS)` from a descriptor of the root, or, where the kernel
   has no `openat2`, `openat(O_NOFOLLOW | O_DIRECTORY)` per directory and
   `O_NOFOLLOW` for the leaf;
4. `fstat` of that descriptor (a regular file within the cap), then a read of
   at most cap + 1 bytes.

Steps 3 and 4 close the race with step 2: nothing is followed by name after the
check, and the type and size decision is made on the descriptor that is read.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from devgraph.config.project_schema import PROPERTY_NAME_PATTERN
from devgraph.paths import FileTooLarge, NotRegularFile, read_fd_bounded
from devgraph.sandbox.limits import INPUT_MAX_FILE_BYTES, SCRIPT_MAX_BYTES

SCHEMA_FILE = "devgraph.schema.yaml"

# 437 in the unified syscall table, which these Linux machines use. Others (alpha,
# ia64, mips) number it differently and take the fallback.
_SYS_OPENAT2 = 437
_OPENAT2_MACHINES = ("x86_64", "aarch64", "riscv64", "s390x", "loongarch64")
_OPENAT2_MACHINE_PREFIXES = ("arm", "ppc64")
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08

# The flags the no-follow open needs. Where any is missing (Windows), or `os.open`
# takes no `dir_fd`, every read is refused rather than made with weaker flags.
_FLAG_NAMES = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC", "O_NOCTTY")
_NO_FOLLOW_OK = (
    all(hasattr(os, name) for name in _FLAG_NAMES) and os.open in os.supports_dir_fd
)
_SAFE_FLAGS = os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY if _NO_FOLLOW_OK else 0
_DIR_FLAGS = (
    os.O_RDONLY | (os.O_DIRECTORY | os.O_NOFOLLOW if _NO_FOLLOW_OK else 0) | _SAFE_FLAGS
)
_LEAF_FLAGS = os.O_RDONLY | (os.O_NOFOLLOW if _NO_FOLLOW_OK else 0) | _SAFE_FLAGS

#: Test-only hook, called with the full path between the `lstat` walk and the open.
_after_check: Callable[[Path], None] | None = None


class InputError(Exception):
    """A refused input. `code` is `input_unavailable`, `input_cap` or `static_reject`;
    the message is one line and never quotes file content."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code = code
        self.reason = reason


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def openat2_platform_ok(platform_name: str, machine: str) -> bool:
    """True on Linux machines where `openat2` is syscall 437."""
    return platform_name == "linux" and (
        machine in _OPENAT2_MACHINES or machine.startswith(_OPENAT2_MACHINE_PREFIXES)
    )


_OPENAT2_PLATFORM = openat2_platform_ok(sys.platform, platform.machine())
_libc = None
if _OPENAT2_PLATFORM:
    try:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    except (OSError, TypeError, AttributeError):
        _OPENAT2_PLATFORM = False


def _openat2(dir_fd: int, rel: str) -> int:
    how = _OpenHow(
        _LEAF_FLAGS, 0, _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS
    )
    fd = _libc.syscall(
        ctypes.c_long(_SYS_OPENAT2),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(os.fsencode(rel)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return fd


def openat2_supported() -> bool:
    """True when the platform, the kernel and any seccomp filter allow `openat2`."""
    if not _OPENAT2_PLATFORM:
        return False
    try:
        fd = _openat2(-100, "/")  # AT_FDCWD; RESOLVE_BENEATH refuses an absolute path
    except OSError as exc:
        return exc.errno not in (errno.ENOSYS, errno.EPERM)
    os.close(fd)
    return True


def _open_fallback(root_fd: int, parts: list[str]) -> int:
    dir_fd = root_fd
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            if dir_fd != root_fd:
                os.close(dir_fd)
            dir_fd = next_fd
        return os.open(parts[-1], _LEAF_FLAGS, dir_fd=dir_fd)
    finally:
        if dir_fd != root_fd:
            os.close(dir_fd)


def _split(rel: str) -> list[str]:
    parts = rel.split("/")
    if not rel or "\x00" in rel or any(part in ("", ".", "..") for part in parts):
        raise InputError("input_unavailable", f"{rel!r} is not a plain relative path")
    return parts


def read_repo_file(
    root: Path, rel: str, *, cap: int, use_openat2: bool | None = None
) -> bytes:
    """The bytes of the regular file `rel` beneath the canonical repository `root`.

    `use_openat2=None` uses `openat2` and falls back to the component walk on
    `ENOSYS` or `EPERM`; `True` requires `openat2`; `False` forces the fallback.
    Raises `InputError` (`input_unavailable`, or `input_cap` over `cap`).
    """
    if not _NO_FOLLOW_OK:
        raise InputError(
            "input_unavailable", "no-follow reads are not supported on this platform"
        )
    root = Path(root)
    # The root must already be the canonical real path (and not `/`): every check
    # below is relative to it, and it is opened without following a final symlink.
    if (
        not root.is_absolute()
        or str(root) != os.path.realpath(root)
        or root.parent == root
    ):
        raise InputError(
            "input_unavailable", "the repository root is not a canonical path"
        )
    # 1. Containment, on components.
    if os.path.isabs(rel) or not (root / rel).is_relative_to(root):
        raise InputError("input_unavailable", f"{rel!r} is outside the repository")
    parts = _split(rel)

    # 2. lstat every component from the root down.
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise InputError("input_unavailable", f"{rel!r}: {exc.strerror}") from None
        if stat.S_ISLNK(st.st_mode):
            raise InputError("input_unavailable", f"{rel!r}: a component is a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            raise InputError(
                "input_unavailable", f"{rel!r}: a component is not a directory"
            )

    if _after_check is not None:
        _after_check(current)

    # 3. Open without following anything; 4. decide on the descriptor.
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise InputError(
            "input_unavailable", f"repository root: {exc.strerror}"
        ) from None
    try:
        fd = None
        if use_openat2 and not _OPENAT2_PLATFORM:
            raise InputError(
                "input_unavailable", "openat2 is not available on this platform"
            )
        if use_openat2 is not False and _OPENAT2_PLATFORM:
            try:
                fd = _openat2(root_fd, rel)
            except OSError as exc:
                if use_openat2 or exc.errno not in (errno.ENOSYS, errno.EPERM):
                    raise
        if fd is None:
            fd = _open_fallback(root_fd, parts)
        try:
            return read_fd_bounded(fd, cap, rel)
        finally:
            os.close(fd)
    except FileTooLarge:
        raise InputError("input_cap", f"{rel!r} is larger than {cap} bytes") from None
    except NotRegularFile:
        raise InputError(
            "input_unavailable", f"{rel!r} is not a regular file"
        ) from None
    except OSError as exc:
        raise InputError("input_unavailable", f"{rel!r}: {exc.strerror}") from None
    finally:
        os.close(root_fd)


def read_provider_script(
    root: Path, name: str, *, use_openat2: bool | None = None
) -> bytes:
    """`.devgraph/providers/<name>.py`, at most `SCRIPT_MAX_BYTES`; over is `static_reject`."""
    if not isinstance(name, str) or not PROPERTY_NAME_PATTERN.fullmatch(name):
        raise InputError("input_unavailable", f"{name!r} is not a provider name")
    try:
        return read_repo_file(
            root,
            f".devgraph/providers/{name}.py",
            cap=SCRIPT_MAX_BYTES,
            use_openat2=use_openat2,
        )
    except InputError as exc:
        if exc.code == "input_cap":
            raise InputError(
                "static_reject",
                f"provider script {name!r} is larger than {SCRIPT_MAX_BYTES} bytes",
            ) from None
        raise


def read_schema_file(root: Path, *, use_openat2: bool | None = None) -> bytes:
    """`devgraph.schema.yaml`, at most `INPUT_MAX_FILE_BYTES`."""
    return read_repo_file(
        root, SCHEMA_FILE, cap=INPUT_MAX_FILE_BYTES, use_openat2=use_openat2
    )
