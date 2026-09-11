# Batch tag calculator

Score labeled seeds with the supplied Wraith / Negative + Rare model and keep the best routes. This is a separate Python tool; it does not change the Seed Pool Program or your source pools.

## Windows quick start

Keep this `tools` folder together. Python 3.9 or newer is required; the packaged Seed Pool Program is not a Python interpreter.

1. Double-click `tagcalc_windows.bat`, or drop one CSV, NDJSON, or `.bspool` file onto it.
2. Enter a shared second-tag position only if the input does not already identify it for each seed. Leave it blank to use saved values.
3. For a pool file, provide its matching `native_search.cfg`. Leave this blank only if the pool already records both tag types through Ante 38.

Results go into a new folder beside the input, named `<input>-tagcalc-<unique suffix>`. Start with `tagcalc_example.csv` to check the workflow. Its seeds and tag lists are illustrative, not verified game results.

## Input and baseline

CSV uses one row per seed: `seed,pool,second_tag,tags`. Keep the original pool label in `pool`. Quote comma-separated tag lists, such as `"r7b,n10b,r12s"`; `r` means Rare, `n` means Negative, and `s` / `b` mean Small / Big.

The tag list must include every Negative and Rare placement through real Ante 38, including an empty list when neither occurs. A manually prepared CSV asserts that its tag list is complete. Missing placements must not be interpreted as absent tags. Ante 39 is ignored. A seed string alone cannot provide these placements without the generator and a matching profile snapshot.

Each seed needs its own starting second-tag position or first-copy baseline:

| Second tag | First Wraith-copy shop exit |
| --- | --- |
| Ante 4 Small (`a4s`) | Ante 4 Big (`a4b`) |
| Ante 4 Big (`a4b`) | Ante 4 Boss (`a4boss`) |

For a known first-copy position, use a `baseline_copy` column. Shared `--second-tag` or `--baseline-copy` values are fallbacks; row-specific values take priority. If both positions are supplied for a row, they must agree. Do not apply one shared baseline to mixed pools that start at different positions.

Use the complete `tags.ndjson` export from this tool for later runs. An older Organizer NDJSON export can omit placements and is not sufficient. For `.bspool` input, `--snapshot` records the full range into a private temporary copy using the native helper; your source pool stays unchanged. This path needs the Brainstorm tools, matching native helper, and snapshot.

## Command-line control

Run these commands from the Brainstorm folder; use a new output directory each time:

```bat
py -3 tools\tagcalc.py --input tools\tagcalc_example.csv --output-dir tagcalc-demo --top 1000
py -3 tools\tagcalc.py --input "exports\AS1-L1.ndjson" --second-tag a4b --output-dir results-AS1-L1
py -3 tools\tagcalc.py --input "seed_pools\AS1-L1.bspool" --snapshot "native_search.cfg" --second-tag a4b --output-dir results-pool
py -3 tools\tagcalc.py --input "seed_pools\AS1-L1.bspool" --snapshot "native_search.cfg" --export-only --output-dir exported-tags
```

`--export-only` saves complete placements and metadata without scoring or requiring a baseline. Use `--workers N` to score with multiple worker processes; the default is one. `--top 1000` controls how many seeds appear in the leaderboard. The single-input `--tags` mode is also available; use `--help` for its options.

## Results

- `leaderboard.csv` and `leaderboard.ndjson`: the highest-scoring seeds and their labels.
- `scores.ndjson`: every processed row, with its status, score, route, and original data.
- `tags.ndjson`: complete A1–38 tag placements and original metadata retained for reuse.
- `errors.ndjson`: invalid rows and their reasons.
- `summary.json`: batch totals and calculation settings.

Check `summary.json`: `complete` means the run finished; `completed_with_errors` means some rows were rejected. A failed or interrupted run may leave partial files. Use a new output folder when retrying.

Open `leaderboard.csv` for manual testing: its first columns show the seed, score, first copy, when to take Hieroglyph and Petroglyph, and a readable redeem plan. Detailed per-redeem copy counts and multipliers are in the NDJSON. The leaderboard contains distinct seed strings; if a seed has multiple input records, its best-scoring record wins (earliest row breaks ties). All records and their separate labels remain in `scores.ndjson`.

## Search time and scoring

The search retains different Blueprint/Brainstorm compositions when either could win later. This fixes a route-pruning error in the supplied script while keeping its probabilities and timing model. It also searches equivalent voucher placements once. Voucher timings are model insertion points; gameplay and voucher availability still need manual testing.

Run this after narrowing the pools. Local single-process tests took about 0.0003 seconds for 4 future tags, 0.004 for 8, 0.026 for 12, and 0.355 for a dense 20-tag case per distinct pattern. Ten thousand similarly dense, distinct patterns could still take about an hour on one process. These are illustrative benchmarks, not a Windows estimate. `--workers` can spread scoring across CPU cores, and repeated tag patterns reuse cached results. Progress prints while processing; Ctrl+C stops workers and marks the run interrupted. There is no automatic resume; the complete tag export can be reused for subsequent runs.

## Comparing the tester's reference

Add `--reference-score 721.77 --reference-seed 5MSXV6` to mark results above the tester's prior best. That reference came from manual online filtering; it has not been rescored here. Comparisons are meaningful only with the same baseline and model assumptions.

The tester also reported these theoretical ceilings, grouped by **first-copy position**:

| First copy | Reported ceiling | First copy | Reported ceiling |
| --- | ---: | --- | ---: |
| A4 Big | 1208.82 | A4 Boss | 1154.63 |
| A5 Big | 1052.26 | A5 Boss | 1005.61 |
| A6 Big | 917.49 | A6 Boss | 879.92 |
| A7 Big | 807.39 | A7 Boss | 771.13 |

These figures are unverified references, not enforced score caps. Scores depend on the supplied probability and timing assumptions. Check the leading routes manually before relying on the ranking.
