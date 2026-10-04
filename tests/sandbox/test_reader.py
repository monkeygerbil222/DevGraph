"""The no-follow reader (spec §3.2, §6, §10.2).

Every refusal runs on both the `openat2` path and the component-wise
`O_NOFOLLOW` fallback.
"""

import os
import threading
from pathlib import Path

import pytest

from devgraph.sandbox import reader
from devgraph.sandbox.limits import INPUT_MAX_FILE_BYTES, SCRIPT_MAX_BYTES
from devgraph.sandbox.reader import (
    InputError,
    read_provider_script,
    read_repo_file,
    read_schema_file,
)

MODES = [
    pytest.param(True, id="openat2"),
    pytest.param(False, id="fallback"),
]


@pytest.fixture(params=MODES)
def use_openat2(request):
    if request.param and not reader.openat2_supported():
        pytest.skip("openat2 is not available on this kernel")
    return request.param


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "realdir").mkdir(parents=True)
    (root / "real.txt").write_bytes(b"hello")
    (root / "realdir" / "f.txt").write_bytes(b"nested")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"TOP-SECRET")
    return root


def _refused(code, fn, *args, **kwargs):
    with pytest.raises(InputError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code
    assert "TOP-SECRET" not in str(info.value)
    return info.value


def test_regular_files_read(repo, use_openat2):
    assert (
        read_repo_file(repo, "real.txt", cap=100, use_openat2=use_openat2) == b"hello"
    )
    assert (
        read_repo_file(repo, "realdir/f.txt", cap=100, use_openat2=use_openat2)
        == b"nested"
    )


def test_reader_refusals_symlinks(repo, use_openat2):
    (repo / "leaf-link.txt").symlink_to(repo / "real.txt")
    (repo / "dir-link").symlink_to(repo / "realdir")
    (repo / "out-link.txt").symlink_to(repo.parent / "outside" / "secret.txt")
    for rel in ("leaf-link.txt", "dir-link/f.txt", "out-link.txt"):
        _refused(
            "input_unavailable",
            read_repo_file,
            repo,
            rel,
            cap=100,
            use_openat2=use_openat2,
        )


@pytest.mark.parametrize(
    "rel",
    [
        "../outside/secret.txt",
        "realdir/../real.txt",
        "./real.txt",
        "realdir//f.txt",
        "",
        "realdir/",
        "a\x00b",
    ],
)
def test_reader_refusals_bad_relative_paths(repo, use_openat2, rel):
    _refused(
        "input_unavailable", read_repo_file, repo, rel, cap=100, use_openat2=use_openat2
    )


def test_reader_refusals_sibling_prefix(repo, use_openat2):
    sibling = repo.parent / "repo-other"
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"TOP-SECRET")
    # An absolute path that string-prefix containment would accept.
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        str(sibling / "secret.txt"),
        cap=100,
        use_openat2=use_openat2,
    )
    # A link into the sibling.
    (repo / "esc").symlink_to(sibling)
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "esc/secret.txt",
        cap=100,
        use_openat2=use_openat2,
    )


def _promptly(fifo: Path, fn, *args, **kwargs) -> str:
    """Run a read that must not block on `fifo`; its `InputError` code, or "read"."""
    outcome = {}

    def attempt():
        try:
            fn(*args, **kwargs)
            outcome["result"] = "read"
        except InputError as exc:
            outcome["result"] = exc.code

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Unblock the stuck open so the daemon thread can finish.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("reading a FIFO blocked")
    return outcome["result"]


def test_reader_refusals_fifo_returns_promptly(repo, use_openat2):
    os.mkfifo(repo / "pipe")
    code = _promptly(
        repo / "pipe", read_repo_file, repo, "pipe", cap=100, use_openat2=use_openat2
    )
    assert code == "input_unavailable"


def test_reader_refusals_directory_and_missing(repo, use_openat2):
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "realdir",
        cap=100,
        use_openat2=use_openat2,
    )
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "missing.txt",
        cap=100,
        use_openat2=use_openat2,
    )
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "real.txt/x",
        cap=100,
        use_openat2=use_openat2,
    )


def test_reader_cap(repo, use_openat2):
    (repo / "exact").write_bytes(b"x" * 10)
    (repo / "over").write_bytes(b"x" * 11)
    assert read_repo_file(repo, "exact", cap=10, use_openat2=use_openat2) == b"x" * 10
    _refused("input_cap", read_repo_file, repo, "over", cap=10, use_openat2=use_openat2)


def test_reader_refusals_swap_after_check(repo, use_openat2, monkeypatch):
    outside = repo.parent / "outside"

    def swap_leaf(path):
        (repo / "real.txt").unlink()
        (repo / "real.txt").symlink_to(outside / "secret.txt")

    monkeypatch.setattr(reader, "_after_check", swap_leaf)
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "real.txt",
        cap=100,
        use_openat2=use_openat2,
    )

    (outside / "f.txt").write_bytes(b"TOP-SECRET")

    def swap_dir(path):
        (repo / "realdir").rename(repo / "realdir.bak")
        (repo / "realdir").symlink_to(outside)

    monkeypatch.setattr(reader, "_after_check", swap_dir)
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "realdir/f.txt",
        cap=100,
        use_openat2=use_openat2,
    )

    # A swap to a symlink that stays inside the repository is refused too.
    (repo / "dir2").mkdir()
    (repo / "dir2" / "g.txt").write_bytes(b"inside")
    (repo / "dir3").mkdir()
    (repo / "dir3" / "g.txt").write_bytes(b"inside")

    def swap_dir_inside(path):
        (repo / "dir2").rename(repo / "dir2.bak")
        (repo / "dir2").symlink_to("dir3")

    monkeypatch.setattr(reader, "_after_check", swap_dir_inside)
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "dir2/g.txt",
        cap=100,
        use_openat2=use_openat2,
    )

    (repo / "plain").write_bytes(b"ok")

    def swap_fifo(path):
        (repo / "plain").unlink()
        os.mkfifo(repo / "plain")

    monkeypatch.setattr(reader, "_after_check", swap_fifo)
    code = _promptly(
        repo / "plain", read_repo_file, repo, "plain", cap=100, use_openat2=use_openat2
    )
    assert code == "input_unavailable"


def test_lstat_walk_refuses_before_any_open(repo, use_openat2, monkeypatch):
    """Step 2 refuses on its own: a symlinked component never reaches the open."""
    reached = []
    monkeypatch.setattr(reader, "_after_check", reached.append)
    (repo / "leaf-link.txt").symlink_to("real.txt")
    (repo / "dir-link").symlink_to("realdir")
    for rel in ("leaf-link.txt", "dir-link/f.txt", "real.txt/x"):
        _refused(
            "input_unavailable",
            read_repo_file,
            repo,
            rel,
            cap=100,
            use_openat2=use_openat2,
        )
    assert reached == []
    assert (
        read_repo_file(repo, "real.txt", cap=100, use_openat2=use_openat2) == b"hello"
    )
    assert reached == [repo / "real.txt"]


def _write_script(root: Path, name: str, data: bytes) -> None:
    providers = root / ".devgraph" / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    (providers / f"{name}.py").write_bytes(data)


def test_provider_script_cap_is_static_reject(repo, use_openat2):
    _write_script(repo, "exact", b"#" * SCRIPT_MAX_BYTES)
    _write_script(repo, "over", b"#" * (SCRIPT_MAX_BYTES + 1))
    assert (
        len(read_provider_script(repo, "exact", use_openat2=use_openat2))
        == SCRIPT_MAX_BYTES
    )
    _refused(
        "static_reject", read_provider_script, repo, "over", use_openat2=use_openat2
    )


def test_provider_script_under_symlinked_dirs(tmp_path, use_openat2):
    elsewhere = tmp_path / "elsewhere"
    _write_script(elsewhere, "routes", b"def derive(ctx): pass\n")

    linked_devgraph = tmp_path / "a"
    linked_devgraph.mkdir()
    (linked_devgraph / ".devgraph").symlink_to(elsewhere / ".devgraph")
    _refused(
        "input_unavailable",
        read_provider_script,
        linked_devgraph,
        "routes",
        use_openat2=use_openat2,
    )

    linked_providers = tmp_path / "b"
    (linked_providers / ".devgraph").mkdir(parents=True)
    (linked_providers / ".devgraph" / "providers").symlink_to(
        elsewhere / ".devgraph" / "providers"
    )
    _refused(
        "input_unavailable",
        read_provider_script,
        linked_providers,
        "routes",
        use_openat2=use_openat2,
    )

    _write_script(tmp_path / "c", "routes", b"def derive(ctx): pass\n")
    assert (
        read_provider_script(tmp_path / "c", "routes", use_openat2=use_openat2)
        == b"def derive(ctx): pass\n"
    )


@pytest.mark.parametrize("name", ["../x", "a/b", "", "Routes", "x\x00"])
def test_provider_script_name_must_be_an_identifier(repo, name):
    _refused("input_unavailable", read_provider_script, repo, name)


def test_schema_file_cap(repo, use_openat2):
    (repo / "devgraph.schema.yaml").write_bytes(b"#" * INPUT_MAX_FILE_BYTES)
    assert len(read_schema_file(repo, use_openat2=use_openat2)) == INPUT_MAX_FILE_BYTES
    (repo / "devgraph.schema.yaml").write_bytes(b"#" * (INPUT_MAX_FILE_BYTES + 1))
    _refused("input_cap", read_schema_file, repo, use_openat2=use_openat2)


def test_openat2_unavailable_falls_back(repo, monkeypatch):
    import errno

    def no_openat2(*args, **kwargs):
        raise OSError(errno.ENOSYS, "no openat2")

    monkeypatch.setattr(reader, "_openat2", no_openat2)
    assert read_repo_file(repo, "real.txt", cap=100) == b"hello"
    (repo / "link").symlink_to(repo / "real.txt")
    _refused("input_unavailable", read_repo_file, repo, "link", cap=100)


@pytest.mark.parametrize("errno_name", ["ENOSYS", "EPERM"])
def test_default_fallback_follows_nothing(repo, monkeypatch, errno_name):
    """Where the kernel refuses openat2, the default path is still no-follow."""
    import errno

    def no_openat2(*args, **kwargs):
        code = getattr(errno, errno_name)
        raise OSError(code, os.strerror(code))

    outside = repo.parent / "outside"

    def swap_leaf(path):
        (repo / "real.txt").unlink()
        (repo / "real.txt").symlink_to(outside / "secret.txt")

    monkeypatch.setattr(reader, "_openat2", no_openat2)
    monkeypatch.setattr(reader, "_after_check", swap_leaf)
    _refused("input_unavailable", read_repo_file, repo, "real.txt", cap=100)

    def swap_dir(path):
        (repo / "realdir").rename(repo / "realdir.bak")
        (repo / "realdir").symlink_to(outside)

    (outside / "f.txt").write_bytes(b"TOP-SECRET")
    monkeypatch.setattr(reader, "_after_check", swap_dir)
    _refused("input_unavailable", read_repo_file, repo, "realdir/f.txt", cap=100)


def test_openat2_resolve_beneath_holds_without_the_path_check(repo, monkeypatch):
    """With `..` let through the lexical check, openat2 itself refuses the escape."""
    if not reader.openat2_supported():
        pytest.skip("openat2 is not available on this kernel")
    monkeypatch.setattr(reader, "_split", lambda rel: rel.split("/"))
    _refused(
        "input_unavailable",
        read_repo_file,
        repo,
        "../outside/secret.txt",
        cap=100,
        use_openat2=True,
    )


def test_root_must_be_canonical(repo, use_openat2):
    linked = repo.parent / "linked-repo"
    linked.symlink_to(repo)
    _refused(
        "input_unavailable",
        read_repo_file,
        linked,
        "real.txt",
        cap=100,
        use_openat2=use_openat2,
    )
    _refused(
        "input_unavailable",
        read_repo_file,
        repo / ".." / "repo",
        "real.txt",
        cap=100,
        use_openat2=use_openat2,
    )
    _refused(
        "input_unavailable",
        read_repo_file,
        Path("/"),
        "etc/hostname",
        cap=100,
        use_openat2=use_openat2,
    )
    _refused(
        "input_unavailable",
        read_repo_file,
        Path("relative"),
        "real.txt",
        cap=100,
        use_openat2=use_openat2,
    )


@pytest.mark.parametrize(
    "platform, machine, expected",
    [
        ("linux", "x86_64", True),
        ("linux", "aarch64", True),
        ("linux", "armv7l", True),
        ("linux", "riscv64", True),
        ("linux", "ppc64le", True),
        ("linux", "s390x", True),
        ("linux", "loongarch64", True),
        ("linux", "mips64", False),
        ("linux", "alpha", False),
        ("linux", "ia64", False),
        ("darwin", "arm64", False),
        ("win32", "AMD64", False),
    ],
)
def test_openat2_platform_gate(platform, machine, expected):
    assert reader.openat2_platform_ok(platform, machine) is expected


def test_unknown_platform_never_calls_openat2(repo, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("openat2 called on an unsupported platform")

    monkeypatch.setattr(reader, "_OPENAT2_PLATFORM", False)
    monkeypatch.setattr(reader, "_openat2", forbidden)
    assert not reader.openat2_supported()
    assert read_repo_file(repo, "real.txt", cap=100) == b"hello"
    (repo / "link").symlink_to("real.txt")
    _refused("input_unavailable", read_repo_file, repo, "link", cap=100)


def test_platform_without_no_follow_flags_refuses_every_read(repo, monkeypatch):
    monkeypatch.setattr(reader, "_NO_FOLLOW_OK", False)
    _refused("input_unavailable", read_repo_file, repo, "real.txt", cap=100)
