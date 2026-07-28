# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Local cross-encoder reranker HTTP service (Phase 1+).

Standalone CPython 3 sidecar spawned by the Connector on launch.
Wraps `sentence_transformers.CrossEncoder('BAAI/bge-reranker-v2-m3')`
and serves rerank requests over HTTP so the IronPython 2.7 plugin can
call it without dragging in PyTorch.

Why a sidecar instead of Ollama:
  Ollama has no `/api/rerank` endpoint and its `/api/embed` only
  accepts a single string per call - it cannot evaluate a true
  (query, document) cross-encoder pair. Cross-encoder reranking needs
  the pair to flow into the model together (attention across both),
  which sentence-transformers handles natively.

Endpoints:
  GET  /health                       → {"model": ..., "ready": bool, "device": "cpu"|"cuda"}
  POST /rerank                       → {"query": str, "documents": [str, ...]}
                                       returns {"scores": [float, ...]} (raw logits)
  GET  /                              → small index page (for humans)

Lifecycle:
  - Writes its PID to LCiA_Extension_Cache/.reranker_service.lock at
    startup; removes the lock on graceful exit (atexit + SIGTERM).
  - The Connector's `_is_reranker_running` checks the lock the same
    way `_is_prefetch_running` checks the embedding-build lock - so a
    Revit restart can't accidentally double-spawn the service.
  - Logs to LCiA_Extension_Cache/reranker_service.log (append mode).

Run standalone:
  python rerank_service.py [--port 11500] [--model BAAI/bge-reranker-v2-m3]

Dependencies:
  pip install sentence-transformers torch
"""
import argparse
import atexit
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


# ─────────────────────────────────────────────────────────────────
# Defaults - kept in sync with constants.py on the IronPython side
# ─────────────────────────────────────────────────────────────────
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_PORT  = 11500
DEFAULT_HOST  = "127.0.0.1"

HERE      = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "LCiA_Extension_Cache")
LOCK_PATH = os.path.join(CACHE_DIR, ".reranker_service.lock")
LOG_PATH  = os.path.join(CACHE_DIR, "reranker_service.log")


# ─────────────────────────────────────────────────────────────────
# PID lock (mirrors embedding_prefetcher.py)
# ─────────────────────────────────────────────────────────────────
def _is_pid_alive(pid):
    """Cross-platform: True iff a process with this PID is running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: OpenProcess + GetExitCodeProcess via ctypes
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            exit_code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(exit_code))
            k.CloseHandle(h)
            return bool(ok) and exit_code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _acquire_lock():
    """Refuse to start if a live sibling already holds the lock."""
    if not os.path.isdir(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
        except Exception:
            pid = 0
        if _is_pid_alive(pid):
            sys.stderr.write(
                "[SKIP] Another reranker service (PID {0}) is already "
                "running. Exiting.\n".format(pid))
            sys.exit(0)
        # Stale lock - clean it up
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
            if pid == os.getpid():
                os.remove(LOCK_PATH)
    except Exception:
        pass


atexit.register(_release_lock)


def _install_signal_handlers():
    def _handler(signum, frame):
        _release_lock()
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, AttributeError):
        pass


# ─────────────────────────────────────────────────────────────────
# Logging - append-mode log file + stderr mirror
# ─────────────────────────────────────────────────────────────────
def _setup_logging():
    if not os.path.isdir(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
    logger = logging.getLogger("rerank_service")
    logger.setLevel(logging.INFO)
    # Stream to stderr (so the parent Connector can capture progress).
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(sh)
    # Append to disk for post-mortem debugging.
    try:
        fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


# ─────────────────────────────────────────────────────────────────
# Model loading (sentence-transformers cross-encoder)
# ─────────────────────────────────────────────────────────────────
_model       = None
_model_name  = None
_device      = "cpu"
_ready       = threading.Event()
_load_error  = None
_log         = None


def _load_model(model_name):
    global _model, _model_name, _device, _load_error
    try:
        # Imports are deferred so that --help / lock-check work even
        # without sentence-transformers installed.
        from sentence_transformers import CrossEncoder
        import torch
    except ImportError as ex:
        _load_error = (
            "sentence-transformers / torch not installed. "
            "Run: pip install sentence-transformers torch"
        )
        _log.error(_load_error + "  ({})".format(ex))
        return False
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _log.info("Loading {0} on {1} ...".format(model_name, device))
    t0 = time.time()
    try:
        _model = CrossEncoder(model_name, device=device, max_length=512)
    except Exception as ex:
        _load_error = "Failed to load model '{0}': {1}".format(model_name, ex)
        _log.error(_load_error)
        return False
    _model_name = model_name
    _device     = device
    elapsed     = time.time() - t0
    _log.info(
        "Model loaded in {0:.1f}s. Warming up with a dummy pair ...".format(elapsed))
    # Warm-up: first inference triggers JIT kernels + caches model
    # weights into VRAM/RAM. Subsequent calls are ~5-10× faster.
    try:
        _ = _model.predict(
            [("warmup query", "warmup document")],
            convert_to_numpy=True,
        )
    except Exception as ex:
        _log.warning("Warm-up pair failed (non-fatal): {0}".format(ex))
    _ready.set()
    _log.info("Reranker ready.")
    return True


# ─────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────
class RerankHandler(BaseHTTPRequestHandler):
    server_version = "RerankService/1.0"
    # Silence default per-request stderr logging - we log meaningfully
    # ourselves and don't want every health-poll to spam the console.
    def log_message(self, format, *args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json({
                "model":  _model_name or "(loading)",
                "ready":  _ready.is_set(),
                "device": _device,
                "error":  _load_error,
            }, status=200 if _ready.is_set() else 503)
            return
        if self.path in ("/", "/index"):
            self._json({
                "service":   "rerank-service",
                "version":   "1.0",
                "model":     _model_name or "(loading)",
                "ready":     _ready.is_set(),
                "device":    _device,
                "endpoints": ["GET /health", "POST /rerank"],
            })
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        if self.path != "/rerank":
            self.send_error(404, "Not found")
            return
        if not _ready.is_set():
            self._json({"error": _load_error or "model not ready"}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw    = self.rfile.read(length) if length > 0 else b""
            body   = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as ex:
            self._json({"error": "bad JSON: {0}".format(ex)}, status=400)
            return
        query = body.get("query") or ""
        docs  = body.get("documents") or []
        if not isinstance(docs, list):
            self._json({"error": "'documents' must be a list"}, status=400)
            return
        if not query or not docs:
            self._json({"scores": []})
            return
        # Cross-encoder forward pass over [(query, doc), ...] pairs.
        # Returns raw logits as a numpy array; cast to plain floats for JSON.
        try:
            pairs = [(query, d if isinstance(d, str) else str(d)) for d in docs]
            t0 = time.time()
            scores = _model.predict(pairs, convert_to_numpy=True)
            ms = (time.time() - t0) * 1000.0
            _log.info("/rerank: {0} pair(s) in {1:.0f} ms".format(len(pairs), ms))
            self._json({"scores": [float(x) for x in scores]})
        except Exception as ex:
            _log.exception("rerank failed")
            self._json({"error": "rerank failed: {0}".format(ex)}, status=500)


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────
def _port_is_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) != 0
    finally:
        s.close()


def main():
    global _log
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",  type=int, default=DEFAULT_PORT,
                        help="Port to listen on (default {0}).".format(DEFAULT_PORT))
    parser.add_argument("--host",  default=DEFAULT_HOST,
                        help="Host to bind (default {0}).".format(DEFAULT_HOST))
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="HuggingFace model id (default {0}).".format(DEFAULT_MODEL))
    args = parser.parse_args()

    _log = _setup_logging()
    _install_signal_handlers()
    _acquire_lock()

    if not _port_is_free(args.host, args.port):
        _log.error(
            "Port {0}:{1} is already in use — another reranker service "
            "may be running. Exiting.".format(args.host, args.port))
        sys.exit(0)

    # Load the model in a background thread so the HTTP server starts
    # answering /health (with ready=False) immediately. The parent
    # Connector polls /health, sees ready=True when the model is up.
    loader = threading.Thread(
        target=_load_model, args=(args.model,), daemon=True)
    loader.start()

    server = HTTPServer((args.host, args.port), RerankHandler)
    _log.info("Reranker HTTP service listening on http://{0}:{1}".format(
        args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Interrupted, shutting down.")
    finally:
        server.server_close()
        _release_lock()


if __name__ == "__main__":
    main()
