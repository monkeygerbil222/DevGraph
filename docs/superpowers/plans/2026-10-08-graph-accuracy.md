# Graph Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** after any sequence of edits, deletes and restores, the graph equals the graph that a fresh `full_scan` of the same files produces, and a `full_scan` equals the expected graph. Four bugs are fixed:

1. same-named functions steal each other's `CALLS`, `EXTENDS` and nested `CONTAINS`;
2. a service removed from a compose file stays;
3. a removed Markdown mention keeps its `MENTIONS`;
4. deleting and restoring an import target, or adding one later, loses the `IMPORTS`/`CALLS`/`EXTENDS` that point at it.

Bug 1's stale-edge companion (1b: a removed call, base or import keeps its edge) is fixed as well.

**Spec:** `docs/superpowers/specs/2026-10-08-graph-accuracy-design.md`. Every task implements the decisions it names (G1–G7).

**Working directory:** this worktree, branch `epic1/graph-accuracy` (from `epic1/test-isolation`). Run `uv sync --extra dev` once, then `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests need the local Neo4j (`bolt://127.0.0.1:7687`, `neo4j`/`devgraph-local-dev`) and skip without it. Never touch `~/.devgraph`; the suite already isolates it.

## Global Constraints

- **Order.** Task 1 lands first. G1 and G2 ship together (spec G2). Task 2 depends on Task 1's edge reset (it adds the `Service -USES->` exception), and Task 4 depends on Task 1's pinned `from_file` (it stores pinned edges). Task 3 is independent.
- **Target ends stay bare-name matched.** Only the source end is pinned, and only to a node of the file being parsed that carries `file`. No IMPORTS-based callee resolution, and no file+scope identity.
- **One transaction per file.** Each retraction (G2, G3, G4) runs inside the same write transaction as the re-upsert it precedes, so a reader never sees a file's edges half gone. No new autocommit `session.run` writes.
- **Nothing outside a file's own nodes is retracted.**
  - G2 matches only `n.file = $file_name OR n.source_file = $file_name OR (n:Module AND n.name = $file_name)`.
  - It deletes only outgoing relationships.
  - Claimed shared nodes (`source`/`sources` without `file`) and provider nodes (`extractor`) are never matched.
- **G5 never re-reads a file.** The relink is one read query plus one `upsert_relationships`, with no cap. It runs after pass 2 and skips batch files and `_find_referrers` files.
- **Deterministic properties.** `name_refs` is `json.dumps(..., sort_keys=True)` of a list sorted by `(rel_type, from_label, from_name, from_file, to_label, to_name)`. `name_ref_targets` is sorted. Incremental and fresh scans must give byte-equal values, because `graph_snapshot` hashes properties.
- **Regression shape.** Every bug's live test ends in either "incremental equals a fresh `full_scan`" or "`full_scan` equals the expected graph". The comparison uses a file-aware edge projection (Task 1 extends `graph_snapshot`). Where the bug is "even after `full_scan`", the test also runs `full_scan` over the polluted graph and checks the result.
- **Fixtures.**
  - Unique repo ids: `f"zz-accuracy-{uuid.uuid4().hex[:8]}"`.
  - `engine.delete_repository` before the test and in a finaliser, plus `delete_repository(f"{repo_id}_fresh")`.
  - `tmp_path` repositories only.
  - Fictional names only: `app.py`, `worker.py`, `helper`, `api`, `db`, `notes.md`.
- **No new config knob, and no new dependency.** No change to MCP tool signatures, apart from the hidden properties (G6).
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, live tests included).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **No wrong source.** Across all eight languages, a by-name edge from a file's `Function`/`Class` is pinned to that file, and edges from file-less nodes and Modules are not. Tests:
   - `test_sources_pinned_to_the_parsed_file[<lang>]` (unit, parametrised over the eight extractors);
   - `test_same_named_functions_keep_their_own_calls` (live).
2. **Retraction is scoped.** A code re-index drops its own stale outgoing edges and nothing else:
   - incoming `MODIFIES`, `MENTIONS`, docs-provider and filesystem edges survive;
   - edges from shared nodes survive;
   - `Service -USES-> Database` survives a compose re-index.

   Tests: `test_reindex_keeps_incoming_and_foreign_edges`, `test_compose_reindex_keeps_owning_service_uses`.
3. **Polluted graphs heal on rescan (G7).** A graph seeded with the old wrong and stale edges equals a fresh scan after one `full_scan`. Tests:
   - `test_full_scan_heals_a_cross_linked_graph`;
   - `test_full_scan_drops_a_removed_service`;
   - `test_full_scan_drops_a_removed_mention`.
4. **Shared-node attribution.** Removing a service unclaims its `Container`. A `Container` another file still claims survives, re-attributed. Test: `test_removed_service_unclaims_its_image`.
5. **Docs notes are safe.** A Markdown file that is both a docs note and a mentions `Document` keeps its note after a mention-changing edit. Test: `test_mentions_replace_keeps_the_docs_note`.
6. **Cross-batch relink.** Each of these equals a fresh scan, with no file read by the relink:
   - delete-then-restore across two batches;
   - a module added after its importer;
   - a supertype added in another directory;
   - a second same-named function added outside the caller's batch.

   Tests: Task 4 live cases, plus `test_relink_reads_no_files`. A real-watcher scenario: `test_an_import_target_deleted_and_restored`.
7. **Watcher equality still holds** with the stricter, file-aware snapshot: every existing `tests/watcher/test_watcher_live.py` scenario passes unchanged.

### Task 1: Pin edge sources and retract a re-indexed file's own outgoing edges (bug 1, 1b; G1, G2, G6)

**Files:**
- `devgraph/indexer/common.py`:
  - `pin_local_sources(result: ExtractionResult, file_path: str) -> ExtractionResult`. It collects `{(n.label, n.name) for n in result.nodes if n.properties.get("file") == file_path}` and sets `from_file = file_path` on every relationship whose `(from_label, from_name)` is in that set and whose `from_file` is `None`. It returns `result`.
  - Update the `GraphRelationship` docstring: the source end of an extracted edge is always known; only the target end stays bare.
- Each of `devgraph/indexer/{python,jsts,java,csharp,cpp,go,rust,kotlin}/extractor.py`: wrap the top-level `extract_*_file` return in `pin_local_sources(result, file_path)`. Check each function for early returns, and pin those too.
- `devgraph/graph/engine.py`:
  - a new `_DELETE_OWNED_EDGES_CYPHER`:
    ```
    MATCH (a {repo_id: $repo_id})-[r]->(b)
    WHERE (a.file = $file_name OR a.source_file = $file_name OR (a:Module AND a.name = $file_name))
    DELETE r
    ```
    Task 2 adds the `Service -USES->` exception, when compose files start going through this path.
  - `_replace_file_nodes_tx` runs it first, before `_DELETE_STALE_FILE_NODES_CYPHER`. Comment why: the file re-emits every edge it owns, and a surviving node otherwise keeps edges the source no longer has.
- `devgraph/graph/schema.py`: add `name_refs` and `name_ref_targets` to `RESERVED_NODE_PROPERTIES` (G6; used in Task 4, reserved now so the schema check and the docs land once).
- `devgraph/mcp/tools.py`: add both to `_DESCRIBE_HIDDEN`.
- `tests/watcher/live_helpers.py`:
  - `graph_snapshot`'s edge tuple gains each end's `coalesce(a.file, a.source_file, a.path, '')`.
  - `fresh_snapshot` gains `mentions_enabled: bool = False` and passes it to `full_scan`.
- `tests/indexer/test_pin_local_sources.py` (new, unit).
- `tests/indexer/test_graph_accuracy_live.py` (new, live). Holds a shared `incremental_equals_fresh(engine, repo_id, root, mentions_enabled=False)` that asserts `graph_snapshot(engine, repo_id) == fresh_snapshot(...)`, with a readable diff on failure (reuse `wait_until_equal`'s diff formatting, without polling).

- [ ] Write failing unit tests:
  - **`test_sources_pinned_to_the_parsed_file[<lang>]`**, parametrised over the eight extractors. Each source defines `main` calling `helper`, a class extending `Base`, and (where the language has them) a nested function. Every `CALLS`/`EXTENDS`/`CONTAINS` whose source is a `Function`/`Class` in that file has `from_file == path`. Every `to_file` is unchanged.
  - **`test_module_and_fileless_sources_stay_bare`.**
    - A Python module-level call keeps `from_file is None` (`Module` source).
    - A C++ out-of-class `void Foo::bar() { baz(); }` keeps the `Class` stub `Foo`'s edges unpinned, while `bar`'s `CALLS` is pinned.
  - **`test_pin_keeps_existing_from_file`.** A relationship that already has a `from_file` is left as it is.
- [ ] Write failing live tests:
  - **`test_same_named_functions_keep_their_own_calls`.**
    - `src/app.py` (`helper`, `main` → `helper`), `src/worker.py` (`main` returning `2`), `full_scan`.
    - The only `CALLS` is `main@src/app.py → helper@src/app.py`. Assert by a file-aware query, and also as `full_scan` equals this expected edge set.
    - Then `index_paths({src/worker.py})` after touching it, and check incremental equals fresh.
  - **`test_same_named_classes_keep_their_own_bases`.** `a.py`: `class K(Base)`; `b.py`: `class K:`, and `class Base:` in `base.py`. Only `K@a.py` EXTENDS `Base`.
  - **`test_removed_call_base_and_import_are_retracted`.** This is bug 1b. `m.py` with `import pkg.util`, `class K(Base)`, `main` → `helper`, and `pkg/util.py`. `full_scan`, then edit all three away and `index_paths({m.py})`: incremental equals fresh. Then `full_scan`: it equals the expected graph (no `IMPORTS`/`EXTENDS`/`CALLS` from `m.py`).
  - **`test_full_scan_heals_a_cross_linked_graph`.** After the app/worker `full_scan`, seed the old wrong edge with `engine.run_cypher` (`MATCH` `main@src/worker.py` and `helper@src/app.py`, `MERGE` `CALLS`). After `full_scan`, the graph equals a fresh scan.
  - **`test_reindex_keeps_incoming_and_foreign_edges`.** With mentions on and a docs-provider schema:
    - a `notes.md` mentioning `` `helper` ``;
    - a seeded `Commit -MODIFIES-> Module src/app.py`;
    - a docs-provider node whose edge targets `Module src/app.py` (reuse `tests/indexer/test_docs_provider_live.py`'s schema scaffolding);
    - an `Endpoint -IMPLEMENTS-> Function` from a FastAPI route in `src/app.py`.

    `index_paths({src/app.py})` keeps the `MENTIONS`, `MODIFIES`, provider and `IMPLEMENTS` edges, and incremental equals fresh. The fresh comparison ignores `Commit` by construction; assert `MODIFIES` directly.
- [ ] Implement G1, G2 and G6 so that both files pass.
- [ ] Run `uv run pytest tests/watcher -q` against the file-aware snapshot. A newly failing watcher scenario is a real accuracy bug: fix it or report it; never loosen the projection.
- [ ] `uv run pytest -q`. Commit "Pin edge sources to their file and retract stale outgoing edges".

### Task 2: Compose files and Containerfiles retract removed services (bug 2; G3)

**Files:**
- `devgraph/indexer/dispatch.py`: `_upsert_container_result(engine, repo_id, result, rel_path)` calls `engine.replace_file_nodes(repo_id, rel_path, nodes, rels)` in place of `upsert_nodes` plus `upsert_relationships`. Update `_index_containerfile` and `_index_compose_file` to pass `rel_path`. Keep the existing comment about `Container` vs `Service` keying, and add one line saying why it replaces.
- `devgraph/graph/engine.py`: add the `AND NOT (a:Service AND type(r) = 'USES' AND NOT b:Volume)` clause to `_DELETE_OWNED_EDGES_CYPHER`, with a comment naming `_owning_service_relationships` as the other writer.
- `tests/indexer/test_graph_accuracy_live.py`.

- [ ] Write failing live tests:
  - **`test_removed_service_is_retracted`.** `compose.yaml` with `api` (`python:3.12`, volume `data:/var/lib`, `volumes: {data: {}}`) and `db` (`postgres:16`). `full_scan`, drop `db`, `index_paths({compose.yaml})`: incremental equals fresh. `Service db`, `Container postgres` and `db -RUNS-> postgres` are gone.
  - **`test_full_scan_drops_a_removed_service`.** The same edit, followed by `full_scan` alone (no `index_paths`), equals the expected graph.
  - **`test_removed_service_unclaims_its_image`.** `compose.yaml` and `compose.override.yaml` both run `postgres:16`. Remove `db` from `compose.yaml`: `Container postgres` survives with `sources == ["compose.override.yaml"]`, and incremental equals fresh.
  - **`test_compose_reindex_keeps_owning_service_uses`.** `compose.yaml` `api` with `build: ./services/api` and `services/api/app.py` using `redis.from_url("redis://cache:6379/0")`. After `full_scan`, `Service api -USES-> <the redis Datastore>` exists. Edit `compose.yaml` (add an unrelated service) and `index_paths({compose.yaml})`: the `USES` edge is kept, and incremental equals fresh.
  - **`test_containerfile_retracts_a_removed_stage`.** A `Containerfile` with two `FROM` stages loses one: incremental equals fresh.
- [ ] Implement G3 and the G2 exception.
- [ ] `uv run pytest -q`. Commit "Retract services removed from compose files".

### Task 3: A full mentions re-index replaces the Document's MENTIONS (bug 3; G4)

**Files:**
- `devgraph/graph/engine.py`: `GraphEngine.replace_mentions(repo_id, doc_name, documents, rels)`. A single `execute_write`:
  1. `MATCH (d:Document {repo_id: $repo_id, name: $doc_name})-[r:MENTIONS]->() DELETE r`;
  2. `_upsert_nodes_tx(tx, documents)`;
  3. `_upsert_relationships_tx(tx, rels)`.

  Wrap it in `_retry_transient`, as `replace_file_nodes` is.
- `devgraph/indexer/mentions/extractor.py`: `index_file` builds the node and relationship dicts once. When `names is None` it calls `engine.replace_mentions(...)`; otherwise it keeps today's additive `_upsert_documents` plus `upsert_relationships`. Update the `names` docstring: "Existing edges are kept; without `names`, the Document's MENTIONS are replaced."
- `tests/indexer/test_mentions_extractor.py` (unit, with a recording stub engine if one exists there; otherwise only the live tests).
- `tests/indexer/test_graph_accuracy_live.py`.

- [ ] Write failing tests:
  - **`test_removed_mention_is_retracted`** (live, `mentions_enabled=True`). `app.py` (`helper`, `other`) and `notes.md` mentioning `` `helper` `` and `` `other` ``. `full_scan`, edit to `` `other` `` only, `index_paths({notes.md}, mentions_enabled=True)`: incremental equals fresh (`fresh_snapshot(..., mentions_enabled=True)`).
  - **`test_full_scan_drops_a_removed_mention`.** The same edit followed by `full_scan` alone equals the expected graph.
  - **`test_mention_relink_stays_additive`.** Add `def later()` to `app.py` while `notes.md` already mentions `` `later` ``: the relink adds `MENTIONS later` and keeps `MENTIONS other`.
  - **`test_mentions_replace_keeps_the_docs_note`.** `docs_path="docs"` and mentions on. `docs/adr-1.md` is a docs note (`id: ADR-1`) mentioning `` `helper` ``; drop the mention and `index_paths`. The `DesignDecision`/`ArchitectureNote` node at `docs/adr-1.md` survives, and incremental equals fresh.
- [ ] Implement G4.
- [ ] `uv run pytest -q`. Commit "Replace a document's mentions on re-index".

### Task 4: Relink by-name referrers when a batch adds their target (bug 4; G5, G7)

**Files:**
- `devgraph/indexer/common.py`: `name_ref_properties(rels: list[dict]) -> dict` returns `{"name_refs": <json>, "name_ref_targets": [...]}`. It covers the relationships with `to_file is None`, minus `repo_id`, and keeps `properties` (`caller_class`). The order and encoding follow the Global Constraints.
- `devgraph/indexer/dispatch.py`:
  - **Module properties.** In each of the eight code branches of `_index_single_path`, after `rels` is built and before `replace_file_nodes`, merge `name_ref_properties(rels)` into the `Module` node dict's `properties` (the node with `label == "Module"` and `name == rel_path`). Do it with one helper, `_with_name_refs(nodes, rels, rel_path)`, and not inline eight times. The Python branch uses the language `rels`, not `api_rels`.
  - **The relink step.** `_relink_name_refs(engine, repo_id, added, skip) -> None`. It returns at once when `added` is empty. Otherwise:
    1. It runs `engine.find_name_ref_modules(repo_id, sorted({name for _l, name in added}), sorted(skip))`.
    2. It decodes each `name_refs`, keeps edges whose `(to_label, to_name)` is in `added`, and adds `repo_id`.
    3. It upserts them with one `engine.upsert_relationships`.
  - **Where it runs.** `index_paths` calls it after the eight pass-2 loops and before the service pass, with `skip = set(by_rel_path) | referrers`. Comment why it is there: every batch node exists, and the edges come from the graph, so no file is read.
  - Update `index_paths`' docstring and `_find_referrers`' docstring to name the new path.
- `devgraph/graph/engine.py`: `find_name_ref_modules(repo_id, names, skip) -> list[str]`, which returns the `name_refs` strings:
  ```
  MATCH (m:Module {repo_id: $repo_id})
  WHERE m.name_refs IS NOT NULL AND NOT m.source_file IN $skip
    AND any(t IN m.name_ref_targets WHERE t IN $names)
  RETURN m.name_refs AS refs
  ```
- `tests/indexer/test_graph_accuracy_live.py`; `tests/watcher/test_watcher_live.py`; `tests/indexer/test_dispatch.py` (unit, for the relink's read-nothing property).

- [ ] Write failing tests:
  - **`test_import_target_deleted_and_restored`** (live). `pkg/a.py`: `from pkg.b import helper`, `main` → `helper`; `pkg/b.py`: `helper`. `full_scan`, then two batches as the watcher sends them: `unlink`, `remove_paths({pkg/b.py})`, then restore and `index_paths({pkg/b.py})`. Incremental equals fresh. Both `pkg/a.py -IMPORTS-> pkg/b.py` and `main@pkg/a.py -CALLS-> helper@pkg/b.py` are present.
  - **`test_module_added_after_its_importer`.** `pkg/a.py` imports `lib.b`; `full_scan`; add `lib/b.py`; `index_paths({lib/b.py})`. Incremental equals fresh.
  - **`test_supertype_added_in_another_directory`.** `app/k.py`: `from base.b import Base`, `class K(Base)`; add `base/b.py` later. Incremental equals fresh. Repeat with Java (`app/K.java` `extends Base`, `base/Base.java` with an import). This is the case `_same_package_subtype_referrers` cannot see.
  - **`test_second_same_named_function_links_outside_callers`.** `a.py` calls `helper` and `b.py` defines it. Add `c.py` defining another `helper`, and `index_paths({c.py})`: `main@a.py -CALLS-> helper@c.py` exists, and incremental equals fresh.
  - **`test_relink_reads_no_files`** (unit, `test_dispatch.py`). Patch `Path.read_text` and `_read_text` to count calls for paths outside the batch, and run `index_paths` on a batch that adds a name that a non-batch Module refers to (stub engine or live). Zero reads outside the batch; the expected edge dicts reach `upsert_relationships`.
  - **`test_name_refs_are_deterministic`** (unit). Two extractions of the same source, with relationships shuffled, give byte-equal `name_refs`.
  - **`test_name_refs_hidden_from_describe_node`** (unit, `tests/mcp/test_describe_node.py`). Neither property appears.
  - **`test_an_import_target_deleted_and_restored`** (live watcher, `tests/watcher/test_watcher_live.py`). Before `start`, write `tools/use.py` (`from pkg.sub.util import helper`, `def go(): return helper()`) and commit. Then `start`, delete `pkg/sub/util.py`, `converges`, `git checkout -- pkg/sub/util.py`, `converges`. Then `git checkout -q -b other`, delete it and commit, `git checkout -q main`, `converges` (the branch-switch form).
- [ ] Implement G5.
- [ ] Docs (spec "Docs to update"):
  - **PROJECT_STATUS.**
    - Drop the `IMPORTS`/supertype/`CALLS` sentence from "Remaining gaps" and describe the `name_refs` relink in its place.
    - Drop the three "Pre-existing bugs found by the watcher fuzz" from the watcher paragraph.
    - Correct the skip-mode sentence: a re-index of the doc now drops the old edge.
    - Add the G7 upgrade note: run `devgraph rescan` once after updating to heal wrong or stale edges.
    - Add the claimed-node edge gap from the spec's "Out of scope".
  - **`HANDOVER_node_identity.md`.** Sources are pinned; only targets are bare-name.
- [ ] `uv run pytest -q`. Commit "Relink by-name referrers when their target is added".
