Brainstorm for Windows x64, with original-pool recovery and saved tag rules in
the Seed Pool Program.

- **Separate original pools:** recover retained L1, L2, L3, L4, or Other
  memberships from a combined Complete pool, even after deleting its original
  input files. Shared seeds are copied into each selected group.
- **Sort by second tag:** choose each pool's inclusive Ante/Small/Big range.
  Find the earliest opposite tag after the first Negative or Rare; when both
  types appear in the first Ante, require another tag in a later Ante. Group
  qualifying seeds by the second tag's type and location.
- **Saved rules:** combine nested AND, OR, and NOT count conditions with
  independent ranges. Save or load recipes, preview destinations and exclusion
  counts, then create the new pools with progress and cancellation.
- **Simpler UI:** clearer descriptions, grouped inputs, optional advanced
  controls, and improved narrow-window layouts across Build and Organize.
- **Correctness fixes:** preserve tag windows through Ante 39 instead of
  silently truncating them; reject invalid counts and excess rules. Derived
  location and rule pools retain source history but cannot incorrectly act as
  exhaustive substitutes for a broader live search.

Recovery uses only memberships still recorded in the Complete pool; removed
seeds and unretained intermediate groups cannot be reconstructed. Tag rules
require recorded placements throughout the ranges they check. New outputs keep
their source unchanged and never overwrite existing files. Rule processing
streams through Python, so large pools can take time to analyze and write.

Download **brainstorm-windows-full.zip**, extract it completely, and run
**Install or Update Brainstorm.bat**. The updater preserves seed pools,
settings, snapshots, and scan checkpoints. Open **Seed Pool Builder.bat** and
choose **Organize / Combine** to use the new tools.

Built from the tagged commit after the Windows and macOS test suites passed,
with the packaged Windows executables checked before publication.
