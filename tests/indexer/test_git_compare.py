"""`compare.open_comparison`: refs, merge base and the changed-file list, on real temporary repositories."""

import os
import re
import sys
from pathlib import Path

import git as git_pkg
import pytest

from devgraph.indexer.git_history import compare
from devgraph.indexer.git_history.compare import CompareError, open_comparison
from tests.indexer.git_compare_helpers import (
    commit_files,
    git,
    record_git_commands,
    record_object_reads,
    two_branch_repo,
)

REPO_ID = "demo"
SHALLOW = "; this is a shallow clone, so older commits may be missing: git fetch --unshallow"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks, exec bits and kill_after_timeout are POSIX")


def unknown(arg, ref):
    return (
        f"{arg} {ref!r} is not a branch, tag or commit in repository {REPO_ID!r}; "
        "refs must exist locally (DevGraph never fetches)"
    )


def changes_of(repo, base="main", head="feature", **kwargs):
    with open_comparison(repo, REPO_ID, base, head, **kwargs) as cmp:
        return [(c.path, c.status) for c in cmp.changes], cmp


def subcommand(command):
    return next(part for part in command[1:] if not part.startswith("-"))


def test_added_removed_modified_and_nested_paths(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a.py": "a = 1\n", "pkg/b.py": "b = 1\n", "pkg/sub/c.txt": "c\n", "d.md": "# d\n"},
        {"pkg/b.py": "b = 2\n", "d.md": None, "pkg/sub/new.go": "package sub\n", "e/f/g.rs": "fn g() {}\n"},
    )
    changes, cmp = changes_of(repo)
    assert changes == [
        ("d.md", "removed"),
        ("e/f/g.rs", "added"),
        ("pkg/b.py", "modified"),
        ("pkg/sub/new.go", "added"),
    ]
    assert cmp.merge_base.hexsha == git(repo, "rev-parse", "main")
    assert cmp.base_commit.hexsha == git(repo, "rev-parse", "main")
    assert cmp.head_commit.hexsha == git(repo, "rev-parse", "feature")
    assert cmp.truncated_reasons == []
    modified = next(c for c in cmp.changes if c.status == "modified")
    assert modified.kind == "blob" and modified.base_blob is not None and modified.head_blob is not None
    assert next(c for c in cmp.changes if c.status == "added").base_blob is None
    assert next(c for c in cmp.changes if c.status == "removed").head_blob is None


def test_changes_on_base_after_branch_point_are_not_reported(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a.py": "a = 1\n", "b.py": "b = 1\n"},
        {"b.py": "b = 2\n"},
        main_after={"a.py": "a = 2\n", "only_main.py": "x = 1\n"},
    )
    changes, _ = changes_of(repo)
    assert changes == [("b.py", "modified")]


def test_merged_head_is_empty(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    changes, cmp = changes_of(repo, base="feature", head="main")
    assert changes == []
    assert cmp.merge_base.hexsha == git(repo, "rev-parse", "feature")
    changes, _ = changes_of(repo, base="feature", head="feature")
    assert changes == []


def test_refs_of_every_shape_resolve(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    commit_files(repo, {"c.py": "c = 1\n"}, "second on main")
    git(repo, "tag", "v1.0", "main")
    git(repo, "branch", "fix#123", "feature")
    full = git(repo, "rev-parse", "feature")
    shapes = {
        "v1.0": git(repo, "rev-parse", "v1.0"),
        full: full,
        full[:7]: full,
        "main~1": git(repo, "rev-parse", "main~1"),
        "HEAD": git(repo, "rev-parse", "HEAD"),
        "refs/heads/feature": full,
        "fix#123": full,
    }
    for ref, sha in shapes.items():
        with open_comparison(repo, REPO_ID, "main", ref) as cmp:
            assert cmp.head_commit.hexsha == sha, ref
            assert cmp.head_ref == ref


def test_exact_rename_and_edited_rename(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"x/old.py": "def kept():\n    return 1\n", "m.py": "m = 1\n", "e1.txt": "", "e2.txt": ""},
        {
            "x/old.py": None,
            "y/new.py": "def kept():\n    return 1\n",
            "m.py": None,
            "n.py": "m = 2\n",
            "e1.txt": None,
            "e2.txt": None,
            "e3.txt": "",
            "e4.txt": "",
        },
    )
    changes, cmp = changes_of(repo)
    assert changes == [
        ("e1.txt", "removed"),
        ("e2.txt", "removed"),
        ("e3.txt", "added"),
        ("e4.txt", "added"),
        ("m.py", "removed"),
        ("n.py", "added"),
        ("y/new.py", "renamed"),
    ]
    renamed = cmp.changes[-1]
    assert renamed.old_path == "x/old.py"
    assert renamed.base_blob.hexsha == renamed.head_blob.hexsha
    assert all(c.old_path is None for c in cmp.changes[:-1])


def test_rename_pairing_is_in_sorted_path_order(tmp_path):
    repo = two_branch_repo(
        tmp_path,
        {"a1.py": "same\n", "a2.py": "same\n", "a3.py": "same\n"},
        {"a1.py": None, "a2.py": None, "a3.py": None, "b1.py": "same\n", "b2.py": "same\n"},
    )
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        got = [(c.path, c.status, c.old_path) for c in cmp.changes]
    assert got == [("a3.py", "removed", None), ("b1.py", "renamed", "a1.py"), ("b2.py", "renamed", "a2.py")]


def test_tree_blob_swap(tmp_path):
    repo = two_branch_repo(tmp_path, {"thing": "a file\n"}, {"thing": None, "thing/inner.py": "x = 1\n"})
    changes, _ = changes_of(repo)
    assert changes == [("thing", "removed"), ("thing/inner.py", "added")]
    changes, _ = changes_of(repo, base="feature", head="main")
    assert changes == []  # main is the merge base: nothing on the head side
    git(repo, "checkout", "-q", "-b", "back", "feature")
    Path(repo, "thing", "inner.py").unlink()
    Path(repo, "thing").rmdir()
    commit_files(repo, {"thing": "a file again\n"}, "file back")
    changes, _ = changes_of(repo, base="feature", head="back")
    assert changes == [("thing", "added"), ("thing/inner.py", "removed")]


@posix_only
def test_mode_only_change_is_not_reported(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "checkout", "-q", "feature")
    Path(repo, "a.py").chmod(0o755)  # the working file too, so checking out main again is clean
    git(repo, "update-index", "--chmod=+x", "a.py")
    git(repo, "commit", "-q", "-m", "exec bit")
    git(repo, "checkout", "-q", "main")
    assert git(repo, "diff", "--name-only", "main", "feature").splitlines() == ["a.py", "b.py"]
    changes, _ = changes_of(repo)
    assert changes == [("b.py", "added")]


@posix_only
def test_symlink_and_submodule_are_listed_not_opened(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    gitlink = "1234567890abcdef1234567890abcdef12345678"
    git(repo, "checkout", "-q", "feature")
    os.symlink("a.py", Path(repo, "link"))
    git(repo, "add", "link")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},vendor/sub")
    git(repo, "commit", "-q", "-m", "link and submodule")
    git(repo, "checkout", "-q", "main")
    link_blob = git(repo, "rev-parse", "feature:link")
    streamed, infoed = record_object_reads(monkeypatch)
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        got = {c.path: (c.status, c.kind) for c in cmp.changes}
    assert got == {"b.py": ("added", "blob"), "link": ("added", "symlink"), "vendor/sub": ("added", "submodule")}
    assert gitlink not in streamed and gitlink not in infoed
    assert link_blob not in streamed


def test_only_cat_file_and_merge_base_run(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n", "d/x.py": "x\n"}, {"a.py": "a = 2\n", "d/y.py": "y\n"})
    calls = record_git_commands(monkeypatch)
    changes, _ = changes_of(repo)
    assert changes == [("a.py", "modified"), ("d/y.py", "added")]
    assert {subcommand(c) for c in calls} <= {"cat-file", "merge-base"}
    merge_bases = [c for c in calls if subcommand(c) == "merge-base"]
    assert len(merge_bases) == 1
    assert merge_bases[0][-3] == "merge-base"
    assert all(re.fullmatch("[0-9a-f]{40}", sha) for sha in merge_bases[0][-2:])


INVALID = [
    ("-h", "it starts with '-'"),
    ("--output=x", "it starts with '-'"),
    ("a..b", "it contains '..'"),
    ("HEAD:secret.txt", "it contains ':'"),
    ("../../x", "it contains '..'"),
    ("", "it is empty or longer than 256 characters"),
    ("a" * 300, "it is empty or longer than 256 characters"),
    ("ma in", "it contains whitespace or a control character"),
    ("a\tb", "it contains whitespace or a control character"),
    ("a\x07b", "it contains whitespace or a control character"),
    ("x*", "it contains one of * ? [ \\"),
    ("x?", "it contains one of * ? [ \\"),
    ("x[1]", "it contains one of * ? [ \\"),
    ("a\\b", "it contains one of * ? [ \\"),
    ("/abs", "it starts with '/'"),
]
REFLOG = "reflog and upstream forms like @{...} aren't supported; pass a branch name, tag or commit SHA"
REFLOG_FORMS = ["HEAD@{1}", "@{-9}", "@{-1}", "@{upstream}", "feature@{u}", "main@{yesterday}", "HEAD@{}", "HEAD@{99}"]


@pytest.mark.parametrize(
    ("ref", "rule"), INVALID + [(form, REFLOG) for form in REFLOG_FORMS], ids=lambda v: repr(v)[:20]
)
def test_invalid_refs_are_rejected_before_git(tmp_path, monkeypatch, ref, rule):
    monkeypatch.setattr(compare.git, "Repo", lambda *a, **k: pytest.fail("the repository was opened"))
    with pytest.raises(CompareError) as err:
        with open_comparison(tmp_path, REPO_ID, ref, "main"):
            pass
    assert type(err.value) is CompareError
    assert str(err.value) == f"branch_a {ref[:100]!r} is not a valid ref: {rule}"


def test_valid_unusual_names_pass_validation():
    for ref in ("fix#123", "feature/ünïcode", "v1.2+build", "a.b", "x@y", "x{1}"):
        compare.validate_ref("branch_a", ref)


def test_unknown_ref(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="nosuch")
    assert str(err.value) == unknown("branch_b", "nosuch")
    with pytest.raises(CompareError) as err:
        changes_of(repo, base="nosuch")
    assert str(err.value) == unknown("branch_a", "nosuch")


@pytest.mark.parametrize("ref", ["HEAD^{tree}", "HEAD~99", "deadbeef", "refs/heads/nosuch"])
def test_unresolvable_refs_are_unknown(tmp_path, ref):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    with pytest.raises(CompareError) as err:
        changes_of(repo, head=ref)
    assert type(err.value) is CompareError
    assert str(err.value) == unknown("branch_b", ref)


def test_all_zero_sha_is_unknown(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    assert Path(repo, ".git", "logs", "HEAD").read_text().startswith("0" * 40)
    calls = record_git_commands(monkeypatch)
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="logs/HEAD")
    assert str(err.value) == unknown("branch_b", "logs/HEAD")
    assert "merge-base" not in {subcommand(c) for c in calls}


def _fail_merge_base(monkeypatch, stderr):
    real = git_pkg.cmd.Git.execute

    def execute(self, command, *args, **kwargs):
        if subcommand(command) == "merge-base":
            raise git_pkg.GitCommandError(["git", "merge-base"], -9, stderr=stderr)
        return real(self, command, *args, **kwargs)

    monkeypatch.setattr(git_pkg.cmd.Git, "execute", execute)


def test_merge_base_failures(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    _fail_merge_base(monkeypatch, 'Timeout: the command "git merge-base" did not complete in 1 secs.')
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "compare_branches timed out finding the merge base of 'main' and 'feature'"
    _fail_merge_base(monkeypatch, "fatal: something")
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "git failed while comparing 'main' and 'feature' in repository 'demo': GitCommandError"


def test_git_missing(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})

    def execute(self, command, *args, **kwargs):
        raise git_pkg.GitCommandNotFound("git", "not found")

    monkeypatch.setattr(git_pkg.cmd.Git, "execute", execute)
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert type(err.value) is CompareError
    assert str(err.value) == (
        "git failed while comparing 'main' and 'feature' in repository 'demo': GitCommandNotFound"
    )


def test_cat_file_failure(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"d/a.py": "a = 1\n"}, {"d/b.py": "b = 1\n"})
    head_tree = git(repo, "rev-parse", "feature^{tree}")
    real_stream = git_pkg.db.GitCmdObjectDB.stream

    def stream(self, binsha):
        if binsha.hex() == head_tree:
            raise OSError("cat-file died")
        return real_stream(self, binsha)

    closed = []
    real_close = git_pkg.Repo.close

    def close(self):
        closed.append(self)
        return real_close(self)

    monkeypatch.setattr(git_pkg.db.GitCmdObjectDB, "stream", stream)
    monkeypatch.setattr(git_pkg.Repo, "close", close)
    with pytest.raises(CompareError) as err:
        changes_of(repo)
    assert str(err.value) == "git failed while comparing 'main' and 'feature' in repository 'demo': OSError"
    assert len(closed) == 1


def test_repo_is_closed_after_the_block(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    closed = []
    real_close = git_pkg.Repo.close
    monkeypatch.setattr(git_pkg.Repo, "close", lambda self: (closed.append(self), real_close(self))[1])
    with open_comparison(repo, REPO_ID, "main", "feature") as cmp:
        assert cmp.changes[0].head_blob.data_stream.read() == b"b = 1\n"
        assert closed == []
    assert closed == [cmp.repo]


def test_not_a_git_repository(tmp_path):
    message = (
        "repository 'demo' is not a git repository at its registered root; "
        "compare_branches needs the repository's own .git"
    )
    plain = Path(tmp_path, "plain")
    plain.mkdir()
    for path in (plain, Path(tmp_path, "missing")):
        with pytest.raises(CompareError) as err:
            changes_of(path)
        assert str(err.value) == message


def test_ref_naming_a_tree(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "tag", "treetag", git(repo, "rev-parse", "main^{tree}"))
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="treetag")
    assert str(err.value) == unknown("branch_b", "treetag")


def test_shallow_clone_without_base(tmp_path):
    src = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    dst = Path(tmp_path, "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "feature", f"file://{src}", str(dst))
    with pytest.raises(CompareError) as err:
        changes_of(dst)
    assert str(err.value) == unknown("branch_a", "main") + SHALLOW


def test_shallow_clone_without_common_history(tmp_path):
    src = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"}, main_after={"c.py": "c = 1\n"})
    dst = Path(tmp_path, "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", "--no-single-branch", f"file://{src}", str(dst))
    with pytest.raises(CompareError) as err:
        changes_of(dst, base="origin/main", head="origin/feature")
    assert str(err.value) == "'origin/main' and 'origin/feature' share no history in repository 'demo'" + SHALLOW


def test_unrelated_histories(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    git(repo, "checkout", "-q", "--orphan", "island")
    commit_files(repo, {"z.py": "z = 1\n"}, "island")
    with pytest.raises(CompareError) as err:
        changes_of(repo, head="island")
    assert str(err.value) == "'main' and 'island' share no history in repository 'demo'"


def test_diff_entry_cap_and_deadline(tmp_path, monkeypatch):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {f"n{i}.py": f"n = {i}\n" for i in range(5)})
    monkeypatch.setattr(compare, "_COMPARE_MAX_DIFF_ENTRIES", 3)
    changes, cmp = changes_of(repo)
    assert len(changes) == 3
    assert cmp.truncated_reasons == ["diff_entries"]
    monkeypatch.undo()

    def clock_from(*values):
        ticks = iter(values)
        last = [0.0]

        def clock():
            last[0] = next(ticks, last[0])
            return last[0]

        return clock

    changes, cmp = changes_of(repo, clock=clock_from(0.0, compare._COMPARE_DEADLINE_S + 1))
    assert changes == []
    assert cmp.truncated_reasons == ["deadline"]

    if sys.platform != "win32":
        calls = record_git_commands(monkeypatch)
        changes, cmp = changes_of(repo, clock=clock_from(0.0, compare._COMPARE_DEADLINE_S - 0.2))
        assert len(changes) == 5 and cmp.truncated_reasons == []
        index = next(i for i, c in enumerate(calls) if subcommand(c) == "merge-base")
        assert calls.kwargs[index]["kill_after_timeout"] == 1
