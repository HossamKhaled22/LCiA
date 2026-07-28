# LCiA - Semantic Material Matching for BIM-Based LCA

LCiA is a [pyRevit](https://github.com/pyrevitlabs/pyRevit) extension for Autodesk Revit that matches BIM materials to environmental datasets of the German [ÖKOBAUDAT](https://www.oekobaudat.de) database. It replaces manual keyword search with a ranked, confidence-scored shortlist: materials are retrieved by meaning as well as by wording, queries are enriched with context read from the model, and every suggestion carries a calibrated confidence value that can be unfolded into its parts. All models run locally, so project data never leaves the machine.

The extension was developed and validated in the MSc thesis *AI-Powered Semantic Engine for Contextual BIM-Based LCA* (RWTH Aachen University, 2026). The thesis describes the six retrieval modes, the confidence calibration, and the benchmark reported below.

## Repository layout

```
LCiA.tab/
├── Database.panel/Ökobaudat Connector.pushbutton/   # dataset search, filters, six retrieval modes,
│   ├── search_helpers.py                            #   confidence scoring, write-back to Revit materials
│   ├── window.py, constants.py, models.py, ...
│   ├── sample_project_a2_sphera_en_v0.6__opus-4.8.json   # frozen ground truth v0.6, English corpus
│   ├── golden_nugget_a2_sphera_de_v0.6__opus-4.8.json    # frozen ground truth v0.6, German corpus
│   ├── tools/                                       # benchmark, tuning, calibration and analysis scripts
│   │   └── gt_build/                                # ground-truth build chain (incl. annotation prompt)
│   └── LCiA_Extension_Cache/                        # frozen 28 June 2026 corpus snapshot (A2 Sphera EN/DE)
│                                                    #   + fitted calibration + tuned fusion parameters
├── Context.panel/ExtractContext.pushbutton/         # material context extraction (35 properties)
│   └── data/                                        # frozen context exports of the two sample models
└── Validation.panel/Validation.pushbutton/          # benchmark front end + the two frozen
                                                     #   validation reports of 5 July 2026 (EN/DE)
```

## Requirements

- Autodesk Revit 2025.4 with pyRevit installed
- [Ollama](https://github.com/ollama/ollama) serving the `bge-m3` embedding model locally
- CPython 3.10 with `lancedb`, `pyarrow`, `sentence-transformers`, and `torch` for the two sidecar services (embedding prefetcher and cross-encoder reranker, model `BAAI/bge-reranker-v2-m3`)

A discrete GPU is recommended; the dense retrieval modes run on CPU but considerably more slowly.

## Installation

1. Clone this repository into your pyRevit extensions folder so that the folder name ends in `.extension` (for example `LCiA.extension/`), or copy `LCiA.tab/` into an existing extension folder.
2. Register the extension folder in pyRevit (Settings → Custom Extension Directories) and reload.
3. Start Ollama and pull `bge-m3`. The reranker sidecar is spawned automatically on first use; if the auto-detected Python interpreter lacks the required packages, set `EMBEDDING_PYTHON_EXE` in `constants.py` to a suitable interpreter.
4. Open the **LCiA** ribbon tab. The repository ships the frozen A2 Sphera cache; because caches older than seven days refresh automatically, the first launch updates it to the live ÖKOBAUDAT and rebuilds the affected embeddings. Any other data stock downloads and embeds on first use.

## Usage

- **Database → Ökobaudat Connector**: search the active data stock in one of six retrieval modes (Regex, Fuzzy, Semantic, Hybrid, Hybrid + Rerank, Semantic + Rerank), filter by classification, indicator values, and module coverage, inspect full ILCD indicator tables, and write a confirmed match back to the Revit material (UUID + indicator data).
- **Context → Extract Context**: read up to 35 material properties from the model (identity, physical, thermal, element context) into the `LCA_Context` parameter and a JSON export; the BIM Context feature folds selected fields into the search query.
- **Validation → Validation**: run the thesis benchmark for any mode, query arm, and language against a frozen ground-truth file, with Recall@K, MRR, and nDCG@10 per run, and export a timestamped HTML report.

## Reproducing the thesis benchmark

The two ground-truth files (`*_v0.6__opus-4.8.json`) were written by the large language model claude-opus-4-8 under the fixed prompts included in `tools/gt_build/` (see the note there on path normalisation) and frozen on 1 June 2026, as described in the thesis (Section 3.6). The Validation panel scores any retrieval configuration against these files; the two committed HTML reports of 5 July 2026 are the benchmark runs behind the result tables of the thesis (54 scoped queries per corpus, scored on the 28 June 2026 cache).

The corpus those runs were scored on is committed too: `LCiA_Extension_Cache/` holds the frozen 28 June 2026 snapshot of the A2 Sphera data stock in both languages (dataset lists with 2,386 EN and 2,344 DE entries, matching Table 2 of the thesis, plus indicator tables and embeddings), together with the fitted calibration curves and tuned fusion parameters, so both the rankings and the displayed confidences reproduce exactly. One caveat for strict re-runs: the live Connector refreshes a cache older than seven days, so restore these files from git (or run the benchmark before letting the refresh complete) to score against the exact thesis corpus. The other three data stocks are not cached in the repository and download on demand, and derived stores are intentionally not committed: the LanceDB index, the remaining caches and all logs rebuild automatically (see `.gitignore`). At search time the plugin reads the committed `.bin` embedding files directly, so the LanceDB store is not needed to run or to reproduce the benchmark.

## Author

Hossamelden Elmalah - MSc thesis, RWTH Aachen University, 2026.

## License

LCiA carries two source licences, mapped file by file in [LICENSING.md](LICENSING.md).

| Part | Licence |
| --- | --- |
| Retrieval engine, ÖKOBAUDAT and ILCD handling, confidence calibration, benchmark and analysis tools (33 files) | [Apache-2.0](LICENSE-APACHE-2.0) |
| pyRevit and Revit integration layer: UI, entry points, write-back to Revit materials (10 files) | [GPL-3.0-or-later](LICENSE) |

The integration layer runs inside [pyRevit](https://github.com/pyrevitlabs/pyRevit), which is GPL-3.0, so those ten files take its terms. Nothing else in the repository depends on pyRevit, and the permissive licence on the rest leaves the retrieval engine and the benchmark free to be reused and built on without a copyleft obligation, in closed-source work as well as open. Each source file states its own licence in an SPDX header.

Copyright is held by Hossamelden Elmalah. Both grants are non-exclusive and leave the author's own rights untouched. If you need the integration layer on terms other than GPL-3.0, please open an issue.

Data is licensed separately from code. The committed ÖKOBAUDAT snapshot is third-party data and is not relicensed here, while the ground truth, the calibration curves and the validation reports are released under CC BY 4.0. [DATA-LICENSE.md](DATA-LICENSE.md) covers all of it. Run-time dependencies, none of which are bundled in this repository, are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Button icons by [Icons8](https://icons8.com).

## Citation

If LCiA, its retrieval engine or the published ground truth feed into your work, please cite the thesis. [CITATION.cff](CITATION.cff) has the details.
