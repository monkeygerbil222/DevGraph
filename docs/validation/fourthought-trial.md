# Repository delivery trial

This fork carries the trial coordinator and execution receipts. Upstream GitHub issues remain the product backlog; the trial submission links to upstream issue 6 (dashboard repository registration). No upstream issue is closed by creating a fork trial receipt.

The trusted coordinator runs on the fork's default branch. Its environment is limited to that exact branch. The state branch rejects force pushes and deletion, including administrator bypass under classic branch protection. The private signing key is an environment secret; only its public key is committed.

## Run

Claude Code must be signed into the intended subscription. Fourthought does not require a model API key.

```bash
./scripts/fourthought doctor .
./scripts/fourthought start .
# For an existing conversation:
./scripts/fourthought resume .
```

Use the existing session's mode: background conversations are opened with `claude attach <native_id>` from `fourthought sessions .`.

Verification uses Python 3.13, the committed dependency lock and the complete pytest suite. Integration tests need the documented local Neo4j service; the Fedora trial uses a dedicated `devgraph-neo4j-trial` container and trial volumes, bound to loopback only. The dashboard rotation test harness was updated to exercise current animation-frame timing and visibility behavior; production rotation code is unchanged.

## Commissioning boundary

The coordinator requires `FOURTHOUGHT_PROTECTION_TOKEN` in the restricted environment. It needs Administration read and Metadata read for this fork so the workflow can verify protected history. The account's broad CLI OAuth credential is deliberately not copied into a hosted workflow.

Publish only verified outcomes: submission, launch, completed stages and product acceptance are distinct milestones.

## Validation recorded for this setup

- `uv run --locked pytest -q`: 535 passed; 13 dependency/deprecation warnings.
- Corrected CLI coverage: 36 passed, including Linux, macOS and Windows paths.
- Dashboard harness independently challenged with three behavior mutations; each failed as intended.
- Framework repair revision `3382dbb`: 245-test suite passed; 28 focused session/usage tests passed; independent review found no blocking correctness or security findings.
- `scripts/fourthought doctor .` passed attachment validation.
- Issue 7 completed planning, implementation, full verification, independent review, required Assurance and Product acceptance at `4db70c583b743774cc13e523b04f55c32656cff2`. Verification recorded 481 passed and 1 skipped.

## Token baseline

Issue 7 is the first measured end-to-end delivery baseline. The seven bounded worker calls reported 63,754 output tokens, 191,201 cache-creation input tokens, 1,486,194 cache-read input tokens, 123 turns, 798,520 ms aggregate model duration and $4.249667 reported model cost. Raw input tokens were 142; this small figure excludes cached context and must not be presented as total input consumption.

The baseline excludes interactive Product shaping and the human/agent effort spent commissioning and repairing the workflow. It therefore cannot establish savings against the previous Delivery Council. It does establish that this version is not yet token-efficient for a two-file low-risk fix. Required Assurance currently causes a reviewer pass before and after Assurance; that duplication is the first optimization candidate. Compare future trials by stage and keep framework-repair cost separate from feature-delivery cost.
