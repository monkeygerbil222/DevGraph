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

The coordinator additionally requires `FOURTHOUGHT_PROTECTION_TOKEN` in the restricted environment. It needs Administration read and Metadata read for this fork so the workflow can verify protected history. The account's broad CLI OAuth credential is deliberately not copied into a hosted workflow. Local attachment checks do not prove that this GitHub credential exists or that a live issue has completed.

Publish only verified outcomes: submission, launch, completed stages and product acceptance are distinct milestones.

## Validation recorded for this setup

- `uv run --locked pytest -q`: 535 passed; 13 dependency/deprecation warnings.
- Corrected CLI coverage: 36 passed, including Linux, macOS and Windows paths.
- Dashboard harness independently challenged with three behavior mutations; each failed as intended.
- Framework source revision `e2b07d3`: 233-test suite passed; final snapshot retry suite 7 passed; independent runtime review found no remaining important findings.
- `scripts/fourthought doctor .` passed attachment validation.
- Existing protection credential returned HTTP 403 for this fork; no claim of successful live delivery is made.
