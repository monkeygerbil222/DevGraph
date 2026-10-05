"""The static scan in a limited subprocess (spec §4.1, §4.2, §5.3, §10.2).

The worker parses and tokenizes the script as data; nothing here runs it.
"""

import ast
import json
import subprocess
import sys
import time

import pytest

from devgraph.sandbox import scan
from devgraph.sandbox.display import script_for_review
from devgraph.sandbox.limits import (
    DENIED_NAMES,
    FIXED_PATH,
    INPUT_ALLOWLIST_MODULES,
    SCAN_OUTPUT_MAX_BYTES,
    SCAN_RLIMIT_AS_BYTES,
    SCAN_RLIMIT_CPU_SECONDS,
)
from devgraph.sandbox.reader import InputError
from devgraph.sandbox.scan import Finding, ScanResult, static_scan

DERIVE = "def derive(ctx):\n    return []\n"


def _rules(text: str) -> set[str]:
    return {finding.rule for finding in static_scan(text).findings}


def _reject(text: str = DERIVE, **kwargs) -> InputError:
    with pytest.raises(InputError) as info:
        static_scan(text, **kwargs)
    assert info.value.code == "static_reject"
    return info.value


def _probe(expression: str) -> str:
    """Worker source that reports `expression` as a finding's message."""
    return (
        "import json, os, resource, sys\n"
        "sys.stdin.read()\n"
        f"value = {expression}\n"
        'print(json.dumps({"findings": [{"rule": "probe", "line": 1, "message": json.dumps(value)}],'
        ' "literal_spans": []}))\n'
    )


# --- a clean script ---------------------------------------------------------


def test_clean_script_has_no_findings():
    text = (
        "import re\nimport json\nimport os.path as osp\nfrom os import path\nfrom os.path import join\n"
        "from collections.abc import Mapping\nfrom pathlib import PurePosixPath, PureWindowsPath\n"
        "import typing as t\n\n"
        "def derive(ctx):\n    while 1:\n        break\n    return [{'path': ctx.path}]\n"
    )
    result = static_scan(text)
    assert isinstance(result, ScanResult)
    assert result.findings == ()


# --- every failure is a reject, never a pass ---------------------------------


def test_scanner_crash_is_reject():
    # SyntaxError, located by line.
    error = _reject("def derive(ctx):\n    return (\n")
    assert "syntax" in error.reason.lower()
    # Deep nesting: RecursionError while building the AST.
    error = _reject("a" + ".b" * 200_000 + "\n" + DERIVE)
    assert "RecursionError" in error.reason
    # Worker killed by RLIMIT_AS: the same script passes under the real limit.
    big = DERIVE + "x = [" + "(1, 'a', [2.0])," * 20_000 + "]\n"
    # Lowered, the limit still lets the worker start and scan a small script.
    assert static_scan(big).findings == ()
    assert static_scan(DERIVE, memory_limit=48 * 1024 * 1024).findings == ()
    error = _reject(big, memory_limit=48 * 1024 * 1024)
    assert (
        "MemoryError" in error.reason
        or "signal" in error.reason
        or "status" in error.reason
    )
    # Worker stalled past the timeout: killed well before its sleep ends.
    started = time.monotonic()
    error = _reject(timeout=0.5, worker_source="import time\ntime.sleep(30)\n")
    assert time.monotonic() - started < 5
    assert "timed out" in error.reason
    # Non-zero exit, even with valid output.
    clean = json.dumps({"findings": [], "literal_spans": []})
    _reject(worker_source=f"import sys\nprint({clean!r})\nsys.exit(3)\n")
    # Malformed JSON.
    _reject(worker_source="print('{not json')\n")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n", id="signal"
        ),
        pytest.param(
            "import sys\nsys.stdout.write('x' * (2 * 1024 * 1024))\n",
            id="oversize-output",
        ),
        pytest.param("print('[' * 200000 + ']' * 200000)\n", id="deeply-nested-json"),
        pytest.param("print('')\n", id="empty-output"),
        pytest.param("print('[]')\n", id="not-an-object"),
        pytest.param(
            'print(\'{"error": "MemoryError"}\')\n', id="worker-reported-exception"
        ),
        pytest.param(
            'print(\'{"findings": [], "literal_spans": [], "x": 1}\')\n', id="extra-key"
        ),
        pytest.param(
            'print(\'{"findings": [{"rule": "x"}], "literal_spans": []}\')\n',
            id="bad-finding",
        ),
        pytest.param(
            'print(\'{"findings": [], "literal_spans": [[3, 2]]}\')\n',
            id="reversed-span",
        ),
        pytest.param(
            'print(\'{"findings": [], "literal_spans": [[0, 999]]}\')\n',
            id="span-past-end",
        ),
        pytest.param(
            'print(\'{"findings": [], "literal_spans": [[0, 5], [4, 6]]}\')\n',
            id="overlap",
        ),
        pytest.param(
            'print(\'{"findings": [], "literal_spans": [[true, 2]]}\')\n',
            id="bool-offset",
        ),
        pytest.param(
            'print(\'{"findings": [{"rule": "x", "line": 1, "message": "m", "y": 2}], "literal_spans": []}\')\n',
            id="extra-finding-key",
        ),
        pytest.param(
            'print(\'{"findings": [{"rule": "Bad Rule", "line": 1, "message": "m"}], "literal_spans": []}\')\n',
            id="bad-rule-name",
        ),
        pytest.param(
            'import json\nprint(json.dumps({"findings": [{"rule": "x", "line": 1, "message": "m" * 1001}],'
            ' "literal_spans": []}))\n',
            id="long-message",
        ),
    ],
)
def test_bad_worker_output_is_reject(source):
    _reject(worker_source=source)


def test_worker_reported_exception_is_named_generically():
    error = _reject(worker_source='print(\'{"error": "MemoryError"}\')\n')
    assert error.reason == "the static scan failed (MemoryError)"
    error = _reject(worker_source='print(\'{"error": "x\\\\u001b[2J"}\')\n')
    assert error.reason == "the static scan failed"


def test_worker_runs_isolated_with_limits_and_a_constructed_environment(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/nonexistent")
    monkeypatch.setenv("DEVGRAPH_SECRET", "x")
    expression = (
        '{"env": dict(os.environ), "flags": [sys.flags.isolated, sys.flags.no_site],'
        ' "as": resource.getrlimit(resource.RLIMIT_AS), "cpu": resource.getrlimit(resource.RLIMIT_CPU),'
        ' "core": resource.getrlimit(resource.RLIMIT_CORE), "fsize": resource.getrlimit(resource.RLIMIT_FSIZE),'
        ' "devgraph": "devgraph" in sys.modules,'
        ' "path": [p for p in sys.path if "site-packages" in p],'
        ' "archive": "locale-archive" in open("/proc/self/maps").read()}'
    )
    (finding,) = static_scan(DERIVE, worker_source=_probe(expression)).findings
    seen = json.loads(finding.message)
    assert seen["env"] == {"PATH": FIXED_PATH, "LC_ALL": "C"}
    assert seen["flags"] == [1, 1]
    assert seen["as"] == [SCAN_RLIMIT_AS_BYTES, SCAN_RLIMIT_AS_BYTES]
    assert seen["cpu"] == [SCAN_RLIMIT_CPU_SECONDS, SCAN_RLIMIT_CPU_SECONDS]
    assert seen["core"] == [0, 0]
    assert seen["fsize"] == [SCAN_OUTPUT_MAX_BYTES + 1, SCAN_OUTPUT_MAX_BYTES + 1]
    assert seen["devgraph"] is False and seen["path"] == []
    # The C locale maps no locale archive, so the 256 MiB budget is the parser's.
    assert seen["archive"] is False


def test_worker_command_line(monkeypatch):
    seen = {}
    real_popen = subprocess.Popen

    def spy(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return real_popen(args, **kwargs)

    monkeypatch.setattr(scan.subprocess, "Popen", spy)
    static_scan(DERIVE)
    assert seen["args"][:4] == [sys.executable, "-I", "-S", "-c"]
    source = seen["args"][4]
    assert source.endswith(scan.WORKER_SOURCE)
    # The limits are set inside the worker, before it reads the script: no
    # preexec_fn, so the scan is safe to start from a threaded parent.
    assert source.startswith("import resource\n")
    assert "preexec_fn" not in seen["kwargs"]


def test_worker_source_imports_only_the_standard_library():
    imported = set()
    for node in ast.walk(ast.parse(scan.WORKER_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"ast", "io", "json", "sys", "tokenize"}


# --- the rules ----------------------------------------------------------------

# One case per row of spec §4.1's escape table. `True` rows are caught; the
# `False` rows pin the spec's statement that the scan is hygiene, not a boundary.
ESCAPE_TABLE = [
    ("().__class__.__base__.__subclasses__()", {"dunder"}),
    ('getattr(ctx, "__glo" + "bals__")', set()),
    ('getattr(ctx, "__globals__")', {"dunder"}),  # a literal-string dunder is caught
    ("import typing\ntyping.sys.modules", set()),  # a module reached as an attribute
    ('"{0.__globals__}".format(ctx)', set()),
    ("(x for x in ()).gi_frame.f_back.f_globals", set()),
    ("import socket", {"import"}),
    ('__import__("socket")', {"denied_name", "dunder"}),
    ("exec('1')", {"denied_name"}),
    ("eval('1')", {"denied_name"}),
    ("compile('1', 'f', 'exec')", {"denied_name"}),
    ("while True:\n    pass", {"while_true"}),
    ("while 1:\n    pass", set()),
    ("def f():\n    return f()", set()),
    ("ｅｖａｌ('1')", {"denied_name"}),
]


@pytest.mark.parametrize(
    "snippet, expected", ESCAPE_TABLE, ids=[row[0][:40] for row in ESCAPE_TABLE]
)
def test_escape_table_rows(snippet, expected):
    assert _rules(snippet + "\n" + DERIVE) == expected


@pytest.mark.parametrize("name", DENIED_NAMES)
def test_denied_names(name):
    assert "denied_name" in _rules(f"x = {name}\n" + DERIVE)


@pytest.mark.parametrize(
    "line",
    [
        "import os",
        "import os as o",
        "from os import system",
        "from os import path, system",
        "import subprocess",
        "import socket.x",
        "import pathlib",
        "from pathlib import Path",
        "from pathlib import *",
        "from . import x",
        "from .x import y",
        "import collectionsx",
        "import jsonx.y",
        "from ctypes import CDLL",
        "import pathlib._local",
        "from pathlib._local import Path",
        "from pathlib._local import PurePath",
        "from os.path import os",
        "from os.path import sys",
        "from typing import sys",
        "from fnmatch import posixpath",
        "from collections import _collections_abc",
        "from re import copyreg",
        # `import a.b` binds `a`, so `a` itself must be allowlisted.
        "import os.path",
        "import os.path, re",
    ],
)
def test_imports_outside_the_allowlist(line):
    assert _rules(line + "\n" + DERIVE) == {"import"}


@pytest.mark.parametrize(
    "line",
    [
        "import re",
        "import os.path as p",
        "import collections.abc",
        "from os import path",
        "from os.path import join, splitext",
        "from collections.abc import Mapping",
        "import json.decoder",
        "from pathlib import PurePath, PurePosixPath",
        "from fnmatch import *",
        "from collections import abc",
        "from json import decoder",
        "from typing import Any",
    ],
)
def test_imports_inside_the_allowlist(line):
    assert _rules(line + "\n" + DERIVE) == set()


@pytest.mark.parametrize(
    "snippet",
    [
        "x = __name__",
        "ctx.__dict__",
        "x = __builtins__",
        "a.b.__class__",
        "match ctx:\n    case object(__class__=c):\n        pass",
        "match ctx:\n    case object(__globals__=g):\n        pass",
        "match ctx:\n    case __x__:\n        pass",
        "match ctx:\n    case [*__x__]:\n        pass",
        "match ctx:\n    case {**__x__}:\n        pass",
        "from json import __builtins__ as b",
        "from json import loads as __loads__",
        "import json as __j__",
        "hasattr(ctx, '__dict__')",
    ],
)
def test_dunders(snippet):
    assert _rules(snippet + "\n" + DERIVE) == {"dunder"}


@pytest.mark.parametrize(
    "snippet", ["x = _private", "x = __mangled", "x = a.__b", "x = ____"]
)
def test_not_dunders(snippet):
    assert _rules(snippet + "\n" + DERIVE) == set()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "def derive():\n    pass\n",
        "def derive(context):\n    pass\n",
        "def derive(ctx, extra):\n    pass\n",
        "def derive(ctx, *args):\n    pass\n",
        "def derive(ctx, **kw):\n    pass\n",
        "def derive(ctx, *, k=1):\n    pass\n",
        "def derive(ctx=None):\n    pass\n",
        "async def derive(ctx):\n    pass\n",
        "class A:\n    def derive(ctx):\n        pass\n",
        "if True:\n    def derive(ctx):\n        pass\n",
        "derive = lambda ctx: []\n",
    ],
)
def test_missing_derive(text):
    assert _rules(text) == {"no_derive"}


def test_positional_only_derive_is_accepted():
    assert _rules("def derive(ctx, /):\n    return []\n") == set()


def test_findings_carry_line_and_message():
    findings = static_scan(DERIVE + "import socket\nx = eval\n").findings
    assert findings == (
        Finding("import", 3, "import of 'socket' is not allowed"),
        Finding("denied_name", 4, "use of 'eval' is not allowed"),
    )


def test_long_import_name_is_a_finding_with_a_truncated_message():
    (finding,) = static_scan(DERIVE + "import " + "a" * 5000 + "\n").findings
    assert finding.rule == "import"
    assert len(finding.message) < 300


def test_grammar_is_pinned_to_the_feature_version():
    # PEP 695 `type` statements are 3.12 syntax; the scan parses as 3.11.
    error = _reject("type X = int\n" + DERIVE)
    assert "syntax error" in error.reason


def test_dotted_import_names_the_module_it_binds_and_the_fix():
    (finding,) = static_scan(DERIVE + "import os.path\n").findings
    assert finding == Finding(
        "import",
        3,
        "import of 'os.path' binds 'os', which is not allowed; "
        "use `import os.path as <name>` or `from os import path`",
    )


def test_every_allowlisted_dotted_module_is_checked_by_its_first_name():
    for module in INPUT_ALLOWLIST_MODULES:
        head = module.split(".")[0]
        rules = _rules(f"import {module}\n" + DERIVE)
        if module == "pathlib":
            assert rules == {"import"}  # only `from pathlib import Pure...`
        elif head in INPUT_ALLOWLIST_MODULES:
            assert rules == set(), module
        else:
            assert rules == {"import"}, module
        assert _rules(f"import {module} as m\n" + DERIVE) == (
            {"import"} if module == "pathlib" else set()
        ), module


def test_finding_message_escapes_non_ascii():
    (finding,) = static_scan(DERIVE + "import modulé\n").findings
    assert finding.message == "import of 'modul\\xe9' is not allowed"


# --- literal spans --------------------------------------------------------------


def _literals(text: str) -> list[str]:
    return [text[start:end] for start, end in static_scan(text).literal_spans]


def _merged_literals(text: str) -> list[str]:
    """Literal text with adjacent spans joined: how the tokenizer splits an
    f-string's text between tokens varies by version; what is covered must not."""
    merged: list[list[int]] = []
    for start, end in static_scan(text).literal_spans:
        if merged and merged[-1][1] == start:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [text[start:end] for start, end in merged]


def test_literal_spans_cover_strings_and_comments_in_code_points():
    text = (
        'é = "naïve"  # café ✓\nｘ = """a\nü"""\ny = b\'\\x00\' + rb"\\d"  # z\n'
        + DERIVE
    )
    assert _literals(text) == [
        '"naïve"',
        "# café ✓",
        '"""a\nü"""',
        "b'\\x00'",
        'rb"\\d"',
        "# z",
    ]


def test_literal_spans_cover_fstring_text_around_doubled_braces():
    text = 'x = f"é{{a}}b{ｅｖａｌ:>{w}}ü}}" + f"{{"\n' + DERIVE
    assert _merged_literals(text) == ["é{{a}}b", ">", "ü}}", "{{"]


def test_literal_spans_multiline_fstring():
    text = 'x = f"""é\n{y}\nü{{\n"""\n' + DERIVE
    assert _merged_literals(text) == ["é\n", "\nü{{\n"]


def test_literal_spans_are_sorted_and_disjoint():
    text = 'a = "x"  # c\nb = f"{\'s\'}t{{"  # d\n' + DERIVE
    spans = static_scan(text).literal_spans
    assert list(spans) == sorted(spans)
    assert all(end <= start for (_, end), (start, _) in zip(spans, spans[1:]))
    assert [text[s:e] for s, e in spans] == ['"x"', "# c", "'s'", "t{{", "# d"]


def test_approval_display_escapes_non_ascii_identifiers():
    """End to end: real spans from the worker drive the approval display."""
    text = (
        "# café ✓\n"
        "def derive(ctx):\n"
        '    x = ｅｖａｌ("naïve")\n'
        "    y = f\"ü{{ {ｅｖａｌ('é')} }}ü\"  # ok ✓\n"
        "    return []\n"
    )
    result = static_scan(text)
    assert {f.rule for f in result.findings} == {"denied_name"}
    shown = script_for_review(text, list(result.literal_spans))
    assert "ｅ" not in shown
    assert shown.count("\\uff45\\uff56\\uff41\\uff4c(") == 2
    assert shown.startswith("# café ✓\n")
    assert '("naïve")' in shown
    assert 'f"ü{{ ' in shown and "('é')} }}ü\"" in shown
    assert "# ok ✓" in shown
