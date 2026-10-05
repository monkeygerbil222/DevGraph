# Custom provider and script sandbox (slice E) — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §2 (`custom` provider), §3 (script
sandbox), §7 (script-defined tools). Stacked on #45, the tip of the epic stack.
**Design only, for sign-off.** No slice below starts until this document is
approved or redirected; nothing in it executes code today.

**Revision 3**, after a re-review: E is Linux- and Podman-only (§4.5); a
complete inspect assertion and a per-image expected environment (§4.3); an
in-container mount and identity report before the script is sent (§4.4);
image pinning by image ID and repository digests (§4.3); no hash-seed
determinism (§3.5); per-file host deadline and bounded retry (§6); hardened
`git ls-files` (§3.2); gate 1 without `Settings` (§5.1); no runs while the
schema is pending (§5.5); named tests and a strict E2a gate (§10).

**Verification.** Claims about Podman, Python and git were checked on one Linux
x86_64 host with rootless Podman 5.8.4 (cgroup v2, systemd cgroup manager,
SELinux enforcing, no AppArmor), CPython 3.14 on the host and a CPython 3.11
image, and git 2.55. Statements marked **Unverified** were not checked and are
E2a's to settle.

What exists now, verified against the stack tip:

- `custom` is inert data: `PROVIDER_KINDS` includes it and
  `CustomProvider(name, params)` (`devgraph/config/project_schema.py`) is
  validated, never loaded; `name` follows `PROPERTY_NAME_PATTERN`.
- The filesystem provider's cleanup (`_DELETE_EXTRACTED_PATHS_CYPHER`,
  `_PRUNE_EXTRACTED_CYPHER`) matches `extractor` **and `name`**, so it cannot
  serve a provider keyed otherwise. `_UNCLAIM_SOURCE_CYPHER` matches *any*
  node whose `source`/`sources` names a file, whatever produced it (§3.4).
- Per-user-type constraints exist on `(repo_id, *key)`, but the engine MERGEs
  on `(repo_id, name[, file])`; declared-key MERGE is E3's.
- `_indexable_paths` follows symlinked files, so §3.2 adds a new reader.
- "Pending" schema is `schema_pending()` (`dispatch.py`): the file's hash
  against the applied hash recorded **in the graph**.
- The project-config switch (`project_config_enabled`, registry column,
  default on) is looked up by `project_switch.py` through `Settings` and fails
  **open**; wrong for scripts (§5.1).
- `Settings` reads `.env` from the working directory and `DEVGRAPH_*`
  variables; `registry_db_path` defaults to `~/.devgraph/registry.sqlite3`.
- MCP: scope pinned at startup, read-only Cypher tools, `run_cypher` off by
  default. Dashboard: loopback, `_LocalHostOnlyMiddleware`,
  `_reject_cross_site_config`, no authentication; schema write routes back the
  form editor; `escapeHtmlVal` does not escape quotes.
- No CI workflow exists (no `.github/workflows/`). Telemetry is off.

---

## 0. Prerequisites (E0.5)

**E1 does not start until all of the following are merged.** They are tracked
and reviewed separately and referenced here only generically.

**E0.5a — present-code fixes**, being prepared as ordinary bug fixes:

1. **Configuration from the working directory.** `.env` is loaded only from
   the DevGraph home directory, never from the current working directory, so
   running DevGraph inside a cloned repository cannot redirect its settings.
2. **Path containment.** "Is this path inside the repository" is decided with
   `Path.is_relative_to` on resolved paths, never by string prefix.
3. **Symlinks.** Repository walks skip symlinks that resolve outside the
   repository.

**E0.5b — project MCP tools become opt-in.** Already decided and being built on
the epic stack: repository-supplied project MCP tools are off per repository
until approved; an approval pins the tools file's hash; approval is CLI-only.
Script providers in E adopt the same trust model (per repository, per hash,
CLI-only, stored outside the repository), extended to one digest per provider
(§5). Script-defined MCP tools in any later slice reuse it unchanged (§7).

E never relies on E0.5a alone: sandbox gates bypass `Settings` entirely
(§5.1), and sandbox inputs use the dedicated reader (§3.2), which refuses every
symlink rather than only those leaving the repository.

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
tray notice. **Linux with rootless Podman only.**

**Non-goals.**

- **macOS and Windows execution.** Custom providers report `unavailable` there
  in E; the follow-up is scoped in §4.5.
- **Docker.** Not a supported sandbox runtime in E (§4.5).
- **`git`, `ast` and `docs` declarative providers** are out of E (Q3). The
  built-in extractors already produce commits, code structure and mentions,
  and a schema can reuse those relationship types with `provider: builtin`.
- **Script-defined and composition MCP tools** are deferred (§7).
- No third-party packages, no network and no writes for scripts.
- **Not a boundary to bet a shared machine on.** The in-repo docs say so in the
  epic's words: defence in depth, proportionate for a local-first, single-user
  tool. §2 states plainly which layer is the boundary.

---

## 2. Threat model

**Assets.** The user's home directory and secrets; the network; other
repositories' graph data; Neo4j and its credentials; the DevGraph processes;
CPU and memory.

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
| T2 | **A repository the user didn't author** ships `.devgraph/providers/*.py` and a schema naming it. | Scripts off per repository by default, independent of the project-config switch. `devgraph add` never runs a script. Each provider needs its own approval of an exact digest. Repository-local `.env` and environment cannot influence any sandbox decision (§5.1). Selecting inputs runs no repository-configured git hook (§3.2). |
| T3 | **A malicious `.devgraph` change in a pulled branch**: script edited, provider added, `inputs` widened, relationship `custom.params` changed. | The digest covers the normalised script text and every declaration entry the provider touches (§5.2). Any change stops the provider; last good graph state stays; nothing runs or prunes while the schema is pending or awaiting approval (§5.5). |
| T4 | **The MCP client**, realistically a coding agent with a shell, possibly prompt-injected. | MCP has no script controls. `enable` and `approve` need a TTY; DevGraph never prints an approve command containing a digest; `run --dry-run` passes the same gates; gates and declarations are never read from graph state (§5.4). Residual, stated plainly: an agent with a shell can approve, so the sandbox must hold. |
| T5 | **Supply chain**: image, Python in it, packages. | Fully-qualified distroless image with no shell and no pip, pinned per architecture by image ID and repository digests (§4.3); pulled only by `devgraph sandbox setup`; `--pull=never` at run; standard-library allowlist; the bootstrap is a constant in DevGraph and the runner shim ships with DevGraph (§4.4). |
| T6 | **Resource exhaustion**: CPU, memory, fork bomb, output flood, huge input set, many concurrent runs. | cgroup limits, host-side per-file and per-run deadlines, conmon `--timeout`, frame-length caps before allocation, per-file and per-run output caps, one sandbox run at a time machine-wide (§6). |
| T7 | **Exfiltration.** Directly over the network; or indirectly: script output becomes node properties that reach the MCP client, a model that may have web access. | `--network=none` plus the hardened flags and seccomp profile (§4.3). For the indirect path: inputs are tracked files only, a secret-name denylist always applies, the matched-file count and sample are shown at approval (§3.2), string sizes are capped. Residuals: a script can copy any declared input into the graph, just as the built-in extractors expose file contents today; and the container can see the host's container-storage path in `/proc/self/mountinfo`, which usually contains the user's login name (identifying, not secret). |
| T8 | **Writes outside the repository's scope.** | Scripts return records; DevGraph injects `repo_id`, takes labels, property names and keys from the declaration, never from records; parameterised writes; endpoints only on repo-scoped labels and matched by `repo_id` (§3.3). |
| T9 | **Reads outside the repository.** | No mounts, checked twice: by inspect and from inside the container before the script is sent (§4.3, §4.4). DevGraph reads declared inputs with the no-follow reader (§3.2). |
| T10 | **A web page reaching the dashboard.** | Approve and enable are not dashboard actions; schema write routes refuse custom declarations (Q13); disable and revoke sit behind the existing guards; sandbox surfaces render with `textContent` (§8). |
| T11 | **Container escape** (kernel or runtime bug). | Rootless, `--userns=nomap` (the user's own UID is not mapped), seccomp-filtered, capability-free container. Residual. |
| T12 | **Hostile host-side parsing**: crafted frames, JSON, YAML or Python source aimed at DevGraph itself. | Lockstep framing (§4.4); strict record parsing (§3.3); the static scan runs in a limited subprocess and any exception is a hard reject (§4.2); YAML alias expansion bounded the same way. |
| T13 | **Host container configuration**: a user or distribution `containers.conf` or `mounts.conf` adding mounts, environment, devices or options. | Scrubbed environment for the podman process (§4.3); full inspect assertion; in-container mount and environment report compared against an allowlist before the script is sent (§4.4). |

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
- **Reserved names.** `custom_sources` and `custom_source` are added to the
  reserved property names: no declaration in any schema (custom or not) may
  declare them as metadata, and no record may set them (§3.3).
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

`ctx.tree` is the provider's matched inputs, not every indexable path, so a
script learns no file names beyond its own inputs.

**Selection.** Candidates are tracked files only, from `git ls-files`. That
command reads the repository's own `.git/config`, and **verified on git 2.55,
a plain `git ls-files -z` runs the program named by a repository-local
`core.fsmonitor`**, so selection would otherwise execute repository code. The
invocation is therefore fixed:

```
git --no-pager -C <real root path>
    -c core.fsmonitor=false -c core.untrackedCache=false
    ls-files -z --cached --sparse -t --stage
```

run with a constructed environment, not a filtered copy of the caller's:
`PATH` fixed, `HOME` set to an empty temporary directory, `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CEILING_DIRECTORIES=<parent of root>`,
`GIT_NO_LAZY_FETCH=1`, `GIT_PAGER=cat`, `LC_ALL=C`, and nothing else (no `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_INDEX_FILE`, `GIT_EXEC_PATH`, `GIT_CONFIG_*`, `GIT_TRACE*`). The git binary
is resolved once to an absolute path from the fixed `PATH`.

Why: command-line `-c` outranks every config file, `include` targets
included, so the hook does not run (verified). `--cached` reads only the index,
not blob content, so no `filter.*` or `textconv` program runs (**Unverified**
beyond git's documentation; E1's test asserts it). `--no-pager`,
`GIT_PAGER=cat` and a piped stdout keep `pager.ls-files` from running.
`GIT_CEILING_DIRECTORIES` stops a directory without its own `.git` from
resolving to an enclosing repository (verified). `ls-files` runs no hooks, so
`core.hooksPath` is irrelevant. A repository owned by another user now fails
`safe.directory`: `input_unavailable`, fail closed.

`--cached` is not enough on its own (found in E1 review, verified on git
2.55). In a partial clone (`extensions.partialClone` naming a promisor
remote) with a sparse index, a plain `ls-files` expands the index, reads the
collapsed directories' tree objects, finds them missing and lazily fetches
them from the promisor remote. That fetch runs whatever the repository
configures: `core.sshCommand` for an ssh URL, `remote.<name>.uploadpack` for a
local one, or an `ext::<program>` URL when repository-local
`protocol.ext.allow=always` (which also overrides a command-line
`protocol.allow=never`). `GIT_NO_LAZY_FETCH=1` is the guard: git refuses
any lazy fetch. `--sparse` is defence in depth for cone mode only: it keeps a
cone-mode sparse index collapsed (no tree objects are read), but with
non-cone patterns (`core.sparseCheckoutCone=false`) git expands the index
anyway and, without the environment guard, the fetch runs (verified; E1's
test covers both layouts).
`GIT_NO_LAZY_FETCH` exists from git 2.45, so an older git (checked with
`git --version` under the same environment) is `input_unavailable`, and
`doctor` reports it. Partial clones are allowed, not refused: blobless and
treeless clones are common, and with both guards no fetch is attempted
(E1's test covers all three transports with marker programs).

`-t --stage` gives each entry's tag and mode. Selection keeps only regular
files and symlinks (modes 100644, 100755, 120000) that are not skip-worktree
(`S`): out-of-cone files of a sparse checkout, collapsed sparse directories
(mode 040000) and gitlinks (submodules, 160000) are dropped the same way
whether or not the index is sparse. Repeated paths (an unmerged entry's
stages) count once.

The real resolved path, not the NFC trust-key form, goes to `-C`, so a
repository directory with an NFD name still resolves. Output is streamed and
filtered as it is read; once the matches exceed the per-run file cap, or the
index exceeds `INDEX_MAX_ENTRIES` (50 × the file cap) entries, git's process
group is killed and the run is `input_cap`, so a huge index is never
buffered whole. One 30 s deadline covers the version check, the read and the
final wait.

Any non-zero exit, or no git binary, means `input_unavailable` and the
provider does not run. Reading the index directly was considered and rejected:
index versions 2–4, split and sparse indexes are more parser surface than four
pinned options.

Candidates are then filtered by the declared globs, `IGNORED_DIR_NAMES`, and a
**secret-name denylist** that always applies and cannot be overridden in E
(`.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`,
`id_ecdsa*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `*.kdbx`,
`*.tfstate*`, `*credential*`, `*secret*`). The denylist matches each path
component after NFKC normalisation and case folding (so fullwidth `ｓｅｃｒｅｔ`
is caught); globs match the NFC-normalised path, case-sensitively.

**Reading.** A new module, `devgraph/sandbox/reader.py`, is the only code that
reads a sandbox input, a provider script, or the schema file whose declaration
feeds a digest. For each path:

1. The root must already be its own real path (`os.path.realpath`), absolute
   and not `/`, and is opened `O_NOFOLLOW | O_DIRECTORY`. Containment is
   decided on path components with `Path.is_relative_to` against it; never by
   string prefix.
2. `lstat` every component from the repository root down. Any symlink or any
   non-directory intermediate is refused.
3. Open without following: `openat2` with
   `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` relative to
   a directory descriptor of the root, called as syscall 437 on Linux machines
   that use the unified syscall table (x86_64, aarch64, arm, riscv64, ppc64,
   s390x, loongarch64); elsewhere, and on kernels without `openat2` (before
   5.6, or `EPERM` from a seccomp filter), component-by-component `openat(dir_fd, part, O_NOFOLLOW |
   O_DIRECTORY)` and `O_NOFOLLOW | O_RDONLY` for the leaf.
4. `fstat` the open descriptor: must be `S_ISREG`, size within the per-file
   cap. Read at most cap + 1 bytes from the descriptor; more is `input_cap`.

Steps 3–4 close the race between check and use: no component is followed by
name after the check, and the type and size decision is made on the
descriptor that is read. (A Windows reader is part of the platform follow-up,
§4.5; it cannot make the same claim. Where the no-follow flags or `dir_fd`
are missing, every read is refused.)

Residuals, stated plainly:
- **Hardlinks are read.** A hardlink inside the repository to a file elsewhere
  on the same filesystem is a regular file and passes every check. Kernel
  `protected_hardlinks` stops links to files the user does not own, not to the
  user's own files; a declared input that is a hardlink to a secret is read.
- **Mount points are crossed.** `RESOLVE_BENEATH` does not stop at a bind
  mount inside the repository (`RESOLVE_NO_XDEV` is not set), so a bind mount
  placed in the work tree is read through.
- **The fallback has a rename race.** The component-wise fallback opens one
  directory at a time. A concurrent rename can move an already-opened
  directory out of the repository; the file then read was beneath the root,
  with no symlink followed, when its parent was opened, but may not be by the
  time it is read. `openat2` resolves the whole path in one call and does not
  have this gap.

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
  field present; no reserved property (`RESERVED_NODE_PROPERTIES`, which now
  includes `custom_sources` and `custom_source`).
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

**Decision: no hash-seed determinism.** The interpreter runs with `-I`, which
implies `-E`, so `PYTHONHASHSEED` is ignored (verified: with
`PYTHONHASHSEED=0` in the environment, `python3 -I` reports
`sys.flags.hash_randomization == 1` and `hash("abc")` differs between runs).
The alternative, a bootstrap that re-executes the interpreter without `-I` so
the seed applies, was rejected: it starts a second interpreter whose start-up
honours `PYTHON*` variables (`PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`), so the
environment would become code-bearing input again, and it buys only output
stability for scripts that depend on `set` iteration order, which is not a
security property. `--env=PYTHONHASHSEED` is therefore not passed.

What remains deterministic: `TZ=UTC`, `LC_ALL=C.UTF-8`, files passed in sorted
order, no `time`, `random` or `os` (beyond `os.path`) in the allowlist, and
DevGraph sorts and de-duplicates records before writing. A script whose output
depends on `set` or `frozenset` iteration order of strings may differ between
runs; `devgraph config scripts run <repo> <name> --dry-run --twice` runs the
provider in two containers, reports any difference and writes nothing, and the
docs name `sorted()` as the fix.

Each `derive` call runs in a **fresh module namespace** (the compiled code
object executed into a new dict), so one file's failure cannot leave state that
skews the next. This holds for non-adversarial scripts only: a hostile script
shares one interpreter across its files and can persist state through mutable
builtins. That is acceptable because isolation between files of the *same*
provider protects nothing the provider could not already reach.

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
| `ｅｖａｌ(…)` (full-width letters) | name rules | yes: the parser NFKC-normalises identifiers, so the AST holds `eval` (verified) |

| | Subprocess + rlimits + seccomp/Landlock | Container (Podman) | WASM (CPython on WASI) | In-process + allowlist |
|---|---|---|---|---|
| Filesystem | Linux only (Landlock) | no mounts, read-only rootfs | no preopens | none |
| Network | Linux only (seccomp) | `--network=none` + seccomp | no sockets in WASI p1 | none |
| CPU / memory / pids | rlimits; Job Objects | cgroups v2 | fuel, store limits | thread timer only |
| macOS / Windows | **no isolation** | inside the Podman machine VM (follow-up, §4.5) | good | none |
| New dependency | libseccomp, per-OS code | none (Podman already required) | `wasmtime` + ~25 MB CPython build | none |
| Start-up | ~50 ms | ~0.3–1 s per run | ~0.2–0.5 s | none |
| Verdict | uneven across platforms | **Recommended** | runner-up | rejected as sole layer |

### 4.2 Host-side static scan

The scan (import allowlist, denied names, dunder attributes, the table above)
gives early, readable findings at approval; it is hygiene, not a boundary. It
parses untrusted source, so it runs in a **separate `python -I -S` subprocess**
with a 5 s wall timeout, `RLIMIT_AS` 256 MiB and `RLIMIT_CPU` 5 s, and an
environment of only `PATH` and `LC_ALL=C` (a named locale such as `C.UTF-8`
makes glibc map its whole locale archive, 222 MiB on Fedora, and the
interpreter then cannot start under that limit). Any non-zero
exit, timeout, signal or exception inside it, including `RecursionError`,
`MemoryError` and `SyntaxError`, is a hard reject (`static_reject`), never a
pass. It parses with `feature_version` set to the image's Python minor version
so that the scan and the container agree on the grammar.

The schema YAML is untrusted input of the same class: alias expansion is
bounded (a document whose expansion exceeds 10,000 nodes is rejected), and
loader exceptions of any type already fail closed (`YAML_LOAD_ERRORS` in
`project_tools.py` is the precedent; E1 extends it to the schema loader's
custom sections).

### 4.3 The container invocation

A dedicated, ephemeral, mount-free container from a pinned image (Q2): the
Neo4j container holds the graph and its credentials, so a script there could
bypass §3's write path.

**The podman process itself** is started with a constructed environment:
`PATH` fixed, `HOME` from the password database (§5.1), `XDG_RUNTIME_DIR`
(`/run/user/<uid>`, checked to exist and be owned by the user), `LANG=C.UTF-8`,
and nothing else. Everything else is absent, in particular `CONTAINER_HOST`,
`CONTAINER_CONNECTION`, `CONTAINERS_CONF`, `CONTAINERS_CONF_OVERRIDE`,
`CONTAINERS_STORAGE_CONF`, `CONTAINERS_REGISTRIES_CONF`, `PODMAN_USERNS`,
`XDG_CONFIG_HOME`, `DOCKER_HOST`, `DOCKER_CONFIG` and every proxy variable.
Verified: rootless Podman 5.8.4 with the systemd cgroup manager creates and
runs a memory-, pids- and CPU-limited `--userns=nomap` container with exactly
these four variables. The binary is resolved once, to an absolute path, from
the fixed `PATH`. `podman info` must report `ServiceIsRemote: false`; a remote
service is refused.

**Create, inspect, start** (instead of a single `podman run`, so the
container can be checked before anything executes):

```
podman create --interactive --pull=never
  --network=none --http-proxy=false --no-hosts --no-hostname
  --read-only --read-only-tmpfs=false
  --log-driver=none --ipc=none --userns=nomap
  --cap-drop=all --security-opt=no-new-privileges
  --security-opt=seccomp=<devgraph-sandbox.json>
  --user=65534:65534 --pids-limit=32 --memory=256m --memory-swap=256m
  --cpus=1 --ulimit=nofile=64:64 --ulimit=core=0
  --timeout=<host wall clock + 5>
  --env=TZ=UTC --env=LC_ALL=C.UTF-8
  --label=devgraph.sandbox=1 --name=devgraph-sandbox-<random 128-bit hex>
  --entrypoint=<interpreter path, per image>
  <registry>/<repository>@sha256:<manifest-list digest>
  -I -S -c <BOOTSTRAP>
```

`<BOOTSTRAP>` is a constant string in DevGraph's source (§4.4). The script
never travels in argv.

Why the non-obvious flags (the set was accepted together by Podman 5.8.4):
`--http-proxy=false` stops Podman copying host proxy variables in (default
`true`). `--no-hosts` and `--no-hostname` drop `/etc/hosts` and
`/etc/hostname`; `--network=none` already drops `resolv.conf`. Revision 2's
`--dns=none` is **removed**: Podman rejects it with "conflicting options: dns
and the network mode: none" (verified). `--read-only-tmpfs=false`: with
`--read-only` alone Podman mounts writable tmpfs on `/dev`, `/dev/shm`, `/run`,
`/tmp` and `/var/tmp`; with `false` nothing is writable and `/dev` is `ro`
(verified). If E2a shows CPython needs a writable path, one
`--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m` is the only permitted exception,
added to the mount allowlist too. `--log-driver=none` keeps file-derived
stdout out of an on-disk log. `--ipc=none` removes `/dev/shm` (verified).
`--userns=nomap` leaves the user's own UID unmapped, so an escape lands as a
subordinate UID; there is no fallback mode, and a host where it fails is
`sandbox_unavailable`. `--entrypoint` is explicit because the image's own is
not trusted. The container name is random because `/run/.containerenv`
exposes it inside.

**Seccomp profile** shipped with DevGraph (`devgraph/sandbox/seccomp.json`):
allowlist-based (default action `ERRNO`), derived from Podman's default and
additionally denying `unshare`, `clone`/`clone3` with any `CLONE_NEW*` flag
(`clone3` returns `ENOSYS` so libc falls back to filterable `clone`), `setns`,
`io_uring_setup`/`enter`/`register`, `bpf`, `userfaultfd`, `keyctl`/`add_key`/
`request_key`, `perf_event_open`, `ptrace`, `process_vm_readv`/`writev`, the
mount family (`mount`, `umount2`, `pivot_root`, `fsopen`, `fsmount`,
`open_tree`, `move_mount`) and `socket`/`socketpair` for every family. The
profile is per architecture where syscall sets differ; E2a proves CPython
starts under it on each supported architecture. (Verified only that the stock
profile is applied: `Seccomp: 2` in `/proc/self/status`, and that `socket()`
succeeds under the stock profile, which is why ours must deny it.)

**Image pinning.** For each supported architecture (x86_64, aarch64) DevGraph's
source records: the manifest-list digest, the per-architecture manifest digest,
the image ID (the config digest), the interpreter path, the image's own
`Config.Env`, and the home directory the image's `/etc/passwd` gives UID 65534.
Verified on Podman 5.8.4 by pulling a multi-architecture image by its list
digest: the container's `Image` is the image ID, `ImageDigest` is the list
digest used to pull, and the image's `RepoDigests` lists both the list digest
and the per-architecture manifest digest. Comparing the list digest with the
image ID, as revision 2 did, could never match.

**Post-create assertion.** `podman inspect` on the created container, and
`podman image inspect` on its image, must show exactly:

| Field | Expected |
|-------|----------|
| `Image` | the pinned image ID for the host architecture |
| `ImageDigest` | the pinned manifest-list digest |
| image `RepoDigests` | contains `<repo>@<list digest>` and `<repo>@<per-arch digest>` |
| `Path`, `Args` | the pinned interpreter path; `-I`, `-S`, `-c`, `<BOOTSTRAP>` |
| `Config.Env` (as a set) | the pinned image's `Config.Env` ∪ {`container=podman`} ∪ {`TZ=UTC`, `LC_ALL=C.UTF-8`} |
| `Config.User`, `Config.Timeout` | `65534:65534`; the computed timeout |
| `Mounts`, `HostConfig.Binds`, `HostConfig.Tmpfs` | empty |
| `HostConfig.Devices`, `HostConfig.DeviceCgroupRules` | empty or absent |
| `HostConfig.Sysctls` | empty or absent |
| `HostConfig.Privileged` | `false` |
| `HostConfig.SecurityOpt` (as a set) | exactly {`no-new-privileges`, `seccomp=<our path>`} |
| `ProcessLabel` | when SELinux is enabled, type `container_t` (not `spc_t`, not empty); when disabled, empty |
| `AppArmorProfile` | when AppArmor is disabled, empty; when enabled, Podman's default profile. **Unverified**: no AppArmor host was available; E2a records the expected name |
| `HostConfig.Ulimits` | exactly `RLIMIT_NOFILE` 64/64 and `RLIMIT_CORE` 0/0 |
| `HostConfig.CapAdd` | empty; `CapDrop` contains every capability Podman would otherwise grant |
| `HostConfig.NetworkMode`, `IpcMode` | `none`, `none` |
| `HostConfig.PidMode`, `UTSMode` | `private`, `private` |
| user namespace | `HostConfig.UsernsMode` reads `private` for `nomap` (verified), so the effective mode is taken from `HostConfig.Annotations["io.podman.annotations.userns"] == "nomap"` and `HostConfig.IDMappings`, whose ranges must not cover intermediate ID 0 (the user) |
| `HostConfig.Memory`, `MemorySwap`, `PidsLimit`, `NanoCpus` | the §6 values |
| `HostConfig.LogConfig.Type` | `none` |
| `HostConfig.ReadonlyRootfs` | `true` |
| `HostConfig.Annotations` | exactly the keys Podman sets for these flags (pids-limit, seccomp, userns) |
| `HostConfig.ExtraHosts`, `Dns` | empty |

`Config.Env` at inspect time lists the image environment, `container=podman`
and ours (verified). Two more variables appear only inside the running
container, `HOME` (from the image's `/etc/passwd`) and `HOSTNAME` (the short
container ID); those are checked by the bootstrap's report (§4.4). The
expected sets are recomputed whenever the pinned digest changes, by the same
reviewed code change.

Any difference, including anything injected by a `containers.conf`, means
`podman rm -f`, `sandbox_unavailable` with the differing field named (never its
value), and no start. The assertion ignores fields not in the table; E2a
additionally diffs a full inspect against a recorded baseline so a Podman
upgrade that adds a security-relevant field is noticed in review.

**Why inspect is not enough.** A `mounts.conf` entry is copied into the
container and **does not appear in `Mounts`**. Verified: a Fedora host's
distribution `mounts.conf` (`/usr/share/rhel/secrets:/run/secrets`) puts a
`/run/secrets` mount in `/proc/self/mountinfo` while `podman inspect` shows
`Mounts: []`. A user-level `~/.config/containers/mounts.conf` naming a secrets
directory would copy that directory in unseen. Hence the in-container report
of §4.4, checked before the script is sent.

**Readiness** (`podman info --format json`), checked before each process's
first run and shown by `doctor`: Linux; rootless; not remote; cgroup v2 with
`memory`, `cpu`, `pids` delegated; seccomp enabled; Podman ≥ 5.0; the pinned
image ID present. Any failure refuses the run; there is **no unsandboxed
fallback** (Q1), and **the Podman socket is never mounted** (Q8).

**Image.** A fully-qualified distroless Python 3 image (no shell, no pip).
Only `devgraph sandbox setup` pulls, by list digest, then checks the image ID.
The pin is refreshed by reviewed code change each DevGraph minor release and
within a week of a high-severity CPython or C-library CVE; `doctor` shows its
age. **Unverified**: that image's interpreter path, `Config.Env` and passwd
entry for 65534; E2a records them.

**Inside**, the runner applies the import allowlist (`re`, `json`, `pathlib`
pure-path classes, `os.path`, `string`, `textwrap`, `collections`,
`itertools`, `functools`, `dataclasses`, `typing`, `math`, `fnmatch`),
restricted builtins (no `open`, `exec`, `eval`, `compile`, `__import__`,
`input`, `breakpoint`) and a per-call CPU timer. Like the static scan, these
are hygiene; the container and the host deadlines are the boundary.

### 4.4 Bootstrap and frame protocol

**Delivery, decided: the bootstrap is in argv; everything else is on stdin.**
`<BOOTSTRAP>` is a short constant in DevGraph's source, visible in inspect
`Args` and compared exactly (§4.3). It contains no repository data. The runner
shim and the script text arrive on stdin, after the report below is accepted.

Frames in both directions are a 4-byte big-endian length followed by that many
bytes of JSON. The sequence is:

1. **Report (container → host), before reading stdin.** The bootstrap reads
   `/proc/self/mountinfo`, `/proc/self/uid_map`, `/proc/self/gid_map`,
   `/proc/self/status` (`Uid`, `CapPrm`, `CapEff`, `CapBnd`, `NoNewPrivs`,
   `Seccomp`) and `os.environ`, lists `/run/secrets` if present, and sends them
   as the first frame. The host compares:
   - **Mounts** against an allowlist of `(mount point, filesystem type, ro|rw)`
     recorded for this flag set: `/` (`overlay`, ro), `/proc` and Podman's
     masked paths under `/proc` and `/sys` (each `ro`), `/dev` (`tmpfs`, ro),
     `/dev/pts`, `/dev/mqueue`, the device nodes `null`, `zero`, `full`, `tty`,
     `random`, `urandom`, `/sys` (`sysfs`, ro), `/sys/fs/cgroup` (`cgroup2`,
     ro) and `/run/.containerenv` (ro). `/run/secrets` is allowed only when the
     listing is empty (verified: the Fedora distribution entry above yields an
     empty directory, because its host entries are dangling symlinks). Any
     other mount point is refused. The table is generated in E2a on a clean
     host and reviewed; Podman major upgrades re-run that test.
   - **Identity**: `Uid` all 65534; every capability set zero; `NoNewPrivs: 1`;
     `Seccomp: 2`; `uid_map`/`gid_map` with no range covering parent ID 0
     (verified: `--userns=nomap` gives `0 1 65536`, so the user's own UID is
     absent).
   - **Environment** equals the inspect `Config.Env` set plus `HOME` with the
     pinned value and `HOSTNAME` equal to the first 12 hex characters of the
     container ID.

   Any mismatch: kill, `sandbox_unavailable` naming the field, nothing sent.
   The report is compared and discarded, never logged: `mountinfo` contains the
   host's storage paths.
2. **Load (host → container).** One frame with the runner shim and the script
   text. The bootstrap compiles the shim, which compiles the script with
   `compile(text, "<provider>", "exec")` and answers with one `ready` frame or
   a `static_reject`-class error.
3. **Lockstep files.** The host sends file *N* as `{seq: N, path, text}` and
   accepts exactly one result frame, which must carry `seq: N`, before sending
   *N + 1*. The host never acts on a path it did not send; a `path` in a
   result is ignored and the host's own record of *N* is used.

Rules for every frame:

- **Length first.** A declared length above the cap for that frame (64 KiB for
  the report and `ready`, 1 MiB plus a fixed envelope for results) is a
  protocol error *before* any buffer is allocated.
- Any extra, out-of-order, oversize or undecodable frame, or bytes on stdout
  before the report, means: kill the container, fail the in-flight file with
  `protocol`, and handle the remaining files as in §6.
- **I/O threads.** stdin is written on its own thread with a per-frame write
  deadline of 5 s, so a container that never reads stdin is killed rather than
  blocking the host. stderr is drained continuously on another thread; bytes
  beyond 8 KiB are discarded but reading never stops. stdout is read by the
  frame parser under the per-file and per-run deadlines (§6).
- **Killing.** On any deadline the host runs `podman kill --signal=KILL
  <name>` then `podman rm -f <name>`. `--timeout` (host wall clock + 5 s) makes
  conmon kill the container even if the host process has died (verified: a
  detached container created with `--timeout=3` exits after 3 s with no client
  attached). At start-up each DevGraph process removes leftover
  `devgraph.sandbox=1` containers older than the maximum wall clock.

The protocol stays runtime-neutral so a WASM runner could replace §4.3
without changing §3 or §5.

### 4.5 Platform scope

**Linux with rootless Podman is the only supported platform in E.** This is
the honest scope: the other platforms each carry unresolved design work, and
none of it should block Linux.

**macOS and Windows** report every custom provider as `unavailable
(platform)`; `approve` and `enable` refuse with the same reason; `doctor` says
so. The follow-up must settle, at least:

- **The podman process environment differs per OS.** The remote client needs
  the connection configuration found through the user's profile: on Windows
  `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and `SystemRoot`; on macOS `HOME`
  and probably `TMPDIR`. The allowlist and the `CONTAINER_*` exclusions must be
  re-derived per OS. **Unverified.**
- **How the seccomp profile reaches the VM.** The profile is a host file; with
  a remote client it is unverified whether the client reads it and sends its
  contents or passes the path for the VM to open. If the latter, the path must
  exist inside the VM, which brings in the VM's mounts of the user's home.
- **The VM is not an extra boundary.** The Podman machine mounts the user's
  home into the VM (macOS) or the Windows drive at `/mnt/c` (WSL2), and Neo4j
  runs in the same VM, so a container escape reaches both. The mount check of
  §4.4 sees only the container, not the VM.
- **A Windows no-follow reader.** `CreateFileW` with
  `FILE_FLAG_OPEN_REPARSE_POINT` stops only at a reparse point in the *final*
  component; intermediate directories are still resolved by name at open time,
  so a junction swapped in after an `lstat` walk would be followed. The reader
  would have to check the opened handle's final path
  (`GetFinalPathNameByHandleW`) against the root and refuse on mismatch: a
  check after open, not an atomic no-follow open as on Linux.
- **Case- and normalisation-insensitive paths** (APFS NFD names, NTFS case
  folding) for globs, the denylist and the trust-store key.

**Docker: dropped from E.** Revision 2 accepted rootless or userns-remapped
Docker. It is dropped because it would need its own verified invocation (no
`--http-proxy=false`, so client-config proxies reach the container as
environment; no `nomap`; different inspect field names and annotation keys),
its own assertion table, its own mount allowlist and its own E2a run, and
Podman is already DevGraph's required runtime. Nothing found argues for
keeping it. **The Docker socket is never mounted** anywhere.

**Headless Docker image:** custom providers are unavailable (Q8).

**CI:** `approve --sha256` from the same repository the CI is testing is
self-approval. The documented pattern takes the digest from a protected CI
secret or variable that pull requests cannot change.

---

## 5. Trust and consent

### 5.1 Where gates and settings live

Nothing that decides whether or how a script runs comes from `Settings`, a
`.env` file or an environment variable:

- **Home.** `<home>` is `pwd.getpwuid(os.getuid()).pw_dir`, never `HOME` or
  `Path.home()`. Tests pass paths as function arguments.
- **Trust store:** a dedicated SQLite file at `<home>/.devgraph/script_trust.sqlite3`.
  Contents: per repository, `scripts_enabled` (absent ⇒ off); per provider,
  approved digests with their approval dates and active/retired state.
- **Key:** every row is keyed by `(repo_id, canonical repository path)`. The
  canonical path is the resolved real path, NFC-normalised. A repository moved,
  re-registered under another id, or a different checkout reusing an id,
  inherits nothing.
- **Gate 1, the project-config switch, without `Settings`.** The script gates
  open the registry at the fixed path `<home>/.devgraph/registry.sqlite3`
  (the default `registry_db_path`), read-only (`mode=ro` URI, short timeout),
  and look the repository up by `repo_id` **and** canonical path. Gate 1 is
  true only when exactly one row matches both and its `project_config_enabled`
  is true. They never call `project_switch.py`, which fails open. If `Settings`
  points the registry elsewhere, the fixed-path registry either lacks the
  repository or disagrees with what DevGraph uses; both read as off, and
  `doctor` explains that custom providers require the default registry
  location. Reading `Settings` for that explanation is display-only and can
  only make the answer more closed.
- **Limits, image pins, seccomp profile, allowlists, denylist:** code
  constants (Q9).
- **Machine-wide lock:** `<home>/.devgraph/sandbox.lock`, an `fcntl` lock
  (§6).
- **Fail closed.** Any error while deciding (trust store or registry missing,
  locked, corrupt, wrong schema version; repository unknown or ambiguous; path
  not canonicalisable) means "scripts off" and "not approved".

### 5.2 Gates and the digest

Three conditions, all checked against one snapshot immediately before every
run, including `run --dry-run`:

1. The repository's **project config is on**, read as in §5.1.
2. **Scripts are enabled** for `(repo_id, canonical path)` in the trust store.
3. The provider's **current digest is approved and active** (§5.6).

Plus one precondition: the repository's **schema is not pending** (§5.5).

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
any Unicode format character (category Cf, which includes the bidi
overrides and isolates U+202A–U+202E and U+2066–U+2069, U+200E/U+200F and
zero-width characters), a line or paragraph separator (U+2028/U+2029, Zl/Zp:
some renderers break lines there while Python's tokenizer does not), or a
private-use or unassigned code point (Co/Cn, as for the tools file and input
globs). Categories come from the host Python's Unicode tables, so a code
point a newer Unicode version assigns can be refused as Cn on an older host.
A script can still write any of these as escape sequences inside a
string literal. CRLF is normalised to LF; that string is hashed,
shown at approval, sent over stdin, and passed to `compile()` as a `str`,
so no decoding step inside the container can reinterpret it.

**NFKC identifiers.** Python normalises identifiers with NFKC, so a script can
spell `eval` with full-width letters and a reviewer reading the text sees
something that is not quite `eval`. The static scan already sees the
normalised name (§4.1). The approval display additionally renders every
non-ASCII character **outside string literals and comments** as a `\u....`
escape (located with `tokenize`; a tokenize failure is `static_reject`), so
any non-ASCII identifier is visibly odd. The scan reports the string and
comment ranges, and everything else counts as code, so a missing range fails
closed to an escape; malformed ranges are `static_reject`. Non-ASCII inside strings and comments
is shown as text, subject to the control-character rule below.

Every repository-sourced string DevGraph prints (script text, paths, params,
labels, sample file names, diffs) is rendered with control, format,
separator, surrogate, private-use and unassigned characters made visible as `\x..`/`\u....` escapes, so terminal escape
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

Gates and declarations are read only from the trust store, the fixed-path
registry and the repository's files, never from graph state such as the
`Repository` node's schema fields. The one graph read on the run path is the
pending check of §5.5, which can only *prevent* a run.

### 5.5 Provider state machine

**Decision: custom providers do not run while the schema is pending.**
Revision 2 said the provider would run "under the applied schema's
declaration", but the applied declaration exists only as graph state, which
§5.4 forbids as a source. Not running is simpler and needs no stored copy.

| State | Meaning | Runs? | Prunes? |
|-------|---------|-------|---------|
| `disabled` | project config off, or scripts off | no | no |
| `unavailable` | sandbox not ready, not Linux, no git work tree, or headless image | no | no |
| `pending` | the schema file differs from the applied schema | no | no |
| `awaiting_approval` | digest never approved, retired or revoked; new provider | no | no |
| `approved` | digest active | yes | yes, only after a successful full run |
| `failing` | approved, last run had errors | yes | only for files that succeeded in a full run |

Rules:

- **Pending.** The check is `schema_pending()`: the schema file's hash against
  the applied hash recorded in the graph. A missing or unreadable record, or
  any error, counts as pending. Graph state is used only to *skip* a run: a
  forged "applied" hash can at worst let the provider run against the current
  file, which is exactly the file its gates and digest were checked against.
  When the schema is applied (debounce elapsed or `rescan --now`), the apply
  triggers one full run per approved provider; that run's snapshot reads the
  schema file again, and if its hash no longer equals the hash just applied,
  the run is skipped as pending.
- **Only a successful full run of an active digest prunes.** A provider in any
  non-running state keeps its nodes as the last good state, shown as stale in
  `scripts list` and the dashboard.
- Graph data for a provider is removed only by removing its declaration (the
  existing removed-type cleanup, scoped by extractor, §3.4) or by `devgraph
  remove`.
- Approving a digest triggers one full provider run (if not pending).

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
| `devgraph doctor` | Providers awaiting approval, pending or failing; the readiness checks of §4.3; image age; registry-location mismatch (§5.1); platform. |

**The project-config switch.** Scripts need the project config on (§5.2).
Turning it off stops scripts; turning it back on **resumes approved providers
without re-approval**, from the CLI (`devgraph config enable`) and from the
dashboard alike. The docs say so, and both the CLI's output and the
dashboard's dry run for enabling project config list the approved providers
that will resume.

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
| CPU per `derive` call | 2 s | runner timer (hygiene) | that file: `timeout` |
| **Wall clock per file** | 5 s from request write to result read | host deadline | kill; that file: `timeout`; rest as below |
| Start-up (report + `ready`) | 10 s | host deadline | `sandbox_unavailable` |
| Wall clock per run | 30 s + 50 ms per file, max 300 s | host kill; conmon `--timeout` at +5 s | in-flight file: `timeout` |
| Memory | 256 MiB, no swap | cgroup | container killed: `memory` |
| Processes | 32 | cgroup | `crash` |
| Output per file | 1,000 records, 1 MiB | frame parser, before allocation | that file: `output_cap` |
| Output per run | 50,000 records, 16 MiB | host | rest of run: `output_cap` |
| stderr | 8 KiB kept, rest drained and discarded | host | none |
| Concurrent sandbox runs | 1 per machine | `<home>/.devgraph/sandbox.lock` | waits up to 60 s, then `busy`; retried on the next event |

**Why a host-side per-file deadline.** The in-container CPU timer is a signal
handler: it cannot interrupt a long C-level call (a catastrophic regular
expression, a huge `str.join`), and a script can catch or disarm it. Without a
host deadline per file, one such file would consume the whole run's wall clock
and every later file would be `aborted`. The host deadline is independent of
anything in the container.

**Breach semantics.** Errors are per input file wherever possible: a failed
file keeps its last good graph state and the run continues. When the
container dies or is killed (per-file deadline, memory, run wall clock, crash,
protocol), results already validated are applied, the in-flight file gets the
named error and is **not** retried, and the remaining files are retried once
in a fresh container:

- The retry container's wall clock is `min(30 s + 50 ms × remaining files,
  300 s − time already spent)`, with conmon `--timeout` at that value + 5 s.
  The 300 s ceiling therefore bounds both containers together.
- If under 10 s of that budget remains, there is no retry and the remaining
  files are `aborted`.
- A second death marks the remaining files `aborted`.

A failed run never prunes (§5.5), never aborts built-in extraction or other
providers, and never retries in a loop.

**Concurrency.** The lock is shared by the tray agent, the CLI and the
dashboard, and held across both containers of a run.

**Writes** are batched: per-file replace transactions grouped up to 500
records, so a 50,000-record run does not hold one large transaction.

---

## 7. Script tools for MCP: deferred

Script-defined and composition tools (epic §7) are out of E (Q4): they run on
arguments chosen by the untrusted caller (T4); a useful one needs a read
channel into Neo4j, which is a new boundary; per-call container start-up is a
poor fit, and a warm pool is a new long-lived process to secure; the
read-only Cypher tools already meet the epic's stated goal. When picked up
they reuse §4 and the project-tools trust model of E0.5b (opt-in per
repository, pinned to a hash, CLI-only approval), with locked-name resolution
and scope pinning.

---

## 8. Failure, observability and telemetry

- **Run record.** One metadata-only line per run in
  `~/.devgraph/sandbox_runs.jsonl` (500 entries, same rotation as
  `mcp_telemetry.jsonl`): `ts`, `repo_id`, `provider`, short digest, file
  count, nodes and edges written, dropped endpoints, duration, outcome,
  error-code counts. Never contents, param values, output, stderr or the
  bootstrap report.
- **Error codes:** `disabled`, `pending`, `awaiting_approval`,
  `sandbox_unavailable`, `input_unavailable`, `static_reject`, `input_cap`,
  `input_decode`, `busy`, `timeout`, `memory`, `output_cap`, `protocol`,
  `schema_violation`, `crash`, `aborted`. Each carries the input path and a
  one-line reason. **Reasons never quote a value** from a script, an input
  file, a record or the container's report: they name the record index, field
  and rule.
- **stderr** is shown only by an interactive `run --dry-run`, rendered with
  controls visible (§5.3). It never reaches a log file, the run record, the
  dashboard or a notice, not even a tail; a script can print file contents.
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

Each is a decision the body already recommends; a "no" changes the named
section.

1. **Runtime.** Rootless Podman on Linux only, no unsandboxed fallback, no
   Docker (§4.3–4.5). *Recommend: yes.*
2. **Container.** Dedicated and ephemeral, not the Neo4j container. *Yes.*
3. **`git`, `ast`, `docs` declarative providers.** Out of E, filed as
   follow-ups; close epic #1 after E. *Yes.*
4. **Script and composition MCP tools.** Defer (§7). *Defer.*
5. **Consent.** `enable` and `approve` CLI-only with a TTY; dashboard disable
   and revoke only; no approve one-liners printed; the sandbox, not consent,
   is the boundary (§2, §5.4). *Yes.*
6. **Digest.** Script text plus the full declaration set, one snapshot per
   run, declaration diff at approval (§5.2). *Yes.*
7. **Multiple digests.** Retire by default; `--keep-previous` up to five
   (§5.6). *Yes.*
8. **Headless Docker image.** Custom providers unavailable; no runtime socket
   ever mounted. *Yes; revisit with a WASM runner.*
9. **Limits.** §6 values as code constants, no override in E. *Yes.*
10. **Non-interactive approval.** `approve --sha256` for the matching digest
    only, digest from a protected CI secret, no `--yes`. *Yes.*
11. **Call granularity.** `derive` per input file, fresh namespace per call.
    *Per file.*
12. **Script location.** Fixed `.devgraph/providers/<name>.py`, real
    directories, 64 KiB, no-follow reader. *Yes.*
13. **Dashboard schema routes.** Refuse creating or editing custom
    declarations in E (entries with `source.provider: custom` or
    `provider: custom`, and changes to `custom_providers`); deleting stays
    allowed, since it only reduces what runs. *Not in E.*
14. **Platform scope.** Linux only; macOS and Windows report
    `unavailable (platform)` and get a follow-up scoped by §4.5. *Yes.*
15. **Required sandbox CI job.** The repository has no CI. E2a adds a workflow;
    making `sandbox-linux` a required check is an upstream setting only the
    maintainer can change. Should E2b's merge wait on it? *Yes; otherwise the
    E2a gate is advisory.*
16. **Non-default registry location.** Gate 1 reads only the default registry
    path (§5.1), so a user who moved the registry cannot run custom providers
    in E. *Accept for E.*

---

## 10. Slice plan

Each slice is one reviewable PR stacked on the previous one, except E0.5a. None
starts before this document is signed off.

| Slice | Content | Executes repository code? | User sign-off before merge |
|-------|---------|--------------------------|----------------------------|
| **E0.5a** Present-code fixes | `.env` from the DevGraph home only, `is_relative_to` containment, symlinks out of the repository skipped (§0), with regression tests. Standalone off master. | No | No: ordinary review. E1 waits for it. |
| **E0.5b** Project MCP tools opt-in | Per-repository opt-in pinned to the tools file's hash, CLI-only approval (§0). On the epic stack. | No | Per its own plan. E1 waits for it. |
| **E1** Schema, trust store, consent | `custom_providers`, custom `NodeSource`, the §3.1 rules (including reserved `custom_sources`/`custom_source`), JSON Schema; the trust store and fixed-path gate 1, fail closed; the no-follow reader; hardened `git ls-files` selection and the denylist; source normalisation, NFKC-aware display and the digest (§5.2–5.3); static scan in a limited subprocess; YAML alias bound; `config scripts list/show/approve/revoke/enable/disable` (Linux; `unavailable (platform)` elsewhere); doctor reporting; `add` notice; dashboard schema routes refuse custom declarations (Q13). | **No**, including git: `core.fsmonitor` and pager overridden (§3.2) | **Yes**: it fixes the consent UX and prompt wording. |
| **E2a** Adversarial suite, tests first | Every container test below, marked `xfail(strict=True)`; the host-side fuzz corpus; the CI workflow. Reviewed before any runner code. | No | **Yes**: the suite is the acceptance bar for E2b. |
| **E2b** Runner | `devgraph/sandbox/`: constructed-environment podman invocation, create/inspect/start, the assertion table, bootstrap report check, readiness checks, seccomp profile, lockstep frame protocol with I/O threads, per-file and per-run deadlines, bounded retry, host frame parser and output validator (§3.3), error codes, machine-wide lock, `devgraph sandbox setup`, `run --dry-run [--twice]`. Removes every `xfail` marker from E2a. Writes nothing to the graph. | **Yes**, dry run only | **Yes**: first code that runs repository scripts. |
| **E3** Provider wiring | Provenance-isolation tests first (§3.4), then declared-key MERGE, keyed endpoint MATCH, extractor-scoped per-file replace and prune; state machine including `pending` (§5.5) and indexer/rescan integration; batched writes; run records; Neo4j parameter logging off; list of `name`-dependent displays. Backward-compatibility snapshot: no schema file ⇒ identical graph. | Yes, from the indexer | **Yes**: scripts start writing to the graph. |
| **E4** Surfaces and docs | Dashboard project-card section (`textContent` only; disable and revoke with dry run; project-config dry-run listing resuming providers); key-value display fallback; rate-limited tray notice; README, PROJECT_STATUS, platform scope (§4.5) and the "defence in depth" note; end-to-end acceptance against the epic's sandbox criterion. | No new paths | **Yes**: final sign-off before the epic closes. |

### 10.1 How the E2a gate binds

- Every container test is `@pytest.mark.xfail(strict=True)` in E2a. With
  `strict=True` an unexpected pass is a failure, so a test cannot silently
  start passing against a half-built runner; E2b must remove each marker, and
  the diff shows exactly which tests it claims.
- Container tests carry a `sandbox` marker. Locally they skip when rootless
  Podman ≥ 5.0 is absent. In CI they never skip: the `sandbox-linux` job sets
  `DEVGRAPH_TEST_REQUIRE_SANDBOX=1` (a test-harness switch, read only by
  `conftest.py`, never by DevGraph), under which a missing or rootful Podman
  fails the session.
- **What runs where:**

  | Where | Runs |
  |-------|------|
  | Existing test job (every OS) | All unit tests: loader rules, trust store, reader, git selection, digest, display escaping, frame parser and validator fuzz corpus, deadlines with a fake runtime. |
  | `sandbox-linux` (ubuntu x86_64, rootless Podman ≥ 5.0, image pre-pulled by digest) | Every `sandbox`-marked test. **Required** status check for E2b and later (Q15). |
  | `sandbox-linux-arm64` (aarch64 runner) | The per-architecture tests: CPython under the seccomp profile, image pin, mount allowlist. Required if an aarch64 runner is available; otherwise aarch64 is unsupported in E. **Unverified**: hosted-runner availability and the Podman version on hosted Ubuntu images; the job may need to install Podman 5. |

### 10.2 Named tests per slice

**E0.5a.** `test_dotenv_in_cwd_is_ignored`, `test_containment_rejects_sibling_prefix`
(`/repo-other` vs `/repo`), `test_walk_skips_symlink_leaving_repo`.

**E1.**

| Test | Asserts |
|------|---------|
| `test_sandbox_decisions_ignore_env_and_dotenv` | With a hostile `.env` in the working directory and in the repository, and `DEVGRAPH_*`, `HOME`, `USERPROFILE` set to other values, gates, trust-store path, registry path, limits and runtime choice are unchanged. |
| `test_gate1_reads_fixed_registry_and_fails_closed` | Missing, locked, corrupt, column-less registry; unknown repo; `repo_id` match with path mismatch; `Settings.registry_db_path` pointing elsewhere: all off. |
| `test_trust_store_fails_closed` | Missing, locked, corrupt, wrong schema version, moved repository. |
| `test_approve_and_enable_refuse_without_tty` | stdin not a TTY, stdout not a TTY, both: refused, nothing written. |
| `test_no_output_contains_approve_command_with_digest` | Every CLI, doctor, `add` and dashboard text output in the suite's scenarios is scanned for `approve` followed anywhere on the line by 64 hex characters; none matches. |
| `test_approve_retires_previous_digests`, `test_keep_previous_caps_at_five`, `test_retired_digest_reprompts`, `test_revoke_stops_active_digest` | §5.6. |
| `test_selection_excludes_untracked_and_ignored_files` | Untracked, ignored and denylisted files never match, whatever the globs. |
| `test_no_git_work_tree_is_input_unavailable` | A non-repository directory nested inside another repository is not resolved to the outer one; no git binary; non-zero exit. |
| `test_ls_files_runs_no_repository_program` | Repository config sets `core.fsmonitor`, `pager.ls-files`, `filter.x.clean`/`smudge`, `diff.x.textconv` with `.gitattributes`, and an `include.path` to a file setting `core.fsmonitor`; a marker file is never created. |
| `test_lazy_fetch_runs_no_repository_program` | A partial clone with a sparse index whose collapsed trees are missing, its promisor remote reached through `core.sshCommand`, `remote.origin.uploadpack`, or `ext::` with repository-local `protocol.ext.allow=always`, each with cone and with non-cone sparse patterns; a marker file is never created, and a control run without the guards creates it. |
| `test_reader_refusals` | Symlinked leaf and intermediate, `..`, FIFO, oversize, swapped-in symlink between check and open. |
| `test_normalisation_rejection_table`, `test_digest_stability_and_sensitivity` | §5.2–5.3. |
| `test_approval_display_escapes_non_ascii_identifiers` | Full-width `eval` is shown escaped; non-ASCII in strings and comments is shown as text; control characters escaped everywhere. |
| `test_reserved_custom_provenance_names` | `custom_sources`/`custom_source` refused as metadata in any schema and in records. |
| `test_scanner_crash_is_reject`, `test_yaml_alias_bound`, `test_dashboard_routes_refuse_custom` | §4.2, Q13. |
| `test_project_config_resume_cli` | Gate evaluation and the `config enable` output: turning project config off then on resumes approved providers without re-approval and lists them. |
| `test_scripts_unavailable_off_linux` | On a non-Linux platform value, `approve`/`enable` refuse and state is `unavailable`. |

**E2a** (all `xfail(strict=True)`, `sandbox` marker unless noted):

| Test | Asserts |
|------|---------|
| `test_network_dns_proxy_hosts_absent` | No sockets, no `resolv.conf`, no `/etc/hosts`, no proxy variables with proxies set on the host. |
| `test_podman_env_is_constructed` | `CONTAINER_HOST`, `CONTAINERS_CONF`, `PODMAN_USERNS`, proxies in the caller's environment have no effect. |
| `test_inspect_assertion_fields` | One case per row of the §4.3 table: a deliberately divergent container is refused naming that field. |
| `test_inspect_env_matches_pinned_image` | Expected set per pinned image ID; an extra variable from `containers.conf` `env` is refused. |
| `test_image_pin_ids_and_repodigests` | Wrong image ID, list digest without per-arch digest, retagged image: refused. |
| `test_mounts_conf_seen_inside_container` | With a temporary podman `HOME` whose `.config/containers/mounts.conf` maps a fixture directory to `/run/secrets`, and the image loaded into that home's storage: `inspect` shows no mount, the report shows it, the run is refused before the script is sent. |
| `test_mount_allowlist_clean_host` | The allowlist matches a clean host exactly (regenerates the reviewed table). |
| `test_userns_excludes_user_uid` | `uid_map`/`gid_map` inside and `IDMappings` outside never cover the user's UID. |
| `test_cpython_starts_under_seccomp[x86_64]`, `[aarch64]` | Bootstrap, shim and a trivial `derive` complete under the shipped profile. |
| `test_denied_syscalls` | `unshare`, `clone` with `CLONE_NEW*`, `setns`, `io_uring_setup`, `bpf`, `ptrace`, `socket`, mount family: each fails. |
| `test_hash_seed_not_fixed` | Inside, `sys.flags.hash_randomization == 1`; `--dry-run --twice` on a script emitting `set` order reports a difference; the same script with `sorted()` does not. |
| `test_readonly_no_writable_paths`, `test_log_driver_none_retains_nothing` | §4.3. |
| `test_fork_bomb`, `test_memory`, `test_cpu_spin`, `test_c_level_hang_hits_per_file_deadline`, `test_caught_timer_hits_per_file_deadline` | §6; the hang and the caught timer fail only their own file and later files still run. |
| `test_retry_wall_clock_bounded` | Two deaths within one run never exceed 300 s + 5 s; under 10 s left means `aborted` without a retry. |
| `test_container_never_reads_stdin` | A bootstrap substitute that never reads: stdin write deadline kills it; host returns within the deadline. |
| `test_host_death_kills_container` | `SIGKILL` the host process mid-run: conmon removes the running container by `--timeout`; the next process start-up sweep removes the leftover. |
| `test_machine_wide_lock` | Two processes: the second waits, then `busy` after 60 s (shortened via argument); the lock is held across the retry container. |
| `test_stderr_never_reaches_logs` | A script writing a marker to stderr: the marker appears in no log file, run record, notice or dashboard response; only interactive `--dry-run` shows it. |
| `test_escape_table` | Each §4.1 escape. |
| `test_flooding` | stdout and stderr floods. |
| `test_hostile_frames` (unit, not `sandbox`) | Oversize lengths, wrong `seq`, extra frames, early output, NaN, huge integers, lone surrogates, deep nesting. |

**E3.** Provenance isolation in both directions on live Neo4j; per-file
lifecycle; cross-repository endpoint isolation; `Repository` endpoint
rejection; `test_no_run_or_prune_while_pending`; `test_no_prune_while_awaiting_approval`;
`test_apply_triggers_single_run_and_rechecks_hash`; backward-compatibility
snapshot.

**E4.** Dashboard route and JS harness tests; `test_project_config_resume_dashboard`
(the dry run lists resuming providers and enabling resumes them without
re-approval); `test_sandbox_surfaces_use_textcontent`; the epic's acceptance
line end to end.
