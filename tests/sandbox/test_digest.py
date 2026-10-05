"""The provider digest (spec §5.2, §10.2)."""

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from devgraph.config.project_schema import parse_project_schema
from devgraph.sandbox.digest import canonical_json, provider_digest, short_digest
from devgraph.sandbox.reader import InputError
from devgraph.sandbox.source import normalise_script

SCHEMA = """
    version: 1
    custom_providers:
      - name: runbook_links
        inputs: ["docs/runbooks/**/*.md"]
        params: {owner_prefix: "team-"}
      - {name: other, inputs: ["*.txt"], params: {k: 1}}
    node_types:
      - label: Runbook
        key: [slug]
        metadata: [{name: slug, required: true}, {name: owner}]
        source: {provider: custom, name: runbook_links}
      - label: Note
        key: [slug]
        metadata: [{name: slug}]
        source: {provider: custom, name: other}
      - label: Gadget
        key: [code]
        metadata: [{name: code}]
    relationships:
      - type: DOCUMENTS
        provider: custom
        custom: {name: runbook_links, params: {depth: 2}}
        from: Runbook
        to: Service
      - type: CITES
        provider: custom
        custom: {name: other}
        from: Note
        to: Service
"""

SCRIPT = "def derive(ctx):\n    return []\n"


def _declaration(text: str = SCHEMA, name: str = "runbook_links") -> dict:
    schema = parse_project_schema(textwrap.dedent(text), Path("devgraph.schema.yaml"))
    return schema.custom_declaration_set(name)


def _digest(text: str = SCHEMA, script: str = SCRIPT) -> str:
    return provider_digest("runbook_links", _declaration(text), script)


def test_golden_vector_pins_the_encoding():
    expected = (
        b"devgraph-script-trust\x00"
        + b"\x01"
        + (1).to_bytes(8, "big") + b"p"
        + (7).to_bytes(8, "big") + b'{"a":1}'
        + (2).to_bytes(8, "big") + b"x\n"
    )
    assert hashlib.sha256(expected).hexdigest() == (
        "7a3e60a476e86c83e2199bed4ed0c4ddc387090286fec58ead1cb5388c802d46"
    )
    assert provider_digest("p", {"a": 1}, "x\n") == (
        "7a3e60a476e86c83e2199bed4ed0c4ddc387090286fec58ead1cb5388c802d46"
    )


def test_canonical_json_form():
    obj = {"b": [3, {"z": 1, "y": "é"}], "a": None, "c": 1.5}
    assert canonical_json(obj) == b'{"a":null,"b":[3,{"y":"\\u00e9","z":1}],"c":1.5}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_is_static_reject(value):
    with pytest.raises(InputError) as info:
        canonical_json({"params": {"x": value}})
    assert info.value.code == "static_reject"
    with pytest.raises(InputError):
        provider_digest("p", {"x": value}, "x\n")


def test_digest_stability_and_sensitivity():
    base = _digest()
    assert len(base) == 64 and set(base) <= set("0123456789abcdef")
    assert short_digest(base) == base[:12]
    assert _digest() == base

    # The same inputs give the same digest in another process with another hash seed.
    code = (
        "import json, sys\n"
        "from devgraph.sandbox.digest import provider_digest\n"
        "args = json.loads(sys.stdin.read())\n"
        "print(provider_digest(*args))\n"
    )
    for seed in ("1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(["runbook_links", _declaration(), SCRIPT]),
            capture_output=True, text=True, env=env, check=True,
        )
        assert out.stdout.strip() == base

    # Each of these changes the digest.
    changed = {
        "provider name": provider_digest("runbook_linkz", _declaration(), SCRIPT),
        "one script character": _digest(script=SCRIPT.replace("[]", "[ ]")),
        "inputs": _digest(SCHEMA.replace("docs/runbooks/**/*.md", "docs/runbooks/*.md")),
        "provider param": _digest(SCHEMA.replace('"team-"', '"team_"')),
        "relationship custom.params": _digest(SCHEMA.replace("depth: 2", "depth: 3")),
        "own node type": _digest(SCHEMA.replace("{name: owner}", "{name: owners}")),
        "own relationship": _digest(SCHEMA.replace("to: Service\n      - type: CITES", "to: Module\n      - type: CITES")),
    }
    for what, digest in changed.items():
        assert digest != base, what
    assert len(set(changed.values())) == len(changed)

    # CRLF vs LF changes the digest of raw text, but not after normalisation, by design.
    crlf = SCRIPT.replace("\n", "\r\n")
    assert _digest(script=crlf) != base
    assert _digest(script=normalise_script(crlf.encode())) == base

    # Another provider's entries and a non-custom node type do not change it.
    unchanged = {
        "other provider param": SCHEMA.replace("params: {k: 1}", "params: {k: 2}"),
        "other provider node type": SCHEMA.replace("metadata: [{name: slug}]\n        source: {provider: custom, name: other}",
                                                   "metadata: [{name: slug}, {name: x}]\n        source: {provider: custom, name: other}"),
        "other provider relationship": SCHEMA.replace(
            "        from: Note\n        to: Service\n", "        from: Note\n        to: Module\n"),
        "non-custom node type": SCHEMA.replace("metadata: [{name: code}]", "metadata: [{name: code}, {name: size}]"),
    }
    for what, text in unchanged.items():
        assert text != SCHEMA, what
        assert _digest(text) == base, what


def test_moving_bytes_between_fields_cannot_collide():
    declaration = _declaration()
    assert provider_digest("ab", declaration, "c") != provider_digest("a", declaration, "bc")
    assert provider_digest("ab", {}, "c") != provider_digest("a", {}, "bc")
    assert provider_digest("p", {}, "") != provider_digest("p", {}, "\x00")
