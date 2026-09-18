# DevGraph

A local-first knowledge graph over explicitly registered codebases, queried through
MCP by AI assistants and through the CLI directly. This file is the project's
glossary: what each term means. Preferred terms apply to DevGraph concepts,
not unrelated uses in source languages or existing API identifiers.

## The graph

**Entity**:
A thing in an indexed codebase that DevGraph tracks — a Module, Class, Function,
Service, Endpoint, Container, and so on.
_Avoid_: component, element, object, item

**Node**:
The graph vertex representing an entity. Say "node" for graph structure — labels,
properties, MERGE identity, orphans. Say "entity" for the thing in the codebase.

**Edge**:
A connection between two nodes.
_Avoid_: relationship
_Note_: the code identifiers stay `RELATIONSHIP_TYPES` / `rel_type`; this rule
governs prose.

**Label**:
An entity's type, drawn from `NODE_LABELS` in `devgraph/graph/schema.py`.
_Avoid_: node type, entity type, kind
_Note_: `entity_type` is a shipped MCP parameter name and `kind` is a Rust-only
node property; neither is the general term.

**`repo_id`**:
The scoping key every graph object carries, and the first part of every node's
identity. A hard security boundary, not a UI filter — a query without it is a bug,
not a broad search.

**File-scoped**:
Said of a node whose identity includes the file that produced it, so two files
declaring the same name yield two nodes. Scoping is decided per node by whether it
carries a `file` property, not by its label. `Container` is deliberately not
file-scoped: it names a shared base image, and two Containerfiles building from the
same image should merge into one node.

**Provenance**:
The record of which source files produced a node, used to scope deletion when one
of them changes or disappears. A node with several producing files survives until
the last of them stops claiming it.

**Container**:
The node label for a base image. When the OCI sense is also in play, say "the
`Container` node" for the graph label and "a running container" for the runtime —
the same rule `Repository` needs.

## Registration

**Repository**:
A codebase the user has explicitly registered. Distinct from `Repository`, the node
label for that codebase's scoping root — say "the registered repo" for the former
and "the `Repository` node" for the latter when both are in play.

**Registry**:
The SQLite allowlist of repositories DevGraph is permitted to touch, plus each
one's per-repo opt-in flags. The registry, not the graph, answers "which repos
exist".
_Avoid_: mounted repos, mount list

**Register**:
To add a repository to the registry.
_Avoid_: mount
_Note_: "mount" now means only the literal Docker bind-mount in `README.md`.
"Register" is also used for two unrelated acts — registering DevGraph with an MCP
client, and an MCP process registering as a tray holder. Qualify it when the
object isn't a repository.

## Running

**Tray app**:
The always-on local process hosting the watcher, the indexer, and the dashboard.
`HeadlessAgent` is the same role without the tray UI.
_Avoid_: the agent, DevGraph Agent, the always-on agent shell, the watcher process

**Agent**:
An AI assistant consuming DevGraph through MCP. Reserved for that sense only —
never for DevGraph's own process.

**Holder**:
An MCP server process that has declared it needs the tray app running. The tray
app shuts down when its last holder disconnects.

## Indexing

**Extractor**:
A parser that turns source files of one kind into entities and edges. Named by what
it reads, not by what it produces — the language extractors, the docs extractor,
the mentions extractor. One extractor may hold more than one decoder when a single
file format carries more than one encoding.

**Decoder**:
One encoding-specific reader inside an extractor. Only formats that mix encodings
in a single file need more than one.

**Rescan**:
Re-running the full file walk for a registered repo. Idempotent, `MERGE`-based, and
identical to what registration does.
_Avoid_: reindex, full rebuild, full scan (as a user-facing verb)

**Recency**:
Git-derived timestamps denormalized onto entity nodes so "what changed lately" is a
property lookup rather than a history walk. Staged forward-only: a recency value
never moves backwards.

## Human-authored knowledge

**Note**:
A Markdown file with front matter declaring it a `Requirement`, `DesignDecision`, or
`ArchitectureNote`. Added with `devgraph annotate`.
_Avoid_: doc, design doc, design intent

**Mention**:
A syntactically plausible reference, found in Markdown, to an entity that already
exists in the graph. Mentions link to existing entities; they never create new ones.

**Document**:
The node representing a scanned Markdown file itself. A single `.md` file can
produce both a `Document` node and a Note node — these are two independent
representations of the same file, and both persist by design.
