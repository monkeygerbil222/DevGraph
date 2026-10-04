# Custom provider and script sandbox (slice E) — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §2 (`custom` provider), §3 (script
sandbox), §7 (script-defined tools). Stacked on #45, the tip of the epic stack.
**Design only, for sign-off.** No slice below starts until this document is
approved or redirected; nothing in it executes code today.

What exists now and is reused unchanged unless this document says otherwise:

- `custom` is accepted as inert data: `PROVIDER_KINDS = ("builtin", "custom",
  "filesystem")` and `CustomProvider(name, params)` in
  `devgraph/config/project_schema.py`, validated and never loaded.
- The filesystem provider (`devgraph/indexer/providers/filesystem.py`) and its
  wiring into `index_paths` / `remove_paths` / `full_scan`
  (`devgraph/indexer/dispatch.py`): provider-owned nodes tagged by `extractor`,
  per-path delete, full-scan prune, an invalid schema skipping the provider
  without deleting anything.
- Per-user-type uniqueness constraints on `(repo_id, *key)`
  (`_user_constraint_statement`). The engine itself still MERGEs every node on
  `(repo_id, name[, file])`; the filesystem provider gets away with it because
  its key is `[path]` and it writes `name = path`. General declared-key MERGE
  does not exist yet and is part of this slice (E3).
- Schema rescan semantics (pending schema, 5-minute debounce, `rescan --now`,
  removed-type cleanup) and the per-repository project-config switch
  (`project_config_enabled`, registry column, **default on**).
- The MCP tool plane (`devgraph/mcp/tool_plane.py`): session scope pinned at
  startup, read-only Cypher tools with injected `repo_id`, timeouts and row
  caps; no MCP tool writes except `run_cypher`, which is off by default.
- The dashboard: loopback bind, `_LocalHostOnlyMiddleware` Host guard,
  `_reject_cross_site` on writes, no authentication.
- Privacy stance: `telemetry_enabled = False`; MCP telemetry is local,
  metadata-only (no arguments, no query text) and never leaves the machine.

---

## 1. Goal and non-goals

**Goal.** Let a repository declare node types and relationships that come from
a small user-written Python script, run that script so that it cannot touch the
host, the network or other repositories, and write its output to the graph
only through DevGraph's own validated, declared-key MERGE. The epic's
acceptance line is the bar: "Sandboxed script cannot import outside the
allowlist, cannot reach the filesystem, and a runaway script fails only its
own file's extraction."

**In scope.**

- `custom` as a node source as well as a relationship provider.
- The `derive(ctx)` contract, the sandbox runner, limits and error taxonomy.
- Trust: per-repository opt-in (off by default) plus a hash-pinned approval
  per script, stored outside the repository.
- Surfaces: CLI, `devgraph doctor`, dashboard status and revoke, tray notice.

**Non-goals.**

- **The `git`, `ast` and `docs` declarative providers are out of slice E.**
  DevGraph already extracts git history (commits, `MODIFIES`), code structure
  in eight languages (`CALLS`, `IMPORTS`, `CONTAINS`, `EXTENDS`) and doc
  mentions (`MENTIONS`) as built-in extractors, and a project schema can
  already reuse those relationship types between its own labels with
  `provider: builtin`. What is missing is a *declarative* way to aim those
  sources at user-defined types (commit selectors, tree-sitter queries,
  mention patterns). Each is a separate format design with no dependency on
  the sandbox, and none is on the epic's acceptance list. Meanwhile `custom`
  covers the gap for anyone who needs it now. Recommendation (Q3): close
  epic #1 after E and file the three as follow-up issues.
- **Script-defined and composition MCP tools are deferred** (§7).
- No third-party packages inside scripts, no network for scripts, no writes
  by scripts, no Windows-native or macOS-native sandbox primitives.
- Not a security boundary to bet a shared machine on. The in-repo docs will
  say so in the epic's words: defence in depth, proportionate for a
  local-first, single-user tool indexing repositories the user registered.

---

## 2. Threat model

**Assets.** The user's home directory and secrets (SSH keys, tokens, browser
data); the network; other repositories' graph data; the Neo4j credentials;
the DevGraph processes (tray, watcher, MCP server, dashboard); CPU and memory.

**Trusted.** The user at an interactive terminal; the DevGraph install; the
registry database under `~/.devgraph/`.

| # | Threat | Mitigation |
|---|--------|------------|
| T1 | **Scripts the user wrote.** Bugs, not malice: infinite loops, huge output, wrong keys. | Limits (§6); output validated against the schema (§3); per-file failure keeps the last good state. |
| T2 | **A repository the user didn't author** (cloned, third-party, vendored) ships `.devgraph/providers/*.py` and a schema naming it. | Scripts are off per repository by default, independent of the project-config switch (which is on by default). `devgraph add` of a cloned repository never runs a script. Even when enabled, each script needs its own approval pinned to its hash. |
| T3 | **A malicious `.devgraph` change in a pulled branch**: an approved script edited, a new provider added, a provider's `inputs` widened. | The approval pins the script bytes *and* the canonical provider declaration (Q6). Any change stops that provider before it runs; the last good graph state stays; the user is told and re-approves. A new provider name has no approval at all. |
| T4 | **The MCP client as an untrusted caller** (a model, possibly prompt-injected). | MCP gains no tool that enables, approves or runs a script. It can read derived nodes like any other node. Script output is untrusted text reaching an agent, exactly as file contents already are; no new privilege. |
| T5 | **Supply chain**: the sandbox image, Python inside it, packages a script wants. | Image pinned by digest, pulled once by an explicit setup step, never updated implicitly. No package installs, no `requirements` file; standard library allowlist only. The runner shim ships with DevGraph and is passed over stdin, not baked into an image. |
| T6 | **Resource exhaustion**: CPU spin, memory bomb, fork bomb, output flood, an enormous input set. | cgroup CPU, memory and pid limits; a host-side wall-clock kill; caps on input files, input bytes and output records, checked before and while writing (§6). |
| T7 | **Exfiltration over the network.** | `--network=none`. No host environment variables are passed. Nothing else is reachable. |
| T8 | **Writes to the graph outside the repository's scope.** | Scripts never write: they return records. DevGraph injects `repo_id`, accepts only labels and types declared for that provider, rejects reserved properties, and matches relationship endpoints only inside the repository (§3). |
| T9 | **Reads outside the repository.** | The container has no mounts. DevGraph reads the declared inputs with its own walker (repo-relative globs, ignored directories excluded, symlinks refused) and streams their contents in. There is no path for the script to open. |
| T10 | **A web page reaching the dashboard** to approve a script. | Approval is not a dashboard action (Q5); revoke and disable are, behind the existing Host guard and cross-site check. |
| T11 | **Container escape** through a kernel or runtime bug. | Residual. Rootless Podman on Linux confines it to the user's own privileges; on macOS and Windows the container also runs inside the Podman machine VM. This is why the docs say "defence in depth". |

**Out of scope.** The user acting against themselves; malware already running
as the user (it can edit the registry directly); a compromised DevGraph
install or Podman binary.

---

## 3. The `derive(ctx)` contract

### 3.1 Declaration

A provider is one script at a fixed, conventional path:
`<repo>/.devgraph/providers/<name>.py`, where `<name>` matches the existing
`custom.name` identifier pattern. No path field exists, so there is nothing to
traverse; the file must be a regular file, not a symlink, at most 256 KiB.
`.devgraph` is already in `IGNORED_DIR_NAMES`, so scripts are never indexed
as code.

```yaml
version: 1
custom_providers:
  - name: runbook_links
    inputs: ["docs/runbooks/**/*.md"]   # repo-relative globs; required
    params: {owner_prefix: "team-"}
node_types:
  - label: Runbook
    key: [slug]
    metadata: [{name: slug, required: true}, {name: owner}]
    source: {provider: custom, name: runbook_links}
relationships:
  - type: DOCUMENTS
    provider: custom
    custom: {name: runbook_links}
    from: Runbook
    to: Service
```

Schema changes (validated in E1, inert until E3):

- New top-level `custom_providers`: `name`, `inputs` (non-empty; each glob
  repo-relative, no `..`, no leading `/`, no drive letter), `params` (the
  existing scalar-only `ScalarParam` map).
- `NodeSource` gains `provider: custom` with `name`; `kind` stays
  filesystem-only. A custom node type has any declared key (not forced to
  `[path]`).
- Every `custom.name` and every custom `source.name` must name a declared
  `custom_providers` entry. A relationship-level `custom.params` stays valid
  (files written today must not break) and reaches the script as that
  declaration's params.
- A custom relationship may end at a built-in label (`to: Service` above).

### 3.2 Inputs

`derive` is called **once per input file** (Q11): incremental indexing
re-derives only changed files, and a failure is confined to that file.

`ctx` is an immutable object built by the runner from what DevGraph sends:

| Field | Content |
|-------|---------|
| `ctx.path` | repo-relative POSIX path of this input file |
| `ctx.text` | its content, decoded UTF-8 (`errors="replace"`) |
| `ctx.tree` | sorted tuple of every repo-relative indexable path (names only, no contents) |
| `ctx.params` | the provider's `params`; read-only mapping |
| `ctx.node_types` | declared node types this provider may emit: label → key tuple, metadata name → type |
| `ctx.relationships` | declared types this provider may emit: type → from labels, to label, that declaration's `custom.params` |

DevGraph, not the script, reads the files: the same walker as `full_scan`,
filtered by the declared globs, regular files only, symlinks refused, ignored
directories excluded. This is the epic's "no filesystem access" guarantee
implemented as read access mediated by DevGraph and scoped to the repository.

### 3.3 Outputs

`derive(ctx)` returns a list of records (a single dict is accepted as a list
of one):

```python
def derive(ctx):
    slug = ctx.path.rsplit("/", 1)[-1].removesuffix(".md")
    out = [{"node": "Runbook", "props": {"slug": slug, "owner": ctx.params["owner_prefix"] + "ops"}}]
    for service in re.findall(r"^service: (\S+)$", ctx.text, re.M):
        out.append({"rel": "DOCUMENTS",
                    "from": {"label": "Runbook", "key": {"slug": slug}},
                    "to": {"label": "Service", "key": {"name": service}}})
    return out
```

DevGraph validates every record before any write, and rejects the whole file's
output on the first violation (named error `schema_violation`, with the record
index and reason):

- `node` must be a label whose source is this provider; `props` keys must be
  declared metadata with matching types; every key component and every
  `required` field present; no reserved property (`repo_id`, `name`, `file`,
  `source_file`, `source`, `sources`, `extractor`).
- `rel` must be a type declared for this provider; `from.label` in its `from`
  list, `to.label` equal to its `to`; endpoint `key` exactly the endpoint
  type's declared key. For a built-in endpoint the key is `{name}`, or
  `{name, file}` for file-scoped labels (`_is_file_scoped`), matching how the
  engine identifies them today. Relationship properties are not supported in
  E (rejected).
- Strings at most 4 KiB each; values only `str | int | float | bool | None`.

**Write path (E3).** DevGraph sets `repo_id`, `extractor = "custom:<name>"`
and claims the node for this input file in `sources` (the existing
multi-producer pattern), then MERGEs on `(repo_id, *declared key)`, which the
existing uniqueness constraint already covers. Relationship endpoints are
`MATCH`ed by `repo_id` plus key, so an edge can never reach another
repository; an endpoint that does not exist is dropped and counted, not
created. Edges carry `source = <input path>` so re-deriving a file replaces
exactly its edges. Per-file replacement mirrors `replace_file_nodes`: unclaim
this file, upsert the new records, delete custom nodes no file claims.

### 3.4 Determinism and idempotence

- Same script, same inputs, same params ⇒ same records. The runner sets
  `PYTHONHASHSEED=0`, `TZ=UTC`, a fixed locale and passes files in sorted
  order; `time`, `random`, `os` (beyond `os.path`) are not in the import
  allowlist. DevGraph sorts and de-duplicates records before writing.
- MERGE on the declared key makes a re-run a no-op on unchanged input; a full
  rescan prunes `custom:<name>` nodes that no current input produces (as the
  filesystem provider's reconcile does).
- `devgraph config scripts run <repo> <name> --dry-run --twice` runs the
  provider twice and reports any difference, so authors can check
  determinism; it writes nothing.

---

## 4. Isolation options

The static allowlist is a lint, not a boundary. CPython has too many paths to
the interpreter's internals for an AST scan to close; the table shows why
every option below needs a real boundary underneath it.

| Escape | Static rule | Caught? |
|--------|-------------|---------|
| `().__class__.__base__.__subclasses__()` | dunder attribute access | yes |
| `getattr(f, "__glo" + "bals__")` | dunder attribute access | **no** (built at run time); `getattr` can be denied, then `operator.attrgetter`, `vars()` … |
| `"{0.__globals__}".format(f)` | dunder attribute access | **no** (inside a string) |
| `(x for x in ()).gi_frame.f_back.f_globals` | not a dunder | **no** unless frame attributes are denylisted one by one |
| `import socket` / `__import__("socket")` | import allowlist; `__import__` as a dunder name | yes / yes |
| `exec`, `eval`, `compile`, `while True:` | name and AST rules | yes; `while 1:` and recursion are not "while True" |

### 4.1 Options compared

| | Subprocess + rlimits + seccomp/Landlock | Container (Podman; Docker) | WASM (CPython on WASI in wasmtime) | In-process + static allowlist |
|---|---|---|---|---|
| Filesystem isolation | Linux: Landlock (5.13+) can deny all paths. macOS: none (`sandbox-exec` deprecated). Windows: none without AppContainer | No mounts at all; read-only rootfs | No preopened directories | None |
| Network isolation | Linux: seccomp denies `socket`. Others: none | `--network=none` | No sockets in WASI preview 1 | None |
| CPU / memory / pids | rlimits (Linux, macOS); Job Objects (Windows) | cgroups v2 | wasmtime fuel and store memory limits | Thread timeout only; memory unbounded |
| Linux | good, with a new native dependency (libseccomp or hand-written syscall filters) | good (rootless; needs cgroup v2 controller delegation) | good | trivially |
| macOS | rlimits only: **no isolation** | Podman machine (Linux VM) | good | trivially |
| Windows | Job Objects only: **no isolation** | Podman machine (WSL2 VM) | good | trivially |
| New dependency | libseccomp binding; per-OS code paths | none: Podman is already required for Neo4j | `wasmtime` wheel plus a ~25 MB CPython WASI build to pin and update | none |
| Start-up cost | ~50 ms | ~0.3–1 s per container (per run, not per file) | ~0.2–0.5 s with a cached compiled module | none |
| Epic fit | — | §3: "Execute inside the existing Podman container" | — | §3's static rules alone |
| Verdict | Strong on Linux only; uneven guarantees across platforms are the wrong shape for a consent prompt | **Recommended** | Strong and portable; runner-up | Not a boundary; rejected as the sole layer |

### 4.2 Recommendation: a dedicated, ephemeral, mount-free Podman container

Reasons: Podman is already a hard dependency on every supported platform;
the epic settled on it; it gives the same filesystem and network guarantees on
Linux, macOS and Windows (the latter two additionally inside a VM); limits are
kernel-enforced; and it adds no Python dependency.

Not "the existing" container literally (Q2): the Neo4j container holds the
graph and its credentials, so a script running there could bypass §3's write
path entirely. Instead:

```
podman run --rm -i --network=none --read-only --cap-drop=all
  --security-opt=no-new-privileges --user=65534:65534
  --pids-limit=32 --memory=256m --memory-swap=256m --cpus=1
  --ulimit=nofile=64 --env=PYTHONHASHSEED=0 --env=TZ=UTC
  --name=devgraph-sandbox-<run id>  <image>@sha256:<digest>
  python -I -S -c "<runner bootstrap>"
```

- **No mounts.** The runner shim, the script source, `ctx.tree`, params and
  the input files go in over stdin as length-prefixed JSON frames; one result
  frame per input file comes back on stdout; stderr is captured, capped at
  8 KiB. No volume means no SELinux relabelling, no Windows path translation
  and no symlink to follow.
- **One container per provider run**, not per file. A full scan of 2,000 inputs
  pays start-up once; an incremental save pays it once per batch.
- Inside, the runner applies the import allowlist (`re`, `json`, `pathlib`
  pure-path classes, `os.path`, `string`, `textwrap`, `collections`,
  `itertools`, `functools`, `dataclasses`, `typing`, `math`, `fnmatch`),
  restricted builtins (no `open`, `exec`, `eval`, `compile`, `__import__`,
  `input`, `breakpoint`) and a per-call timer. These, like the static scan,
  are hygiene; the container is the boundary.
- Image: a slim official Python 3.13 image pinned by digest in DevGraph's
  source, pulled by `devgraph sandbox setup` (and offered by the setup menu).
  Changing the digest is a reviewed code change.
- `podman info` is checked before the first run: rootless, cgroup v2, and the
  `memory`, `cpu` and `pids` controllers delegated. If any is missing,
  DevGraph **refuses to run** with a doctor hint rather than run without
  limits. There is **no unsandboxed fallback** (Q1).
- Docker is accepted as the runtime when the user has configured it instead
  of Podman (same flags; rootless Docker recommended); it is not the default.
- The headless Docker image (`devgraph.agent.headless`) has no container
  runtime inside it: custom providers are unavailable there in E, reported as
  such by doctor and the dashboard (Q8).

WASM remains the documented alternative should the container start-up cost or
the headless gap prove a problem; the runner protocol (frames over
stdin/stdout) is deliberately runtime-neutral so a WASM runner could replace
the `podman run` line without changing §3 or §5.

---

## 5. Trust and consent

Two independent gates, both stored in the registry database (outside every
repository), both required, both checked immediately before every run:

1. **Per-repository opt-in**: a new registry column `scripts_enabled`,
   **default 0**, independent of `project_config_enabled` (default 1). With
   the project config on and scripts off, the schema's custom node types and
   relationships are declared (constraints provisioned) but never filled.
2. **Per-script approval**: a new registry table
   `script_trust(repo_id, provider, digest, approved_at)`. `digest` is
   SHA-256 over the script bytes plus the canonical JSON of that provider's
   declaration (`inputs`, `params`, and the node and relationship types it may
   emit), so widening inputs or retargeting output re-prompts (Q6). Up to five
   digests per `(repo_id, provider)` are kept, so switching between two
   approved branches does not re-prompt (Q7).

**Prompt** (`devgraph config scripts approve <repo> [<name>]`, interactive TTY
only): shows the repository, provider name, script path and size, the declared
inputs and output types, the static-scan findings (advisory; a hard reject
stops here), and the script text, or a unified diff against the most recent
approved version when one exists. The user types the provider name to
approve. `--sha256 <hex>` approves non-interactively only when the given digest
equals the current one (for CI and scripted setups, Q10); there is no
`--yes`.

**On change.** A digest not in the approved set means that provider does not
run. Its existing nodes and edges stay (last good state), it is reported as
`awaiting approval`, and approving it triggers one full provider run.

| Surface | Behaviour |
|---------|-----------|
| `devgraph add` / `register` | Registers with scripts off. If the schema declares custom providers, prints that they will not run and the two commands that would enable them. |
| CLI | `devgraph config scripts list [--repo]` (state per provider: disabled, awaiting approval, approved, failing), `show`, `approve`, `revoke`, `enable <repo>`, `disable <repo>`, `run --dry-run`. |
| Tray agent / watcher | Never prompts, never approves. Unapproved providers are skipped; a tray notification fires once per new digest. |
| Headless agent, CI | Never prompts. Runs only already-approved digests (approval via `--sha256` in CI). |
| MCP | No script controls at all. `devgraph://project-tools` and the envelope `notices` gain nothing in E. |
| Dashboard | Per project card: scripts on/off state, each provider's state, short digest, approved-at, last run outcome and counts, error code. Actions: **disable** scripts for the repository and **revoke** an approval, both behind the existing guards with a dry run. Enable and approve are CLI-only (Q5). |
| `devgraph doctor` | Lists providers awaiting approval, failing providers, sandbox readiness (`podman info` checks, image present at the pinned digest). |

**Revoke** removes the digest(s); **disable** turns the repository's opt-in
off. Both stop future runs immediately. Neither deletes graph data by itself;
the next full rescan prunes the provider's nodes, exactly as removing the
declaration would. `devgraph remove` deletes the repository's trust rows.

---

## 6. Limits

Defaults (settings, per-repository override not offered in E; Q9):

| Limit | Default | Enforced by | On breach |
|-------|---------|-------------|-----------|
| Input files per run | 5,000 | host, before start | whole run refused: `input_cap` |
| Input bytes per file / per run | 1 MiB / 32 MiB | host, before start | file skipped: `input_cap` / run refused |
| CPU per `derive` call | 2 s | runner timer | that file: `timeout` |
| Wall clock per run | 30 s + 50 ms per input file, max 300 s | host kill of the container | in-flight file: `timeout`; see below |
| Memory | 256 MiB, no swap | cgroup | container killed: `memory` |
| Processes | 32 | cgroup | `crash` (fork fails inside) |
| Output per file | 1,000 records, 1 MiB serialised | host, while reading frames | that file: `output_cap` |
| Output per run | 50,000 records | host | rest of run: `output_cap` |
| Network | none | `--network=none` | the call fails inside the script |

**Breach semantics.** Errors are per input file wherever possible: a failed
file keeps its last good graph state (nothing is unclaimed or deleted for it),
and the run continues. When the container itself dies (memory, wall clock,
crash), results already received are applied, the in-flight file is failed
with the named error, and the remaining files are retried once in a fresh
container; a second death marks them `aborted` with last good state kept. A
failure never aborts built-in extraction or other providers, and never
retries in a loop: the next attempt is the next change event or rescan.

---

## 7. Script tools for MCP: deferred

Script-defined and composition tools (epic §7) are **out of slice E**
(Q4). Reasons:

- They run at call time on arguments chosen by the untrusted caller (T4), so
  the input is adversarial by default, unlike indexing input chosen by the
  repository owner.
- A useful tool script needs graph data. That means either giving the sandbox
  a read channel into Neo4j (a new boundary to design) or having DevGraph run
  declared read-only Cypher first and pass the rows in, which is a composition
  tool rather than a script tool.
- Container start-up (0.3–1 s) on every call is a poor fit for an
  interactive tool; a warm pool is a new long-lived process to secure.
- The read-only Cypher tools already cover the epic's stated goal ("draw
  exactly the information they want").

When picked up, they reuse the §4 runner, the §5 trust store (approval keyed
by tool id), locked-name resolution, scope pinning and the existing envelope.

---

## 8. Failure, observability and telemetry

- **Run record.** Each provider run appends one metadata-only line to a local
  ring file `~/.devgraph/sandbox_runs.jsonl` (500 entries, same rotation as
  `mcp_telemetry.jsonl`): `ts`, `repo_id`, `provider`, short digest, input
  file count, nodes and relationships written, dropped endpoints, duration,
  outcome, and error-code counts. Never file contents, params values, script
  output or stderr.
- **Errors** carry a stable code (`awaiting_approval`, `disabled`,
  `sandbox_unavailable`, `static_reject`, `input_cap`, `timeout`, `memory`,
  `output_cap`, `schema_violation`, `crash`, `aborted`) plus the input path
  and a one-line reason. The dashboard and doctor show these.
- **stderr** and the traceback are shown in full by `run --dry-run`; in agent
  runs only the last 2 KiB of a failing file's stderr goes to the local log
  file (`devgraph logs`), since a script may print file content.
- **Privacy.** Nothing is sent anywhere. `telemetry_enabled` stays off and is
  not consulted; the run record is local diagnostics like the MCP store.
  The README and PROJECT_STATUS privacy wording gains one sentence for it.

---

## 9. Open questions for the user

1. **Isolation runtime.** Rootless Podman container, mount-free, with no
   unsandboxed fallback when Podman or cgroup delegation is missing.
   *Recommend: yes.*
2. **Which container.** The epic says "the existing Podman container"; this
   design uses a dedicated, ephemeral container from a digest-pinned image,
   because the Neo4j container holds the graph and its credentials.
   *Recommend: dedicated container.*
3. **`git`, `ast`, `docs` declarative providers.** Out of E; close epic #1
   after E and file them as follow-up issues. *Recommend: yes.*
4. **Script and composition MCP tools.** Defer to a follow-up (§7).
   *Recommend: defer.*
5. **Where approval happens.** CLI with a TTY only; the dashboard shows state
   and can disable or revoke but not enable or approve. *Recommend: CLI-only
   approval.*
6. **What the hash pins.** Script bytes plus the provider's declaration
   (inputs, params, output types), not the script alone. *Recommend: both.*
7. **Branch switching.** Keep up to five approved digests per provider so
   moving between approved branches does not re-prompt. *Recommend: five.*
8. **Headless Docker image.** Custom providers unavailable there in E (no
   Docker socket mount, no WASM runner yet). *Recommend: unavailable, reported
   by doctor; revisit with a WASM runner if asked for.*
9. **Default limits.** The §6 table, global settings only, no per-repository
   override in E. *Recommend: as tabled.*
10. **Non-interactive approval.** `approve --sha256 <hex>` succeeds only for
    the matching digest; no blanket `--yes`. *Recommend: yes.*
11. **Call granularity.** `derive` once per input file, not once per
    repository. *Recommend: per file* (incremental, failure isolation); a
    whole-tree mode can follow if a real script needs cross-file state.
12. **Script location.** Fixed convention `.devgraph/providers/<name>.py`, no
    path field. *Recommend: convention.*

---

## 10. Slice plan

Each slice is one reviewable PR stacked on the previous one. None starts
before this document is signed off.

| Slice | Content | Executes user code? | User sign-off before merge |
|-------|---------|--------------------|----------------------------|
| **E1** Schema, trust store, static scan | `custom_providers`, custom `NodeSource`, cross-reference validation, JSON Schema, `config show/validate` output; registry `scripts_enabled` column and `script_trust` table with migrations; digest computation; static scanner (hard reject + findings); `config scripts list/show/approve/revoke/enable/disable`; doctor reporting; `add` notice. | No | **Yes**: it fixes the consent UX and prompt wording. |
| **E2** Sandbox runner | `devgraph/sandbox/`: Podman invocation, readiness checks, frame protocol, runner shim and in-container allowlist, limits and error taxonomy, `devgraph sandbox setup`, `config scripts run --dry-run [--twice]`. Adversarial test suite (network, fork bomb, memory, CPU spin, `/etc` and `$HOME` reads, env leakage, stdout flooding, each §4 escape) against real Podman, skipped where Podman is absent. Writes nothing to the graph. | **Yes**, dry run only | **Yes**: first code that runs repository scripts. |
| **E3** Provider wiring | Output validation (§3.3); declared-key MERGE and keyed endpoint MATCH in `GraphEngine`; per-file claim, replace and prune for `custom:<name>`; `index_paths` / `remove_paths` / `full_scan` and rescan integration; breach semantics (§6); run records (§8). Backward-compat snapshot: no schema file ⇒ identical graph. | Yes, from the indexer | **Yes**: scripts start writing to the graph. |
| **E4** Surfaces and docs | Dashboard project-card section (state, digest, last run, errors; disable and revoke with dry run); tray notification; README, PROJECT_STATUS and an in-repo "defence in depth, not a security boundary" note; end-to-end acceptance against the epic's sandbox criterion. | No new paths | No, unless Q5 changes to allow dashboard approval. |

## Testing (summary)

E1: loader rules, migrations, digest stability across line endings and
platforms, scanner table rows. E2: live Podman adversarial suite and limit
breaches, protocol fuzzing of malformed frames. E3: live Neo4j per-file
lifecycle, cross-repository endpoint isolation, reserved-property rejection,
determinism check, backward-compatibility snapshot. E4: dashboard route and JS
harness tests, the epic's acceptance line end to end.
