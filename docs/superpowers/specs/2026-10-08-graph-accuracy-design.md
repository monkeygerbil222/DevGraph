# Graph accuracy: wrong, stale and missing edges — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1. Four bugs that reviewers' fuzzing
and real-client runs found. Each one exists on upstream `master` and on this
branch. The watcher-correctness fuzz listed bugs 2–4 as pre-existing
(PROJECT_STATUS, the watcher paragraph); bug 4 is the "Remaining gaps"
cross-batch referrer gap. Revised after a code-checked review (two live-confirmed
criticals: edge ownership and shared-node recreation).

## Problem

Each bug was reproduced with a scratch repository, `full_scan`, `index_paths`
and `remove_paths` against the local Neo4j. The edges below are
`label:name@file`.

| # | Bug | Reproduction | Wrong graph |
| --- | --- | --- | --- |
| 1 | Same-named functions in different files steal each other's call edges | `src/app.py`: `helper()` and `main()` calling `helper()`; `src/worker.py`: `main()` returning `2`; `full_scan` | an extra `Function:main@src/worker.py -CALLS-> Function:helper@src/app.py` |
| 1b | A call, base class or import removed from a file keeps its edge, even after `full_scan` | `m.py` with `class K(Base)` and `main()` calling `helper()`; edit both away; `full_scan` | `Class:K@m.py -EXTENDS-> Class:Base@m.py` and `Function:main@m.py -CALLS-> Function:helper@m.py` survive |
| 2 | A service removed from `compose.yaml` stays, even after `full_scan` | `compose.yaml` with `api` (`python:3.12`) and `db` (`postgres:16`); drop `db`; `index_paths`, then `full_scan` | `Service:db@compose.yaml`, `Container:postgres` and `db -RUNS-> postgres` survive |
| 3 | A mention removed from Markdown keeps its `MENTIONS` edge, even after `full_scan` | `notes.md` mentions `` `helper` `` and `` `other` ``; edit to mention only `` `other` ``; `index_paths`, then `full_scan` | `Document:notes.md -MENTIONS-> Function:helper@app.py` survives |
| 4 | Deleting and restoring an import target loses `IMPORTS`/`CALLS` | `pkg/a.py`: `from pkg.b import helper`, `main()` calls `helper()`; `remove_paths(pkg/b.py)`, then restore and `index_paths(pkg/b.py)` | `Module:pkg/a.py -IMPORTS-> Module:pkg/b.py` and `Function:main@pkg/a.py -CALLS-> Function:helper@pkg/b.py` missing. The same happens when `lib/b.py` is added after `pkg/a.py` imports it. A `full_scan` heals this one. |

Bug 1b is not in the reported list. It was found while reproducing bug 1,
and it is why bug 1 can't be healed by a rescan alone. Docs notes have the
same flaw: a `links`/`supersedes`/`decided_by` entry removed from a note keeps
its edge. That is now in scope (G2).

## Root causes

1. **The edge source is matched by bare name.**
   `_emit_call` (`devgraph/indexer/python/extractor.py:416-436`) emits
   `CALLS` with `from_file=None`. `_upsert_relationships_tx`
   (`devgraph/graph/engine.py:462-481`) then matches the source as
   `(a:Function {repo_id, name})`. That pattern matches every `main` in the
   repository, so each file's calls are written from every same-named
   function. The same holds for:
   - `EXTENDS` (`python/extractor.py:518-526`);
   - `CONTAINS` from a parent *function* to a nested one
     (`python/extractor.py:569-579`, where `from_file` is set only for a
     `Class` parent);
   - the same three emit sites in each of the other seven code extractors
     (`jsts`, `java`, `csharp`, `cpp`, `go`, `rust`, `kotlin`).

   The handover's "deliberate over-linking" refers to the *target* end,
   because a callee can live anywhere. The source end is a node of the file
   being parsed, except in one case: a Rust `impl Trait for Foo` (and a Go
   method) can sit in a different file from `Foo`. That makes the source a
   by-name reference too.
2. **A re-index never retracts edges.** This is the shared cause of 1b, 2,
   3 and the docs-note flaw.
   - **Code files (1b).** `_replace_file_nodes_tx`
     (`engine.py:493-517`) deletes only the file's nodes that are no longer
     extracted, then MERGEs the rest. A surviving node keeps every outgoing
     edge it ever had.
   - **Compose files and Containerfiles (2).** `_index_compose_file` and
     `_index_containerfile` (`devgraph/indexer/dispatch.py:1635-1646`) go
     through `_upsert_container_result` (`dispatch.py:1673-1693`), which
     only upserts.
   - **Markdown mentions (3).** `mentions.extractor.index_file`
     (`devgraph/indexer/mentions/extractor.py:387-400`) only upserts.
     PROJECT_STATUS's claim that "a rescan drops them" (for skip mode) is
     therefore wrong.
   - **Docs notes.** `docs.extractor.index_file`
     (`devgraph/indexer/docs/extractor.py:182-215`) only upserts.

   An edge carries nothing about who wrote it, so a retraction can't be
   scoped safely by the edge's source node. A docs note writes
   `Module -DOCUMENTED_BY-> note`, out of a code file's Module. `conv.rs`
   writes `Foo -EXTENDS-> Display`, out of `foo.rs`'s `Foo`. A compose file's
   `Service` gets `USES` edges from Python files. The reviewer confirmed
   live that a source-scoped retraction deletes the first two.
3. **The re-claim recreates shared nodes.** `_replace_file_nodes_tx` calls
   `_unclaim_source_tx(file)` (`engine.py:161-180`) for every claim the file
   holds. A node that only this file claims is `DETACH DELETE`d, then
   re-created by the upsert, which loses its incoming edges (`MENTIONS`,
   `Service -USES->`). The Python branch already does this to its
   `Datastore`/`Endpoint` claims on every save, because `_index_datastores`
   and `_index_apis` write them *after* the replace. G3 would add compose
   `Container`s to the same path.
4. **Nothing finds code referrers of an added node (4).**
   - `remove_paths` → `delete_nodes_by_source_file` (`engine.py:46-51`)
     `DETACH DELETE`s the removed file's Module and symbols, together with
     every incoming `IMPORTS`/`CALLS`/`EXTENDS`.
   - When the file comes back, `_expand_with_reverse_dependents`
     (`dispatch.py:1047-1090`) follows only *existing* `IMPORTS` edges, and
     there are none left.
   - `_find_referrers` (`dispatch.py:1125-1152`) knows only docs notes,
     handler stubs and same-directory JVM subtypes.
   - "Added" is decided by `(label, name)` (`list_file_nodes`,
     `engine.py:1026-1047`). A symbol that moves between files in one batch
     is therefore not "added", even though every edge to its new node is
     missing.

## Decisions

| # | Decision |
| --- | --- |
| G1 | **Pin the source end, and stamp the writer.** A shared helper, `own_edges(result, file_path)` in `devgraph/indexer/common.py`, does two things. It sets `from_file = file_path` on every relationship whose source `(label, name)` is one of `result.nodes` with `properties.file == file_path`. It also sets `origin = file_path` on every relationship. All eight code extractors call it before they return. A Module source is never pinned: it has `source_file`, not `file`, and its name is already the path. A source that isn't a file-scoped node of this file is never pinned either: the C++ out-of-class `Class` stub, or a Rust/Go type defined elsewhere. Target ends stay bare-name matched (over-linking by design). |
| G2 | **Ownership is "edges this file wrote".** `GraphRelationship` and the rel dicts gain `origin`. `_upsert_relationships_tx` sets `r.origin = row.origin` when it is given. Every built-in writer of extracted edges stamps the file it read: the code extractors (G1), compose and Containerfile, the Python owning-service and API passes, docs notes, mentions, and the G5 relink (the referrer's path). Two retractions use it. **Code, compose and Containerfile re-index:** in the same single scan as the stale-node delete, `_replace_file_nodes_tx` deletes `(a)-[r]->()` where `a` is owned by the file (`a.file = F OR a.source_file = F OR (a:Module AND a.name = F)`) and `coalesce(r.origin, F) = F`. A legacy edge with no origin counts as the file's own, and the first rescan stamps or drops it (G7). **Docs note re-index:** a new `replace_doc_note` deletes `()-[r]->()` with `r.origin = F` and `type(r) IN [DOCUMENTED_BY, SATISFIES, SUPERSEDES, DECIDED_BY]`. For a legacy edge with no origin, it deletes outgoing `SUPERSEDES`/`DECIDED_BY` from the note's nodes and incoming `DOCUMENTED_BY`/`SATISFIES` into them. This retracts removed `links`/`supersedes`/`decided_by`. Edges another file wrote out of an owned node keep their own origin and survive (`DOCUMENTED_BY`, a Rust `impl` edge, `Service -USES-> Datastore`); no per-label exception is needed. Edges with no built-in writer are never touched: provider, filesystem, `MODIFIES`, `RESOLVES`. Their sources are never file-owned nodes. |
| G3 | **Compose files and Containerfiles go through `replace_file_nodes`, and a replace re-claims instead of recreating.** `_upsert_container_result` calls `engine.replace_file_nodes(repo_id, rel_path, nodes, rels)`. `_unclaim_source_tx` gains `keep`: the `(label, name)` pairs of claimed nodes (no `file`, with `source == F`) in the new extraction. It drops only the claims that are gone, and the upsert then rewrites the kept claims' properties through `_claim_nodes_tx`. `delete_nodes_by_source_file` passes no keep (everything goes). The Python branch moves its `Datastore`/`Endpoint`/handler-stub node writes and its API relationships into the same `replace_file_nodes` call (extract first, write once), so a single-claimant `Datastore` keeps its `MENTIONS`. Compose writes only `Container` and `Service` nodes: `Volume`/`Network` are extracted but never upserted, and so are their `USES` edges. |
| G4 | **A full mentions re-index replaces the Document's `MENTIONS`.** `GraphEngine.replace_mentions(repo_id, file_name, nodes, rels)` (the same signature as `replace_file_nodes`) deletes `(:Document {name: file_name})-[:MENTIONS]->()` and writes the nodes and edges in one transaction. `index_file` uses it when `names is None`. The relink (`names=...`) stays additive. `replace_file_nodes` is not reused: it would delete a docs note at the same path as stale. |
| G5 | **Relink by-name edges when a batch adds their endpoint.** Three Module properties are written at extraction, always, as an explicit empty list when there is nothing to record. `name_refs` is the file's by-name edges as sorted, de-duplicated strings joined by `\x1f`: `rel_type, from_label, from_name, from_file, to_label, to_name, caller_class`. "By-name" means `to_file` is empty, or the source is unpinned and not a Module. `name_ref_targets` is their sorted distinct `to_name`s. `name_ref_sources` is the sorted distinct `from_name`s of unpinned non-Module sources. These properties are written only by the pass-1 replace; the pass-2 node re-upsert omits them. "Added" is keyed by `(label, name, file)` (`file` = `coalesce(file, source_file, path)`, or the name for a Module). After pass 2, `index_paths` runs one read: Modules outside the batch and outside the `_find_referrers` files whose targets or sources meet the added names. The read returns `[e IN m.name_refs WHERE split(e, '\x1f')[5] IN $names OR split(e, '\x1f')[2] IN $names]`, with the list-property name filter applied first. Python keeps the entries whose `(to_label, to_name)`, or unpinned `(from_label, from_name)`, matches an added node, and upserts them with `origin = m.name`. No file is read, and there is no cap. `full_scan` skips G5 (`index_paths(..., relink_outside=False)`), because it has no files outside the batch. |
| G6 | **Hidden and reserved.** `name_refs`, `name_ref_targets` and `name_ref_sources` join `RESERVED_NODE_PROPERTIES` and `describe_node`'s `_DESCRIBE_HIDDEN`, like `claims`. |
| G7 | **Automatic upgrade.** `INDEX_FORMAT = 2` in `dispatch.py`. `full_scan` stamps `Repository.index_format = INDEX_FORMAT` when it finishes. `index_outdated(engine, repo_id)` is true when the Repository node's `coalesce(index_format, 1)` is below the constant. Three places act on it. `SchemaRescanScheduler.run_once` treats an outdated repository like a pending schema, but with no quiet period: it runs `full_scan` under `run_exclusive` and calls `on_rescanned`. `catch_up` runs `full_scan` instead of the incremental pass when the index is outdated, so the agent's start catch-up upgrades too. `devgraph status` lists every active repository with an outdated index as "rescan pending". No user action is needed: the first rescan stamps origins, drops the wrong and stale edges, and fills `name_refs`. |

## Interactions checked

- **Watcher batch semantics.**
  - G2 runs inside each file's pass-1 transaction, and pass 2 re-creates
    in-batch edges as before.
  - A docs note and a code file it links to can be in the same batch. The
    note's edge has the note's origin, so the code file's replace can't
    delete it, and the note's docs pass re-writes it.
  - G5 runs after pass 2. A delete-then-restore split across two batches (a
    git checkout or a branch switch) is exactly its case.
  - A symbol moving between files within one batch is "added" under
    `(label, name, file)`. The mentions relink and `_find_referrers` get the
    pair projection of that set. A moved symbol therefore also gains the
    `MENTIONS` that a fresh scan gives it. Docs entries that `_read_docs_batch`
    reports as `existing` (field-keyed, moving between files) are still not
    "added".
- **Shared-node attribution.**
  - G2 never matches a claimed node: it has no `file`/`source_file`.
  - G3 keeps kept claims' nodes in place, re-attributed by `_claim_nodes_tx`,
    and deletes a node only when its last claim goes.
  - `Service -USES-> Datastore` and `Endpoint -CALLS-> Service` carry the
    Python file's origin, so a compose re-index keeps them.
- **Docs provider.**
  - Provider edges are non-built-in types written without an origin, out
    of provider nodes, so neither retraction matches them.
  - Provider edges into code nodes are incoming edges.
  - `_relink_docs` is unchanged.
- **Mentions.** The relink stamps the doc's origin, as the full index does.
  Skip mode: adding a second node with an already-mentioned name still
  leaves the old edge until the doc is re-indexed (G4 now drops it then) or
  rescanned.
- **Cost.** `name_refs` was 348 KiB as JSON for `cli/main.py`. The compact,
  de-duplicated form drops the keys, `repo_id` and repeats. It is written
  once per file save and never re-upserted in pass 2. The G5 read filters
  Modules on the short name lists before splitting any entry. A benchmark
  pins the read at about 5,000 Modules.

## Out of scope (known gaps, recorded in PROJECT_STATUS)

- **Same-file sibling collisions** (file+scope identity) and IMPORTS-based
  call precision. These are the handover's open items 1 and 2.
- **Edges a file stops writing out of a node it doesn't own.** For example,
  a Python file stops using a store or route: `Service -USES-> Datastore`
  and `Endpoint -IMPLEMENTS->`. Several files can write the same edge, and
  one `origin` can't record them all. They retract on the source's own
  re-index, or never.
- **A compose-only batch that adds `build:`.** It gets no owning-service
  `USES`/`CALLS` until the owned Python files are re-indexed. The fuzz
  excludes it.
- **Go/Rust methods defined in another file than their type.** Their
  `CONTAINS` is pinned to the method's file, where the type doesn't live,
  so it is never created (fresh and incremental alike). The C++
  out-of-class method has the same gap: its `CONTAINS` comes from a
  file-less `Class` stub, under a `from_file` the stub doesn't have.
- **C#/TS partial classes.** Each file's `partial class Foo : Base` is its
  own node, so `EXTENDS` hangs off the declaring file's node only (noted in
  `HANDOVER_node_identity.md`).
- **Recency and `MODIFIES` after delete-and-restore.** This still needs
  `devgraph rescan --full`.
- **Deletions don't relink.** For example, a skip-mode ambiguous name stays
  unlinked after the duplicate goes.

## Docs to update

- **PROJECT_STATUS.**
  - In "Remaining gaps", drop the `IMPORTS`/supertype/`CALLS` cross-batch
    gap.
  - In the watcher paragraph, drop the three "Pre-existing bugs found by the
    watcher fuzz".
  - Correct the skip-mode sentence.
  - Say the index upgrade is automatic (G7), and list "Out of scope" above.
- **`HANDOVER_node_identity.md`.**
  - Sources are pinned; only targets are bare-name.
  - Edges carry `origin`.
  - The C#/TS partial-`EXTENDS` note.
- **Docstrings.** `GraphRelationship` (`common.py`), and `index_paths` and
  `_find_referrers` (`dispatch.py`).
