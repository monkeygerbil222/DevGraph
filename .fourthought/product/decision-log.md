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
GitHub coordination is disabled. Product may investigate and shape locally;
engineering dispatch is blocked until protected coordinator state, role actors,
verification commands and required independent review are configured and tested.
Do not merge automatically or change upstream settings.
