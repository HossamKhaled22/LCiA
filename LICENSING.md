# Licensing

LCiA carries two source licences. Which one applies depends on the file, and
every source file states its own licence in an SPDX header at the top. That
header governs; this page explains the split.

| Part of the repository | Licence |
| --- | --- |
| Retrieval engine, ÖKOBAUDAT and ILCD handling, confidence calibration, benchmark and analysis tools | [Apache-2.0](LICENSE-APACHE-2.0) |
| pyRevit and Revit integration layer: UI, entry points, write-back to Revit materials | [GPL-3.0-or-later](LICENSE) |

Data files fall outside both licences. Their provenance and terms are set out in
[DATA-LICENSE.md](DATA-LICENSE.md). Components needed at run time are listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), and none of them is bundled
here.

## Why the split

The extension runs inside [pyRevit](https://github.com/pyrevitlabs/pyRevit),
which is licensed under GPL-3.0. Ten files import pyrevit or are loaded by it,
and since they execute in the pyRevit process and exchange objects with it, they
form a combined work with pyRevit. Those ten are released under GPL-3.0-or-later.

Nothing else in the repository refers to pyRevit. The retrieval engine, the
ÖKOBAUDAT and ILCD handling, the confidence calibration and the benchmark code
all run without it, so there is no reason to burden them with copyleft. They
are released under Apache-2.0, which keeps the methods reported in the thesis
open to being read, checked, cited and reused, in closed-source work as well as
open.

## Files under GPL-3.0-or-later

```
LCiA.tab/bundle.yaml
LCiA.tab/Context.panel/bundle.yaml
LCiA.tab/Context.panel/ExtractContext.pushbutton/script.py
LCiA.tab/Context.panel/ExtractContext.pushbutton/ExtractContext.xaml
LCiA.tab/Database.panel/Ökobaudat Connector.pushbutton/script.py
LCiA.tab/Database.panel/Ökobaudat Connector.pushbutton/window.py
LCiA.tab/Database.panel/Ökobaudat Connector.pushbutton/revit_mapper.py
LCiA.tab/Database.panel/Ökobaudat Connector.pushbutton/OkobaudatConnector.xaml
LCiA.tab/Validation.panel/Validation.pushbutton/script.py
LCiA.tab/Validation.panel/Validation.pushbutton/Validation.xaml
```

The three `.xaml` files hold the WPF markup that pyRevit loads for those windows,
and the two `bundle.yaml` files describe the ribbon layout to pyRevit, so both
belong with the layer they serve.

## Files under Apache-2.0

Every other source file, 33 in all. The substantial ones are `search_helpers.py`,
which holds the regex and fuzzy matching, the field-weighted BM25F scorer, the
reciprocal rank fusion, the embedding client and the confidence calibration;
`api_client.py`, `ilcd_parser.py` and `models.py` on the ÖKOBAUDAT side;
`query_context.py` and `rerank_service.py` for query enrichment and reranking;
and everything under `tools/`, including the ground-truth build chain in
`tools/gt_build/`.

## Autodesk Revit

Revit and the Revit API belong to Autodesk. Neither is in this repository, and
neither licence here grants any right in Autodesk software. Running LCiA requires
a Revit licence obtained from Autodesk.

## .NET types inside Apache-2.0 files

Two of the permissively licensed files reach into the .NET base class library.
`api_client.py` imports `System.Net` through IronPython, which is how HTTP works
inside Revit, and `search_helpers.py` selects a .NET backend at import time but
falls back to `urllib` when it runs under CPython 3 in the offline tools. The
base class library is Microsoft's and carries no copyleft obligation, so neither
import affects the licence of those files.

## Ownership

Copyright in LCiA is held by Hossamelden Elmalah. Releasing the code under these
licences transfers no ownership and places no limit on what the author may do
with the work. Both grants are non-exclusive, and the author stays free to
license LCiA under different terms.

The Apache-2.0 files may be used commercially under Apache-2.0 with no further
permission. Anyone who needs the pyRevit layer on terms other than GPL-3.0 should
open an issue.

## Contributions

A contribution is accepted under the licence of the file it changes. Please open
an issue before starting anything substantial, because a larger contribution may
need a contributor licence agreement to keep the licensing coherent.

## What this means in practice

Reusing the retrieval engine or the benchmark in another project falls under
Apache-2.0. Keep the copyright and licence notices, say what you changed, and
that is the whole obligation; your own project can stay closed. Distributing a
modified extension is a different matter, because the combined work includes the
GPL-3.0 files, so the modified source has to be published under
GPL-3.0-or-later. Using LCiA internally triggers no obligation at all under
either licence, however heavily it is modified. Datasets and ground truth follow
their own terms, which are in [DATA-LICENSE.md](DATA-LICENSE.md).
