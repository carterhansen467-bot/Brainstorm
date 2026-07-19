# Automatically Attached Seed Pools

Research and implementation plan for letting Brainstorm automatically use a
completed `.bspool` when the active in-game filters match the pool's embedded
criteria. This is intentionally stricter than the manual Seed Pool selector:
automatic selection must never make a search faster by silently excluding
otherwise-valid seeds.

## Existing pieces we can reuse

- Every `.bspool` already embeds its model version, natural/expanded seed
  space, half-open searched rank range, completion/coverage state, profile
  catalog hash, criteria hash, pool ID, and cumulative route criteria.
- Brainstorm already reads those headers in Lua and the native helper already
  streams pool ranks through the ordinary active filters.
- `pool_open` validates model/catalog compatibility and replays the pool's
  embedded tag, Legendary, voucher, Charm, and Omen route before accepting a
  record. Automatic attachment therefore only needs to choose a safe candidate;
  it must not invent a second membership implementation.

The external comparison found the same long-term direction in izanagi1995's
Balatro seed finder: its stated next step is an index of all seeds tagged by
properties such as “Perkeo on first Charm Tag.” It does not currently provide
runtime filter-to-index matching, compatibility invalidation, or in-game
integration, so Brainstorm's existing self-describing pool format remains the
better foundation here.

## Physical eligibility and the two pool roles

Every automatically selected pool must satisfy these base gates before its
filter is compared with the active search:

1. `complete 1` and `coverage_complete 1`.
2. Natural seed space and a valid declared half-open rank range.
3. Current route model, non-empty membership, and valid pool/catalog/criteria
   identities.
4. Initially, no composite union/difference expression. Those need a Boolean
   implication matcher rather than a single conjunction matcher.
5. A current-profile catalog match. The native helper remains authoritative.

After those gates, classify the attachment by coverage:

- An **accelerator** may cover only its declared range. It is searched first,
  but exhaustion or automatic failure always continues with unrestricted seed
  generation. It can improve time-to-first-result but cannot prove absence.
- An **authoritative** pool additionally requires `range_start 0` and
  `range_end 1785793904896`, covering the entire natural `34^8` space. It may
  replace unrestricted generation and its exhaustion is definitive.

The Builder now exposes separate `attachment_accelerator_*` and
`attachment_authoritative_*` eligibility results. For example, the local
`perkeo-a1-small-small-charm.bspool` is a completed 100M test with 19,777
records: it is accepted as an accelerator and rejected as authoritative.

## Attachment contract

Use an explicit sidecar named `<pool>.attached`, created only by an **Attach to
Brainstorm** action after the pool passes every gate. Keeping policy outside the
binary pool preserves `.bspool` compatibility and makes attachment reversible
without rewriting a multi-gigabyte pool.

The sidecar should be line based so both Python and Lua can parse it without a
JSON dependency. It should bind to:

- attachment schema, enabled state, and accelerator/authoritative role;
- exact pool filename, pool ID, catalog hash, criteria hash, and snapshot ID;
- a versioned canonical in-game filter signature;
- the pool file size/mtime or stronger immutable snapshot identity.

Replacing, updating, refiltering, or deleting the pool then invalidates the
marker. Completed-pool deletion already includes `.attached` among the related
files it removes.

## Exact filter matching

Build a canonical active request from the in-game settings and compare it with
a canonical request derived by the Builder from the pool criteria. Do not use
`criteria_hash` as the runtime filter signature: a refilter's criteria hash
also binds its source snapshot/range, so semantically equivalent cumulative
routes can intentionally have different hashes. Fields must include:

- collected/observed tag key, blind/Ante window, and minimum count;
- Legendary key, Negative requirement, Soul depth, route window, source, and
  full versus Shop+Charm route coverage;
- voucher target windows, exclusions, and purchase-route semantics;
- every active filter category not represented by the pool.

For the motivating case, the safe active request is specifically the classic
Ante-1 Small Charm Tag plus the classic Perkeo/Negative setting. The tag filter
proves that the hypothetical classic Charm reward actually exists. A pool rule
`legendary j_perkeo 1 small 1 small <negative> charm` contains those candidates;
the default full-route pool may additionally contain Omen-recovered candidates,
but the ordinary active filters recheck and reject those extras. Filename text
and display labels are never used for matching.

The actual safety relation is **active predicate implies pool predicate**: every
valid active result must be present in the chosen pool, while the pool may be a
broader prefilter because active filters run again. Phase one should support
exact equivalence plus only a small explicit implication lattice with direct
proofs—for example, full Legendary routes are broader than Shop+Charm-only
routes, `any` source is broader than one specific source, and a wider location
window is broader than a contained window. Extra active joker/pack constraints
are also safe after the pool predicate is implied. It must never use a narrower
pool merely because several fields look similar.

## Runtime selection and failure behavior

1. A manually selected pool always wins.
2. With manual selection set to None, scan only `.attached` markers, validate
   their bound pool headers, and find proven compatible signature matches.
3. Choose the compatible pool with the fewest records; authority is a
   tie-break because it can terminate a miss. If an accelerator exhausts or a
   pool-specific validation fails, retire only that marker and try the next
   compatible attachment before falling back to unrestricted generation.
4. Serialize that path through the existing `poolfile` directive. Keep running
   the current active filters over every decoded member as a correctness check.
5. Show “Automatically using: <pool>” in the in-game UI and search telemetry.
   Attached candidates are enriched rather than random full-space samples, so
   geometric “chance by now” is deliberately unavailable during pool phases
   and rebased when unrestricted generation begins.
6. If an accelerator is exhausted, or any automatically chosen pool is missing,
   stale, or catalog-incompatible, warn and continue with unrestricted native
   search. An authoritative pool may finish on exhaustion. Preserve today's
   hard stop for a manually selected pool, where falling back would violate the
   user's stated restriction.

## Required proof matrix

- Exact motivating Perkeo/Charm match chooses a compatible attachment and
  returns byte-identical results to unrestricted search over a controlled range.
- Every single signature-field mismatch refuses the pool.
- A completed partial pool is accepted only as an accelerator. Provisional,
  empty, expanded-space, old-model, composite, stale-ID, and catalog-mismatched
  pools refuse automatic selection.
- Manual selection overrides automatic selection.
- Multiple matching pools deterministically choose the smallest.
- Exhausting the smallest accelerator advances to the next compatible marker
  before unrestricted generation.
- Accelerator exhaustion and automatic incompatibility fall back safely;
  authoritative exhaustion does not; manual incompatibility aborts.
- Windows and macOS package/update tests preserve user pool and attachment
  sidecars.

## Current implementation status

Implemented on the seed-pool integrity branch:

- separate accelerator and authoritative physical eligibility with current
  catalog comparison in the Builder;
- a versioned canonical Python criteria signature covering cumulative tag,
  Legendary, route-policy, Soul-depth, voucher, and exclusion predicates;
- atomic, identity-bound `.attached` creation/removal and stale-marker display;
- Attach/Detach Builder controls, completed-pool cleanup, and Windows update
  preservation;
- bounded Lua marker/header validation, canonical signature reconstruction,
  manual-selector precedence, deterministic automatic selection, and a small
  conservative tag/Legendary implication lattice;
- session-local effective-pool routing, main-thread criteria replay, accelerator
  exhaustion/failure chaining, authoritative exhaustion revalidation, warning
  telemetry, and live attached-pool status;
- one shared native/Python writer-lock protocol covering scan, refilter,
  convert, merge, Organizer publication, attachment creation/removal, and
  deletion; the stable lock inode is retained intentionally after a pool is
  removed;
- Python, local HTTP, production-Lua, fallback-state, header, packaging, and
  existing native pool equivalence regressions.

Still intentionally deferred until dedicated differential proof exists:

- voucher attachment matching;
- composite Boolean membership;
- broader Legendary source implications such as `any` versus `charm`;
- automatic matching for the in-game Legendary-anywhere route;
- avoiding the negligible chance of revisiting an accelerator member after the
  unrestricted phase begins.

## Implementation sequence

1. Expand the differential matrix for every currently accepted implication and
   deterministic multi-pool choice.
2. Exercise stale-profile/open failures and accelerator fallback through the
   real native status protocol on both platforms.
3. Add each deferred predicate family one at a time only after proving its
   implication direction against unrestricted search.
