## What and why

<!-- What changes, and what went wrong (or would go wrong) without it. -->

## Checks

- [ ] `go test ./...` and `cd client && python3 -m pytest -q` pass
- [ ] A runnable check covers the change - the smallest thing that fails if the logic breaks
- [ ] No code, key tables, or report definitions copied from any board's firmware (see CONTRIBUTING.md)

## If this adds or changes a board profile

- [ ] The report layout was established from descriptors or hardware, not inferred from a similar board
- [ ] A test asserts the exact bytes for a known coordinate
- [ ] `client/agentkvm/install.py` detection updated

## If this touches authentication, the look-before-act rule, or the watchdog

<!-- These fail closed on purpose. Say what changes and why it is still safe. -->
