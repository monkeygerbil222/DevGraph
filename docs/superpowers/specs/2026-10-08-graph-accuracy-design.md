# Graph accuracy: wrong, stale and missing edges — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1. Four bugs that reviewers' fuzzing
and real-client runs found. Each one exists on upstream `master` and on this
branch. The watcher-correctness fuzz listed bugs 2–4 as pre-existing
(PROJECT_STATUS, the watcher paragraph); bug 4 is the "Remaining gaps"
cross-batch referrer gap.

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
and it is why bug 1 can't be healed by a rescan alone.

## Root causes

1. **The CALLS source is matched by bare name.**
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
   because a callee can live anywhere. The *source* end is always a node of
   the file being parsed, so leaving it unpinned is a bug, not a design
   choice.
2. **A re-index never retracts edges.** This is the shared cause of 1b, 2
   and 3.
   - **Code files (1b).** `_replace_file_nodes_tx`
     (`engine.py:493-517`) deletes only the file's nodes that are no longer
     extracted, then MERGEs the rest. A surviving node keeps every outgoing
     edge it ever had, so a removed call, base or import stays. It also
     means that, once bug 1 is fixed, a wrong edge already in a user's graph
     would survive a `full_scan`.
   - **Compose files and Containerfiles (2).** `_index_compose_file` and
     `_index_containerfile` (`devgraph/indexer/dispatch.py:1635-1646`) go
     through `_upsert_container_result` (`dispatch.py:1673-1693`), which
     only upserts. They never call `replace_file_nodes`, so a removed
     `Service`, its `RUNS`/`USES` edges and its now-unclaimed `Container`
     stay. `prune_stale_files` only prunes files that are gone from disk.
   - **Markdown mentions (3).** `mentions.extractor.index_file`
     (`devgraph/indexer/mentions/extractor.py:387-400`) upserts the
     `Document` and its `MENTIONS` and never deletes the old ones. A full
     re-index (`names=None`) is as additive as the relink (`names=...`).
     PROJECT_STATUS's claim that "a rescan drops them" (for skip mode) is
     therefore wrong.
3. **Nothing finds code referrers of an added node (4).**
   - `remove_paths` → `delete_nodes_by_source_file` (`engine.py:46-51`)
     `DETACH DELETE`s the removed file's Module and symbols, together with
     every incoming `IMPORTS`/`CALLS`/`EXTENDS`.
   - When the file comes back, `_expand_with_reverse_dependents`
     (`dispatch.py:1047-1090`) follows only *existing* `IMPORTS` edges, and
     there are none left.
   - `_find_referrers` (`dispatch.py:1125-1152`) knows only docs notes,
     handler stubs and same-directory JVM subtypes.

   No code importer, caller or subtype outside the batch is ever re-linked.
   The same gap covers a newly added module, a supertype in another
   directory, and `CALLS` from outside the batch.

## Decisions

| # | Decision |
| --- | --- |
| G1 | **Pin the source end to the file being parsed.** A shared helper, `pin_local_sources(result, file_path)` in `devgraph/indexer/common.py`, sets `from_file = file_path` on every relationship whose source `(label, name)` is one of `result.nodes` with `properties.file == file_path`. All eight code extractors call it before they return. Module sources are never pinned: a Module has `source_file`, not `file`, and its name is already the path. File-less nodes are never pinned: the C++ out-of-class `Class` stub and API handler stubs. Target ends stay bare-name matched (over-linking by design). |
| G2 | **A replace retracts the file's own outgoing edges.** `_replace_file_nodes_tx` deletes every outgoing edge of a node the file owns (`n.file = F`, `n.source_file = F`, or the Module named `F`) before it re-upserts the extraction, in the same transaction. A cross-file target outside the batch still exists, so pass 1 re-creates its edge; a target inside the batch is re-created by pass 2, as today. There is one exception: `(:Service)-[:USES]->(x)` where `x` is not a `Volume`. A Python file's owning-service pass writes it (`_owning_service_relationships`), not the compose file. G2 only touches nodes with `file`/`source_file`, so claimed shared nodes (`Container`, `Datastore`, `Endpoint`, handler stubs) and provider nodes are never touched. Incoming edges (`MODIFIES`, `MENTIONS`, docs and provider edges) are kept, as before. G1 and G2 ship together: G2 without G1 would make a re-index drop over-linked edges that a fresh scan writes. |
| G3 | **Compose files and Containerfiles go through `replace_file_nodes`.** `_upsert_container_result` calls `engine.replace_file_nodes(repo_id, rel_path, nodes, rels)`. A removed `Service` is deleted as a stale file-scoped node. A `Container`/`Volume`/`Network` loses this file's claim, and is deleted once no file claims it (`_unclaim_source_tx`). With G2, removed `RUNS`/`USES Volume` edges go too. |
| G4 | **A full mentions re-index replaces the Document's `MENTIONS`.** A new `GraphEngine.replace_mentions(repo_id, doc_name, document, rels)` deletes `(:Document {name: doc_name})-[:MENTIONS]->()` and writes the Document and the new edges in one transaction. `index_file` uses it when `names is None`. The relink (`names=...`) stays additive. `replace_file_nodes` is not reused: a docs note at the same path has the same `source_file` and would be deleted as stale. |
| G5 | **Record each code file's by-name edges on its Module, and relink them when a batch adds their target.** Two reserved Module properties are written at extraction: `name_refs` and `name_ref_targets`. `name_refs` is a JSON string, with sorted keys, of the file's relationships that have no `to_file` (`IMPORTS`, `CALLS`, `EXTENDS`), each pinned per G1. `name_ref_targets` is their sorted distinct `to_name`s. After pass 2, `index_paths` asks the graph for every Module outside the batch (and outside the re-indexed referrers) whose `name_ref_targets` meets the batch's added names. It decodes those Modules' `name_refs`, keeps the edges whose `(to_label, to_name)` was added, and upserts them. No file is re-read and nothing is re-parsed, so there is no cap. This one lookup covers: an import target deleted and restored, a newly added module, a supertype in any directory, and `CALLS` from outside the batch, including a second same-named function (fresh-scan over-linking). |
| G6 | **Hidden and reserved.** `name_refs` and `name_ref_targets` join `RESERVED_NODE_PROPERTIES` and `describe_node`'s `_DESCRIBE_HIDDEN`, like `claims`. |
| G7 | **Upgrade.** A graph written before this slice keeps its wrong and stale edges, and has no `name_refs`, until each file is re-indexed. One `devgraph rescan` heals it (G2 resets every file, G1 stops the wrong edges, G5 fills `name_refs`). This is noted in PROJECT_STATUS. |

## Interactions checked

- **Watcher batch semantics.**
  - G2 runs inside each file's pass-1 transaction, so a reader never sees a
    file's edges half gone.
  - Pass 2 still re-creates the edges to targets in the same batch that
    sorted later.
  - G5 runs after pass 2, when every batch node exists. It skips batch files
    and `_find_referrers` files, because they were just fully re-indexed.
  - A delete-then-restore split across two watcher batches (git checkout,
    branch switch) is exactly G5's case: the restored nodes are absent from
    `previous_nodes`, so they count as added.
  - Within one batch, the nodes survive the replace, so their incoming edges
    are never lost in the first place.
  - A `full_scan` has no files outside the batch, so G5's lookup returns
    nothing.
- **Shared-node attribution.**
  - G2 never touches nodes keyed by `source`/`sources`.
  - G3 drops a compose file's claims through the existing unclaim path,
    inside the same transaction as the re-claim. A `Container` that another
    compose file or Containerfile also claims survives, and it is
    re-attributed from its first source.
  - The `Service -USES-> Datastore` exception in G2 keeps the Python owning
    pass's edges when a compose file is re-indexed.
- **Docs provider.**
  - Provider nodes carry no `file`/`source_file`, and provider edges are
    never built-in types (`delete_extracted_edges`), so G2 and G4 can't
    delete them.
  - Provider edges *into* code nodes are incoming edges, which G2 keeps.
  - `_relink_docs` already handles nodes added by G5's scenarios.
  - G4 deletes only `MENTIONS` from the `Document`, never a docs note that
    shares its path.
- **Mentions relink.**
  - Unchanged: a restored function's `MENTIONS` come back through
    `_mention_referrers`, within its existing cap.
  - Skip mode: adding a second node with an already-mentioned name still
    leaves the old edge until the doc is re-indexed or rescanned. With G4,
    a re-index now does drop it.

## Out of scope

- **Same-file sibling collisions** (file+scope identity) and IMPORTS-based
  call precision. These are the handover's open items 1 and 2.
- **Edges from claimed shared nodes that a file stops producing.**
  - `Endpoint -IMPLEMENTS->` and `Endpoint -CALLS-> Service` from a Python
    file.
  - `Service -USES-> Datastore` when a Python file stops using a store.

  These nodes are claim-keyed, not file-keyed, so G2 can't tell whose edge
  is whose. Recorded in PROJECT_STATUS as a remaining gap.
- **Recency and `MODIFIES` after delete-and-restore.** This still needs
  `devgraph rescan --full`, as already documented.
- **Deletions don't relink.** For example, a skip-mode ambiguous name stays
  unlinked after the duplicate goes.

## Docs to update

- **PROJECT_STATUS.**
  - In "Remaining gaps", drop the `IMPORTS`/supertype/`CALLS` cross-batch
    gap.
  - In the watcher paragraph, drop the three "Pre-existing bugs found by the
    watcher fuzz".
  - Correct the skip-mode sentence.
  - Add the G7 upgrade note and the claimed-node edge gap.
- **`HANDOVER_node_identity.md`.** Note that `CALLS`/`EXTENDS`/nested
  `CONTAINS` sources are now file-pinned, and only targets stay bare-name.
- **The `GraphRelationship` docstring** (`common.py`). The source end of an
  extracted edge is known.
