Brainstorm for Windows x64, with faster second-tag sorting and pool creation.

- **Faster rule checking:** two local 50,000-seed benchmarks measured 17–32% higher throughput when checking every seed against the rules.
- **Faster pool creation and verification:** the same benchmarks measured 22–36% higher throughput by avoiding unnecessary compression attempts and repeated small-number encoding. Results depend on your computer and the pool's metadata.
- **Identical results:** every benchmark output pool was byte-for-byte identical to the previous version. Full verification, corruption detection and safe publication remain in place; regression tests also check exact compression bytes and codec selection.

- **Score pools:** select the separated pools directly, or use Score these pools after sorting by second tag. Each pool keeps its own first-copy position.
- **Native scoring:** the C helper searches tag redeem routes and Hieroglyph/Petroglyph placements using the tested scoring model. No separate Python installation is needed.
- **Simple leaderboards:** see Seed, Scaling score, Hieroglyph and Petroglyph; show starting positions and source details as needed. Keep the top 1000 distinct seeds or choose another limit.
- **Combined results:** switch between individual pools and a combined leaderboard without recombining the source pools.
- **Saved progress:** cancel safely and resume the same scoring job after reopening the app. Original pool identities and calculation settings are checked before reuse.
- **Recorded evidence:** scoring requires both tag types through original Ante 38. Missing placements can be recorded with the matching native_search.cfg; original seeds and labels are retained.

Scores and voucher placements follow the supplied theoretical model. Manual gameplay testing is still needed. Ante 39 tag placements and unverified theoretical ceilings are not used to eliminate seeds.

Close Balatro and both Seed Pool Program windows before running Install or Update Brainstorm.bat. The updater preserves your pools, settings and profile snapshot.
