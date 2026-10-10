"""JS/TS CALLS resolve through scope, imports and types (the extractor's
tiers): same-file functions, imported bindings (with a barrel's prefix), `this`
and its bases, namespace imports and typed receivers are pinned to the files
they can be in; an untyped receiver keeps a bare-name edge unless its method
is stoplisted; a bare call nothing defines or imports links nothing, except
in a classic script.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (JS/TS).
"""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.resolver_config import ResolverConfig


def calls(source, file_path="src/app.ts", config=None):
    """(caller, callee, to_file, confidence) per CALLS row."""
    result = extract_js_file(textwrap.dedent(source), file_path, "repo", config)
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"])
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def targets(source, caller, callee, file_path="src/app.ts", config=None):
    return {(f, conf) for c, n, f, conf in calls(source, file_path, config) if (c, n) == (caller, callee)}


def candidates(base):
    return {f"{base}.{ext}" for ext in ("js", "jsx", "ts", "tsx")} | {
        f"{base}/index.{ext}" for ext in ("js", "jsx", "ts", "tsx")
    }


def test_a_function_defined_in_the_file_resolves_here_nested_included():
    source = """
        export function main() { helper(); inner(); }
        function helper() {}
        const arrow = () => { helper(); };
        export function outer() { function inner() {} inner(); }
    """
    assert targets(source, "main", "helper") == {("src/app.ts", "resolved")}
    assert targets(source, "arrow", "helper") == {("src/app.ts", "resolved")}
    assert targets(source, "outer", "inner") == {("src/app.ts", "resolved")}
    assert targets(source, "main", "inner") == {("src/app.ts", "resolved")}


def test_an_imported_name_goes_to_its_files_and_their_prefix():
    source = """
        import { save } from '../api/users';
        import { fetchPosts } from '../api';
        export async function persist(u) { await save(u); return fetchPosts(u.id); }
    """
    got = targets(source, "persist", "save", "src/pages/Profile.tsx")
    assert got == {(f, "resolved") for f in candidates("src/api/users")} | {("src/api/users/", "package")}
    barrel = targets(source, "persist", "fetchPosts", "src/pages/Profile.tsx")
    assert ("src/api/", "package") in barrel and ("src/api/index.ts", "resolved") in barrel


def test_a_renamed_import_calls_the_exported_name():
    source = "import { save as saveDraft } from '../lib/helpers.js';\nexport function keep(f) { saveDraft(f); }\n"
    got = targets(source, "keep", "save", "src/pages/Settings.tsx")
    assert ("src/lib/helpers.js", "resolved") in got and ("src/lib/helpers.ts", "resolved") in got
    assert targets(source, "keep", "saveDraft", "src/pages/Settings.tsx") == set()


def test_an_alias_resolves_through_the_tsconfig(tmp_path):
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}')
    source = "import { capitalize } from '@/lib/text';\nexport function title(s) { return capitalize(s); }\n"
    got = targets(source, "title", "capitalize", "src/pages/P.tsx", ResolverConfig(tmp_path))
    assert ("src/lib/text.ts", "resolved") in got and ("src/lib/text/", "package") in got


def test_an_external_package_links_nothing():
    source = """
        import _ from 'lodash';
        import { useState } from 'react';
        export function f(u) { useState(0); return _.capitalize(u); }
    """
    assert targets(source, "f", "useState") == set()
    assert targets(source, "f", "capitalize") == set()


def test_a_namespace_import_or_require_binding_resolves_its_members():
    source = """
        import * as http from './http';
        const util = require('./util');
        const { read, write: put } = require('./io');
        export function f() { http.get(); util.fmt(); read(); put(); }
    """
    assert ("src/http.ts", "resolved") in targets(source, "f", "get")
    assert ("src/util.js", "resolved") in targets(source, "f", "fmt")
    assert ("src/io.js", "resolved") in targets(source, "f", "read")
    assert ("src/io.js", "resolved") in targets(source, "f", "write")


def test_this_resolves_to_the_class_its_in_file_base_or_an_imported_base():
    source = """
        import { Base } from './base';
        class Local { shared() {} }
        export class A extends Local { run() { this.own(); this.shared(); } own() {} }
        export class B extends Base { run() { this.inherited(); super.other(); } }
    """
    assert targets(source, "run", "own") == {("src/app.ts", "resolved")}
    assert targets(source, "run", "shared") == {("src/app.ts", "resolved")}
    assert ("src/base.ts", "resolved") in targets(source, "run", "inherited")
    assert ("src/base.ts", "resolved") in targets(source, "run", "other")


def test_constructor_parameter_properties_and_fields_type_this_members():
    source = """
        import { UserRepo } from './UserRepo';
        import { Logger } from '@/lib/logger';
        import { Cache } from './cache';
        export class Service {
          private cache: Cache | null = null;
          private items: Item[] = [];
          constructor(private repo: UserRepo, private readonly log: Logger, plain: number) {}
          rename() { this.repo.save(); this.cache.get(); this.items.push(1); this.items.store(); }
        }
    """
    assert ("src/UserRepo.ts", "resolved") in targets(source, "rename", "save")
    assert ("src/cache.ts", "resolved") in targets(source, "rename", "get")
    # `items` is an array: push is stoplisted, store stays a bare name.
    assert targets(source, "rename", "push") == set()
    assert targets(source, "rename", "store") == {(None, "name")}


def test_typed_and_constructed_locals_and_static_calls_resolve_to_their_class():
    source = """
        import { UserService } from '@/services/UserService';
        import { Repo } from './repo';
        export function f(r: Repo | undefined, s: string) {
          const svc = new UserService();
          const other: Repo = make();
          svc.rename(); r.find(); other.find(); Repo.create(); new Repo().load(); s.trim();
        }
    """
    assert targets(source, "f", "rename") == set()  # no tsconfig: '@/...' is external
    assert ("src/repo.ts", "resolved") in targets(source, "f", "find")
    assert ("src/repo.ts", "resolved") in targets(source, "f", "create")
    assert ("src/repo.ts", "resolved") in targets(source, "f", "load")
    assert targets(source, "f", "trim") == set()


def test_an_untyped_receiver_keeps_a_bare_row_off_its_own_caller():
    result = extract_js_file("export class R { save(x) { x.save(); } }\n", "src/r.ts", "repo")
    (row,) = [r for r in result.relationships if r.rel_type == "CALLS"]
    assert (row.to_file, row.properties["confidence"], row.no_self) == (None, "name", True)


def test_library_receivers_literals_and_stoplisted_methods_link_nothing():
    source = """
        export function f(res, el) {
          JSON.parse('1'); Object.keys({}); console.info('x'); 'a'.toUpperCase(); [1].map(g);
          res.json(); el.addEventListener('x', g); window.localStorage.getItem('k');
        }
    """
    assert calls(source) == set()


def test_a_bare_call_defined_nowhere_links_nothing_in_a_module_but_a_script_shares_globals():
    module = "export function f() { trackEvent(); fetch('/x'); }\n"
    script = "function initLegacy() { trackEvent('x'); fetch('/x'); }\n"
    assert targets(module, "f", "trackEvent", "src/a.js") == set()
    assert targets(script, "initLegacy", "trackEvent", "public/legacy.js") == {(None, "name")}
    assert targets(script, "initLegacy", "fetch", "public/legacy.js") == set()


def test_a_parameter_shadows_an_import_of_its_name():
    source = "import { save } from './store';\nexport function f(save) { save(); }\n"
    assert targets(source, "f", "save") == set()


def test_a_resolved_call_suppresses_the_bare_row_of_the_same_name():
    source = "import { save } from './store';\nexport function f(x) { save(); x.save(); }\n"
    assert all(conf != "name" for _f, conf in targets(source, "f", "save"))
