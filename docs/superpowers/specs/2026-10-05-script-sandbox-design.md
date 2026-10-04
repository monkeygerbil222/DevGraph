# Custom provider and script sandbox (slice E) — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §2 (`custom` provider), §3 (script
sandbox), §7 (script-defined tools). Stacked on #45, the tip of the epic stack.
**Design only, for sign-off.** No slice below starts until this document is
approved or redirected; nothing in it executes code today.

**Revision 2.** Revised after an adversarial security review. The main
changes: sandbox settings and gates no longer come from `.env` or environment
variables and fail closed (§5.1); the Podman invocation is hardened and checked
after creation (§4.3); inputs are read by a new no-follow reader restricted to
tracked files (§3.2); custom provenance is isolated from built-in cleanup
(§3.4); the approved text is exactly the executed text (§5.3); the host treats
every frame and record as hostile (§4.4, §3.3); the slice plan gains a
prerequisite slice and a tests-first slice (§10).

What exists now, verified against the stack tip:

- `custom` is accepted as inert data: `PROVIDER_KINDS = ("builtin", "custom",
  "filesystem")` and `CustomProvider(name, params)` in
  `devgraph/config/project_schema.py`, validated and never loaded. `name`
  follows `PROPERTY_NAME_PATTERN` (lower-case ASCII identifier, ≤ 64 chars).
- The filesystem provider (`devgraph/indexer/providers/filesystem.py`) and its
  wiring into `index_paths` / `remove_paths` / `full_scan`
  (`devgraph/indexer/dispatch.py`). Its nodes are tagged by `extractor` and
  keyed by path in `name`; `_DELETE_EXTRACTED_PATHS_CYPHER` and
  `_PRUNE_EXTRACTED_CYPHER` (`devgraph/graph/engine.py`) match on `extractor`
  **and `name`**, so they cannot serve a provider whose key is not `name`.
- Built-in multi-producer nodes are claimed in `source`/`sources`;
  `_UNCLAIM_SOURCE_CYPHER` matches *any* node in the repository with no
  `file`/`source_file` whose `source` or `sources` names the file, whatever
  produced it. Reusing `sources` for custom nodes would let a built-in
  re-index of the same path unclaim and delete them (§3.4).
- Per-user-type uniqueness constraints on `(repo_id, *key)`
  (`_user_constraint_statement`). The engine still MERGEs every node on
  `(repo_id, name[, file])`. General declared-key MERGE does not exist yet
  (E3).
- `_indexable_paths` walks with `rglob` and `Path.is_file()`, which follows
  symlinked files: the current walker does **not** refuse symlinks, so §3.2
  specifies a new reader rather than reusing it.
- Schema rescan semantics (pending schema, 5-minute debounce, `rescan --now`,
  removed-type cleanup) and the per-repository project-config switch
  (`project_config_enabled`, registry column, **default on**). The switch
  lookup (`devgraph/config/project_switch.py`) fails **open**: a missing,
  locked or unreadable registry, or an unknown path, reads as "enabled". That
  is acceptable for declarative config and wrong for scripts (§5.1).
- `Settings` (`devgraph/config/settings.py`) reads `env_file=".env"` relative
  to the current working directory and `DEVGRAPH_*` environment variables;
  `registry_db_path` is one of those settings.
- The MCP tool plane (`devgraph/mcp/tool_plane.py`): scope pinned at startup,
  read-only Cypher tools with injected `repo_id`; no writing tool except
  `run_cypher`, off by default.
- The dashboard: loopback bind, `_LocalHostOnlyMiddleware`,
  `_reject_cross_site_config` on config writes, no authentication. Its schema
  write routes (`POST/PUT/DELETE /config/{scope}/schema/{section}[/{name}]`)
  back the form editor, "Copy to…" and add/replace. `escapeHtmlVal` in
  `static/index.html` escapes `& < >` but not quotes.
- Privacy: `telemetry_enabled = False`; MCP telemetry is local and
  metadata-only.

---

## 0. Prerequisites in today's code (E0.5)

Three defects in present code undermine any trust decision built on top of
them. They are being triaged separately and are referenced here only
generically. **E1 does not start until all three are fixed and merged**:

1. **Working-directory configuration.** Settings are read from a `.env` in the
   current working directory, so running DevGraph inside a cloned repository
   lets that repository redirect settings such as the registry location.
2. **Symlink following.** Repository walks and reads follow symlinks, so a
   repository can make DevGraph read files outside it.
3. **String-prefix containment.** At least one "is this path inside the
   repository" check compares path strings by prefix rather than by path
   components.

E1 additionally never relies on the fixes alone: sandbox settings bypass
`Settings` entirely (§5.1) and inputs use the dedicated reader (§3.2).

---

## 1. Goal and non-goals

**Goal.** Let a repository declare node types and relationships that come from
a small user-written Python script, run that script so that it cannot touch the
host, the network or other repositories, and write its output only through
DevGraph's own validated, declared-key MERGE. The epic's acceptance line is
the bar: "Sandboxed script cannot import outside the allowlist, cannot reach
the filesystem, and a runaway script fails only its own file's extraction."

**In scope.** `custom` as a node source and a relationship provider; the
`derive(ctx)` contract, runner, limits and error taxonomy; trust (per-repository
opt-in, off by default, plus a digest-pinned approval per provider, stored
outside the repository); CLI, `devgraph doctor`, dashboard status and revoke,
tray notice.

**Non-goals.**

- **`git`, `ast` and `docs` declarative providers** are out of E (Q3). The
  built-in extractors already produce commits, code structure and mentions,
  and a schema can reuse those relationship types with `provider: builtin`.
  Declarative selectors over them are separate format designs with no sandbox
  dependency and are not on the acceptance list.
- **Script-defined and composition MCP tools** are deferred (§7).
- No third-party packages, no network and no writes for scripts; no
  macOS-native or Windows-native sandbox primitives.
- **Not a boundary to bet a shared machine on.** The in-repo docs say so in the
  epic's words: defence in depth, proportionate for a local-first, single-user
  tool. §2 states plainly which layer is the boundary.

---

## 2. Threat model

**Assets.** The user's home directory and secrets; the network; other
repositories' graph data; Neo4j and its credentials; the DevGraph processes;
CPU and memory; and, on macOS and Windows, the Podman machine VM that also runs
Neo4j.

**Trusted.** The DevGraph install, the Podman binary, and the user at an
interactive terminal. The registry and trust store are trusted *as files*; who
can write them is discussed under T4.

**The boundary.** Anything running as the user can approve a script: it can
write the trust store directly, or drive the CLI under a pseudo-terminal. The
consent gates (§5) protect against *accidental* execution of code the user has
not looked at; they are not a boundary against a process already running as
the user. **The container is the boundary**, and §4 is written on that basis.

| # | Threat | Mitigation |
|---|--------|------------|
| T1 | **Scripts the user wrote**: loops, huge output, wrong keys. | Limits (§6); output validated against the declaration (§3.3); per-file failure keeps the last good state. |
| T2 | **A repository the user didn't author** ships `.devgraph/providers/*.py` and a schema naming it. | Scripts off per repository by default, independent of the project-config switch. `devgraph add` never runs a script. Each provider needs its own approval of an exact digest. Repository-local `.env` and environment cannot influence any sandbox decision (§5.1). |
| T3 | **A malicious `.devgraph` change in a pulled branch**: script edited, provider added, `inputs` widened, relationship `custom.params` changed. | The digest covers the normalised script text and every declaration entry the provider touches (§5.2). Any change stops the provider; last good graph state stays; nothing prunes while awaiting approval (§5.5). |
| T4 | **The MCP client**, realistically a coding agent with a shell, possibly prompt-injected. | MCP has no script controls. `enable` and `approve` need a TTY; DevGraph never prints an approve command containing a digest; `run --dry-run` passes the same gates; gates and declarations are never read from graph state (§5.4). Residual, stated plainly: an agent with a shell can approve, so the sandbox must hold. |
| T5 | **Supply chain**: image, Python in it, packages. | Fully-qualified, manifest-list-digest-pinned distroless image with no shell and no pip; pulled only by `devgraph sandbox setup`; `--pull=never` at run; standard-library allowlist; the runner shim ships with DevGraph and goes over stdin (§4.3). |
| T6 | **Resource exhaustion**: CPU, memory, fork bomb, output flood, huge input set, many concurrent runs. | cgroup limits, host-side kill, conmon `--timeout`, frame-length caps before allocation, per-file and per-run output caps, one sandbox run at a time machine-wide (§6). |
| T7 | **Exfiltration.** Directly over the network; or indirectly: script output becomes node properties that reach the MCP client, a model that may have web access. | `--network=none` plus the hardened flags and seccomp profile (§4.3). For the indirect path: inputs are tracked files only, a secret-name denylist always applies, the matched-file count and sample are shown at approval (§3.2), string sizes are capped. Residual: a script can copy any declared input into the graph, just as the built-in extractors expose file contents today. |
| T8 | **Writes outside the repository's scope.** | Scripts return records; DevGraph injects `repo_id`, takes labels, property names and keys from the declaration, never from records; parameterised writes; endpoints only on repo-scoped labels and matched by `repo_id` (§3.3). |
| T9 | **Reads outside the repository.** | No mounts. DevGraph reads declared inputs with the no-follow reader (§3.2). |
| T10 | **A web page reaching the dashboard.** | Approve and enable are not dashboard actions; schema write routes refuse custom declarations (Q13); disable and revoke sit behind the existing guards; sandbox surfaces render with `textContent` (§8). |
| T11 | **Container escape** (kernel or runtime bug). | Rootless, user-namespaced, seccomp-filtered, capability-free container. On macOS and Windows the Podman machine VM is **not** an extra boundary: it mounts the user's home (macOS) or `/mnt/c` (WSL), and an escape also reaches Neo4j in the same VM (§4.5). Residual. |
| T12 | **Hostile host-side parsing**: crafted frames, JSON, YAML or Python source aimed at DevGraph itself. | Lockstep framing (§4.4); strict record parsing (§3.3); the static scan runs in a limited subprocess and any exception is a hard reject (§4.2); YAML alias expansion bounded the same way. |

**Out of scope.** Malware already running as the user beyond T4's residual; a
compromised DevGraph install or Podman binary; multi-user machines.

---

## 3. The `derive(ctx)` contract

### 3.1 Declaration

A provider is one script at a fixed path, `<repo>/.devgraph/providers/<name>.py`
(Q12); there is no path field to traverse. `.devgraph` and `providers` must be
real directories, and the script a regular file of at most **64 KiB**, all
checked by the reader in §3.2. `.devgraph` is already in `IGNORED_DIR_NAMES`,
so scripts are never indexed as code.

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

Schema rules (validated in E1, inert until E3):

- New top-level `custom_providers`: `name`, `inputs` (non-empty; each glob
  repo-relative, no `..` segment, no leading `/`, no drive letter or UNC
  prefix, no backslash), `params` (the existing scalar-only `ScalarParam`).
- `NodeSource` gains `provider: custom` with `name`. A custom node type has any
  declared key.
- Every custom `custom.name` and `source.name` must name a declared
  `custom_providers` entry. Relationship-level `custom.params` stays valid and
  reaches the script; it is part of the digest (§5.2).
- A custom relationship may end at a built-in label, but only a repo-scoped one
  (`_REPO_SCOPED_LABELS`); `Repository` is rejected as an endpoint.
- A custom relationship type may not share its name with a built-in type or a
  type from another provider, because removed-type cleanup deletes edges by
  type (§3.4).
- `custom_providers` and custom sources are accepted only in a repository's own
  schema. The global scope rejects them: a script path is repository-relative,
  so a global declaration would run every repository's file of that name.

### 3.2 Inputs and the no-follow reader

`derive` is called **once per input file** (Q11).

| `ctx` field | Content |
|-------|---------|
| `ctx.path` | repo-relative POSIX path of this input file |
| `ctx.text` | its content, strict UTF-8; a file that does not decode is skipped with `input_decode` |
| `ctx.tree` | sorted tuple of the provider's matched input paths (names only) |
| `ctx.params` | merged provider and relationship params; read-only |
| `ctx.node_types`, `ctx.relationships` | what this provider may emit, from the declaration |

`ctx.tree` is narrowed from "every indexable path" to the provider's matched
inputs: the wider list disclosed the names of every file in the repository to
a script that only needs its own inputs.

**Selection.** Candidates are tracked files only: `git ls-files -z` in the
repository, run with a scrubbed environment (no `GIT_DIR`, `GIT_WORK_TREE` or
`GIT_CONFIG_*` passthrough). A repository with no git work tree reports
`input_unavailable` and its providers do not run. Candidates are then filtered
by the declared globs, `IGNORED_DIR_NAMES`, and a **secret-name denylist** that
always applies and cannot be overridden in E (`.env*`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `.netrc`, `.npmrc`,
`.pypirc`, `.git-credentials`, `*.kdbx`, `*.tfstate*`, `*credential*`,
`*secret*`). Matching is on the NFC-normalised path, case-folded on every
platform for the denylist and on case-insensitive volumes (macOS default,
Windows) for the globs.

**Reading.** A new module, `devgraph/sandbox/reader.py`, is the only code that
reads a sandbox input, a provider script, or the schema file whose declaration
feeds a digest. For each path:

1. Containment is decided on path components with `Path.is_relative_to`
   against the canonical repository root; never by string prefix.
2. `lstat` every component from the repository root down. Any symlink, any
   non-directory intermediate, and on Windows any reparse point
   (`st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`, which covers junctions
   and OneDrive placeholders) is refused.
3. Open without following. Linux: `openat2` with
   `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` relative to
   a directory descriptor of the root. Other POSIX: component-by-component
   `openat(dir_fd, part, O_NOFOLLOW | O_DIRECTORY)`, then
   `O_NOFOLLOW | O_RDONLY` for the leaf. Windows: `CreateFileW` with
   `FILE_FLAG_OPEN_REPARSE_POINT`, refusing a reparse point.
4. `fstat` the open descriptor: must be `S_ISREG`, size within the per-file
   cap. Read at most cap + 1 bytes from the descriptor; more is `input_cap`.

Steps 2–4 close the race between check and use: the decision is made on the
descriptor that is read.

**Shown at approval:** the matched-file count, a sample (first 20 sorted
paths), how many tracked files the denylist excluded, and the total input
bytes. A later run whose matched count exceeds twice the approved count is
still allowed (inputs legitimately grow), but `scripts list` shows the growth.

### 3.3 Outputs and host-side validation

`derive(ctx)` returns a list of records (a single dict is a list of one):

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

Everything arriving from the container is hostile. The host parses each
result frame with a strict decoder:

- NaN, `Infinity` and `-Infinity` rejected (`parse_constant` raises); integers
  outside int64 rejected, with the digit count checked before conversion;
  floats must be finite.
- Strings: lone surrogates and NUL rejected; length limits counted in UTF-8
  bytes (4 KiB per value, 256 bytes per key component).
- No value nested more than two levels below a record (`record → from → key`
  is the deepest legal shape).
- **Any** parser or validator exception, of any type, means `schema_violation`
  for that file and nothing from it is written.

Then each record is validated, and the whole file's output is rejected on the
first violation (`schema_violation`, with the record index, field name and
rule; never the offending value, §8):

- `node` must equal a label whose source is this provider; `props` keys must be
  declared metadata with matching types; every key component and `required`
  field present; no reserved property (`RESERVED_NODE_PROPERTIES` plus
  `custom_sources`).
- `rel` must equal a type declared for this provider; `from.label` in its
  `from` list, `to.label` equal to its `to`; endpoint `key` exactly the
  endpoint type's declared key (`{name}` or `{name, file}` for built-in labels,
  following `_is_file_scoped`). Relationship properties are rejected in E.
- Values only `str | int | float | bool | None`.

**Identifiers come from the declaration.** The host looks each record's label,
type and property names up in the validated declaration and uses the
declaration's own strings, which already match the schema identifier
patterns, when it builds Cypher. Record values are only ever query
parameters. Nothing from a record is interpolated into query text.

### 3.4 Write path and provenance isolation (E3)

Custom nodes and edges carry their own provenance, disjoint from every
built-in field:

| On | Property | Value |
|----|----------|-------|
| node | `repo_id` | injected |
| node | `extractor` | `custom:<name>` |
| node | `custom_sources` | input paths that currently claim the node |
| edge | `extractor` | `custom:<name>` |
| edge | `custom_source` | the input path whose run produced it |

Custom nodes never carry `name`, `file`, `source_file`, `source` or `sources`,
so `_UNCLAIM_SOURCE_CYPHER`, `_DELETE_BY_SOURCE_FILE_CYPHER`,
`_DELETE_STALE_FILE_NODES_CYPHER` and the filesystem provider's queries cannot
match them. Custom lifecycle uses new queries, each scoped by
`extractor = $extractor` and keyed on the **declared key**, never on `name`:

- **Upsert:** `MERGE (n:<Label> {repo_id: $repo_id, <k1>: $k1, …})`, covered by
  the existing `(repo_id, *key)` constraint; add the input path to
  `custom_sources`.
- **Per-file replace:** for one input path, delete its edges
  (`custom_source = $path AND extractor = $extractor`), remove the path from
  `custom_sources` on this extractor's nodes, upsert the new records, then
  delete this extractor's nodes whose `custom_sources` is empty. One
  transaction per file, or per batch of files (§6).
- **Prune** (only inside a successful full run of an approved digest, §5.5):
  delete this extractor's nodes whose `[label, key values]` pair is not in the
  run's produced set.
- **Removed-type cleanup** for a custom type deletes by type *and* extractor.

Edge endpoints are `MATCH`ed on `repo_id` plus the declared key; a missing
endpoint is dropped and counted, never created. A custom edge to a built-in
node disappears if a built-in re-index deletes that node, and returns on that
input's next derive, as any edge into a deleted node does today.

**Acceptance test written first in E3:** a repository where a custom provider
and the built-in extractors share input paths and label neighbourhoods;
indexing, re-indexing, deleting and pruning on either side leaves every node
and edge of the other side untouched, in both directions.

**Display.** Custom nodes have no `name`, and today the dashboard graph,
node details and several MCP tools return `n.name`. E3 lists the affected
surfaces; E4 adds a fallback that renders the declared key values instead of
blank. `name` is not written as a shim, because built-in queries key on it.

### 3.5 Determinism

The runner sets `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`, passes files in
sorted order and has no `time`, `random` or `os` (beyond `os.path`) in the
allowlist; DevGraph sorts and de-duplicates records before writing. Each
`derive` call runs in a **fresh module namespace** (the compiled code object
executed into a new dict), so one file's failure cannot leave state that skews
the next. Both properties hold for non-adversarial scripts only: a hostile
script shares one interpreter across its files and can persist state through
mutable builtins. That is acceptable because isolation between files of the
*same* provider protects nothing the provider could not already reach.
`devgraph config scripts run <repo> <name> --dry-run --twice` reports any
difference between two runs and writes nothing.

---

## 4. Isolation

### 4.1 Options compared

The static allowlist is a lint, not a boundary; CPython has too many paths to
its internals for an AST scan to close:

| Escape | Static rule | Caught? |
|--------|-------------|---------|
| `().__class__.__base__.__subclasses__()` | dunder attribute access | yes |
| `getattr(f, "__glo" + "bals__")` | dunder attribute access | **no** (built at run time) |
| `"{0.__globals__}".format(f)` | dunder attribute access | **no** (inside a string) |
| `(x for x in ()).gi_frame.f_back.f_globals` | not a dunder | **no** unless frame attributes are listed one by one |
| `import socket` / `__import__("socket")` | import allowlist; dunder name | yes / yes |
| `exec`, `eval`, `compile`, `while True:` | name and AST rules | yes; `while 1:` and recursion are not |

| | Subprocess + rlimits + seccomp/Landlock | Container (Podman) | WASM (CPython on WASI) | In-process + allowlist |
|---|---|---|---|---|
| Filesystem | Linux only (Landlock) | no mounts, read-only rootfs | no preopens | none |
| Network | Linux only (seccomp) | `--network=none` + seccomp | no sockets in WASI p1 | none |
| CPU / memory / pids | rlimits; Job Objects | cgroups v2 | fuel, store limits | thread timer only |
| macOS / Windows | **no isolation** | inside the Podman machine VM | good | none |
| New dependency | libseccomp, per-OS code | none (Podman already required) | `wasmtime` + ~25 MB CPython build | none |
| Start-up | ~50 ms | ~0.3–1 s per run | ~0.2–0.5 s | none |
| Verdict | uneven across platforms | **Recommended** | runner-up | rejected as sole layer |

### 4.2 Host-side static scan

The scan (import allowlist, denied names, dunder attributes, the table above)
gives early, readable findings at approval; it is hygiene, not a boundary. It
parses untrusted source, so it runs in a **separate `python -I -S` subprocess**
with a 5 s wall timeout and, on POSIX, `RLIMIT_AS` 256 MiB and `RLIMIT_CPU`
5 s. Any non-zero exit, timeout, signal or exception inside it, including
`RecursionError`, `MemoryError` and `SyntaxError`, is a hard reject
(`static_reject`), never a pass. It parses with `feature_version` set to the
image's Python minor version so that the scan and the container agree on the
grammar.

The schema YAML is untrusted input of the same class: alias expansion is
bounded (a document whose expansion exceeds 10,000 nodes is rejected), and
loader exceptions of any type already fail closed (`YAML_LOAD_ERRORS` in
`project_tools.py` is the precedent; E1 extends it to the schema loader's
custom sections).

### 4.3 The container invocation

A dedicated, ephemeral, mount-free container from a digest-pinned image (Q2):
the Neo4j container holds the graph and its credentials, so a script there
could bypass §3's write path.

**The podman process itself** is started with an allowlisted environment:
`PATH` set to a fixed value, `HOME`, `XDG_RUNTIME_DIR`, `LANG=C.UTF-8`, and
nothing else. In particular `CONTAINER_HOST`, `CONTAINER_CONNECTION`,
`CONTAINERS_CONF`, `CONTAINERS_CONF_OVERRIDE`, `CONTAINERS_STORAGE_CONF`,
`CONTAINERS_REGISTRIES_CONF`, `DOCKER_HOST`, `DOCKER_CONFIG` and every proxy
variable are dropped. The binary is resolved once, to an absolute path, from
that fixed `PATH`.

**Create, inspect, start** (instead of a single `podman run`, so the
container can be checked before anything executes):

```
podman create --interactive --pull=never
  --network=none --http-proxy=false --no-hosts --dns=none
  --read-only --read-only-tmpfs=false
  --log-driver=none --ipc=none --userns=nomap
  --cap-drop=all --security-opt=no-new-privileges
  --security-opt=seccomp=<devgraph-sandbox.json>
  --user=65534:65534 --pids-limit=32 --memory=256m --memory-swap=256m
  --cpus=1 --ulimit=nofile=64:64 --ulimit=core=0
  --timeout=<wall-clock seconds>
  --env=PYTHONHASHSEED=0 --env=TZ=UTC --env=LC_ALL=C.UTF-8
  --label=devgraph.sandbox=1 --name=devgraph-sandbox-<random 128-bit hex>
  <registry>/<repository>@sha256:<manifest-list digest>
  /usr/bin/python3 -I -S -c "<bootstrap>"
```

Why each non-obvious flag:

- `--http-proxy=false`: Podman otherwise copies the host's proxy variables in.
- `--no-hosts`, `--dns=none`: no injected `/etc/hosts` or `resolv.conf`
  describing the host's network.
- `--read-only-tmpfs=false`: `--read-only` otherwise still mounts writable
  tmpfs on `/tmp`, `/var/tmp` and `/run`. The runner needs no writable path; if
  E2a shows CPython does, a single `--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m`
  is the only exception.
- `--log-driver=none`: otherwise stdout, which carries file-derived output, is
  retained in the container log on disk.
- `--userns=nomap` (rootless; `auto` where `nomap` is unsupported): the user's
  own UID is not mapped into the container, so an escape lands as an
  unprivileged subordinate UID rather than as the user.
- `--pull=never`: run never fetches; only `sandbox setup` pulls.
- The container name is random and carries no repository or user information,
  because `/run/.containerenv` exposes it inside the container.

**Seccomp profile** shipped with DevGraph (`devgraph/sandbox/seccomp.json`):
allowlist-based (default action `ERRNO`), derived from Podman's default and
additionally denying `unshare`, `clone`/`clone3` with any `CLONE_NEW*` flag
(`clone3` returns `ENOSYS` so libc falls back to filterable `clone`), `setns`,
`io_uring_setup`/`enter`/`register`, `bpf`, `userfaultfd`, `keyctl`/`add_key`/
`request_key`, `perf_event_open`, `ptrace`, `process_vm_readv`/`writev`, the
mount family (`mount`, `umount2`, `pivot_root`, `fsopen`, `fsmount`,
`open_tree`, `move_mount`) and `socket`/`socketpair` for every family.

**Post-create assertion.** `podman inspect` on the created container must show:
`Mounts` empty; `Config.Env` exactly the three variables above plus the
image's own `PATH` (compared as a set against a constant); network mode
`none`; `HostConfig.ReadonlyRootfs` true; `CapAdd` empty and all capabilities
dropped; the seccomp profile path ours; `LogConfig.Type` `none`; the image ID
equal to the pinned digest's. Any difference, including volumes or environment
injected by a user's `containers.conf` or `mounts.conf`, means
`podman rm -f`, `sandbox_unavailable` with the differing field named, and no
start. Then `podman start --attach --interactive`, and `podman rm -f` in a
`finally`.

**Readiness** (`podman info --format json` and, on macOS and Windows,
`podman machine inspect`), checked before the first run of each process and
reported by `doctor`: rootless; cgroup v2 with `memory`, `cpu` and `pids`
delegated; seccomp enabled; user namespaces available; Podman ≥ 5.0; the
machine not rootful; image present at the pinned digest. Any failure refuses
the run. There is **no unsandboxed fallback** (Q1).

**Docker** is accepted only when `docker info` reports rootless mode or
`userns-remap`; otherwise it is refused. Docker has no `--http-proxy=false`;
proxies from the client config file reach the container as environment, which
the post-create assertion catches and refuses. The runtime choice is made by
`devgraph sandbox setup --runtime docker` and stored in the trust store
(§5.1), not in settings. **The Docker or Podman socket is never mounted**
anywhere, including the headless image (Q8).

**Image.** A fully-qualified distroless Python 3 image (no shell, no pip, no
package manager), pinned by its manifest-list digest in DevGraph's source.
`devgraph sandbox setup` is the only command that pulls. The digest is
refreshed by a reviewed code change with each DevGraph minor release and
within a week of a high-severity CPython or C-library CVE; `doctor` shows the
pinned image's age.

**Inside**, the runner applies the import allowlist (`re`, `json`, `pathlib`
pure-path classes, `os.path`, `string`, `textwrap`, `collections`,
`itertools`, `functools`, `dataclasses`, `typing`, `math`, `fnmatch`),
restricted builtins (no `open`, `exec`, `eval`, `compile`, `__import__`,
`input`, `breakpoint`) and a per-call CPU timer. Like the static scan, these
are hygiene; the container is the boundary.

### 4.4 Frame protocol

Frames in both directions are a 4-byte big-endian length followed by that many
bytes of JSON. The bootstrap, runner shim and script text arrive first; then:

- **Lockstep.** The host sends file *N* as `{seq: N, path, text}` and accepts
  exactly one result frame, which must carry `seq: N`, before sending *N + 1*.
  The host never acts on a path it did not send; a `path` in a result is
  ignored and the host's own record of *N* is used.
- **Length first.** A declared length above the per-file output cap (1 MiB
  plus a fixed envelope) is a protocol error *before* any buffer is allocated.
- Any extra, out-of-order, oversize or undecodable frame, or output before the
  first request, means: kill the container, fail the in-flight file with
  `protocol`, and handle the remaining files as in §6.
- **I/O threads.** stdin is written on its own thread with a per-frame
  deadline. stderr is drained continuously on another; bytes beyond 8 KiB are
  discarded but reading never stops, so a full pipe can never stall the
  container or the host. stdout is read by the frame parser under the run's
  deadline.
- **Killing.** On any deadline the host runs `podman kill --signal=KILL
  <name>` then `podman rm -f <name>`; `--timeout` makes conmon kill the
  container even if the host process has died. At start-up each DevGraph
  process removes leftover `devgraph.sandbox=1` containers older than the
  maximum wall clock.

The protocol stays runtime-neutral so a WASM runner could replace §4.3
without changing §3 or §5.

### 4.5 Platform notes

- **Linux:** rootless Podman, user namespace without the user's UID, seccomp,
  cgroups. This is the strongest configuration.
- **macOS:** the container runs in the Podman machine VM, which by default
  mounts the user's home directory into the VM. The VM is therefore **not** an
  extra layer: an escape from the container into the VM reaches the home
  directory, and Neo4j, which runs in the same VM. Docs say so.
- **Windows:** the same holds for WSL2, which mounts `C:` at `/mnt/c`. Paths
  are compared case-insensitively and reparse points are refused (§3.2).
- **macOS paths** are compared after NFC normalisation (APFS preserves NFD
  names created by some tools) and case-folded on case-insensitive volumes.
- **Headless Docker image:** custom providers are unavailable (Q8).
- **CI:** `approve --sha256` from the same repository the CI is testing is
  self-approval. The documented pattern takes the digest from a protected CI
  secret or variable that pull requests cannot change.

---

## 5. Trust and consent

### 5.1 Where gates and settings live

Nothing that decides whether or how a script runs comes from `Settings`, a
`.env` file or an environment variable:

- **Trust store:** a dedicated SQLite file at a fixed path,
  `<home>/.devgraph/script_trust.sqlite3`, where `<home>` is taken from the
  password database on POSIX (`pwd.getpwuid(os.getuid()).pw_dir`) and from
  the profile known folder on Windows, never from `HOME` or `USERPROFILE`.
  Tests pass a path as a function argument. It is deliberately separate from
  the registry, whose location is still a setting.
- **Contents:** per repository, `scripts_enabled` (default absent ⇒ off);
  per provider, approved digests; the runtime choice (Podman by default).
- **Key:** every row is keyed by `(repo_id, canonical repository path)`. The
  canonical path is the resolved real path, NFC-normalised, case-folded on
  case-insensitive volumes. A repository moved, re-registered under another id,
  or a different checkout reusing an id, inherits nothing.
- **Limits, image digest, seccomp profile, allowlists, denylist:** code
  constants (Q9).
- **Fail closed.** Any error while deciding (store missing, locked, corrupt,
  wrong schema version, repository unknown, path not canonicalisable) means
  "scripts off" and "not approved". This deliberately differs from
  `project_switch.py`, which fails open; the script gates never call it for
  their own decision.

### 5.2 Gates and the digest

Three conditions, all checked against one snapshot immediately before every
run, including `run --dry-run`:

1. The repository's **project config is on** (`project_config_enabled`).
2. **Scripts are enabled** for `(repo_id, canonical path)` in the trust store.
3. The provider's **current digest is approved and active** (§5.6).

**Digest.** SHA-256 over a domain-separated, versioned, length-prefixed
encoding:

```
"devgraph-script-trust\x00" || u8 version (=1)
  || field(provider name)
  || field(canonical JSON of the declaration set)
  || field(normalised script text, UTF-8)
field(x) = u64 big-endian byte length || x
```

The declaration set is the full canonical entries (not a projection): the
`custom_providers` entry, every node type whose source is this provider, and
every relationship with `custom.name` equal to it, including relationship-level
`custom.params`. Canonical JSON: sorted keys, no insignificant whitespace,
`ensure_ascii`, lists in declared order.

**One snapshot per run.** The run reads the schema file and the script once,
through the reader, computes the digest from those bytes, checks the gates,
and hands the *same* in-memory text and declaration to the runner. Nothing is
re-read between the check and the run.

### 5.3 Approved text is executed text

The script is read once and must be strict UTF-8 without a BOM. It is
rejected (`static_reject`) if it contains a PEP 263 coding cookie, NUL, a CR
not followed by LF, a form feed, any other C0/C1 control except tab and LF,
or any Unicode format character (category Cf, which includes the bidi
overrides and isolates U+202A–U+202E and U+2066–U+2069, U+200E/U+200F and
zero-width characters). CRLF is normalised to LF; that string is hashed,
shown at approval, sent over stdin, and passed to `compile()` as a `str`,
so no decoding step inside the container can reinterpret it.

Every repository-sourced string DevGraph prints (script text, paths, params,
labels, sample file names, diffs) is rendered with control and format
characters made visible as `\x..`/`\u....` escapes, so terminal escape
sequences cannot repaint the prompt.

### 5.4 Approval and enablement

`devgraph config scripts approve <repo> [<name>]` and
`devgraph config scripts enable <repo>` both require stdin and stdout to be a
TTY. The approve prompt shows the repository and canonical path, provider
name, script path and size, the declared inputs with the matched-file count
and sample (§3.2), the full declaration set (or a **declaration diff** against
the last approved one), the static-scan findings, and the script text (or a
unified diff against the last approved version). The user types the provider
name to approve.

`--sha256 <hex>` approves non-interactively only when it equals the current
digest (Q10); it is for CI with the digest from a protected secret. There is
no `--yes`. **No DevGraph output ever prints an approve command containing a
digest**: `show` prints the digest on its own line, and the `add` notice and
doctor hints print the interactive commands only. This removes the copy-paste
path a shell-equipped agent would otherwise be handed; it does not stop an
agent determined to approve (§2).

Gates and declarations are read only from the trust store and the repository's
files, never from graph state such as the `Repository` node's schema fields.

### 5.5 Provider state machine

| State | Meaning | Runs? | Prunes? |
|-------|---------|-------|---------|
| `disabled` | project config off, or scripts off | no | no |
| `unavailable` | sandbox not ready, no git work tree, or headless image | no | no |
| `awaiting_approval` | digest never approved, retired, or revoked; new provider; pending schema | no | no |
| `approved` | digest active | yes | yes, only after a successful full run |
| `failing` | approved, last run had errors | yes | only for files that succeeded in a full run |

Rules:

- **Only a successful full run of an active digest prunes.** A provider that is
  disabled, unavailable, awaiting approval or revoked keeps its nodes as the
  last good state, shown as stale in `scripts list` and the dashboard.
- A schema change still in the 5-minute pending window is not the snapshot:
  the provider runs (if at all) under the applied schema's declaration.
- Graph data for a provider is removed only by removing its declaration (the
  existing removed-type cleanup, scoped by extractor, §3.4) or by `devgraph
  remove`.
- Approving a digest triggers one full provider run.

### 5.6 Multiple digests

A new approval **retires** every previous digest for that provider by default;
`approve --keep-previous` keeps them active, up to five. A retired digest is
never active again: checking out an old branch whose provider matches a
retired digest re-prompts. When a kept, older digest becomes active (for
example after a branch switch), `scripts list`, the dashboard and the tray
notice say so with that approval's date.

### 5.7 Surfaces

| Surface | Behaviour |
|---------|-----------|
| `devgraph add` / `register` | Scripts off. If the schema declares custom providers, prints that they will not run and the interactive `enable`/`approve` commands. |
| CLI | `config scripts list [--repo]`, `show`, `approve [--sha256] [--keep-previous]`, `revoke`, `enable`, `disable`, `run --dry-run [--twice]`. |
| Tray / watcher | Never prompts or approves. Skips non-`approved` providers. Notifies once per new digest, at most once per provider per hour. |
| Headless agent, CI | Never prompts; runs only active digests. |
| MCP | No script controls; nothing new in `devgraph://project-tools` or `notices`. |
| Dashboard | Per project card: scripts state, each provider's state, short digest, approved-at, last run outcome and counts, error code. Actions: **disable** and **revoke**, behind the existing guards, with a dry run. Schema write routes refuse custom declarations (Q13). |
| `devgraph doctor` | Providers awaiting approval or failing, the readiness checks of §4.3, image age. |

**The project-config switch.** Scripts need the project config on (§5.2).
Turning it off stops scripts; turning it back on **resumes approved providers
without re-approval**. The docs say so, and the dashboard's dry run for
enabling project config lists the approved providers that will resume.

**Revoke** removes digests; **disable** turns scripts off. Both stop future
runs immediately and neither deletes graph data (§5.5). `devgraph remove`
deletes the repository's trust rows.

---

## 6. Limits

All limits are code constants in E (Q9); none comes from settings, `.env` or
the repository.

| Limit | Value | Enforced by | On breach |
|-------|-------|-------------|-----------|
| Script size | 64 KiB | reader | `static_reject` |
| Input files per run | 5,000 | host, before start | run refused: `input_cap` |
| Input bytes per file / per run | 1 MiB / 32 MiB | reader | file skipped / run refused: `input_cap` |
| CPU per `derive` call | 2 s | runner timer | that file: `timeout` |
| Wall clock per run | 30 s + 50 ms per file, max 300 s | host kill; conmon `--timeout` | in-flight file: `timeout` |
| Memory | 256 MiB, no swap | cgroup | container killed: `memory` |
| Processes | 32 | cgroup | `crash` |
| Output per file | 1,000 records, 1 MiB | frame parser, before allocation | that file: `output_cap` |
| Output per run | 50,000 records, 16 MiB | host | rest of run: `output_cap` |
| stderr | 8 KiB kept, rest drained and discarded | host | none |
| Concurrent sandbox runs | 1 per machine | file lock in `~/.devgraph/` | waits up to 60 s, then `busy`; retried on the next event |

**Concurrency.** The lock is shared by the tray agent, the CLI and the
dashboard. On macOS and Windows the sandbox container shares the Podman
machine VM's memory with Neo4j; one run at a time at 256 MiB keeps the
sandbox's share bounded, and `doctor` warns when the machine has under 2 GiB.

**Writes** are batched: per-file replace transactions grouped up to 500
records, so a 50,000-record run does not hold one large transaction.

**Breach semantics.** Errors are per input file wherever possible: a failed
file keeps its last good graph state and the run continues. When the
container dies (memory, wall clock, crash, protocol), results already
validated are applied, the in-flight file gets the named error, and the
remaining files are retried once in a fresh container; a second death marks
them `aborted`. A failed run never prunes (§5.5), never aborts built-in
extraction or other providers, and never retries in a loop.

---

## 7. Script tools for MCP: deferred

Script-defined and composition tools (epic §7) are out of E (Q4): they run on
arguments chosen by the untrusted caller (T4); a useful one needs a read
channel into Neo4j, which is a new boundary; per-call container start-up is a
poor fit, and a warm pool is a new long-lived process to secure; the
read-only Cypher tools already meet the epic's stated goal. When picked up
they reuse §4, the §5 trust store keyed by tool id, locked-name resolution and
scope pinning.

---

## 8. Failure, observability and telemetry

- **Run record.** One metadata-only line per run in
  `~/.devgraph/sandbox_runs.jsonl` (500 entries, same rotation as
  `mcp_telemetry.jsonl`): `ts`, `repo_id`, `provider`, short digest, file
  count, nodes and edges written, dropped endpoints, duration, outcome,
  error-code counts. Never contents, param values, output or stderr.
- **Error codes:** `disabled`, `awaiting_approval`, `sandbox_unavailable`,
  `input_unavailable`, `static_reject`, `input_cap`, `input_decode`, `busy`,
  `timeout`, `memory`, `output_cap`, `protocol`, `schema_violation`, `crash`,
  `aborted`. Each carries the input path and a one-line reason. **Reasons never
  quote a value** from a script, an input file or a record: they name the
  record index, field and rule.
- **stderr** is shown only by an interactive `run --dry-run`, rendered with
  controls visible (§5.3). Agent runs never log any of it, not even a tail; a
  script can print file contents.
- **Neo4j query log.** Neo4j can log query parameters, which here would be
  script output. E3 sets `db.logs.query.parameter_logging_enabled=false` in
  both compose files (a no-op on editions without a query log).
- **Dashboard.** Every sandbox surface sets text with `textContent`, not
  `innerHTML`. `escapeHtmlVal` does not escape quotes, so it is not used for
  any value that could reach an attribute.
- **Privacy.** Nothing leaves the machine; `telemetry_enabled` stays off and is
  not consulted. README and PROJECT_STATUS gain one sentence for the run
  record.

---

## 9. Open questions for the user

1. **Isolation runtime.** Rootless Podman, mount-free, with the hardened flags,
   seccomp profile, post-create inspect assertion and widened readiness check
   of §4.3; no unsandboxed fallback; Docker only when rootless or
   userns-remapped. *Recommend: yes.*
2. **Which container.** A dedicated, ephemeral container from a digest-pinned
   image, not the Neo4j container. *Recommend: dedicated.*
3. **`git`, `ast`, `docs` declarative providers.** Out of E; file as follow-ups
   and close epic #1 after E. *Recommend: yes.*
4. **Script and composition MCP tools.** Defer (§7). *Recommend: defer.*
5. **Where consent happens.** `enable` and `approve` CLI-only with a TTY; the
   dashboard can disable and revoke only; no approve one-liners printed;
   accepting that anything running as the user can approve, so the sandbox is
   the boundary. *Recommend: yes.*
6. **What the digest pins.** A domain-separated, versioned, length-prefixed
   encoding of the normalised script text and the full canonical declaration
   set, including relationship-level `custom.params`, from one snapshot per
   run, with a declaration diff at approval. *Recommend: yes.*
7. **Multiple digests.** A new approval retires earlier digests by default;
   `--keep-previous` keeps up to five; a retired digest re-prompts; an older
   kept digest becoming active is shown. *Recommend: yes.*
8. **Headless Docker image.** Custom providers unavailable; no runtime socket
   is ever mounted. *Recommend: unavailable, reported by doctor; revisit with
   a WASM runner.*
9. **Limits.** The §6 values as code constants: no settings, `.env`,
   environment or per-repository override in E. *Recommend: yes.*
10. **Non-interactive approval.** `approve --sha256 <hex>` only for the
    matching digest, no `--yes`, documented for CI with the digest held in a
    protected secret, never taken from the repository under test.
    *Recommend: yes.*
11. **Call granularity.** `derive` once per input file, in a fresh module
    namespace per call (failure isolation, not adversary isolation).
    *Recommend: per file.*
12. **Script location.** Fixed `.devgraph/providers/<name>.py`; `.devgraph` and
    `providers` must be real directories; 64 KiB cap; read by the no-follow
    reader. *Recommend: yes.*
13. **Dashboard schema routes.** May the dashboard's schema write routes (form
    editor, "Copy to…", add/replace) create or edit custom provider
    declarations once E1 lands? *Recommend: not in E.* Custom declarations are
    edited in the YAML file and approved with the CLI; the routes refuse any
    entry with `source.provider: custom` or `provider: custom` and any change
    to `custom_providers`. Deleting such an entry stays allowed, since it only
    reduces what runs.

---

## 10. Slice plan

Each slice is one reviewable PR stacked on the previous one, except E0.5. None
starts before this document is signed off.

| Slice | Content | Executes repository code? | User sign-off before merge |
|-------|---------|--------------------------|----------------------------|
| **E0.5** Present-code prerequisites | The three fixes of §0 (working-directory `.env`, symlink following, string-prefix containment), with regression tests. Standalone off master. | No | No: ordinary review; it is a bug fix. E1 waits for it to merge. |
| **E1** Schema, trust store, consent | `custom_providers`, custom `NodeSource`, the §3.1 cross-reference and scope rules, JSON Schema; the trust store at its fixed path with fail-closed gates keyed by `(repo_id, canonical path)`; the no-follow reader and tracked-file selection with the denylist; source normalisation and the digest (§5.2–5.3); static scan in a limited subprocess; YAML alias bound; `config scripts list/show/approve/revoke/enable/disable`; doctor reporting; `add` notice; dashboard schema routes refuse custom declarations (Q13). | No | **Yes**: it fixes the consent UX and prompt wording. |
| **E2a** Adversarial suite, tests first | The full suite as `xfail` against real Podman, skipped where Podman is absent: network, DNS, proxy and `/etc/hosts` leakage; `containers.conf`/`mounts.conf` volume and env injection caught by the inspect assertion; `CONTAINER_HOST`/`DOCKER_HOST` in the caller's environment; writable tmpfs; log retention; fork bomb, memory, CPU spin; `/etc` and `$HOME` reads; namespace, `io_uring`, `bpf`, `ptrace` and socket syscalls; each §4.1 escape; stdout and stderr flooding; a hostile-frame fuzz corpus (oversize lengths, wrong `seq`, extra frames, NaN, huge integers, lone surrogates, deep nesting). Reviewed before any runner code. | No | **Yes**: the suite is the acceptance bar for E2b. |
| **E2b** Runner | `devgraph/sandbox/`: scrubbed-environment podman invocation, create/inspect/start, readiness checks, seccomp profile, lockstep frame protocol with I/O threads and host-side kill, host frame parser and output validator (§3.3), limits and error codes, machine-wide lock, `devgraph sandbox setup`, `run --dry-run [--twice]`. E2a's tests flip from `xfail` to pass. Writes nothing to the graph. | **Yes**, dry run only | **Yes**: first code that runs repository scripts. |
| **E3** Provider wiring | Provenance-isolation tests first (§3.4), then declared-key MERGE, keyed endpoint MATCH, extractor-scoped per-file replace and prune; state machine (§5.5) and indexer/rescan integration; batched writes; run records; Neo4j parameter logging off; list of `name`-dependent displays. Backward-compatibility snapshot: no schema file ⇒ identical graph. | Yes, from the indexer | **Yes**: scripts start writing to the graph. |
| **E4** Surfaces and docs | Dashboard project-card section (`textContent` only; disable and revoke with dry run; project-config dry-run warning); key-value display fallback; rate-limited tray notice; README, PROJECT_STATUS, platform notes (§4.5) and the "defence in depth" note; end-to-end acceptance against the epic's sandbox criterion. | No new paths | **Yes**: final sign-off before the epic closes. |

## Testing (summary)

E0.5: regression tests for each prerequisite. E1: loader and scope rules,
trust-store fail-closed paths (missing, locked, corrupt, unknown repository,
moved repository), reader refusals (symlinked leaf and intermediate, `..`,
FIFO, oversize, reparse point on Windows CI), denylist, normalisation and
rejection table (cookie, NUL, lone CR, form feed, bidi, zero-width), digest
stability across line endings and its sensitivity to every declaration field,
scanner crash-as-reject, dashboard route refusals. E2a/E2b: as tabled. E3:
live Neo4j provenance isolation in both directions, per-file lifecycle,
cross-repository endpoint isolation, `Repository` endpoint rejection,
reserved-property rejection, no prune while awaiting approval, determinism,
backward-compatibility snapshot. E4: dashboard route and JS harness tests, the
epic's acceptance line end to end.
