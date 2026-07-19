# Future Changes

This is the handoff list for work intentionally deferred from the current
release. Point future coding sessions to this file before beginning the next
Seed Pool Tools overhaul.

## Seed Pool Builder

- Add a safe way to delete seed pools from inside the Builder. It should show
  the exact selected file and its related state/manifest files, prevent deletion
  of an active input or output, and require clear confirmation.

## Search performance follow-up

- Do not add the separate metadata-free Builder reachability precheck unless a
  later profile changes the tradeoff. Its exact prototype improved tested
  workloads by only about 0.7-3.5% while duplicating voucher-route semantics.

## Organize / Combine (merge and splice)

- Overhaul the entire merge/splice program. The current interface and workflow
  are not reliably usable yet; treat this as a redesign and validation task,
  not a small polish pass.
- Re-test inspection, category splitting, ambiguous-occurrence handling,
  union, intersection, difference, distributed-part merging, provenance, and
  output publication end to end before calling the replacement ready.
