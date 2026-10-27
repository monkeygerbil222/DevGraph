"""JS/TS imports that reach a function under another name (audit 12 item 4):
a barrel imported by its index file, renamed and default re-exports, a
default-exported function imported under another name, and a destructured
dynamic import. Fixture code is fictional."""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file, export_surface
from devgraph.indexer.resolver_config import ResolverConfig

MATH = """
    export function add(a: number, b: number) { return a + b; }
    export const mul = (a: number, b: number) => a * b;
    export default function sub(a: number, b: number) { return a - b; }
"""
STRINGS = "export function upper(s: string) { return s.toUpperCase(); }\nexport function lower(s: string) { return s; }\n"
BARREL = """
    export * from './math';
    export { upper as shout } from './strings';
    export { default as subtract } from './math';
"""


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def lib(root):
    write(root, "src/lib/math.ts", MATH)
    write(root, "src/lib/strings.ts", STRINGS)
    write(root, "src/lib/index.ts", BARREL)


def targets(root, rel, source, callee, caller="f"):
    """(to_file, confidence) of the caller's CALLS rows to `callee`."""
    path = write(root, rel, source)
    result = extract_js_file(path.read_text(), rel, "repo", ResolverConfig(root))
    return {
        (r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS" and (r.from_name, r.to_name) == (caller, callee)
    }


def test_barrel_imported_by_index_file(tmp_path):
    lib(tmp_path)
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": '
          '{"@lib/*": ["src/lib/*"], "@lib": ["src/lib/index.ts"]}}}')
    for spec in ("./lib/index", "./lib/index.js", "@lib", "@lib/index"):
        got = targets(tmp_path, "src/a.ts", f"import {{ mul }} from '{spec}';\nexport function f() {{ mul(1, 2); }}\n",
                      "mul")
        assert ("src/lib/", "package") in got, spec
        assert ("src/lib/index/", "package") not in got, spec


def test_renamed_reexport_links_original(tmp_path):
    lib(tmp_path)
    source = "import { shout } from './lib';\nexport function f() { shout('x'); }\n"
    assert ("src/lib/strings.ts", "resolved") in targets(tmp_path, "src/a.ts", source, "upper")
    assert targets(tmp_path, "src/a.ts", source, "shout") == set()
    # Imported under yet another name, and by the index file.
    source = "import { shout as yell } from './lib/index';\nexport function f() { yell('x'); }\n"
    assert ("src/lib/strings.ts", "resolved") in targets(tmp_path, "src/a.ts", source, "upper")


def test_default_reexport_links_default_function(tmp_path):
    lib(tmp_path)
    source = "import { subtract } from './lib';\nexport function f() { subtract(1, 2); }\n"
    assert targets(tmp_path, "src/a.ts", source, "sub") == {("src/lib/math.ts", "resolved")}
    assert targets(tmp_path, "src/a.ts", source, "subtract") == set()
    # `export { default } from` keeps the default: a default import of it.
    write(tmp_path, "src/lib/minus.ts", "export { default } from './math';\n")
    source = "import minus from './lib/minus';\nexport function f() { minus(1, 2); }\n"
    assert targets(tmp_path, "src/a.ts", source, "sub") == {("src/lib/math.ts", "resolved")}


def test_default_import_of_function_under_other_name(tmp_path):
    lib(tmp_path)
    source = "import minus from './lib/math';\nexport function f() { minus(1, 2); }\n"
    assert targets(tmp_path, "src/a.ts", source, "sub") == {("src/lib/math.ts", "resolved")}
    assert targets(tmp_path, "src/a.ts", source, "minus") == set()
    # A component: `export default function Button`, `const Card = ...; export default Card`.
    write(tmp_path, "src/components/Button.tsx", "export default function Button() { return <button/>; }\n")
    write(tmp_path, "src/components/Card.tsx", "const Card = () => <div/>;\nexport default Card;\n")
    source = """
        import Btn from './components/Button';
        import Panel from './components/Card';
        export function Page() { return <div><Btn/><Panel/></div>; }
    """
    assert targets(tmp_path, "src/Page.tsx", source, "Button", "Page") == {
        ("src/components/Button.tsx", "resolved")
    }
    assert targets(tmp_path, "src/Page.tsx", source, "Card", "Page") == {("src/components/Card.tsx", "resolved")}


def test_destructured_dynamic_import(tmp_path):
    write(tmp_path, "src/lib/lazy.ts", "export function heavy() { return 1; }\n")
    source = "export async function f() { const { heavy, other: o } = await import('./lib/lazy'); heavy(); o(); }\n"
    assert ("src/lib/lazy.ts", "resolved") in targets(tmp_path, "src/a.ts", source, "heavy")
    assert ("src/lib/lazy.ts", "resolved") in targets(tmp_path, "src/a.ts", source, "other")


def test_export_surface_follows_chains_and_stars(tmp_path):
    lib(tmp_path)
    # A barrel of the barrel: its `export *` carries the renames on.
    write(tmp_path, "src/index.ts", "export * from './lib';\nexport { shout as cry } from './lib';\n")
    surface = export_surface("src/index.ts", ResolverConfig(tmp_path))
    assert surface["shout"][0] == "upper" and "src/lib/strings.ts" in surface["shout"][1]
    assert surface["cry"] == surface["shout"]
    assert surface["subtract"] == ("sub", ("src/lib/math.ts",))
    assert "default" not in surface  # `export *` leaves the default out
    # A cycle reads as nothing past it, whichever file is read first.
    write(tmp_path, "src/x.ts", "export * from './y';\nexport { a as b } from './y';\n")
    write(tmp_path, "src/y.ts", "export * from './x';\nexport function a() {}\n")
    first = export_surface("src/x.ts", ResolverConfig(tmp_path))
    config = ResolverConfig(tmp_path)
    export_surface("src/y.ts", config)
    assert export_surface("src/x.ts", config) == first


def test_module_records_its_export_aliases(tmp_path):
    lib(tmp_path)
    result = extract_js_file(textwrap.dedent(BARREL), "src/lib/index.ts", "repo", ResolverConfig(tmp_path))
    module = next(n for n in result.nodes if n.label == "Module")
    assert module.properties["js_exports"] == sorted(module.properties["js_exports"])
    assert len(module.properties["js_exports"]) == 2  # shout and subtract
    plain = extract_js_file(STRINGS, "src/lib/strings.ts", "repo", ResolverConfig(tmp_path))
    # Written empty too, so an edit dropping the last alias clears it.
    assert next(n for n in plain.nodes if n.label == "Module").properties["js_exports"] == []
