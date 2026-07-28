# Third-party notices

LCiA needs the components below at run time. None of them is bundled in this
repository; each is installed, downloaded or licensed separately by the user.
They are listed so that the terms attached to them are on the record. Nothing
here grants any right in them, and their own licences govern.

## Needed to run the extension

| Component | Licence | Source |
| --- | --- | --- |
| pyRevit | GPL-3.0 | https://github.com/pyrevitlabs/pyRevit |
| Autodesk Revit and the Revit API | Proprietary, under the Autodesk licence agreement | https://www.autodesk.com/products/revit |
| .NET base class library, reached through IronPython inside Revit | MIT (.NET) | https://github.com/dotnet/runtime |

pyRevit is why the integration layer of LCiA is itself under GPL-3.0-or-later.
[LICENSING.md](LICENSING.md) sets out the reasoning.

## Needed for the local model services

| Component | Licence | Source |
| --- | --- | --- |
| Ollama | MIT | https://github.com/ollama/ollama |
| BAAI/bge-m3, embedding model weights | MIT | https://huggingface.co/BAAI/bge-m3 |
| BAAI/bge-reranker-v2-m3, cross-encoder weights | Apache-2.0 | https://huggingface.co/BAAI/bge-reranker-v2-m3 |
| sentence-transformers | Apache-2.0 | https://github.com/UKPLab/sentence-transformers |
| PyTorch | BSD-3-Clause | https://github.com/pytorch/pytorch |
| LanceDB | Apache-2.0 | https://github.com/lancedb/lancedb |
| Apache Arrow, PyArrow | Apache-2.0 | https://github.com/apache/arrow |

Ollama pulls the embedding weights and sentence-transformers pulls the reranker
weights on first use. No model weights are redistributed here.

## Icons

The button icons are by [Icons8](https://icons8.com), used unmodified under the
Icons8 free licence, which asks for a link back in return.
[DATA-LICENSE.md](DATA-LICENSE.md) has the detail.

## Data sources

| Source | Role | Terms |
| --- | --- | --- |
| ÖKOBAUDAT (BBSR / BMWSB) | environmental datasets, queried live and cached | see [DATA-LICENSE.md](DATA-LICENSE.md) |
| soda4LCA | serves the ÖKOBAUDAT REST API; reached over HTTP only, no code reused | https://bitbucket.org/okusche/soda4lca |
| Autodesk Revit sample projects | source of the two benchmark material inventories | see [DATA-LICENSE.md](DATA-LICENSE.md) |

## Corrections

If a component is listed wrongly here, or if material in this repository
infringes your rights, open an issue and it will be corrected or removed.
