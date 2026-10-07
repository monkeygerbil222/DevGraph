"""A seeded fuzz of the graph accuracy work against a live Neo4j: random edits,
deletes, restores, renames and symbol moves over a small multi-language
repository, indexed incrementally as one batch or as one batch per path, must
leave the same graph as a fresh `full_scan` after every step.

See docs/superpowers/specs/2026-10-08-graph-accuracy-design.md. CI runs three
fixed seeds of 15 steps. `DEVGRAPH_ACCURACY_FUZZ_SEEDS` (a comma list) and
`DEVGRAPH_ACCURACY_FUZZ_STEPS` lengthen it locally.

The spec's "Out of scope" gaps are left out by construction:
- compose services have `image:` only, never `build:`;
- no datastore or route code (so no handler stub either);
- no method moves out of its type's file: only free functions move, and the
  Go method and Rust inherent `impl` stay with their type;
- no C++, and no C#/TS partial classes;
- the default `mentions_ambiguous_mode` (`all`);
- a docs note's `id` never changes in place;
- a renamed docs note's old path is removed before its new path is indexed,
  when they go in separate batches (the id moving to another file first
  leaves the old path in its edges' `origins`).
"""

import os
import random
import uuid
from dataclasses import dataclass, field, replace

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

SEEDS = [int(s) for s in os.environ.get("DEVGRAPH_ACCURACY_FUZZ_SEEDS", "1,2,3").split(",") if s.strip()]
STEPS = int(os.environ.get("DEVGRAPH_ACCURACY_FUZZ_STEPS", "15"))
DOCS = "docs"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-accfuzz-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


# --- the repository model --------------------------------------------------------


@dataclass
class File:
    """One file's content, as the fields the fuzz edits; `render` writes it."""

    kind: str  # py, rs, go, java, compose, docker, notes, note, static
    head: str = ""  # Go/Java package, a note's id, or a static file's text
    extra: str = ""  # fixed code that never changes (a type and its methods)
    imports: list = field(default_factory=list)
    bases: dict = field(default_factory=dict)  # class -> base or None
    funcs: dict = field(default_factory=dict)  # function -> callees
    impl: bool | None = None  # rs: `impl Display for Foo {}`, when the file has the toggle
    services: dict = field(default_factory=dict)  # compose: service -> image
    froms: list = field(default_factory=list)
    mentions: list = field(default_factory=list)
    links: list = field(default_factory=list)
    supersedes: str | None = None

    def copy(self):
        return replace(
            self, imports=list(self.imports), bases=dict(self.bases),
            funcs={k: list(v) for k, v in self.funcs.items()}, services=dict(self.services),
            froms=list(self.froms), mentions=list(self.mentions), links=list(self.links),
        )


def render(f: File) -> str:
    if f.kind == "py":
        parts = [f"import {m}\n" for m in f.imports]
        parts += [f"\n\nclass {c}({b}):\n    pass\n" if b else f"\n\nclass {c}:\n    pass\n" for c, b in f.bases.items()]
        parts += [
            f"\n\ndef {name}():\n" + "".join(f"    {g}()\n" for g in calls) + "    return 0\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts) or "X = 1\n"
    if f.kind == "rs":
        parts = [f"use crate::{m};\n" for m in f.imports]
        parts.append("\n" + f.extra)
        if f.impl:
            parts.append("\nimpl Display for Foo {}\n")
        parts += [
            f"\npub fn {name}() {{\n" + "".join(f"    {g}();\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
    if f.kind == "go":
        parts = [f"package {f.head}\n"]
        if f.imports:
            parts.append("\nimport (\n" + "".join(f'\t"{m}"\n' for m in f.imports) + ")\n")
        parts.append("\n" + f.extra)
        parts += [
            f"\nfunc {name}() {{\n" + "".join(f"\t{g}()\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
    if f.kind == "java":
        ((cls, base),) = f.bases.items()
        parts = [f"package {f.head};\n\n"] + [f"import {m};\n" for m in f.imports]
        parts.append(f"\npublic class {cls}" + (f" extends {base}" if base else "") + " {\n")
        parts += [
            f"    void {name}() {{\n" + "".join(f"        {g}();\n" for g in calls) + "    }\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts) + "}\n"
    if f.kind == "compose":
        return "services:\n" + "".join(f"  {s}:\n    image: {i}\n" for s, i in f.services.items()) if f.services \
            else "services: {}\n"
    if f.kind == "docker":
        return "".join(f"FROM {i}\nRUN true\n" for i in f.froms) or "# No stages.\n"
    if f.kind == "notes":
        return "# Notes\n\n" + "".join(f"Uses `{m}`.\n" for m in f.mentions)
    if f.kind == "note":
        front = [f"type: design_decision", f"id: {f.head}"]
        if f.links:
            front.append("links: [" + ", ".join(f.links) + "]")
        if f.supersedes:
            front.append(f"supersedes: {f.supersedes}")
        return "---\n" + "\n".join(front) + "\n---\n# Note\n"
    return f.head


RS_FOO = "pub struct Foo;\n\nimpl Foo {\n    pub fn new() -> Foo {\n        Foo\n    }\n}\n"
GO_SERVER = "type Server struct{}\n\nfunc (s *Server) Run() {\n\tServe()\n}\n"


def initial_repo() -> dict:
    return {
        "py/a.py": File("py", imports=["py.b"], funcs={"main": ["helper", "util"]}),
        "py/b.py": File("py", bases={"Base": None}, funcs={"helper": ["util"]}),
        "py/c.py": File("py", imports=["py.b"], bases={"Child": "Base"}, funcs={"main": [], "util": []}),
        "rs/foo.rs": File("rs", extra=RS_FOO, funcs={"parse": ["render"]}),
        "rs/display.rs": File("rs", extra="pub trait Display {}\n", funcs={"render": []}),
        "rs/conv.rs": File("rs", imports=["display::Display"], impl=True, funcs={"convert": ["parse"]}),
        "java/app/K.java": File("java", head="app", imports=["base.Base"], bases={"K": "Base"},
                                funcs={"run": ["helper", "assist"]}),
        "java/base/Base.java": File("java", head="base", bases={"Base": None}, funcs={"assist": []}),
        "go/go.mod": File("static", head="module example.com/fz\n\ngo 1.22\n"),
        "go/a/a.go": File("go", head="a", imports=["example.com/fz/b"], funcs={"Start": ["Serve"]}),
        "go/b/b.go": File("go", head="b", extra=GO_SERVER, funcs={"Serve": [], "Stop": []}),
        "compose.yaml": File("compose", services={"api": "python:3.12", "web": "nginx:1.27"}),
        "Dockerfile": File("docker", froms=["python:3.12"]),
        "notes.md": File("notes", mentions=["helper", "Foo", "Base", "K", "Serve", "api", "python"]),
        "docs/adr-1.md": File("note", head="ADR-1", links=["py/a.py"]),
        "docs/adr-2.md": File("note", head="ADR-2", supersedes="ADR-1"),
    }


IMPORTS = {
    "py": ["py.a", "py.b", "py.c"],
    "rs": ["display::Display", "foo::Foo"],
    "go": ["example.com/fz/a", "example.com/fz/b"],
    "java": ["base.Base", "app.K"],
}
BASES = {"py": ["Base", "Child", "Foo", "Missing"], "java": ["Base", "K"]}
SERVICES = ["api", "web", "worker", "db"]
IMAGES = ["python:3.12", "nginx:1.27", "postgres:16", "alpine:3.20"]
EXTRA_NAMES = ["Foo", "Display", "Server", "K", "Child", "python", "postgres", "py/b.py", "missing"]
CODE = ("py", "rs", "go", "java")
MOVABLE = ("py", "rs", "go")  # free functions only: a Java method stays in its class


class Fuzz:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.files = initial_repo()
        self.deleted: list[tuple[str, File]] = []
        self.counter = 0
        # (old, new) paths of the docs notes renamed in the current step.
        self.moved_notes: list[tuple[str, str]] = []

    def _fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def _pick(self, pred):
        paths = sorted(p for p, f in self.files.items() if pred(f))
        return self.rng.choice(paths) if paths else None

    def _func_names(self) -> list:
        return sorted({n for f in self.files.values() for n in f.funcs} | {"Run", "new", "missing_fn"})

    # Each operation returns (description, touched paths), or None when it can't apply.

    def op_call(self):
        path = self._pick(lambda f: f.kind in CODE and f.funcs)
        if not path:
            return None
        fn = self.rng.choice(sorted(self.files[path].funcs))
        calls = self.files[path].funcs[fn]
        if calls and self.rng.random() < 0.5:
            gone = calls.pop(self.rng.randrange(len(calls)))
            return f"remove call {fn}->{gone} in {path}", {path}
        new = self.rng.choice([n for n in self._func_names() if n not in calls])
        calls.append(new)
        return f"add call {fn}->{new} in {path}", {path}

    def op_base(self):
        path = self._pick(lambda f: f.kind in BASES and f.bases)
        if not path:
            return None
        f = self.files[path]
        cls = self.rng.choice(sorted(f.bases))
        if f.bases[cls]:
            f.bases[cls] = None
            return f"remove base of {cls} in {path}", {path}
        f.bases[cls] = self.rng.choice([b for b in BASES[f.kind] if b != cls])
        return f"set base {cls}({f.bases[cls]}) in {path}", {path}

    def op_import(self):
        path = self._pick(lambda f: f.kind in IMPORTS)
        if not path:
            return None
        f = self.files[path]
        m = self.rng.choice(IMPORTS[f.kind])
        if m in f.imports:
            f.imports.remove(m)
            return f"remove import {m} in {path}", {path}
        f.imports.append(m)
        return f"add import {m} in {path}", {path}

    def op_impl(self):
        path = self._pick(lambda f: f.impl is not None)
        if not path:
            return None
        self.files[path].impl = not self.files[path].impl
        return f"{'add' if self.files[path].impl else 'remove'} impl Display for Foo in {path}", {path}

    def op_add_function(self):
        path = self._pick(lambda f: f.kind in CODE)
        if not path:
            return None
        name = self._fresh("fn")
        names = self._func_names()
        self.files[path].funcs[name] = self.rng.sample(names, self.rng.randrange(3))
        return f"add function {name} in {path}", {path}

    def op_remove_function(self):
        path = self._pick(lambda f: f.kind in CODE and f.funcs)
        if not path:
            return None
        name = self.rng.choice(sorted(self.files[path].funcs))
        del self.files[path].funcs[name]
        return f"remove function {name} from {path}", {path}

    def op_move_function(self):
        source = self._pick(lambda f: f.kind in MOVABLE and f.funcs)
        if not source:
            return None
        lang = self.files[source].kind
        name = self.rng.choice(sorted(self.files[source].funcs))
        target = self._pick(lambda f: f.kind == lang and f is not self.files[source] and name not in f.funcs)
        if not target:
            return None
        self.files[target].funcs[name] = self.files[source].funcs.pop(name)
        return f"move function {name} from {source} to {target}", {source, target}

    def op_rename(self):
        path = self._pick(lambda f: f.kind not in ("compose", "docker", "static"))
        if not path:
            return None
        stem, dot, ext = path.rpartition(".")
        new = f"{stem}_{self._fresh('r')}{dot}{ext}"
        self.files[new] = self.files.pop(path)
        if self.files[new].kind == "note":
            self.moved_notes.append((path, new))
        return f"rename {path} to {new}", {path, new}

    def op_delete(self):
        path = self._pick(lambda f: True)
        if not path:
            return None
        self.deleted.append((path, self.files.pop(path)))
        return f"delete {path}", {path}

    def op_restore(self):
        if not self.deleted:
            return None
        path, f = self.deleted.pop()
        self.files[path] = f
        return f"restore {path}", {path}

    def op_service(self):
        path = self._pick(lambda f: f.kind == "compose")
        if not path:
            return None
        services = self.files[path].services
        name = self.rng.choice(SERVICES)
        if name in services:
            del services[name]
            return f"remove service {name} in {path}", {path}
        services[name] = self.rng.choice(IMAGES)
        return f"add service {name} ({services[name]}) in {path}", {path}

    def op_from(self):
        path = self._pick(lambda f: f.kind == "docker")
        if not path:
            return None
        froms = self.files[path].froms
        image = self.rng.choice(IMAGES)
        if image in froms:
            froms.remove(image)
            return f"remove FROM {image} in {path}", {path}
        froms.append(image)
        return f"add FROM {image} in {path}", {path}

    def op_mention(self):
        path = self._pick(lambda f: f.kind == "notes")
        if not path:
            return None
        mentions = self.files[path].mentions
        name = self.rng.choice(self._func_names() + EXTRA_NAMES + SERVICES)
        if name in mentions:
            mentions.remove(name)
            return f"remove mention {name} in {path}", {path}
        mentions.append(name)
        return f"add mention {name} in {path}", {path}

    def op_link(self):
        path = self._pick(lambda f: f.kind == "note")
        if not path:
            return None
        links = self.files[path].links
        target = self.rng.choice(sorted({p for p, f in self.files.items() if f.kind in CODE} | {"py/b.py"}))
        if target in links:
            links.remove(target)
            return f"remove link {target} in {path}", {path}
        links.append(target)
        return f"add link {target} in {path}", {path}

    def op_supersedes(self):
        path = self._pick(lambda f: f.kind == "note")
        if not path:
            return None
        f = self.files[path]
        f.supersedes = None if f.supersedes else ("ADR-2" if f.head == "ADR-1" else "ADR-1")
        return f"{'set' if f.supersedes else 'remove'} supersedes in {path}", {path}

    OPS = [
        "call", "base", "import", "impl", "add_function", "remove_function", "move_function",
        "rename", "delete", "restore", "service", "from", "mention", "link", "supersedes",
    ]

    def draw(self):
        while True:
            done = getattr(self, f"op_{self.rng.choice(self.OPS)}")()
            if done:
                return done

    def write(self, root, touched):
        for rel in touched:
            path = root / rel
            if rel in self.files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render(self.files[rel]))
            elif path.exists():
                path.unlink()


def check(engine, repo_id, root, seed, step, log):
    expected = fresh_snapshot(engine, repo_id, root, mentions_enabled=True, docs_path=DOCS)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail(
            f"seed {seed}, step {step}: graph does not equal a fresh full_scan\noperations:\n"
            + "\n".join(log) + "\n" + snapshot_diff(expected, actual)
        )


@pytest.mark.parametrize("seed", SEEDS)
def test_graph_accuracy_fuzz(engine, repo_id, tmp_path, seed):
    rng = random.Random(seed)
    fuzz = Fuzz(rng)
    root = tmp_path
    fuzz.write(root, set(fuzz.files))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, docs_path=DOCS, mentions_enabled=True)
    log: list[str] = []
    check(engine, repo_id, root, seed, "initial", log)

    for step in range(STEPS):
        fuzz.moved_notes = []
        # The foreign-source edge (set (b)) is toggled on every seed.
        ops = [fuzz.op_impl()] if step == 0 else []
        ops += [fuzz.draw() for _ in range(rng.randint(1, 3))]
        touched = set().union(*(paths for _desc, paths in ops))
        fuzz.write(root, touched)
        gone = {rel for rel in touched if rel not in fuzz.files}
        changed = touched - gone
        split = rng.random() < 0.5
        log.append(f"step {step} ({'split' if split else 'one batch'}): " + "; ".join(d for d, _p in ops))
        if split:
            order = rng.sample(sorted(touched), len(touched))
            if fuzz.moved_notes:
                # A renamed note's old path goes first (see the module docstring).
                order.sort(key=lambda rel: not (rel in gone and rel.startswith(f"{DOCS}/")))
            for rel in order:
                if rel in gone:
                    remove_paths(engine, repo_id, root, {root / rel})
                else:
                    index_paths(engine, repo_id, root, {root / rel}, docs_path=DOCS, mentions_enabled=True)
        else:
            if gone:
                remove_paths(engine, repo_id, root, {root / rel for rel in gone})
            if changed:
                index_paths(engine, repo_id, root, {root / rel for rel in changed}, docs_path=DOCS,
                            mentions_enabled=True)
        check(engine, repo_id, root, seed, step, log)
