"""JS/TS export aliases through `export *` cycles and deep re-export chains
(audit 12 item 4 review): each file is read once, a chain of any depth is
followed, and a file's surface doesn't depend on which file is read first.
Fixture code is fictional."""

import time

from devgraph.indexer.jsts.extractor import export_surface
from devgraph.indexer.resolver_config import ResolverConfig


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_star_cycle_of_twelve_resolves_quickly_and_whole(tmp_path):
    n = 12
    for i in range(n):
        stars = "".join(f"export * from './m{j}';\n" for j in range(n) if j != i)
        write(tmp_path, f"src/m{i}.ts", stars + f"function f{i}() {{}}\nexport {{ f{i} as g{i} }};\n")
    started = time.perf_counter()
    surface = export_surface("src/m0.ts", ResolverConfig(tmp_path))
    assert time.perf_counter() - started < 1.0
    # Every member sees every other member's alias, whichever is read first.
    assert {name: value[0] for name, value in surface.items()} == {f"g{i}": f"f{i}" for i in range(n)}
    config = ResolverConfig(tmp_path)
    export_surface("src/m7.ts", config)
    assert export_surface("src/m0.ts", config) == surface


def test_deep_chain_is_followed_whatever_is_cached(tmp_path):
    depth = 20
    for i in range(depth):
        write(tmp_path, f"src/chain/c{i}.ts", f"export {{ x{i + 1} as x{i} }} from './c{i + 1}';\n")
    write(tmp_path, f"src/chain/c{depth}.ts", f"export function base() {{}}\nexport {{ base as x{depth} }};\n")
    expected = ("base", (f"src/chain/c{depth}.ts",))
    assert export_surface("src/chain/c0.ts", ResolverConfig(tmp_path))["x0"] == expected
    # Read from the far end first: the same answer.
    config = ResolverConfig(tmp_path)
    for i in reversed(range(depth + 1)):
        export_surface(f"src/chain/c{i}.ts", config)
    assert export_surface("src/chain/c0.ts", config)["x0"] == expected


def test_a_file_re_exporting_from_itself_resolves_to_the_definition(tmp_path):
    write(tmp_path, "src/s.ts", "function f() {}\nexport { f as g };\nexport { g as h } from './s';\n"
          "export { h as i } from './s';\n")
    surface = export_surface("src/s.ts", ResolverConfig(tmp_path))
    assert surface["g"] == surface["h"] == surface["i"] == ("f", ("src/s.ts",))
