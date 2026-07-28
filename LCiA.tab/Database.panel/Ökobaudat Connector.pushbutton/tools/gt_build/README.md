# Ground-truth build chain (v0.6)

This folder holds the frozen inputs and tooling behind ground truth v0.6, the reference set every benchmark run in the thesis is scored against.

## Annotation prompts

`prompt_1_snowdon_en_groundtruth_v0.6.md` and `prompt_2_golden_nugget_de_groundtruth_v0.6.md` are the fixed instruction prompts under which claude-opus-4-8 wrote the two frozen ground-truth files on 1 June 2026 (thesis, Section 3.6). The instruction content - every rule, boundary, example and schema - is reproduced exactly as run; two notes for the reader:

- The six machine-local path lines inside the prompts have been normalised to this repository's layout; at run time they pointed at the same files on the development workstation, under the former names `eLCA.extension` / `eLCA.tab` / `eLCA_Extension_Cache`. No other line was changed. The volatile `materials_context.json` they reference corresponds to the frozen `materials_context - Snowden - En.json` and `materials_context - Nugget - De.json` exports committed under `data/`, and the v0.3 schema reference corresponds to the committed `*_v0.6__opus-4.8.json` files.
- The prompts frame the task as one pass of a multi-annotator design (several model IDs, later human adjudication). Only the claude-opus-4-8 pass was executed and frozen as v0.6; the further annotator passes and the expert resolution of the flagged borderline cases remained open at submission and are listed as future work in the thesis (Sections 5.3 and 5.4).

## Files

- `_query.py` / `_query_de.py` - deterministic taxonomy query CLI over the EN/DE dataset caches (the enumeration tool the prompts prescribe instead of the plugin's own search)
- `_dump_indexes.py` - flattens a cache into a `uuid / name / classification` TSV sorted by classification
- `_opus_decisions.py` / `_gn_decisions.py` - the frozen per-material decisions (EN / DE)
- `_opus_assemble.py` / `_gn_assemble.py` - assemble the v0.6 JSON files from the decisions, validating every invariant against the cache
- `_eval_stats.py` - quick statistics over an assembled ground-truth file
- `manifest.json`, `skips.json`, `materials_index.tsv` - candidate-stage intermediates written by `tools/build_groundtruth_candidates.py` and `_dump_indexes.py`; they precede the annotator's final MAP/SKIP decisions, so their counts differ from the 55 materials mapped in the frozen v0.6 files
