# Data licensing and provenance

The licences in [LICENSING.md](LICENSING.md) cover source code only. They do not
reach the data committed here. This page records where each body of data came
from and on what terms it can be used. Where the data belongs to someone else,
the repository grants no rights in it and claims none.

## ÖKOBAUDAT corpus snapshot

Files: `ds_cache_v2_*.json`, `indicators_*.json` and `embeddings_*.bin` in
`LCiA.tab/Database.panel/Ökobaudat Connector.pushbutton/LCiA_Extension_Cache/`.

These come from ÖKOBAUDAT, the environmental database for the German
construction sector, maintained by the Federal Institute for Research on
Building, Urban Affairs and Spatial Development (BBSR) for the Federal Ministry
for Housing, Urban Development and Building (BMWSB). They were retrieved through
the public ÖKOBAUDAT REST API on 28 June 2026, from the EN 15804+A2 (Sphera MLC)
data stock in German and English, in the ILCD exchange format.

The snapshot is committed for one reason. Datasets are revised and withdrawn over
time, so a live query would no longer reproduce the figures published in the
thesis, and freezing the corpus is what makes the benchmark repeatable.

The datasets remain the property of their declaration owners and of the programme
operator, and they stay subject to the ÖKOBAUDAT terms of use published at
<https://www.oekobaudat.de>. No licence to them is granted here, and neither
Apache-2.0 nor GPL-3.0 extends to these files. Anyone reusing the data for
anything beyond reproducing this benchmark should read those terms first, and
should take current data from ÖKOBAUDAT rather than from this snapshot.

The `.bin` files hold BGE-M3 vectors computed from ÖKOBAUDAT dataset names and
classification paths. Being derived from the source data, they fall under the
same terms.

## Ground truth, calibration and benchmark reports

Files: the two `*_v0.6__opus-4.8.json` ground-truth files; everything in
`tools/gt_build/`; the `calibration_*.json`, `confidence_*.json` and
`hybrid_params_*.json` files in `LCiA_Extension_Cache/`; and the two frozen
`validation_report_*.html` files in the Validation panel.

The two ground-truth files were produced on 1 June 2026 by the language model
claude-opus-4-8, working from fixed annotation prompts written by the author. The
prompts are included in `tools/gt_build/`, and the author supplied no labels.
Under the model provider's terms in force at the time, the output belongs to the
author. Prompts, assembly scripts, fitted calibration curves, tuned fusion
parameters and the frozen validation reports are the author's own work.

All of this is released under
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
Share it and adapt it, commercially or otherwise, with credit;
[CITATION.cff](CITATION.cff) has the citation.

ÖKOBAUDAT datasets appear in these files as UUIDs. An identifier is a factual
reference, but the datasets it points to remain subject to the section above.

## Material context exports

Files: `materials_context - Snowden - En.json` and
`materials_context - Nugget - De.json` in
`LCiA.tab/Context.panel/ExtractContext.pushbutton/data/`.

Both are property extracts read out of sample projects shipped with Autodesk
Revit 2025.4, Snowdon Towers Sample Architectural and
BIM_Projekt_Golden_Nugget-Architektur_und_Ingenieurbau, used exactly as
installed. They contain material names and material property values, nothing
else. No Autodesk geometry, model file or other content is redistributed here,
and Autodesk keeps all rights in the original sample projects. The extracts are
published so that the benchmark can be repeated, and they will be removed if
Autodesk objects.

The extraction itself is the author's work and is released under CC BY 4.0, so
far as the extracted values are protectable at all.

## Icons

Files: `icon.png` in each `.pushbutton` folder.

The button icons are the work of [Icons8](https://icons8.com) and are used
unmodified under the Icons8 free licence, which is granted in exchange for a link
back to icons8.com. That link appears in the README and again here. Icons8
retains all rights in the icons. The Apache-2.0 and GPL-3.0 licences of this
repository do not apply to them, they may not be extracted and reused as icons in
their own right, and the free licence does not permit derivative copies.

## Takedown

If you hold rights in anything committed here and object to its publication,
please open an issue at <https://github.com/HossamKhaled22/LCiA/issues>. It will
be taken down.
