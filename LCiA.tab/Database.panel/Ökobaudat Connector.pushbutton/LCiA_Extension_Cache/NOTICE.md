# NOTICE, third-party data

The dataset and indicator files in this folder are a snapshot of ÖKOBAUDAT, taken
through the public REST API on 28 June 2026 from the EN 15804+A2 (Sphera MLC)
data stock in German and English:

- `ds_cache_v2_a2_sphera_{en,de}.json`, the dataset lists
- `indicators_a2_sphera_{en,de}.json`, the ILCD indicator tables
- `embeddings_a2_sphera_{en,de}.bin`, BGE-M3 vectors computed from those two

ÖKOBAUDAT is maintained by the Federal Institute for Research on Building, Urban
Affairs and Spatial Development (BBSR) for the Federal Ministry for Housing,
Urban Development and Building (BMWSB). The datasets belong to their declaration
owners and to the programme operator, and stay subject to the ÖKOBAUDAT terms of
use at <https://www.oekobaudat.de>.

The Apache-2.0 and GPL-3.0 licences of this repository do not apply to these
files, and no licence to the ÖKOBAUDAT data is granted here. The snapshot exists
so that the benchmark published in the thesis can be scored against the exact
corpus it was measured on. For current data, use ÖKOBAUDAT directly.

The calibration and parameter files kept alongside them (`calibration_*.json`,
`confidence_*.json`, `hybrid_params_*.json`, `query_context_v1.json`) are the
author's own work, released under CC BY 4.0.

[DATA-LICENSE.md](../../../../DATA-LICENSE.md) in the repository root carries the
full statement.
