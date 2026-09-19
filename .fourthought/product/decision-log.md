# Fedora workflow trial

The owner requested that useful local history be tidied and pushed to a branch,
then that development move to the Fedora laptop to try hook-enabled Delivery Council.

The retained domain/workflow documentation is commit
`757229acd9b4741f170fb34f675ebe26d9cc7f96`, based on upstream
`3988d80`. The local origin is the contributor fork; upstream is the original
project. Verify remote heads before planning work.

Use `CONTEXT.md` for settled terminology. Investigate upstream issues and their
existing branches before selecting a small delivery slice. Issues 1, 3 and 4
have upstream branches; issue 2 needs reproduction against the current graph
settling code; issue 6 lacks registration persistence; issue 5 has console-only
logging. These are intake observations to verify, not completed acceptance.

No individual feature or issue has yet been selected by the owner. Do not infer
approval for the full schema epic or all open issues. Start by recommending a
small issue and concrete acceptance criteria.

This branch attaches the Council trial separately from the upstream-facing docs.

GitHub coordination is enabled. Protected coordinator state, role actors,
verification commands and the required independent review policy are configured;
engineering dispatch is permitted. The coordinator signing key and protection
token live in the `fourthought-coordinator` environment and have not yet been
exercised by a real dispatch — treat the first run as a live test of sealing and
signature verification, not as settled evidence.

Issue boundary, split by purpose:

- `monkeygerbil222/DevGraph` (origin, the contributor fork) is canonical for
  Fourthought coordination. Every work contract, claim, receipt and sealed
  record the coordinator writes belongs here. `.fourthought/config.json`
  `github.repository` must always name this repository.
- `HaydenSchmidtDOC/DevGraph` (upstream) is the human-facing tracker. Read
  upstream issues for intake evidence and reference them by URL, but never
  write Fourthought state there and never create a coordination issue there.

When a Fourthought record cites upstream work, link the upstream issue in the
body and keep the canonical record in the fork. Do not merge automatically or
change upstream settings.
