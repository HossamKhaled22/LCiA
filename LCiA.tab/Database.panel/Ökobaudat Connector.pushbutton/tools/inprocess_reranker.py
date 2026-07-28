# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""In-process cross-encoder reranker - a drop-in, faster-transport twin of the
HTTP sidecar (rerank_service.py), for OFFLINE CPython tools only.

WHY THIS EXISTS
  The live plugin runs in IronPython 2.7 inside Revit and CANNOT import torch,
  so it must reach the reranker over HTTP (rerank_service.py). Offline CPython
  tools (ablations, benchmarks) CAN import torch directly, so they can skip the
  ~3 s/query HTTP hop. Same work, faster transport.

ACCURACY GUARANTEE
  This mirrors rerank_service.py's model setup EXACTLY - same model id, same
  device selection, same `max_length=512`, same `.predict(pairs,
  convert_to_numpy=True)` call, default fp32 (NO half precision) - so the raw
  logits are *identical* to the sidecar's, not merely close. Verified at run
  time by ladder_modes.py's in-process-vs-sidecar cross-check (max |Δlogit| and
  identical top-K ordering reported before the numbers are trusted).

Interface matches search_helpers.LocalRerankerClient so it is a drop-in:
    r = InProcessReranker()
    if r.is_available():
        logits = r.rerank(query, [doc1, doc2, ...])   # list[float], same order
"""
from __future__ import print_function
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

try:
    from constants import RERANKER_MODEL
except Exception:
    RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class InProcessReranker(object):
    """CPython-only cross-encoder reranker matching rerank_service.py exactly."""

    def __init__(self, model_name=None, device=None, max_length=512, warmup=True):
        self.model_name = model_name or RERANKER_MODEL
        self.model = self.model_name          # mirror LocalRerankerClient.model
        self.device = device
        self.max_length = max_length
        self.last_error = None
        self._ce = None
        try:
            from sentence_transformers import CrossEncoder
            import torch
            if self.device is None:
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            # EXACTLY rerank_service.py:194 - no fp16, same max_length.
            self._ce = CrossEncoder(self.model_name, device=self.device,
                                    max_length=self.max_length)
            if warmup:
                self._ce.predict([("warmup query", "warmup document")],
                                 convert_to_numpy=True)
        except Exception as ex:
            self.last_error = u"{0}".format(ex)
            self._ce = None

    def is_available(self):
        return self._ce is not None

    def rerank(self, query, docs):
        """Raw logits per (query, doc) - same shape/order as the sidecar's
        POST /rerank 'scores'. Empty list on bad input or failure."""
        if self._ce is None or not query or not docs:
            return []
        try:
            pairs = [(query, d if isinstance(d, str) else str(d)) for d in docs]
            scores = self._ce.predict(pairs, convert_to_numpy=True)
            return [float(x) for x in scores]
        except Exception as ex:
            self.last_error = u"{0}".format(ex)
            return []
