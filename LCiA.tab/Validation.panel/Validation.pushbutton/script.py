# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: GPL-3.0-or-later
__title__   = "Validation"
__version__ = "Version: 0.1"
__doc__     = """Phase 0 baseline benchmark — Recall@K against a ground-truth JSON.

Pick a Scope of Revit materials (or use the whole project), pick a
ground-truth JSON, then click Run Regex Match or Run Fuzzy Match. The
window shows Recall@1/3/5/10/20/All and an expandable card per query
with the top-K returned ÖKOBAUDAT candidates.

Mirrors the search code from tools/phase0_benchmark.py via the shared
search_helpers module in the Ökobaudat Connector pushbutton.
"""
#pylint: disable=import-error,invalid-name,broad-except
import os
import sys
import io
import json
import time
import traceback

from pyrevit import revit, DB, forms, script

from System.Windows.Data import CollectionViewSource, PropertyGroupDescription
from System.Windows import Visibility, Window, Application
from System.Windows.Controls import Expander
from System.Windows.Media import VisualTreeHelper, SolidColorBrush, Color, ColorConverter

# Make the shared search_helpers module importable. It lives in the
# Connector pushbutton (single source of truth shared with tools/phase0_benchmark.py).
HERE      = os.path.dirname(os.path.abspath(__file__))
CONNECTOR = os.path.abspath(os.path.join(
    HERE, "..", "..", "Database.panel", u"Ökobaudat Connector.pushbutton"
))
if os.path.isdir(CONNECTOR) and CONNECTOR not in sys.path:
    sys.path.insert(0, CONNECTOR)

# Hybrid mode shares the RRF k constant with the live Connector grid so
# both benchmarks (offline + Validation) and the live UI fuse on the
# same value (Cormack 2009 canonical default = 60).
try:
    from constants import (HYBRID_RRF_K, RERANKER_BASE_URL,
                           RERANKER_TIMEOUT_MS, RERANKER_TOP_K,
                           RERANKER_MODEL)
except Exception:
    HYBRID_RRF_K        = 60
    RERANKER_BASE_URL   = "http://127.0.0.1:11500"
    RERANKER_TIMEOUT_MS = 15000
    RERANKER_TOP_K      = 20
    RERANKER_MODEL      = "BAAI/bge-reranker-v2-m3"

from search_helpers import (
    build_searchable, search_regex, search_fuzzy,
    topk_hit, rank_of_first_hit,
    # Phase 0 v7 - BM25F is now the canonical ranker; per-row tooltip
    # calls `explain_score` (single source of truth shared with the live
    # Ökobaudat Connector grid). Mode-agnostic `ConfidenceCalibrator`
    # produces the displayed Score% and drives the Min Score slider so
    # the threshold has the same semantics across modes (Phase 0 → 1).
    CorpusStats, IsotonicCalibrator, bm25f_score, explain_score,
    expand_query_with_typos, soft_bound, ConfidenceCalibrator,
    # Phase 1 - dense semantic retrieval (cosine over bge-m3 embeddings).
    # Loaded lazily in _load_datasets; semantic mode is hidden when the
    # `.bin` sidecar is missing or Ollama is not reachable.
    EmbeddingIndex, OllamaEmbeddingClient, search_semantic,
    build_semantic_haystack,
    # Phase 1+ - Hybrid (Reciprocal Rank Fusion of BM25F + semantic).
    reciprocal_rank_fusion, rrf_max_score, bm25f_ranking,
    # Phase 1+ - Cross-encoder reranker (local sidecar).
    LocalRerankerClient,
    # Evaluation metrics - single source of truth shared with
    # tools/phase0_benchmark.py so the offline benchmark and the
    # in-Revit Validation report cannot drift on what "Wilson CI" or
    # "nDCG@10" mean. Used by `_run` and the report builders.
    wilson_ci, bootstrap_ci, mrr, ndcg_at_k, mcnemar_pvalue,
)
try:
    # Phase 1.7c: module-version sentinel surfaced in warning summaries
    # so the user can verify the hotfix actually loaded (IronPython 2.7
    # caches modules in-process; a Revit restart is needed for edits to
    # take effect).
    from search_helpers import SEARCH_HELPERS_VERSION as _SH_VER
except ImportError:
    _SH_VER = "unknown"
from classification_labels import translate_path
# Phase 1+ T5 - shared BIM-context query builder (same module the offline
# ablation + the live Connector use). Enriches the SEMANTIC / hybrid query.
import query_context

logger = script.get_logger()

CACHE_DIR = os.path.join(CONNECTOR, "LCiA_Extension_Cache")
SETTINGS_FILE = os.path.join(HERE, "_settings.json")

DATASTOCK_KEYS = ("a2_sphera", "a1_sphera", "a2_ecoinvent", "project_epds")


# ──────────────────────────────────────────────────────────────────
# Data binding rows
# ──────────────────────────────────────────────────────────────────

class CandidateRow(object):
    """One ranked candidate inside an expanded card. Bound to the DataGrid."""

    def __init__(self, query, header_subtitle, header_hit, rank, uuid,
                 match_name, classification, score, is_hit, score_tooltip=None):
        # Group header bindings (we duplicate the header text on each
        # row so the GroupStyle template can read it from Items[0]).
        self.GroupKey        = query                # used for grouping
        self.HeaderSubtitle  = header_subtitle      # "[Class: Glass]"
        self.HeaderHitText   = header_hit           # "Hit ✓  Rank 1" / "Miss - not found in ranked list" / etc.
        # Candidate-row columns
        self.HitMarker       = u"★" if is_hit else u""
        self.Rank            = unicode(rank)
        self.ScoreDisplay    = u"{:.0f}%".format(score * 100.0) if score is not None else u""
        self.ScoreTooltip    = score_tooltip        # multi-line hover text explaining why the score is what it is
        self.MatchName       = (match_name or u"")[:80]
        self.Classification  = classification or u""
        self.UUID            = uuid or u""


class EmptyRow(object):
    """Placeholder row shown for a query that yielded zero candidates
    (above min-score) - keeps the Expander group visible."""

    def __init__(self, query, header_subtitle, header_hit):
        self.GroupKey       = query
        self.HeaderSubtitle = header_subtitle
        self.HeaderHitText  = header_hit
        self.HitMarker      = u""
        self.Rank           = u""
        self.ScoreDisplay   = u""
        self.ScoreTooltip   = None
        self.MatchName      = u"(no candidates above min score)"
        self.Classification = u""
        self.UUID           = u""


# ──────────────────────────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────────────────────────

class ValidationWindow(forms.WPFWindow):
    """Phase 0 Recall@K validation window."""

    def __init__(self, pre_materials=None, settings=None):
        self._init_complete = False     # guards on_scope_changed during XAML / settings load
        xaml_path = os.path.join(HERE, "Validation.xaml")
        forms.WPFWindow.__init__(self, xaml_path)
        self.Title = __title__

        self._pre_materials  = pre_materials or {}     # {name: DB.Material}
        self._datasets       = []                      # list of dataset dicts (active datastock + lang)
        self._searchables    = []                      # parallel list - searchable dicts per dataset
        self._corpus_stats   = None                    # BM25F CorpusStats; rebuilt on every _load_datasets
        self._calibrator     = None                    # legacy unbalanced isotonic - tooltip-only
        self._bm25_params    = None                    # optional tuned params from cache dir
        self._hybrid_params  = None                    # optional tuned RRF params (pool/k/weights) per source+lang
        self._confidence_cals = {}                     # mode -> ConfidenceCalibrator (Phase 0→1 bridge)
        # Phase 1 - semantic backing. Both are loaded in `_load_datasets`
        # alongside the BM25F corpus stats. `_semantic_available` gates
        # the semantic run button on the XAML; the button still exists
        # in the UI but is disabled when either backing is missing.
        self._embedding_index    = None
        self._ollama_client      = None
        self._semantic_available = False
        self._gt_entries     = []                      # parsed ground-truth entries
        self._scope_names    = []                      # ordered list of Revit material names in scope
        self._matched_pairs  = []                      # [(query_string, gt_entry_or_None)] after scope+gt join
        self._results        = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}   # mode -> list of {query, ranked_all, hit_rank, uuids_all, ...}
        self._summary        = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}   # mode -> dict(r1, r3, r5, r10, r20, rall, duration, denom, n_scope)
        # Name-only baseline summary per mode, populated only when a dense mode
        # is run with BIM context ON (so the metrics table can show
        # "before | after"). None ⇒ render that mode's cells single-valued.
        self._summary_baseline = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._last_run_mode  = None
        # Results-grid display pref: when False, the non-match groups
        # ("skipped: label=…" / "not in ground-truth file") are hidden so only
        # the scored materials remain. Toggled by the footer "Hide/Show skipped"
        # button; never affects the metrics (always computed on matches only).
        self._show_skipped   = True
        self._gt_path        = u""
        self._gt_meta        = {}                               # top-level fields of the GT JSON (version, methodology, ranking_basis, ...)
        self._pending_pick   = None                             # flag for entry-point loop ("pick_elements" / "pick_materials")
        # BIM-context state (mirrors the Connector's robust design): the
        # default field set is held here, not derived solely from XAML
        # checkbox defaults, so a flaky read or a stale settings file can
        # never silently collapse enrichment to name-only.
        self._context_default_fields = ["name", "class"]
        self._context_stale  = False                            # metric rows stale after a context toggle
        self._ladder_results = None                             # last "Measure field value" ladder table
        self._ladder_running = False                            # re-entrancy guard for the (long) ladder run

        self._restore_settings(settings or self._load_settings_file())
        self._init_ui()

        self._init_complete = True
        # Show modally from inside __init__ (matches the SmartMatch pattern).
        # Calling ShowDialog here ensures the WPF modal state is fully
        # torn down when __init__ returns, so the subsequent PickObjects
        # call can properly switch Revit's contextual ribbon into pick
        # mode (Finish / Cancel buttons on the ribbon).
        self.ShowDialog()

    # ── Settings persistence ────────────────────────────────────────

    def _load_settings_file(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with io.open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings_file(self):
        try:
            data = {
                "datastock":  self._current_datastock(),
                "lang":       u"en" if self.radio_lang_en.IsChecked else u"de",
                "scope":      self._current_scope_key(),
                "top":        self.combo_topn.SelectedIndex,
                "threshold":  float(self.slider_threshold.Value),
                "gt_path":    self._gt_path,
                "settings_schema": 3,   # bump when the context block format changes
                "context_on":     self._context_on(),
                "context_fields": self._context_fields(),
            }
            with io.open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                f.write(unicode(json.dumps(data, ensure_ascii=False, indent=2)))
        except Exception as ex:
            logger.error(u"Could not save settings: {}".format(ex))

    def _restore_settings(self, s):
        try:
            ds = s.get("datastock", u"a2_sphera")
            radio = {
                "a2_sphera":    self.radio_a2_sphera,
                "a1_sphera":    self.radio_a1_sphera,
                "a2_ecoinvent": self.radio_a2_ecoinvent,
                "project_epds": self.radio_project_epds,
            }.get(ds, self.radio_a2_sphera)
            radio.IsChecked = True
        except Exception:
            pass
        try:
            lang = s.get("lang", u"en")
            if lang == u"de":
                self.radio_lang_de.IsChecked = True
            else:
                self.radio_lang_en.IsChecked = True
        except Exception:
            pass
        try:
            scope_map = {
                u"pick_elements":    self.scope_pick_elements,
                u"elements_view":    self.scope_elements_view,
                u"elements_project": self.scope_elements_project,
                u"pick_materials":   self.scope_pick_materials,
                u"materials_project": self.scope_materials_project,
            }
            item = scope_map.get(s.get("scope", u"materials_project"),
                                 self.scope_materials_project)
            item.IsSelected = True
        except Exception:
            pass
        try:
            top_idx = s.get("top", 1)
            if 0 <= top_idx < self.combo_topn.Items.Count:
                self.combo_topn.SelectedIndex = top_idx
        except Exception:
            pass
        try:
            thr = s.get("threshold", 50.0)
            self.slider_threshold.Value = float(thr)
            self.text_threshold.Text    = u"{}%".format(int(round(float(thr))))
        except Exception:
            pass
        try:
            # The simplified UI has a single 'BIM context' master checkbox; the
            # per-field boxes are hidden and no longer drive the run (the fixed
            # best-combo `_BEST_CONTEXT_FIELDS` applies). Restore only the master
            # toggle. A pre-v2 block may be poisoned by the old read bug, so
            # ignore it (default off); the next save rewrites it at schema 3.
            schema = int(s.get("settings_schema", 0) or 0)
            master = self._ctl("chk_context")
            if master is not None:
                master.IsChecked = (bool(s.get("context_on", False))
                                    if schema >= 2 else False)
        except Exception:
            pass
        # Ground-truth path: persisted last-used, fall back to the copy
        # in the Connector pushbutton root.
        path = s.get("gt_path") or u""
        if not path or not os.path.exists(path):
            default = os.path.join(CONNECTOR, "sample_project_a2_sphera_en_v0.6__opus-4.8.json")
            if os.path.exists(default):
                path = default
        self._gt_path = path

    # ── UI init ─────────────────────────────────────────────────────

    def _init_ui(self):
        self._refresh_groundtruth_label()
        self._load_datasets()
        self._refresh_scope_materials()
        # Sync btn_run's label with the default-checked radio (Fuzzy in XAML).
        self._update_run_button_label()
        self._refresh_context_preview()

    def _refresh_groundtruth_label(self):
        if self._gt_path:
            self.text_groundtruth_path.Text = self._gt_path
            try:
                self.text_groundtruth_path.ToolTip = self._gt_path
            except Exception:
                pass
        else:
            self.text_groundtruth_path.Text = u"(none — click Browse to choose)"

    # ── Helpers reading UI state ────────────────────────────────────

    def _current_datastock(self):
        if self.radio_a1_sphera.IsChecked:    return "a1_sphera"
        if self.radio_a2_ecoinvent.IsChecked: return "a2_ecoinvent"
        if self.radio_project_epds.IsChecked: return "project_epds"
        return "a2_sphera"

    def _current_lang(self):
        return "en" if self.radio_lang_en.IsChecked else "de"

    def _current_scope_key(self):
        if self.scope_pick_elements.IsSelected:    return u"pick_elements"
        if self.scope_elements_view.IsSelected:    return u"elements_view"
        if self.scope_elements_project.IsSelected: return u"elements_project"
        if self.scope_pick_materials.IsSelected:   return u"pick_materials"
        if self.scope_materials_project.IsSelected: return u"materials_project"
        return u"materials_project"

    def _current_topk(self):
        """Return integer K from the Top dropdown (3, 5, 10, or 20)."""
        try:
            txt = unicode(self.combo_topn.SelectedItem.Content).strip()
        except Exception:
            return 5
        try:
            return int(txt)
        except Exception:
            return 5

    def _current_threshold(self):
        """Slider value 0-100 mapped to a fuzzy_score threshold 0.0-1.0."""
        try:
            return float(self.slider_threshold.Value) / 100.0
        except Exception:
            return 0.5

    # ── BIM-context query enrichment (Phase 1+ T5) ──────────────────
    # When chk_context is on, the SEMANTIC / hybrid query is enriched with
    # each material's textual BIM context fields (from its ground-truth
    # entry, which carries the same fields ExtractContext writes). BM25F
    # always uses the bare name. The per-field checkboxes let the user
    # toggle each field and re-run to MEASURE its value against the GT -
    # the in-tool counterpart of the offline ablation ladder.
    _CONTEXT_FIELD_CHK = {
        "class":                "chk_ctx_class",
        "description":          "chk_ctx_description",
        "concrete_grade":       "chk_ctx_concrete_grade",
        "structural_class":     "chk_ctx_structural_class",
        "host_categories":      "chk_ctx_host_categories",
        "density":              "chk_ctx_density",
        "thermal_conductivity": "chk_ctx_thermal_conductivity",
        "host_types":           "chk_ctx_host_types",
    }

    # The universal best combo (tools/ladder_modes.md): the one field set that
    # wins across every dense mode and both languages. Applied by the single
    # 'BIM context' checkbox. `build_enriched_query` emits these in
    # query_context.FIELD_KEYS registry order, so the embedded string matches
    # the offline ladder regardless of the order listed here.
    _BEST_CONTEXT_FIELDS = ["name", "class", "description",
                            "concrete_grade", "structural_class"]

    def _ctl(self, name):
        """Resolve an x:Named control robustly. pyRevit's wpf.LoadComponent
        normally injects named elements as attributes, but fall back to the
        WPF NameScope via FindName if injection missed one. The bare
        `getattr(self, "chk_ctx_*")` was returning None for the context
        checkboxes, which silently collapsed the field set to name-only and
        made BIM-context enrichment a no-op."""
        ctl = getattr(self, name, None)
        if ctl is None:
            try:
                ctl = self.FindName(name)
            except Exception:
                ctl = None
        return ctl

    def _pump_ui(self):
        """Best-effort WPF DoEvents - lets the window repaint during the long
        synchronous ladder run so Revit doesn't show 'Not Responding'. Silent
        no-op if the dispatcher call fails (falls back to a plain freeze)."""
        try:
            from System.Windows.Threading import (Dispatcher, DispatcherFrame,
                DispatcherPriority, DispatcherOperationCallback)
            frame = DispatcherFrame()

            def _stop(arg):
                frame.Continue = False
                return None
            Dispatcher.CurrentDispatcher.BeginInvoke(
                DispatcherPriority.Background,
                DispatcherOperationCallback(_stop), None)
            Dispatcher.PushFrame(frame)
        except Exception:
            pass

    def _context_on(self):
        box = self._ctl("chk_context")
        if box is None:
            return False
        try:
            return bool(box.IsChecked)
        except Exception:
            return False

    def _context_fields(self):
        """Active dense-query context fields. With the simplified UI the single
        'BIM context' checkbox applies the fixed best-combo field set
        (`_BEST_CONTEXT_FIELDS` - the universal winner from
        tools/ladder_modes.md); per-field experimentation moved into the
        'Measure field value' dialog. Off ⇒ name-only (== baseline)."""
        if self._context_on():
            return list(self._BEST_CONTEXT_FIELDS)
        return ["name"]

    def _enrich_query(self, gt):
        """Build the enriched dense query from a GT entry (or None)."""
        if not gt:
            return None
        try:
            return query_context.build_enriched_query(
                gt, self._context_fields(), lang=self._current_lang())
        except Exception as ex:
            logger.error(u"Context enrich failed: {}".format(ex))
            return None

    def on_context_changed(self, sender, e):
        if not self.IsLoaded:
            return
        try:
            self._context_stale = True
            self._refresh_context_preview()
            self._save_settings_file()
        except Exception:
            pass

    def _sample_match_gt(self):
        """First 'match' ground-truth entry (loads the GT file lazily) - used
        to preview the enriched query before a full run has been done."""
        if not self._gt_entries:
            try:
                self._load_ground_truth()
            except Exception:
                pass
        for ent in (self._gt_entries or []):
            if ent.get("label") == u"match":
                return ent
        for ent in (self._gt_entries or []):
            return ent
        return None

    def _refresh_context_preview(self):
        """Render the resolved enriched query for a sample matched material so
        the user sees exactly what the dense path will embed. Empty when off."""
        lbl = self._ctl("lbl_context_preview")
        if lbl is None:
            return
        try:
            if not self._context_on():
                lbl.Text = u""
                return
            gt = self._sample_match_gt()
            fields = self._context_fields()
            if gt is None:
                lbl.Text = (u"BIM context ON (fields: {0}) — pick a ground-truth "
                            u"file to preview the enriched query.").format(
                                u", ".join(fields))
                return
            name = (gt.get("name") or u"").strip()
            dq = self._enrich_query(gt) or name
            stale = u"     ⟳ re-run to apply" if self._context_stale else u""
            if not dq or dq == name:
                lbl.Text = (u"BIM context ON — “{0}” adds no fields; "
                            u"query unchanged.{1}").format(name, stale)
            else:
                lbl.Text = (u"Enriched query e.g. “{0}”:  {1}      "
                            u"(dense modes + fuzzy; BM25F & regex use the "
                            u"name){2}").format(name, dq, stale)
        except Exception:
            pass

    # ── Field-value ladder ("Measure field value") ──────────────────
    # The ladder is now built dynamically from the fields the user picks in the
    # Measure dialog (`_build_ladder`). Display order, labels and the noise flag
    # match the shipped offline study (tools/ladder_modes.md) so the in-tool
    # ladder reproduces it row-for-row. `build_enriched_query` emits the actual
    # query in query_context.FIELD_KEYS order, so the embedded string for a
    # given field SET is identical regardless of this display order.
    _LADDER_FIELD_ORDER = ["class", "description", "concrete_grade",
                           "structural_class", "host_categories", "density",
                           "thermal_conductivity", "host_types"]
    _FIELD_LABEL = {
        "class":                u"class",
        "description":          u"description",
        "concrete_grade":       u"concrete grade",
        "structural_class":     u"struct class",
        "host_categories":      u"categories",
        "density":              u"density",
        "thermal_conductivity": u"λ thermal",
        "host_types":           u"host types",
    }
    _NOISE_FIELDS = set(["density", "thermal_conductivity", "host_types"])
    # Default tick set in the picker = the shipped best combo minus 'name'.
    _LADDER_DEFAULT_FIELDS = set(["class", "description",
                                  "concrete_grade", "structural_class"])
    _MODE_DISPLAY = {u"semantic": u"Semantic (cosine)",
                     u"hybrid":   u"Hybrid (RRF)",
                     u"reranker": u"Reranker (cross-encoder)",
                     u"fuzzy":    u"Fuzzy (Levenshtein — expect noise)"}

    def _build_ladder(self, selected_fields):
        """Cumulative ladder restricted to `selected_fields`, in the display
        order above, always starting at the name-only baseline. Returns a list
        of (label, [field_keys], is_noise) tuples."""
        sel = set(selected_fields)
        ladder = [(u"name (baseline)", ["name"], False)]
        cum = ["name"]
        for key in self._LADDER_FIELD_ORDER:
            if key not in sel:
                continue
            cum = cum + [key]
            ladder.append((u"+ " + self._FIELD_LABEL.get(key, key),
                           list(cum), key in self._NOISE_FIELDS))
        return ladder

    def on_measure_fields_click(self, sender, e):
        """Open the field + mode picker, then run a cumulative field-set ladder
        against the loaded ground truth and rank each set by MRR / Recall@K for
        each chosen dense mode - so the user can re-measure which BIM-context
        fields improve accuracy on a new project."""
        if self._ladder_running:
            return
        if not self._datasets:
            self._load_datasets()
        # NOTE: the semantic-backing requirement is enforced AFTER the picker
        # (only when a dense mode is actually picked) so a Fuzzy-only run -
        # which needs no Ollama/embeddings - can proceed even when the semantic
        # backing is down. The picker itself greys out unavailable dense modes.
        if not self._load_ground_truth():
            forms.alert(u"Pick a ground-truth JSON first (Browse…).",
                        title=u"Validation")
            return
        self._refresh_scope_materials()
        prepared = []
        for q, gt in self._join_scope_to_gt():
            if gt is None or gt.get("label") != u"match":
                continue
            acc = set(gt.get("acceptable_uuids", []) or [])
            cu = gt.get("correct_uuid", u"") or u""
            if cu:
                acc.add(cu)
            if acc:
                prepared.append((gt, acc))
        if not prepared:
            forms.alert(
                u"No in-scope material matched a ground-truth 'match' entry "
                u"with acceptable UUIDs — nothing to measure.",
                title=u"Validation")
            return
        picked = self._show_field_picker()
        if not picked:
            return
        fields, modes = picked
        if not modes:
            forms.alert(u"Pick at least one mode to measure.",
                        title=u"Measure field value")
            return
        # Dense modes need the semantic backing; fuzzy does not. Enforce the
        # requirement now (the picker greys unavailable dense modes, so this
        # only fires defensively) and pick the right cost message.
        dense_picked = [m for m in modes
                        if m in (u"semantic", u"hybrid", u"reranker")]
        if dense_picked and (not self._semantic_available
                or self._embedding_index is None
                or self._ollama_client is None):
            forms.alert(
                u"The chosen dense mode(s) {0} need the semantic backing: the "
                u"embedding cache (.bin) and a reachable Ollama server. Start "
                u"Ollama (`ollama serve`) and ensure the cache is built, or "
                u"pick Fuzzy only (no Ollama needed).".format(
                    u", ".join(dense_picked)),
                title=u"Validation")
            return
        ladder = self._build_ladder(fields)
        lang = self._current_lang()
        if dense_picked:
            # Precompute the unique-query count (cheap; no embedding) so the
            # user gets an accurate cost estimate and can cancel before the
            # long run. Embeddings are mode-independent (reused), so the cost
            # is unique queries × embedding latency regardless of mode count.
            uniq = set()
            for _label, fl, _noise in ladder:
                for gt, _acc in prepared:
                    uniq.add(query_context.build_enriched_query(
                        gt, fl, lang=lang))
            est_lo = max(1, int(round(len(uniq) * 1.0 / 60.0)))
            est_hi = max(est_lo + 1, int(round(len(uniq) * 3.0 / 60.0)))
            rer_note = u""
            if u"reranker" in modes:
                rer_note = (u"\nReranker adds a sidecar call on the top-{0} per "
                            u"(material × field set).".format(RERANKER_TOP_K))
            fuz_note = (u"\nFuzzy (also picked) is lexical — no embedding — and "
                        u"is included to confirm enrichment adds noise."
                        if u"fuzzy" in modes else u"")
            msg = (u"Field-value ladder: {0} matched materials × {1} field sets "
                   u"× {2} mode(s) = {3} unique queries to embed via Ollama.\n\n"
                   u"Estimated ~{4}–{5} min (embedding-bound; vectors are reused "
                   u"across modes).{6}{7}\n\nRevit stays busy (wait cursor) until "
                   u"done. Continue?").format(
                       len(prepared), len(ladder), len(modes), len(uniq),
                       est_lo, est_hi, rer_note, fuz_note)
        else:
            # Fuzzy-only - lexical, no Ollama, runs in seconds.
            msg = (u"Field-value ladder (Fuzzy only — lexical, no Ollama): {0} "
                   u"matched materials × {1} field sets = {2} fuzzy searches.\n\n"
                   u"This documents that BIM-context enrichment adds noise to "
                   u"the lexical path. Runs in a few seconds. Continue?").format(
                       len(prepared), len(ladder), len(prepared) * len(ladder))
        if not forms.alert(msg, title=u"Measure field value",
                           ok=False, yes=True, no=True):
            return
        result = self._compute_field_ladder(prepared, ladder, modes)
        if result is not None:
            self._ladder_results = result
            self._show_ladder_window(result)

    def _show_field_picker(self):
        """Modal dialog: pick which BIM-context fields and which dense modes to
        measure. Defaults to the shipped best combo + every available mode.
        Returns (fields_list, modes_list) or None if cancelled."""
        from System.Windows import (Window, Thickness, SizeToContent,
            WindowStartupLocation, FontWeights, HorizontalAlignment, TextWrapping)
        from System.Windows.Controls import (StackPanel, TextBlock, CheckBox,
            Button, Orientation)
        from System.Windows.Media import Brushes

        sem_ok = bool(self._semantic_available and self._embedding_index is not None
                      and self._ollama_client is not None)
        hyb_ok = bool(sem_ok and self._corpus_stats is not None)
        rer_ok = bool(hyb_ok and getattr(self, "_reranker_available", False))

        panel = StackPanel(); panel.Margin = Thickness(18)
        title = TextBlock()
        title.Text = u"Measure field value — pick fields + modes"
        title.FontSize = 14; title.FontWeight = FontWeights.Bold
        title.Margin = Thickness(0, 0, 0, 2)
        panel.Children.Add(title)
        sub = TextBlock()
        sub.Text = (u"Builds a cumulative ladder (name → +each field) and ranks "
                    u"every set by MRR / Recall@K against the loaded ground "
                    u"truth, for each chosen mode. Defaults = the shipped best "
                    u"combo on the dense modes; tick Fuzzy to confirm the "
                    u"lexical path takes on noise.")
        sub.FontSize = 10.5; sub.Foreground = Brushes.Gray
        sub.TextWrapping = TextWrapping.Wrap; sub.MaxWidth = 430
        sub.Margin = Thickness(0, 0, 0, 10)
        panel.Children.Add(sub)

        fhdr = TextBlock(); fhdr.Text = u"Fields"
        fhdr.FontWeight = FontWeights.SemiBold; fhdr.Margin = Thickness(0, 0, 0, 4)
        panel.Children.Add(fhdr)
        field_boxes = {}
        for key in self._LADDER_FIELD_ORDER:
            cb = CheckBox()
            lbl = self._FIELD_LABEL.get(key, key)
            is_noise = key in self._NOISE_FIELDS
            cb.Content = (lbl + u"  ⚠") if is_noise else lbl
            cb.FontSize = 12; cb.Margin = Thickness(0, 2, 0, 2)
            cb.IsChecked = key in self._LADDER_DEFAULT_FIELDS
            if is_noise:
                cb.Foreground = Brushes.Chocolate
            src = self._ctl(self._CONTEXT_FIELD_CHK.get(key, u""))
            try:
                if src is not None and src.ToolTip:
                    cb.ToolTip = src.ToolTip
            except Exception:
                pass
            field_boxes[key] = cb
            panel.Children.Add(cb)

        mhdr = TextBlock(); mhdr.Text = u"Modes"
        mhdr.FontWeight = FontWeights.SemiBold; mhdr.Margin = Thickness(0, 10, 0, 4)
        panel.Children.Add(mhdr)
        mode_boxes = {}
        # (mode, available?, default-checked?, label). Fuzzy needs no backing
        # (lexical, no Ollama) so it is always available, but it is OFF by
        # default - it exists so the user can re-confirm, on a new project,
        # that BIM-context enrichment adds noise to the lexical path.
        for mode, ok, default_on, label in (
                (u"semantic", sem_ok, sem_ok, u"Semantic"),
                (u"hybrid",   hyb_ok, hyb_ok, u"Hybrid"),
                (u"reranker", rer_ok, rer_ok, u"Reranker"),
                (u"fuzzy",    True,   False,  u"Fuzzy (lexical — expect noise)")):
            cb = CheckBox(); cb.Content = label; cb.FontSize = 12
            cb.Margin = Thickness(0, 2, 0, 2)
            cb.IsChecked = default_on
            cb.IsEnabled = ok
            if not ok:
                cb.ToolTip = (u"Unavailable — needs the semantic backing "
                              u"(embeddings + Ollama)" +
                              (u" and the rerank sidecar"
                               if mode == u"reranker" else u"") + u".")
            elif mode == u"fuzzy":
                cb.ToolTip = (u"Lexical Levenshtein match — no Ollama needed. "
                              u"Off by default; tick it to verify that "
                              u"BIM-context enrichment adds noise to fuzzy on "
                              u"this ground truth.")
            mode_boxes[mode] = cb
            panel.Children.Add(cb)

        holder = {u"ok": False}
        win = Window()

        def _measure(s, ev):
            holder[u"ok"] = True
            win.DialogResult = True
            win.Close()

        def _cancel(s, ev):
            win.DialogResult = False
            win.Close()

        btnrow = StackPanel(); btnrow.Orientation = Orientation.Horizontal
        btnrow.HorizontalAlignment = HorizontalAlignment.Right
        btnrow.Margin = Thickness(0, 14, 0, 0)
        b_ok = Button(); b_ok.Content = u"Measure"; b_ok.MinWidth = 90
        b_ok.Margin = Thickness(0, 0, 8, 0); b_ok.Click += _measure
        b_cancel = Button(); b_cancel.Content = u"Cancel"; b_cancel.MinWidth = 70
        b_cancel.Click += _cancel
        btnrow.Children.Add(b_ok); btnrow.Children.Add(b_cancel)
        panel.Children.Add(btnrow)

        win.Title = u"Measure field value"
        win.Content = panel
        win.SizeToContent = SizeToContent.WidthAndHeight
        win.WindowStartupLocation = WindowStartupLocation.CenterOwner
        try:
            win.Owner = self
        except Exception:
            pass
        win.ShowDialog()
        if not holder[u"ok"]:
            return None
        fields = [k for k in self._LADDER_FIELD_ORDER
                  if field_boxes[k].IsChecked]
        modes = [m for m in (u"semantic", u"hybrid", u"reranker", u"fuzzy")
                 if mode_boxes[m].IsChecked and mode_boxes[m].IsEnabled]
        return (fields, modes)

    @staticmethod
    def _rank_first_uuid(uuids, accept):
        """1-indexed rank of the first uuid in `accept`, or None."""
        for i, u in enumerate(uuids, 1):
            if u in accept:
                return i
        return None

    def _compute_field_ladder(self, prepared, ladder, modes):
        """Run the cumulative `ladder` for each chosen mode and tally
        MRR / R@1/5/10 / nDCG@10 per (mode, variant). Mirrors `_run`'s hybrid
        primitives so the numbers match the main run. Caching keeps it fast:
        each unique enriched query is embedded ONCE (reused across modes and
        across the semantic leg of hybrid/reranker); BM25F is cached per unique
        material NAME (the name is constant across field variants); the
        reranker re-scores only the top-K per (name, query). The optional
        `fuzzy` mode is lexical (no embedding) and is keyed by the enriched
        query - it documents that enrichment adds noise to the lexical path."""
        lang = self._current_lang()
        semantic_on = u"semantic" in modes
        hybrid_on   = u"hybrid" in modes
        reranker_on = u"reranker" in modes
        _hp = self._hybrid_params or {}
        _k  = int(_hp.get(u"k", HYBRID_RRF_K))
        _wb = float(_hp.get(u"w_bm25", 1.0))
        _ws = float(_hp.get(u"w_sem", 1.0))

        # uuid -> searchable haystack dict (for the reranker docs)
        if self._searchables and len(self._searchables) == len(self._datasets):
            uuid_to_sh = dict((d.get(u"uuid", u""), s)
                              for d, s in zip(self._datasets, self._searchables))
            bm_items = [(d.get(u"uuid", u""), s)
                        for d, s in zip(self._datasets, self._searchables)]
        else:
            uuid_to_sh = {}
            bm_items = []
            for d in self._datasets:
                sh = build_searchable(d, lang=lang)
                uuid_to_sh[d.get(u"uuid", u"")] = sh
                bm_items.append((d.get(u"uuid", u""), sh))

        embed_cache  = {}   # s -> vector or None
        sem_by_s     = {}   # s -> [uuid, ...]
        bm25_by_name = {}   # name -> [uuid, ...]
        hyb_cache    = {}   # (name, s) -> [uuid, ...]
        rer_cache    = {}   # (name, s) -> [uuid, ...]
        fuz_by_s     = {}   # s -> [uuid, ...]  (fuzzy filter on the query `s`)
        seen_q       = set()  # every distinct enriched query (for the subtitle)

        def sem_uuids_for(s):
            if s in sem_by_s:
                return sem_by_s[s]
            if s not in embed_cache:
                embed_cache[s] = self._ollama_client.embed(s)
            vec = embed_cache[s]
            if vec is None:
                sem_by_s[s] = []
                return []
            ranked = search_semantic(
                s, self._datasets, searchables=self._searchables,
                embedding_index=self._embedding_index,
                ollama_client=self._ollama_client, lang=lang, query_vector=vec)
            lst = [it.get(u"uuid", u"") for (it, _sc) in ranked]
            sem_by_s[s] = lst
            return lst

        def bm25_uuids_for(name):
            if name in bm25_by_name:
                return bm25_by_name[name]
            if self._corpus_stats is None or not bm_items:
                bm25_by_name[name] = []
                return []
            try:
                expanded = expand_query_with_typos(name.lower(), self._corpus_stats)
            except Exception:
                expanded = None
            lst = bm25f_ranking(name.lower(), bm_items, self._corpus_stats,
                                params=self._bm25_params, expanded_query=expanded)
            bm25_by_name[name] = lst
            return lst

        def hyb_uuids_for(name, s):
            key = (name, s)
            if key in hyb_cache:
                return hyb_cache[key]
            bm  = bm25_uuids_for(name)
            sem = sem_uuids_for(s)
            if bm and sem:
                rrf = reciprocal_rank_fusion([bm, sem], k=_k, weights=[_wb, _ws])
                lst = [u for u, _sc in sorted(rrf.items(),
                       key=lambda t: t[1], reverse=True)]
            else:
                lst = bm or sem
            hyb_cache[key] = lst
            return lst

        def rer_uuids_for(name, s):
            key = (name, s)
            if key in rer_cache:
                return rer_cache[key]
            hyb = hyb_uuids_for(name, s)
            lst = hyb
            topk = hyb[:RERANKER_TOP_K]
            if topk and self._reranker_client is not None:
                docs = [build_semantic_haystack(uuid_to_sh.get(u, {})) or u""
                        for u in topk]
                logits = self._reranker_client.rerank(s, docs)
                if logits and len(logits) == len(topk):
                    reord = [u for u, _l in sorted(zip(topk, logits),
                             key=lambda t: t[1], reverse=True)]
                    lst = reord + hyb[RERANKER_TOP_K:]
            rer_cache[key] = lst
            return lst

        def fuz_uuids_for(s):
            # The live Fuzzy mode exactly: _fuzzy_match_inner filter, then
            # survivors ranked by BM25F (same _rerank_with_bm25f path the live
            # grid uses; falls back to _fuzzy_score order if corpus stats are
            # absent). Keyed by `s` (fuzzy depends on the whole query, not just
            # the name), so a given enriched string is searched once.
            if s in fuz_by_s:
                return fuz_by_s[s]
            ranked = search_fuzzy(s, self._datasets,
                                  searchables=self._searchables)
            ranked = self._rerank_with_bm25f(s, ranked)
            lst = [it.get(u"uuid", u"") for (it, _sc) in ranked]
            fuz_by_s[s] = lst
            return lst

        def uuids_for(mode, name, s):
            if mode == u"semantic":
                return sem_uuids_for(s)
            if mode == u"hybrid":
                return hyb_uuids_for(name, s)
            if mode == u"fuzzy":
                return fuz_uuids_for(s)
            return rer_uuids_for(name, s)

        n = len(prepared)
        out_by_mode = dict((m, []) for m in modes)
        Mouse = None
        btn = self._ctl("btn_measure_fields")
        self._ladder_running = True
        try:
            from System.Windows.Input import Mouse, Cursors
            Mouse.OverrideCursor = Cursors.Wait
        except Exception:
            Mouse = None
        if btn is not None:
            try:
                btn.IsEnabled = False
            except Exception:
                pass
        t0 = time.time()
        try:
            for vi, (label, fields, is_noise) in enumerate(ladder):
                logger.info(u"[ladder] {0}/{1}  {2}  (modes={3})".format(
                    vi + 1, len(ladder), label, u",".join(modes)))
                acc_m = dict((m, {"rr": 0.0, "h1": 0, "h5": 0, "h10": 0,
                                  "ndcg": 0.0}) for m in modes)
                for ei, (gt, acc) in enumerate(prepared):
                    name = (gt.get("name") or u"").strip()
                    s = query_context.build_enriched_query(gt, fields, lang=lang)
                    seen_q.add(s)
                    correct = gt.get("correct_uuid", u"") or u""
                    accept_only = list(gt.get("acceptable_uuids", []) or [])
                    for m in modes:
                        rk_list = uuids_for(m, name, s)
                        first = self._rank_first_uuid(rk_list, acc)
                        A = acc_m[m]
                        if first is not None:
                            A["rr"] += 1.0 / first
                            if first <= 1:
                                A["h1"] += 1
                            if first <= 5:
                                A["h5"] += 1
                            if first <= 10:
                                A["h10"] += 1
                        A["ndcg"] += ndcg_at_k(rk_list, correct, accept_only, k=10)
                    if ei % 3 == 0:
                        self._pump_ui()   # keep the window painting
                for m in modes:
                    A = acc_m[m]
                    out_by_mode[m].append({
                        "label": label, "fields": fields, "is_noise": is_noise,
                        "mrr": A["rr"] / n if n else 0.0,
                        "r1": A["h1"] / float(n) if n else 0.0,
                        "r5": A["h5"] / float(n) if n else 0.0,
                        "r10": A["h10"] / float(n) if n else 0.0,
                        "ndcg": A["ndcg"] / n if n else 0.0,
                    })
        finally:
            self._ladder_running = False
            if btn is not None:
                try:
                    btn.IsEnabled = True
                except Exception:
                    pass
            if Mouse is not None:
                try:
                    Mouse.OverrideCursor = None
                except Exception:
                    pass
        by_mode = {}
        for m in modes:
            rows = out_by_mode[m]
            base = rows[0]["mrr"] if rows else 0.0
            best = 0
            for i, r in enumerate(rows):
                r["delta"] = r["mrr"] - base
                if r["mrr"] > rows[best]["mrr"]:
                    best = i
            by_mode[m] = {"rows": rows, "best_idx": best}
        secs = time.time() - t0
        logger.info(u"[ladder] done: {0} variants × {1} mode(s), {2} unique "
                    u"queries embedded, {3:.0f}s".format(
                        len(ladder), len(modes), len(embed_cache), secs))
        return {"modes": modes, "by_mode": by_mode, "n": n, "secs": secs,
                "unique": len(embed_cache), "unique_q": len(seen_q),
                "lang": lang}

    def _show_ladder_window(self, result):
        """Render the multi-mode ladder: one table per chosen dense mode,
        winner ★-highlighted, noise rows flagged, Δ-vs-baseline per row."""
        from System.Windows import (Window, Thickness, GridLength, SizeToContent,
            WindowStartupLocation, FontWeights, HorizontalAlignment, TextWrapping)
        from System.Windows.Controls import (Grid, TextBlock, Border, ScrollViewer,
            StackPanel, ColumnDefinition, RowDefinition)
        from System.Windows.Media import Brushes

        headers = [u"Field set", u"MRR", u"R@1", u"R@5", u"R@10",
                   u"Δ vs name"]
        widths  = [200, 64, 52, 52, 56, 80]

        def build_table(rows, best):
            grid = Grid()
            for w in widths:
                cd = ColumnDefinition(); cd.Width = GridLength(w)
                grid.ColumnDefinitions.Add(cd)
            for _ in range(len(rows) + 1):
                rd = RowDefinition(); rd.Height = GridLength.Auto
                grid.RowDefinitions.Add(rd)
            if rows and 0 <= best < len(rows):
                hl = Border(); hl.Background = Brushes.Honeydew
                Grid.SetRow(hl, best + 1); Grid.SetColumn(hl, 0)
                Grid.SetColumnSpan(hl, len(headers))
                grid.Children.Add(hl)

            def put(text, r, c, bold=False, fg=None, right=False):
                tb = TextBlock(); tb.Text = text
                tb.Margin = Thickness(8, 3, 8, 3); tb.FontSize = 12
                if bold:
                    tb.FontWeight = FontWeights.Bold
                if fg is not None:
                    tb.Foreground = fg
                tb.HorizontalAlignment = (HorizontalAlignment.Right if right
                                          else HorizontalAlignment.Left)
                Grid.SetRow(tb, r); Grid.SetColumn(tb, c)
                grid.Children.Add(tb)

            for c, h in enumerate(headers):
                put(h, 0, c, bold=True, fg=Brushes.Gray, right=(c > 0))
            for i, r in enumerate(rows):
                gr = i + 1
                fg = Brushes.Chocolate if r["is_noise"] else None
                tag = u"  ★" if i == best else (u"  ⚠" if r["is_noise"] else u"")
                put(r["label"] + tag, gr, 0, bold=(i == best), fg=fg)
                put(u"{0:.3f}".format(r["mrr"]), gr, 1,
                    bold=(i == best), fg=fg, right=True)
                put(u"{0:.0f}%".format(r["r1"] * 100), gr, 2, fg=fg, right=True)
                put(u"{0:.0f}%".format(r["r5"] * 100), gr, 3, fg=fg, right=True)
                put(u"{0:.0f}%".format(r["r10"] * 100), gr, 4, fg=fg, right=True)
                d = r["delta"]
                dt = u"—" if i == 0 else (
                    (u"+{0:.3f}" if d >= 0 else u"{0:.3f}").format(d))
                put(dt, gr, 5, fg=fg, right=True)
            return grid

        panel = StackPanel(); panel.Margin = Thickness(18)
        title = TextBlock()
        title.Text = u"BIM-context field value — cumulative ladder"
        title.FontSize = 14; title.FontWeight = FontWeights.Bold
        title.Margin = Thickness(0, 0, 0, 4)
        panel.Children.Add(title)
        sub = TextBlock()
        modes_disp = u", ".join(self._MODE_DISPLAY.get(m, m)
                                for m in result["modes"])
        emb = result.get("unique", 0)
        if emb:
            cost = (u"{0} unique queries embedded once, reused across: "
                    u"{1}").format(emb, modes_disp)
        else:
            # Fuzzy-only run - no embedding happened.
            cost = (u"{0} unique queries · lexical only, no embedding · "
                    u"{1}").format(result.get("unique_q", result["n"]),
                                   modes_disp)
        sub.Text = (u"{0} matched materials · {1} · {2:.0f}s · lang {3}").format(
            result["n"], cost, result["secs"],
            (result.get("lang") or u"").upper())
        sub.FontSize = 11; sub.Foreground = Brushes.Gray
        sub.TextWrapping = TextWrapping.Wrap; sub.MaxWidth = 600
        sub.Margin = Thickness(0, 0, 0, 12)
        panel.Children.Add(sub)

        for m in result["modes"]:
            md = result["by_mode"].get(m) or {"rows": [], "best_idx": 0}
            mh = TextBlock(); mh.Text = self._MODE_DISPLAY.get(m, m)
            mh.FontSize = 12.5; mh.FontWeight = FontWeights.Bold
            mh.Margin = Thickness(0, 8, 0, 4)
            panel.Children.Add(mh)
            panel.Children.Add(build_table(md.get("rows", []),
                                           md.get("best_idx", 0)))

        note = TextBlock()
        note.Text = (u"Each row embeds name + the listed fields (dense path; "
                     u"BM25F always uses the name).  Fuzzy, when shown, matches "
                     u"the enriched string lexically — included to confirm "
                     u"enrichment adds noise to it.  ★ = most accurate set per "
                     u"mode on this ground truth.  ⚠ = fields the offline study "
                     u"flags as noise (better kept as deterministic filters).  "
                     u"Vectors are embedded once and reused across modes; BM25F "
                     u"is cached per material name; the reranker re-scores only "
                     u"the top-K per query.")
        note.FontSize = 10.5; note.Foreground = Brushes.Gray
        note.TextWrapping = TextWrapping.Wrap; note.Margin = Thickness(0, 12, 0, 0)
        note.MaxWidth = 600
        panel.Children.Add(note)

        sv = ScrollViewer(); sv.Content = panel
        sv.MaxHeight = 720

        win = Window()
        win.Title = u"Field-value ladder"
        win.Content = sv
        win.SizeToContent = SizeToContent.WidthAndHeight
        win.WindowStartupLocation = WindowStartupLocation.CenterOwner
        try:
            win.Owner = self
        except Exception:
            pass
        win.ShowDialog()

    def _show_mode(self):
        if getattr(self, "radio_show_semantic_reranked", None) is not None \
                and self.radio_show_semantic_reranked.IsChecked:
            return u"semantic_reranked"
        if getattr(self, "radio_show_hybrid_reranked", None) is not None \
                and self.radio_show_hybrid_reranked.IsChecked:
            return u"hybrid_reranked"
        if getattr(self, "radio_show_hybrid", None) is not None \
                and self.radio_show_hybrid.IsChecked:
            return u"hybrid"
        if getattr(self, "radio_show_semantic", None) is not None \
                and self.radio_show_semantic.IsChecked:
            return u"semantic"
        return u"regex" if self.radio_show_regex.IsChecked else u"fuzzy"

    def _update_semantic_button_state(self):
        """Reflect prerequisite availability (Ollama / embeddings / corpus stats /
        reranker sidecar) on the per-row Show radios. When a mode is unavailable
        the matching radio is greyed and its tooltip names the missing piece.
        Run is dispatched by the single shared btn_run, so there's nothing
        button-side to update here - the radio's IsEnabled state both signals
        availability AND prevents the user from selecting an unavailable mode."""
        # Semantic - needs embedding sidecar + reachable Ollama.
        radio = getattr(self, "radio_show_semantic", None)
        if radio is not None:
            if self._semantic_available:
                radio.IsEnabled = True
                radio.ToolTip = (u"Phase 1 dense retrieval — rank by cosine "
                                 u"similarity over bge-m3 embeddings.")
            else:
                radio.IsEnabled = False
                if self._embedding_index is None:
                    radio.ToolTip = (
                        u"Semantic mode disabled: no embedding sidecar. "
                        u"Run `python embedding_prefetcher.py "
                        u"--source <src> --lang <de|en>` to enable.")
                elif self._ollama_client is None:
                    radio.ToolTip = (u"Semantic mode disabled: Ollama client "
                                     u"could not be constructed.")
                else:
                    last = self._ollama_client.last_error or u"unknown"
                    radio.ToolTip = (
                        u"Semantic mode disabled: Ollama not reachable. "
                        u"Start it with `ollama serve`. ({})").format(last)

        # Hybrid - needs BOTH semantic backings AND a BM25F corpus.
        radio_hy = getattr(self, "radio_show_hybrid", None)
        if radio_hy is not None:
            if self._semantic_available and self._corpus_stats is not None:
                radio_hy.IsEnabled = True
                radio_hy.ToolTip = (
                    u"Hybrid retrieval — Reciprocal Rank Fusion "
                    u"(Cormack 2009, k=60) of BM25F + bge-m3 cosine "
                    u"rankings. Robust to score-scale differences.")
            else:
                radio_hy.IsEnabled = False
                if not self._semantic_available:
                    radio_hy.ToolTip = (
                        u"Hybrid mode disabled: semantic backing is "
                        u"required (Hybrid fuses BM25F + cosine).")
                else:
                    radio_hy.ToolTip = (
                        u"Hybrid mode disabled: BM25F corpus stats not loaded.")

        # Hybrid+Rerank - Hybrid prerequisites PLUS the reranker sidecar.
        radio_hr = getattr(self, "radio_show_hybrid_reranked", None)
        if radio_hr is not None:
            if (self._semantic_available and self._corpus_stats is not None
                    and getattr(self, "_reranker_available", False)):
                radio_hr.IsEnabled = True
                radio_hr.ToolTip = (
                    u"Full reference stack: BM25F + bge-m3 → RRF → "
                    u"cross-encoder reranker on the top-{0}.").format(
                        RERANKER_TOP_K)
            else:
                radio_hr.IsEnabled = False
                if not getattr(self, "_reranker_available", False):
                    radio_hr.ToolTip = (
                        u"Hybrid+Rerank disabled: local rerank sidecar "
                        u"not reachable at {0}. Open the Ökobaudat "
                        u"Connector (auto-spawns the sidecar) or run "
                        u"`python rerank_service.py` manually.").format(
                            RERANKER_BASE_URL)
                elif not self._semantic_available:
                    radio_hr.ToolTip = (
                        u"Hybrid+Rerank disabled: semantic backing "
                        u"required (sidecar + Ollama).")
                else:
                    radio_hr.ToolTip = (
                        u"Hybrid+Rerank disabled: BM25F corpus stats "
                        u"not loaded.")

        # Semantic+Rerank ablation - needs semantic backing + reranker sidecar,
        # but NOT BM25F corpus stats (no fusion).
        radio_sr = getattr(self, "radio_show_semantic_reranked", None)
        if radio_sr is not None:
            if (self._semantic_available
                    and getattr(self, "_reranker_available", False)):
                radio_sr.IsEnabled = True
                radio_sr.ToolTip = (
                    u"Ablation: pure cosine → cross-encoder reranker on the "
                    u"top-{0} (no BM25F / RRF). Compare against Hybrid+Rerank "
                    u"to see whether the lexical leg helps.").format(
                        RERANKER_TOP_K)
            else:
                radio_sr.IsEnabled = False
                if not self._semantic_available:
                    radio_sr.ToolTip = (
                        u"Semantic+Rerank disabled: semantic backing "
                        u"required (embeddings + Ollama).")
                else:
                    radio_sr.ToolTip = (
                        u"Semantic+Rerank disabled: local rerank sidecar "
                        u"not reachable at {0}.").format(RERANKER_BASE_URL)

    # ── Data loading ────────────────────────────────────────────────

    def _load_datasets(self):
        """Load the cached dataset list for the current datastock + language."""
        ds = self._current_datastock()
        lg = self._current_lang()
        path = os.path.join(CACHE_DIR, "ds_cache_v2_{}_{}.json".format(ds, lg))
        self._datasets    = []
        self._searchables = []
        if not os.path.exists(path):
            self.text_status.Text = u"Dataset cache missing: {} ({}). Open the Connector first to populate it.".format(ds, lg)
            return
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._datasets    = data.get("results", []) or []
            # Pass the active language so the haystack uses the English
            # classification when EN is selected - keeps search results
            # consistent with what the user sees in the Classification column.
            self._searchables = [build_searchable(d, lang=lg) for d in self._datasets]
            # Build BM25F CorpusStats over the loaded corpus. Cheap; ~50ms.
            try:
                self._corpus_stats = CorpusStats.build(self._searchables)
            except Exception as ex:
                logger.error(u"CorpusStats build failed: {}".format(ex))
                self._corpus_stats = None
            # Optional isotonic calibrator + tuned params, if present in cache dir
            cal_path = os.path.join(
                CACHE_DIR, "calibration_{}_{}.json".format(ds, lg))
            self._calibrator = None
            if os.path.exists(cal_path):
                try:
                    with io.open(cal_path, "r", encoding="utf-8") as cf:
                        self._calibrator = IsotonicCalibrator.from_dict(json.load(cf))
                except Exception as ex:
                    logger.error(u"Calibrator load failed: {}".format(ex))
            params_path = os.path.join(CACHE_DIR, "bm25_params_v1.json")
            self._bm25_params = None
            if os.path.exists(params_path):
                try:
                    with io.open(params_path, "r", encoding="utf-8") as pf:
                        self._bm25_params = json.load(pf).get("params")
                except Exception as ex:
                    logger.error(u"BM25 params load failed: {}".format(ex))
            # Tuned hybrid-fusion params (symmetric pool + per-ranker weights),
            # per source+lang - same artifact the live Connector consumes so
            # the benchmark matches the live hybrid ranker. Absent ⇒ default.
            hp_path = os.path.join(
                CACHE_DIR, u"hybrid_params_{}_{}.json".format(ds, lg))
            self._hybrid_params = None
            if os.path.exists(hp_path):
                try:
                    with io.open(hp_path, "r", encoding="utf-8") as pf:
                        self._hybrid_params = json.load(pf).get("params")
                except Exception as ex:
                    logger.error(u"Hybrid params load failed: {}".format(ex))
            # Per-mode confidence calibrators (Phase 0 → 1 bridge). Loads
            # `confidence_<mode>_<datastock>_<lang>.json` when present;
            # falls back to the per-mode sigmoid default otherwise.
            self._confidence_cals = {}
            for mode in (u"bm25f", u"semantic", u"hybrid", u"reranker"):
                try:
                    cp = os.path.join(
                        CACHE_DIR,
                        u"confidence_{}_{}_{}.json".format(mode, ds, lg))
                    d = None
                    if os.path.exists(cp):
                        with io.open(cp, "r", encoding="utf-8") as ccf:
                            d = json.load(ccf)
                    self._confidence_cals[mode] = ConfidenceCalibrator.from_dict(mode, d)
                except Exception as ex:
                    logger.error(u"Confidence calibrator load failed for {}: {}".format(mode, ex))
                    self._confidence_cals[mode] = ConfidenceCalibrator(mode=mode)
            # Phase 1 - semantic backing. Same architecture as the
            # Connector: `.bin` sidecar (built by embedding_prefetcher.py)
            # for the dataset vectors, Ollama HTTP for the per-query
            # embedding. Either missing → semantic mode disabled, button
            # greyed out, no crash.
            self._embedding_index    = None
            self._ollama_client      = None
            self._semantic_available = False
            try:
                bin_path = os.path.join(
                    CACHE_DIR, u"embeddings_{}_{}.bin".format(ds, lg))
                if os.path.exists(bin_path):
                    self._embedding_index = EmbeddingIndex.load_bin(bin_path)
            except Exception as ex:
                logger.error(u"Embedding sidecar load failed: {}".format(ex))
                self._embedding_index = None
            try:
                self._ollama_client = OllamaEmbeddingClient(
                    model=u"bge-m3", timeout_ms=10000)
                if (self._embedding_index is not None
                        and self._ollama_client.is_available()):
                    self._semantic_available = True
            except Exception as ex:
                logger.error(u"Ollama client setup failed: {}".format(ex))
                self._ollama_client = None
            # Phase 1+ - cross-encoder reranker. The live Connector
            # auto-spawns the sidecar; the Validation pushbutton just
            # connects to it (assumes the user has the Connector open or
            # has run `python rerank_service.py` themselves).
            self._reranker_client    = None
            self._reranker_available = False
            try:
                self._reranker_client = LocalRerankerClient(
                    base_url=RERANKER_BASE_URL,
                    timeout_ms=RERANKER_TIMEOUT_MS)
                if self._reranker_client.is_available():
                    self._reranker_available = True
            except Exception as ex:
                logger.error(u"Reranker client setup failed: {}".format(ex))
                self._reranker_client = None
            self._update_semantic_button_state()
            self.text_status.Text = u"Loaded {} datasets from {} ({}).".format(
                len(self._datasets), ds, lg)
        except Exception as ex:
            logger.error(u"Failed to load dataset cache: {}".format(ex))
            self.text_status.Text = u"Error loading datasets: {}".format(ex)

    def _rerank_with_bm25f(self, query, ranked):
        """Replace the legacy `_fuzzy_score` numbers on a ranked list with
        BM25F scores (calibrated when a calibrator is loaded, else
        soft-bounded). The binary candidate set is preserved - only the
        score values change and the list is re-sorted descending. Single
        source of truth shared with the live Connector grid and the
        offline `tools/phase0_benchmark.py`.
        """
        if not self._corpus_stats or not query:
            return ranked
        nl = (query or u"").lower()
        try:
            expanded = expand_query_with_typos(nl, self._corpus_stats)
        except Exception as ex:
            logger.error(u"Typo expansion failed: {}".format(ex))
            expanded = None
        out = []
        for ds, _legacy in ranked:
            hay = build_searchable(ds, lang=self._current_lang())
            try:
                raw = bm25f_score(nl, hay, self._corpus_stats,
                                  params=self._bm25_params,
                                  expanded_query=expanded)
            except Exception as ex:
                logger.error(u"BM25F score error for '{}': {}".format(
                    ds.get("name", u"?"), ex))
                raw = 0.0
            # Mode-agnostic confidence. Same `apply(raw)` interface for
            # BM25F today, cosine in Phase 1 - the Min Score slider keeps
            # its meaning across modes.
            cc = self._confidence_cals.get(u"bm25f")
            if cc is None:
                cc = ConfidenceCalibrator(mode=u"bm25f")
            score = cc.apply(raw)
            out.append((ds, score))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def _load_ground_truth(self):
        """Read the ground-truth JSON the user has chosen. Returns list of entries (incl. skip)."""
        self._gt_entries = []
        self._gt_meta    = {}
        if not self._gt_path or not os.path.isfile(self._gt_path):
            return False
        try:
            with io.open(self._gt_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._gt_entries = data.get("entries", []) or []
            # Stash everything except 'entries' for the export report header.
            self._gt_meta = {k: v for (k, v) in data.items() if k != "entries"}
            return True
        except Exception as ex:
            forms.alert(u"Could not read ground truth:\n{}".format(ex),
                        title=u"Validation")
            return False

    # ── Revit scope resolution ──────────────────────────────────────

    def _collect_view_materials(self):
        """Materials referenced by elements in the active view."""
        doc  = revit.doc
        view = doc.ActiveView
        names = []
        try:
            els = DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType().ToElements()
            seen = set()
            for el in els:
                try:
                    mat_ids = el.GetMaterialIds(False)
                except Exception:
                    continue
                for mid in mat_ids:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m = doc.GetElement(mid)
                    if m is not None and m.Name:
                        names.append(m.Name)
        except Exception as ex:
            logger.error(u"Error collecting view materials: {}".format(ex))
        return sorted(set(names))

    def _collect_project_elements_materials(self):
        """Materials referenced by ALL elements in the project (whole-doc scan)."""
        doc = revit.doc
        names = []
        try:
            els = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements()
            seen = set()
            for el in els:
                try:
                    mat_ids = el.GetMaterialIds(False)
                except Exception:
                    continue
                for mid in mat_ids:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m = doc.GetElement(mid)
                    if m is not None and m.Name:
                        names.append(m.Name)
        except Exception as ex:
            logger.error(u"Error collecting project elements: {}".format(ex))
        return sorted(set(names))

    def _collect_all_materials(self):
        """All Material elements in the project."""
        doc = revit.doc
        names = []
        try:
            mats = DB.FilteredElementCollector(doc).OfClass(DB.Material).ToElements()
            for m in mats:
                if m.Name:
                    names.append(m.Name)
        except Exception as ex:
            logger.error(u"Error collecting all materials: {}".format(ex))
        return sorted(set(names))

    def _refresh_scope_materials(self):
        """Re-read scope. For 'Pick' choices, the user's selection happens
        via re-entry from script.py (not in this method)."""
        scope = self._current_scope_key()
        is_pick_scope = scope in (u"pick_elements", u"pick_materials")
        if self._pre_materials and is_pick_scope:
            # We were re-launched from a Pick flow and the user is still
            # on the pick scope - use the picked set. Switching to any
            # live-collection scope falls through to the branches below.
            names = sorted(self._pre_materials.keys())
        elif scope == u"elements_view":
            names = self._collect_view_materials()
        elif scope == u"elements_project":
            names = self._collect_project_elements_materials()
        elif scope == u"materials_project":
            names = self._collect_all_materials()
        else:
            # Pick scope without pre_materials - shouldn't happen because
            # on_scope_changed closes the window for re-pick before we get here.
            names = []
        self._scope_names = names
        self.text_status.Text = u"Scope: {} material(s) in scope.".format(len(names))

    # ── Scope ↔ Ground-truth join ───────────────────────────────────

    def _join_scope_to_gt(self):
        """Produce a list of (query_name, gt_entry_or_None) pairs.

        For each Revit material name in scope, find the ground-truth entry
        whose `name` matches (case-insensitive, exact first; substring
        fallback). Revit materials not in the ground-truth file are still
        included (gt_entry=None) so they render as "not in ground-truth file"
        cards.
        """
        gt_by_lower = {}
        for e in (self._gt_entries or []):
            nm = (e.get("name", u"") or u"").strip()
            if nm:
                gt_by_lower.setdefault(nm.lower(), e)

        pairs = []
        for nm in self._scope_names:
            key = (nm or u"").strip().lower()
            entry = gt_by_lower.get(key)
            if entry is None:
                # Substring fallback
                for k, v in gt_by_lower.items():
                    if k and (k in key or key in k):
                        entry = v
                        break
            pairs.append((nm, entry))
        self._matched_pairs = pairs
        return pairs

    # ── Run benchmark - single unified handler ─────────────────────
    # The 5 old per-mode handlers (on_run_regex / on_run_fuzzy / on_run_semantic
    # / on_run_hybrid / on_run_hybrid_reranked) collapsed into one click handler.
    # The active mode is read from whichever "Show" radio is currently checked
    # next to the metrics rows. on_show_mode_changed keeps btn_run's label
    # in sync via _update_run_button_label.

    _RUN_BUTTON_LABELS = {
        u"regex":           u"▶  Run Regex Match",
        u"fuzzy":           u"▶  Run Fuzzy Match",
        u"semantic":          u"▶  Run Semantic Match",
        u"semantic_reranked": u"▶  Run Semantic + Rerank Match",
        u"hybrid":            u"▶  Run Hybrid Match",
        u"hybrid_reranked":   u"▶  Run Hybrid + Rerank Match",
    }

    def _update_run_button_label(self):
        """Rewrite btn_run's Content to match the currently selected mode."""
        btn = getattr(self, "btn_run", None)
        if btn is None:
            return
        btn.Content = self._RUN_BUTTON_LABELS.get(
            self._show_mode(), u"▶  Run")

    def on_run_click(self, sender, e):
        """Run the benchmark for whichever mode the user picked via radio.
        Per-mode availability guards (semantic / hybrid / hybrid+rerank
        prerequisites) live here so the alert text stays as informative as
        the old per-mode buttons were."""
        if self._ladder_running:
            return
        mode = self._show_mode()

        if mode == u"semantic":
            if not self._semantic_available:
                forms.alert(
                    u"Semantic mode unavailable.\n\n"
                    u"Either the embedding sidecar is missing "
                    u"(run `python embedding_prefetcher.py`) or the local "
                    u"Ollama server is not reachable (start it with "
                    u"`ollama serve`).",
                    title=u"Validation")
                return

        elif mode == u"hybrid":
            if not self._semantic_available:
                forms.alert(
                    u"Hybrid mode unavailable.\n\n"
                    u"Hybrid retrieval fuses BM25F + bge-m3 cosine via RRF "
                    u"and therefore needs both backings. The semantic side "
                    u"is currently disabled — either the embedding sidecar "
                    u"is missing (run `python embedding_prefetcher.py`) or "
                    u"Ollama is not reachable (start it with `ollama serve`).",
                    title=u"Validation")
                return
            if self._corpus_stats is None:
                forms.alert(
                    u"Hybrid mode unavailable: BM25F corpus stats not loaded.",
                    title=u"Validation")
                return

        elif mode == u"hybrid_reranked":
            if not getattr(self, "_reranker_available", False):
                forms.alert(
                    u"Hybrid+Rerank mode unavailable.\n\n"
                    u"The local cross-encoder sidecar (rerank_service.py) "
                    u"is not reachable at {0}. Open the Ökobaudat Connector "
                    u"first — it auto-spawns the sidecar — or run "
                    u"`python rerank_service.py` manually in a separate "
                    u"terminal.".format(RERANKER_BASE_URL),
                    title=u"Validation")
                return
            if not self._semantic_available:
                forms.alert(
                    u"Hybrid+Rerank mode unavailable: semantic backing "
                    u"required.", title=u"Validation")
                return
            if self._corpus_stats is None:
                forms.alert(
                    u"Hybrid+Rerank mode unavailable: BM25F corpus stats "
                    u"not loaded.", title=u"Validation")
                return

        elif mode == u"semantic_reranked":
            # Ablation: pure cosine → cross-encoder rerank (no BM25F/RRF).
            # Needs the semantic backing + the reranker sidecar; NOT corpus stats.
            if not self._semantic_available:
                forms.alert(
                    u"Semantic+Rerank mode unavailable.\n\n"
                    u"Needs the embedding sidecar (run "
                    u"`python embedding_prefetcher.py`) and a reachable Ollama "
                    u"server (`ollama serve`).", title=u"Validation")
                return
            if not getattr(self, "_reranker_available", False):
                forms.alert(
                    u"Semantic+Rerank mode unavailable.\n\n"
                    u"The local cross-encoder sidecar (rerank_service.py) is "
                    u"not reachable at {0}. Open the Ökobaudat Connector first "
                    u"(it auto-spawns the sidecar) or run "
                    u"`python rerank_service.py` manually.".format(
                        RERANKER_BASE_URL),
                    title=u"Validation")
                return

        self._run(mode)

    def _run(self, mode):
        if not self._datasets:
            self._load_datasets()
            if not self._datasets:
                forms.alert(u"No dataset cache for the selected datastock/language.\n\nOpen the Ökobaudat Connector to populate the cache first.",
                            title=u"Validation")
                return
        if not self._load_ground_truth():
            forms.alert(u"Pick a ground-truth JSON first (Browse…).",
                        title=u"Validation")
            return
        self._refresh_scope_materials()
        pairs = self._join_scope_to_gt()
        if not pairs:
            forms.alert(u"No Revit materials in scope.", title=u"Validation")
            return

        # Context-bearing modes with BIM context ON are run twice - a
        # name-only baseline and the enriched best-combo - so the metrics
        # table can show "before | after" per cell. The enriched pass is the
        # one whose candidate list renders. Fuzzy is included here on purpose:
        # NOT because enrichment helps it (it does not - see
        # tools/ladder_modes.md), but so the lexical path gets the same
        # before|after treatment and the table proves, comprehensively, that
        # enrichment adds noise to fuzzy. Regex is a pattern mode (its query is
        # a regex, not free text) so enrichment is never applicable to it.
        dual = mode in (u"semantic", u"semantic_reranked", u"hybrid",
                        u"hybrid_reranked", u"fuzzy")
        if dual and self._context_on():
            base_summary, _base_pq = self._run_pass(mode, False, pairs)
            self._summary_baseline[mode] = base_summary
            summary, per_query = self._run_pass(mode, True, pairs)
        else:
            self._summary_baseline[mode] = None
            summary, per_query = self._run_pass(mode, False, pairs)
        self._results[mode] = per_query
        self._summary[mode] = summary
        self._last_run_mode = mode
        # Toggle "Showing" to the just-ran mode for visibility
        if mode == u"regex":
            self.radio_show_regex.IsChecked = True
        elif mode == u"semantic" and getattr(self, "radio_show_semantic", None) is not None:
            self.radio_show_semantic.IsChecked = True
        elif mode == u"semantic_reranked" and getattr(self, "radio_show_semantic_reranked", None) is not None:
            self.radio_show_semantic_reranked.IsChecked = True
        elif mode == u"hybrid" and getattr(self, "radio_show_hybrid", None) is not None:
            self.radio_show_hybrid.IsChecked = True
        elif mode == u"hybrid_reranked" and getattr(self, "radio_show_hybrid_reranked", None) is not None:
            self.radio_show_hybrid_reranked.IsChecked = True
        else:
            self.radio_show_fuzzy.IsChecked = True
        self._update_metric_cells()
        self._refresh_cards()
        self._save_settings_file()

    def _rerank_topk(self, ranked, query, dq, reranker_cc):
        """Cross-encoder rerank pass on the top-`RERANKER_TOP_K` of `ranked`
        (a list of (item, score)). Re-orders the head by raw logit (calibrator-
        independent, matching the live Connector) and leaves the tail unchanged.
        Returns (new_ranked, meta, failed); `meta` carries rerank_logits/before/
        after for the tooltip (None on sidecar failure). Used by the
        `semantic_reranked` ablation; the working `hybrid_reranked` path keeps
        its own inline copy untouched."""
        head_n = RERANKER_TOP_K
        head, tail = ranked[:head_n], ranked[head_n:]
        docs = []
        for (ds, _sc) in head:
            hay = build_searchable(ds, lang=self._current_lang())
            docs.append(build_semantic_haystack(hay) or ds.get(u"name", u""))
        rr_logits = None
        if self._reranker_client is not None and docs:
            rr_logits = self._reranker_client.rerank(dq or query, docs)
        if not rr_logits or len(rr_logits) != len(head):
            return ranked, None, True
        head_before_rank = {head[i][0].get(u"uuid", u""): i + 1
                            for i in range(len(head))}
        head_rescored = []
        for i, (ds, _sc) in enumerate(head):
            logit = rr_logits[i]
            head_rescored.append((ds, reranker_cc.apply(logit), logit))
        head_rescored.sort(key=lambda t: t[2], reverse=True)
        rerank_logits, rerank_before, rerank_after = {}, {}, {}
        for j, (ds, _sc, logit) in enumerate(head_rescored):
            uuid = ds.get(u"uuid", u"")
            rerank_logits[uuid] = logit
            rerank_before[uuid] = head_before_rank.get(uuid)
            rerank_after[uuid]  = j + 1
        meta = {u"rerank_logits": rerank_logits,
                u"rerank_before": rerank_before,
                u"rerank_after":  rerank_after}
        new_ranked = [(ds, sc) for (ds, sc, _l) in head_rescored] + tail
        return new_ranked, meta, False

    def _run_pass(self, mode, context_on, pairs):
        """One search + metrics pass over `pairs`. Returns (summary_dict,
        per_query); does NOT mutate self._summary / _results / radios - the
        caller (`_run`) decides baseline vs enriched. `context_on` enables BIM-
        context enrichment for the dense modes AND fuzzy (the latter only to
        document its noise); regex is never enriched."""
        # Run search for each query and tally Recall@K (denominator = number
        # of pairs whose GT entry exists with label='match'; pairs with no
        # GT entry or label='skip' are not counted but still rendered).
        semantic_cc = None
        hybrid_cc   = None
        reranker_cc = None
        rrf_max     = None
        if mode == u"semantic" or mode == u"semantic_reranked":
            # `search_semantic` already ranks by cosine; do NOT re-rank
            # with BM25F afterwards (that would replace the semantic
            # ordering with a lexical one). `dense_query` (the BIM-context-
            # enriched query) is embedded when present; else the bare name.
            # `semantic_reranked` adds a cross-encoder pass on the top-K of
            # this pure-cosine list (no RRF) - the fusion-free ablation.
            def search_fn(q, ds, searchables=None, dense_query=None):
                return search_semantic(
                    dense_query or q, ds, searchables=searchables,
                    embedding_index=self._embedding_index,
                    ollama_client=self._ollama_client,
                    lang=self._current_lang())
            semantic_cc = (self._confidence_cals.get(u"semantic")
                           or ConfidenceCalibrator(mode=u"semantic"))
            if mode == u"semantic_reranked":
                reranker_cc = (self._confidence_cals.get(u"reranker")
                               or ConfidenceCalibrator(mode=u"reranker"))
        elif mode == u"hybrid" or mode == u"hybrid_reranked":
            # Phase 1+ - Reciprocal Rank Fusion (Cormack 2009), with
            # an optional cross-encoder rerank pass on the top-K
            # candidates when `mode == u"hybrid_reranked"`.
            # `search_fn` returns (item, raw_rrf) tuples. The fusion
            # also produces per-ranker rank dicts so the per-row
            # tooltip can show "BM25F: #2, Semantic: #1" provenance.
            hybrid_cc = (self._confidence_cals.get(u"hybrid")
                         or ConfidenceCalibrator(mode=u"hybrid"))
            reranker_cc = (self._confidence_cals.get(u"reranker")
                           or ConfidenceCalibrator(mode=u"reranker"))
            # Tuned fusion params (symmetric pool + per-ranker weights), same
            # artifact + helpers the live Connector uses → the benchmark
            # matches the live hybrid ranker. Absent ⇒ live default.
            _hp = self._hybrid_params or {}
            _k  = int(_hp.get(u"k", HYBRID_RRF_K))
            _pool = _hp.get(u"pool", u"asymmetric")
            _wb = float(_hp.get(u"w_bm25", 1.0))
            _ws = float(_hp.get(u"w_sem", 1.0))
            rrf_max   = rrf_max_score(2, k=_k, weights=[_wb, _ws])
            self._last_rrf_bm25f_ranks = {}   # query_index -> {uuid: rank}
            self._last_rrf_sem_ranks   = {}
            self._last_rrf_raw         = {}   # query_index -> {uuid: raw_rrf}
            def search_fn(q, ds, searchables=None, dense_query=None):
                dq = dense_query or q
                # BM25F path - always on the bare name `q`. Symmetric pool
                # (tuned) ranks the full corpus via the shared
                # `bm25f_ranking`; else the legacy fuzzy-filtered pool.
                if _pool == u"symmetric" and self._corpus_stats is not None:
                    if searchables and len(searchables) == len(ds):
                        items = [(d.get(u"uuid", u""), s)
                                 for d, s in zip(ds, searchables)]
                    else:
                        items = [(d.get(u"uuid", u""),
                                  build_searchable(d, lang=self._current_lang()))
                                 for d in ds]
                    try:
                        expanded = expand_query_with_typos(
                            q.lower(), self._corpus_stats)
                    except Exception:
                        expanded = None
                    bm25f_uuids = bm25f_ranking(
                        q.lower(), items, self._corpus_stats,
                        params=self._bm25_params, expanded_query=expanded)
                    items_by_uuid = dict((d.get(u"uuid", u""), d) for d in ds)
                else:
                    fuzzy_ranked = search_fuzzy(q, ds, searchables=searchables)
                    bm25f_ranked = self._rerank_with_bm25f(q, fuzzy_ranked)
                    bm25f_uuids = [it.get(u"uuid", u"") for (it, _) in bm25f_ranked]
                    items_by_uuid = {}
                    for (it, _) in bm25f_ranked:
                        items_by_uuid[it.get(u"uuid", u"")] = it
                # Semantic path - on the (possibly enriched) dense query `dq`.
                sem_ranked = search_semantic(
                    dq, ds, searchables=searchables,
                    embedding_index=self._embedding_index,
                    ollama_client=self._ollama_client,
                    lang=self._current_lang())
                sem_uuids   = [it.get(u"uuid", u"") for (it, _) in sem_ranked]
                for (it, _) in sem_ranked:
                    items_by_uuid.setdefault(it.get(u"uuid", u""), it)
                rrf_scores  = reciprocal_rank_fusion(
                    [bm25f_uuids, sem_uuids], k=_k, weights=[_wb, _ws])
                bm25f_ranks = {u: i + 1 for i, u in enumerate(bm25f_uuids)}
                sem_ranks   = {u: i + 1 for i, u in enumerate(sem_uuids)}
                # Stash for the tooltip (lookup by uuid in the candidate
                # row loop).
                self._curr_rrf_bm25f_ranks = bm25f_ranks
                self._curr_rrf_sem_ranks   = sem_ranks
                self._curr_rrf_raw_lookup  = dict(rrf_scores)
                out = []
                for uuid, raw_rrf in rrf_scores.items():
                    if raw_rrf <= 0.0 or uuid not in items_by_uuid:
                        continue
                    out.append((items_by_uuid[uuid], raw_rrf))
                out.sort(key=lambda t: t[1], reverse=True)
                return out
        elif mode == u"fuzzy":
            def search_fn(q, ds, searchables=None, dense_query=None):
                # Fuzzy = the LIVE Connector's fuzzy mode exactly: the
                # _fuzzy_match_inner binary filter, then survivors RANKED BY
                # BM25F via the shared _rerank_with_bm25f → bm25f_score path
                # (same params + calibrator the Connector uses). It falls back
                # to _fuzzy_score order only when corpus stats aren't loaded.
                # Fuzzy also honours BIM-context enrichment - NOT because it
                # helps (it does not: tools/ladder_modes.md showed it HURTS the
                # lexical path) but so the metrics table can show
                # name-only | +context and document the noise comprehensively.
                # The enriched string drives BOTH the filter and the BM25F
                # re-rank (matching the offline --fuzzy-only test). With BIM
                # context OFF, dense_query is None ⇒ bare name ⇒ the standard
                # fuzzy mode, so the baseline pass is the true Connector fuzzy.
                fq = dense_query or q
                return self._rerank_with_bm25f(
                    fq, search_fuzzy(fq, ds, searchables=searchables))
        else:
            def search_fn(q, ds, searchables=None, dense_query=None):
                # Regex is a pattern mode - the query is a regex literal, not
                # free text - so BIM-context enrichment is never applicable.
                # Always name-only (single pass).
                return search_regex(q, ds, searchables=searchables)
        TOPK = (1, 3, 5, 10, 20)
        sums    = {k: 0 for k in TOPK}
        any_hit = 0
        denom   = 0
        per_query = []   # list of dicts
        # New metrics (Phase 0 v8 - fair-scoring instrumentation):
        #   sums_corr / any_hit_corr     R@K when only the *canonical*
        #                                correct_uuid counts as a hit
        #                                (Section B in the report)
        #   rr_per_q / rr_per_q_corr     per-query 1/rank for MRR
        #                                (acceptable + correct variants)
        #   ndcg_per_q                   per-query graded nDCG@10
        #                                (correct=rel 2, acceptable=rel 1)
        #   hits_per_q / hits_per_q_corr per-query 0/1 hit vectors at
        #                                R@1 and R@10 - fed to McNemar's
        #                                test at report-build time
        # All arrays are appended to in the same per-query order across
        # every mode so the McNemar pairs line up across modes.
        sums_corr       = {k: 0 for k in TOPK}
        any_hit_corr    = 0
        rr_per_q        = []
        rr_per_q_corr   = []
        ndcg_per_q      = []
        hits_per_q      = {1: [], 10: []}
        hits_per_q_corr = {1: [], 10: []}
        # Phase 1.7b: aggregate the reranker fallback warnings. The
        # previous one-warning-per-query implementation produced a wall
        # of red text in the log on UTF-8-quirky datasets; the user
        # would rather see a single summary at the end.
        rerank_failures = 0
        rerank_first_error = None

        t0 = time.time()
        # BIM-context enrichment applies to the dense modes AND to fuzzy
        # (the latter only to document its noise - see the fuzzy search_fn
        # above). The GT entry supplies the fields (the same data ExtractContext
        # writes live). BM25F always uses the bare name; regex is never enriched.
        context_modes = (u"semantic", u"semantic_reranked", u"hybrid",
                         u"hybrid_reranked", u"fuzzy")
        context_on  = bool(context_on) and (mode in context_modes)
        if mode in context_modes:
            # Diagnostic: prove the resolved context state at run time. If
            # `on=False` or `dq == query` while you expected enrichment, the
            # reads failed - this line localises it without a debugger.
            _smp = None
            for _q, _gt in pairs:
                if _gt:
                    _smp = (_q, self._enrich_query(_gt) if context_on else None)
                    break
            logger.info(u"[bim-context] mode={0} on={1} fields={2} | "
                        u"sample query={3!r} dq={4!r}".format(
                            mode, context_on, self._context_fields(),
                            _smp[0] if _smp else None,
                            _smp[1] if _smp else None))
            # This run reflects the current context state → clear the stale flag
            # and refresh the inline preview (drops the "re-run to apply" hint).
            self._context_stale = False
            self._refresh_context_preview()
        for query, gt in pairs:
            dq = self._enrich_query(gt) if context_on else None
            ranked = search_fn(query, self._datasets,
                               searchables=self._searchables, dense_query=dq)
            rrf_meta = None
            if mode == u"semantic" or mode == u"semantic_reranked":
                # Convert raw cosine → calibrated confidence so the
                # displayed Score % is on the same [0,1] scale as the
                # BM25F modes. The Min Score slider then has identical
                # semantics across modes.
                ranked = [(ds, semantic_cc.apply(raw)) for (ds, raw) in ranked]
                if mode == u"semantic_reranked":
                    # Fusion-free ablation: cross-encoder rerank the top-K of
                    # the pure-cosine list. Same sidecar + top-K as
                    # hybrid_reranked, just a different first-pass ranker.
                    ranked, rerank_meta, failed = self._rerank_topk(
                        ranked, query, dq, reranker_cc)
                    rrf_meta = rerank_meta
                    if failed:
                        rerank_failures += 1
                        if rerank_first_error is None:
                            rerank_first_error = (
                                self._reranker_client.last_error
                                if self._reranker_client else u"no client")
            elif mode == u"hybrid" or mode == u"hybrid_reranked":
                # `search_fn` already returned (item, raw_rrf). Normalise
                # to [0, 1] (rank-1 in both rankers → 1.0) and apply the
                # hybrid calibrator. Stash the per-ranker ranks alongside
                # so the per-row tooltip can show "BM25F: #2, Semantic: #1".
                rrf_meta = {
                    u"bm25f_ranks": getattr(self, "_curr_rrf_bm25f_ranks", {}) or {},
                    u"sem_ranks":   getattr(self, "_curr_rrf_sem_ranks", {}) or {},
                    u"raw_rrf":     getattr(self, "_curr_rrf_raw_lookup", {}) or {},
                }
                norm_list = []
                for (ds, raw_rrf) in ranked:
                    norm = (raw_rrf / rrf_max) if rrf_max > 0 else 0.0
                    if norm > 1.0:
                        norm = 1.0
                    norm_list.append((ds, hybrid_cc.apply(norm), raw_rrf, norm))
                # ranked_all stays as (item, calibrated_score) so the
                # downstream `(it, sc) in filtered` consumers don't need
                # changes; the raw values are preserved on rrf_meta.
                ranked = [(ds, sc) for (ds, sc, _r, _n) in norm_list]
                if mode == u"hybrid_reranked":
                    # Cross-encoder rerank pass on the top-K. We slice
                    # the head, call the sidecar, and rebuild the
                    # ranked list with the reranked head followed by
                    # the unchanged tail.
                    head_n = RERANKER_TOP_K
                    head, tail = ranked[:head_n], ranked[head_n:]
                    docs = []
                    for (ds, _sc) in head:
                        hay = build_searchable(ds, lang=self._current_lang())
                        docs.append(build_semantic_haystack(hay) or
                                    ds.get(u"name", u""))
                    rr_logits = None
                    if self._reranker_client is not None and docs:
                        rr_logits = self._reranker_client.rerank(dq or query, docs)
                    if rr_logits and len(rr_logits) == len(head):
                        # Re-score head with the reranker calibrator;
                        # leave tail at its RRF score (much lower) so
                        # head/tail remain monotonically decreasing.
                        head_before_rank = {head[i][0].get(u"uuid", u""): i + 1
                                            for i in range(len(head))}
                        head_rescored = []
                        for i, (ds, _sc) in enumerate(head):
                            logit = rr_logits[i]
                            head_rescored.append((ds, reranker_cc.apply(logit),
                                                  logit))
                        # Rank by the RAW cross-encoder logit (t[2]), NOT the
                        # calibrated score (t[1]). Ordering must use the
                        # model's raw relevance signal so it is independent of
                        # the calibrator's shape (a degenerate/flat calibrator
                        # must never collapse the rerank ordering). Matches
                        # tools/phase1_benchmark.py and the live Connector.
                        head_rescored.sort(
                            key=lambda t: t[2], reverse=True)
                        # rrf_meta stores the rerank logits + before/after
                        # ranks; consumed by `_format_hybrid_reranked_tooltip_validation`.
                        rerank_logits = {}
                        rerank_before = {}
                        rerank_after  = {}
                        for j, (ds, _sc, logit) in enumerate(head_rescored):
                            uuid = ds.get(u"uuid", u"")
                            rerank_logits[uuid] = logit
                            rerank_before[uuid] = head_before_rank.get(uuid)
                            rerank_after[uuid]  = j + 1
                        rrf_meta[u"rerank_logits"] = rerank_logits
                        rrf_meta[u"rerank_before"] = rerank_before
                        rrf_meta[u"rerank_after"]  = rerank_after
                        head = [(ds, sc) for (ds, sc, _l) in head_rescored]
                    else:
                        rerank_failures += 1
                        if rerank_first_error is None:
                            rerank_first_error = (
                                self._reranker_client.last_error
                                if self._reranker_client else u"no client")
                    ranked = head + tail
            else:
                # Replace the legacy `_fuzzy_score` values with BM25F so the
                # Validation tool reports the same scores the live Connector
                # grid and the offline benchmark report. Recall@K is computed
                # on the same candidate set - the re-ranking only changes
                # the *order* (and therefore rank_of_first_hit and Recall@K).
                ranked = self._rerank_with_bm25f(query, ranked)
            ranked_all  = ranked
            uuids_all   = [it.get("uuid", u"") for (it, _) in ranked_all]

            entry = {
                "query": query,
                "gt":    gt,
                "ranked_all": ranked_all,
                "uuids_all":  uuids_all,
                "ranked_len": len(ranked),
                "hit_rank": None,
                "is_match_entry": False,
                "best_score": (ranked_all[0][1] if ranked_all else None),
                "best_uuid":  (ranked_all[0][0].get("uuid", u"") if ranked_all else u""),
                "best_name":  (ranked_all[0][0].get("name", u"") if ranked_all else u""),
                "rrf_meta":   rrf_meta,   # only populated for hybrid mode
            }
            if gt is not None and gt.get("label") == u"match":
                acceptable_only = list(gt.get("acceptable_uuids", []) or [])
                correct_uuid    = gt.get("correct_uuid", u"") or u""
                acceptable = set(acceptable_only)
                if correct_uuid:
                    acceptable.add(correct_uuid)
                rk = rank_of_first_hit(ranked, acceptable)
                entry["acceptable"] = acceptable
                entry["hit_rank"]   = rk if rk <= len(ranked) else None
                entry["is_match_entry"] = True
                denom += 1
                for k in TOPK:
                    if topk_hit(ranked, acceptable, k):
                        sums[k] += 1
                if entry["hit_rank"] is not None:
                    any_hit += 1
                # Exact-correct R@K - only the canonical correct_uuid counts.
                rk_corr = None
                if correct_uuid:
                    for i, (ds, _sc) in enumerate(ranked, start=1):
                        if ds.get(u"uuid", u"") == correct_uuid:
                            rk_corr = i
                            break
                entry["hit_rank_correct"] = rk_corr
                for k in TOPK:
                    if rk_corr is not None and rk_corr <= k:
                        sums_corr[k] += 1
                if rk_corr is not None:
                    any_hit_corr += 1
                # Ranking quality (MRR + nDCG@10) and McNemar accumulators.
                rr_per_q.append(
                    1.0 / float(rk) if entry["hit_rank"] is not None else 0.0)
                rr_per_q_corr.append(
                    1.0 / float(rk_corr) if rk_corr is not None else 0.0)
                ndcg_per_q.append(
                    ndcg_at_k(uuids_all, correct_uuid, acceptable_only, k=10))
                for kk in (1, 10):
                    hits_per_q[kk].append(
                        1 if topk_hit(ranked, acceptable, kk) else 0)
                    hits_per_q_corr[kk].append(
                        1 if (rk_corr is not None and rk_corr <= kk) else 0)
            else:
                entry["acceptable"] = set()
                entry["hit_rank_correct"] = None
            per_query.append(entry)

        duration = time.time() - t0

        # Phase 1.7b: emit ONE aggregated rerank-failure summary instead
        # of N per-query warnings. Quotes the first observed error so
        # the user can diagnose without trawling the full log.
        if rerank_failures:
            logger.warning(
                u"Reranker fell back to RRF for {0} of {1} queries "
                u"[search_helpers v{2}]. First error: {3}. If R@K "
                u"numbers look identical to the Hybrid row, the reranker "
                u"effectively no-opped — check the sidecar log at "
                u"LCiA_Extension_Cache/reranker_service.log. If the "
                u"version above is not '1.7d' or later, restart Revit "
                u"so IronPython reloads search_helpers.".format(
                    rerank_failures, len(pairs), _SH_VER,
                    rerank_first_error or u"(none captured)"))

        summary = {
            # Acceptable-set Recall@K (existing - preserved):
            "r1":       sums[1],
            "r3":       sums[3],
            "r5":       sums[5],
            "r10":      sums[10],
            "r20":      sums[20],
            "rall":     any_hit,
            "denom":    denom,
            "duration": duration,
            "n_scope":  len(pairs),
            # 95% Wilson binomial CI on each acceptable-set cell
            # (Section A - reports a (point, lo, hi) triple):
            "r1_ci":    wilson_ci(sums[1],  denom),
            "r3_ci":    wilson_ci(sums[3],  denom),
            "r5_ci":    wilson_ci(sums[5],  denom),
            "r10_ci":   wilson_ci(sums[10], denom),
            "r20_ci":   wilson_ci(sums[20], denom),
            "rall_ci":  wilson_ci(any_hit,  denom),
            # Exact-correct Recall@K - only the canonical correct_uuid
            # counts as a hit (Section B):
            "r1_corr":   sums_corr[1],
            "r3_corr":   sums_corr[3],
            "r5_corr":   sums_corr[5],
            "r10_corr":  sums_corr[10],
            "r20_corr":  sums_corr[20],
            "rall_corr": any_hit_corr,
            "r1_corr_ci":   wilson_ci(sums_corr[1],  denom),
            "r3_corr_ci":   wilson_ci(sums_corr[3],  denom),
            "r5_corr_ci":   wilson_ci(sums_corr[5],  denom),
            "r10_corr_ci":  wilson_ci(sums_corr[10], denom),
            "r20_corr_ci":  wilson_ci(sums_corr[20], denom),
            "rall_corr_ci": wilson_ci(any_hit_corr,  denom),
            # Ranking quality with 1000-replicate bootstrap 95% CI
            # (Section C - Burges 2005 nDCG@10 graded relevance):
            "mrr":      bootstrap_ci(rr_per_q),       # (point, lo, hi)
            "mrr_corr": bootstrap_ci(rr_per_q_corr),
            "ndcg10":   bootstrap_ci(ndcg_per_q),
            # Per-query 0/1 hit vectors at R@1 and R@10 retained for
            # cross-mode pairwise McNemar tests at report-build time
            # (Section D - only valid because every mode iterates the
            # same `pairs` list in the same order):
            "hits_per_q_acceptable": hits_per_q,
            "hits_per_q_correct":    hits_per_q_corr,
        }
        return summary, per_query

    # Shared 5-tier value scale for EVERY number in the metric table - single-
    # value cells AND both halves of the before|after cells, Recall% directly
    # and MRR/nDCG ×100. One absolute system so a colour means the same thing
    # in every column: <15 red, 15–30 orange, 30–45 amber, 45–70 green,
    # ≥70 emerald ("excellent"); no data → subtle grey.
    _TIER_HEX = (
        (15.0, u"#DC2626"),   # red
        (30.0, u"#EA580C"),   # orange
        (45.0, u"#CA8A04"),   # amber
        (70.0, u"#16A34A"),   # green
    )
    _TIER_TOP_HEX = u"#047857"   # emerald (≥70)

    def _pct_brush(self, pct):
        """Brush for a 0–100 value on the shared 5-tier scale (Recall% direct,
        MRR/nDCG passed as ×100). None → subtle grey. Brushes are built once
        and frozen so every cell shares the same immutable instances."""
        if pct is None:
            return self.Resources["SubtleFg"]
        cache = getattr(self, "_tier_brush_cache", None)
        if cache is None:
            def _b(h):
                br = SolidColorBrush(ColorConverter.ConvertFromString(h))
                br.Freeze()
                return br
            cache = [(edge, _b(h)) for (edge, h) in self._TIER_HEX]
            cache.append((None, _b(self._TIER_TOP_HEX)))
            self._tier_brush_cache = cache
        for edge, brush in cache:
            if edge is None or pct < edge:
                return brush
        return cache[-1][1]

    @staticmethod
    def _set_split_cell(tb, before, after, before_brush, after_brush, sep_brush):
        """Render a 'before | after' cell with EACH half coloured by its own
        value-tier brush (one colour system across the whole table); the ' | '
        separator uses sep_brush. WPF Inlines let the runs differ in colour."""
        from System.Windows.Documents import Run
        tb.Inlines.Clear()
        r1 = Run(before); r1.Foreground = before_brush
        sep = Run(u" | "); sep.Foreground = sep_brush
        r2 = Run(after); r2.Foreground = after_brush
        tb.Inlines.Add(r1); tb.Inlines.Add(sep); tb.Inlines.Add(r2)

    def _explain_score(self, query, haystack_text, score, mode,
                       entry=None, uuid=None):
        """Return a multi-line tooltip explaining the score of one
        (query, candidate) pair for the active mode.

        BM25F modes (regex, fuzzy) delegate to `search_helpers.explain_score`
        - the single source of truth shared with the live Connector grid
        and `tools/phase0_benchmark.py`. Semantic and hybrid modes have
        their own formatters because cosine has no per-term decomposition
        and RRF combines ranks rather than scores.

        `entry` and `uuid` are only consumed by the hybrid branch (it
        looks up the per-ranker rank stashed on the entry under
        `rrf_meta`). Returns None when score is None."""
        if score is None:
            return None
        if mode == u"semantic":
            return self._format_semantic_tooltip_validation(
                query, haystack_text, score)
        if mode == u"hybrid":
            return self._format_hybrid_tooltip_validation(
                query, haystack_text, score, entry, uuid)
        if mode == u"hybrid_reranked" or mode == u"semantic_reranked":
            return self._format_hybrid_reranked_tooltip_validation(
                query, haystack_text, score, entry, uuid)
        if self._corpus_stats is None:
            return None
        try:
            nl = (query or u"").lower()
            if isinstance(haystack_text, dict):
                hay = haystack_text
            else:
                hay = {u"name": (haystack_text or u"").lower(),
                       u"classification": u""}
            try:
                expanded = expand_query_with_typos(nl, self._corpus_stats)
            except Exception:
                expanded = None
            ex = explain_score(nl, hay, self._corpus_stats,
                               params=self._bm25_params,
                               calibrator=self._calibrator,
                               expanded_query=expanded,
                               confidence=self._confidence_cals.get(u"bm25f"))
            return self._format_bm25f_tooltip(ex, mode)
        except Exception as ex:
            return (u"Score {:.0f}% — BM25F text-match.\n"
                    u"(Breakdown unavailable: {})".format(score * 100.0, ex))

    def _format_semantic_tooltip_validation(self, query, haystack_text, score):
        """Render the semantic-mode Score tooltip for the Validation grid.

        Mirrors the structure of the live Connector's
        `_format_semantic_tooltip` (formula, embedded text, model,
        cosine scale, calibrator anchors) so the explanation matches
        across the two UIs. The raw cosine is omitted here because
        the Validation pushbutton's per-row score-column carries only
        the calibrated confidence; the calibrator-anchor table below
        tells the user roughly which cosine corresponds to the
        displayed Score %.
        """
        try:
            cc = (self._confidence_cals.get(u"semantic")
                  or ConfidenceCalibrator(mode=u"semantic"))
            anchors = [0.40, 0.50, 0.60, 0.70, 0.80]
            pct = {a: cc.apply(a) * 100.0 for a in anchors}
            model = (self._embedding_index.model
                     if self._embedding_index is not None else u"bge-m3")
            dim = (self._embedding_index.dim
                   if self._embedding_index is not None else 1024)
            base_url = (getattr(self._ollama_client, "base_url", None)
                        or u"http://localhost:11434")

            lines = []
            lines.append(u"Score: {0:.0f}%   ({1})".format(
                score * 100.0, cc.source()))
            lines.append(
                u"(Calibrated cosine confidence; raw cosine in [-1, 1]"
                u" maps via the sigmoid below.)")

            lines.append(u"")
            lines.append(u"Formula")
            lines.append(
                u"    raw   = <embed(query), embed(dataset)>"
                u"     # dot product over L2-norm vectors")
            lines.append(
                u"    score = ConfidenceCalibrator[semantic].apply(raw)"
                u"   # mode-agnostic confidence in [0, 1]")

            lines.append(u"")
            lines.append(u"Embedded text")
            if query:
                lines.append(u"    Query:    \"{0}\"".format(query))
            if haystack_text:
                if isinstance(haystack_text, dict):
                    hay_text = build_semantic_haystack(haystack_text) or u""
                else:
                    hay_text = unicode(haystack_text) if haystack_text else u""
                if hay_text:
                    if len(hay_text) > 240:
                        hay_text = hay_text[:240] + u"..."
                    lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            lines.append(u"")
            lines.append(u"Model")
            lines.append(u"    {0} (dim={1})".format(model, dim))
            lines.append(u"    Served by Ollama @ {0}".format(base_url))

            lines.append(u"")
            lines.append(u"Cosine rule-of-thumb (bge-m3, technical DE/EN text)")
            lines.append(u"    >= 0.85       near-duplicate / same dataset")
            lines.append(u"    0.70 - 0.85   strongly related (synonyms, paraphrases)")
            lines.append(u"    0.50 - 0.70   related (overlapping concepts)")
            lines.append(u"    0.30 - 0.50   weakly related (broad category overlap)")
            lines.append(u"    < 0.30        essentially unrelated")

            lines.append(u"")
            lines.append(u"Confidence mapping at the active calibrator")
            lines.append(
                u"    cos 0.40 -> {0:>2.0f}%       cos 0.50 -> {1:>2.0f}%".format(
                    pct[0.40], pct[0.50]))
            lines.append(
                u"    cos 0.60 -> {0:>2.0f}%       cos 0.70 -> {1:>2.0f}%".format(
                    pct[0.60], pct[0.70]))
            lines.append(
                u"    cos 0.80 -> {0:>2.0f}%".format(pct[0.80]))

            return u"\n".join(lines)
        except Exception as ex:
            return u"Semantic score breakdown unavailable: {}".format(ex)

    def _format_hybrid_tooltip_validation(self, query, haystack_text, score,
                                          entry, uuid):
        """Render the Score tooltip for a hybrid-mode (RRF-fused) row.

        Looks up the per-ranker rank from `entry["rrf_meta"]` (stashed by
        `_run`) so it can show the same "BM25F: #2, Semantic: #1"
        provenance the live Connector tooltip shows. Mirrors the live
        Connector's `_format_hybrid_tooltip` layout."""
        try:
            cc = (self._confidence_cals.get(u"hybrid")
                  or ConfidenceCalibrator(mode=u"hybrid"))
            meta = (entry or {}).get(u"rrf_meta") or {}
            bm25f_rank = (meta.get(u"bm25f_ranks") or {}).get(uuid)
            sem_rank   = (meta.get(u"sem_ranks")   or {}).get(uuid)
            raw_rrf    = (meta.get(u"raw_rrf")     or {}).get(uuid, 0.0)
            max_rrf    = rrf_max_score(2, k=HYBRID_RRF_K)
            norm_rrf   = (raw_rrf / max_rrf) if max_rrf > 0 else 0.0
            if norm_rrf > 1.0:
                norm_rrf = 1.0
            bm25f_contrib = (1.0 / (HYBRID_RRF_K + bm25f_rank)) if bm25f_rank else 0.0
            sem_contrib   = (1.0 / (HYBRID_RRF_K + sem_rank))   if sem_rank   else 0.0

            def rank_str(r):
                return u"#{0}".format(r) if r else u"(absent)"

            lines = []
            lines.append(u"Score: {0:.0f}%   ({1})".format(
                score * 100.0, cc.source()))
            lines.append(
                u"Raw RRF = {0:.4f}     Normalised = {1:.4f}"
                u"   (1.0 = rank-1 in both rankers)".format(raw_rrf, norm_rrf))

            lines.append(u"")
            lines.append(u"Per-ranker contributions")
            lines.append(u"    BM25F:     {0:<10} -> 1/(k+r) = {1:.4f}".format(
                rank_str(bm25f_rank), bm25f_contrib))
            lines.append(u"    Semantic:  {0:<10} -> 1/(k+r) = {1:.4f}".format(
                rank_str(sem_rank), sem_contrib))

            lines.append(u"")
            lines.append(u"Formula   (Cormack, Clarke & Buttcher, SIGIR 2009)")
            lines.append(
                u"    rrf(d)  = sum_r  1 / (k + rank_r(d))"
                u"     # k = {0}, r = ranker".format(HYBRID_RRF_K))
            lines.append(
                u"    norm(d) = rrf(d) / (R / (k + 1))"
                u"             # R = 2 rankers")
            lines.append(
                u"    score   = ConfidenceCalibrator[hybrid].apply(norm)")

            if query or haystack_text:
                lines.append(u"")
                lines.append(u"Embedded text")
                if query:
                    lines.append(u"    Query:    \"{0}\"".format(query))
                if haystack_text:
                    if isinstance(haystack_text, dict):
                        hay_text = build_semantic_haystack(haystack_text) or u""
                    else:
                        hay_text = unicode(haystack_text) if haystack_text else u""
                    if hay_text:
                        if len(hay_text) > 240:
                            hay_text = hay_text[:240] + u"..."
                        lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            return u"\n".join(lines)
        except Exception as ex:
            return u"Hybrid score breakdown unavailable: {}".format(ex)

    def _format_hybrid_reranked_tooltip_validation(self, query, haystack_text,
                                                    score, entry, uuid):
        """Tooltip for a Hybrid+Rerank row. Combines the RRF context
        (BM25F rank, semantic rank, raw RRF) with the cross-encoder
        rerank logit + rank-before/after for the head rows."""
        try:
            cc = (self._confidence_cals.get(u"reranker")
                  or ConfidenceCalibrator(mode=u"reranker"))
            meta = (entry or {}).get(u"rrf_meta") or {}
            bm25f_rank   = (meta.get(u"bm25f_ranks") or {}).get(uuid)
            sem_rank     = (meta.get(u"sem_ranks")   or {}).get(uuid)
            raw_rrf      = (meta.get(u"raw_rrf")     or {}).get(uuid, 0.0)
            logit        = (meta.get(u"rerank_logits") or {}).get(uuid)
            rank_before  = (meta.get(u"rerank_before") or {}).get(uuid)
            rank_after   = (meta.get(u"rerank_after")  or {}).get(uuid)
            # No RRF ranks ⇒ the fusion-free Semantic+Rerank ablation
            # (first pass is pure cosine, not RRF).
            is_semrr = (bm25f_rank is None and sem_rank is None)

            def rank_str(r):
                return u"#{0}".format(r) if r else u"(absent)"

            lines = []
            if logit is not None:
                lines.append(u"Score: {0:.0f}%   ({1})".format(
                    score * 100.0, cc.source()))
                tag = u""
                if rank_after is not None and rank_before is not None:
                    if rank_after == rank_before:
                        tag = u"  (rank #{0}, unchanged by reranker)".format(rank_after)
                    else:
                        delta = rank_before - rank_after
                        arrow = u"↑" if delta > 0 else u"↓"
                        tag = u"  (rank #{0}, was #{1} after RRF, {2}{3})".format(
                            rank_after, rank_before, arrow, abs(delta))
                lines.append(u"Rerank logit = {0:+.3f}{1}".format(logit, tag))
            else:
                # Tail row - not reranked, still first-pass-scored
                if is_semrr:
                    lines.append(u"Score: {0:.0f}%   (cosine, not reranked — "
                                 u"tail row)".format(score * 100.0))
                else:
                    lines.append(u"Score: {0:.0f}%   (RRF, not reranked — tail row)".format(
                        score * 100.0))
                    lines.append(u"Raw RRF = {0:.4f}   (head row reranker did not run on this row)".format(
                        raw_rrf))

            lines.append(u"")
            if is_semrr:
                lines.append(u"First-pass (pure cosine — Semantic, no fusion)")
            else:
                lines.append(u"First-pass (Reciprocal Rank Fusion)")
                lines.append(u"    BM25F:     {0}".format(rank_str(bm25f_rank)))
                lines.append(u"    Semantic:  {0}".format(rank_str(sem_rank)))
                lines.append(u"    Raw RRF:   {0:.4f}".format(raw_rrf))

            lines.append(u"")
            if is_semrr:
                lines.append(u"Formula  (Semantic + Cross-encoder rerank)")
                lines.append(u"    first    = cosine(query, dataset)")
                lines.append(
                    u"    head     = top-{0} by cosine".format(RERANKER_TOP_K))
            else:
                lines.append(u"Formula  (Hybrid + Cross-encoder rerank)")
                lines.append(
                    u"    rrf(d)   = sum_r  1 / (k + rank_r(d))     # k = {0}".format(HYBRID_RRF_K))
                lines.append(
                    u"    head     = top-{0} by rrf".format(RERANKER_TOP_K))
            lines.append(
                u"    logit    = CrossEncoder(query, dataset)   # head only")
            lines.append(
                u"    score    = ConfidenceCalibrator[reranker].apply(logit)")

            lines.append(u"")
            lines.append(u"Model")
            model_name = RERANKER_MODEL
            if self._reranker_client is not None and self._reranker_client.model:
                model_name = self._reranker_client.model
            lines.append(u"    {0}".format(model_name))
            lines.append(u"    Served by local sidecar @ {0}".format(RERANKER_BASE_URL))

            if query or haystack_text:
                lines.append(u"")
                lines.append(u"Embedded text")
                if query:
                    lines.append(u"    Query:    \"{0}\"".format(query))
                if haystack_text:
                    if isinstance(haystack_text, dict):
                        hay_text = build_semantic_haystack(haystack_text) or u""
                    else:
                        hay_text = unicode(haystack_text) if haystack_text else u""
                    if hay_text:
                        if len(hay_text) > 240:
                            hay_text = hay_text[:240] + u"..."
                        lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            return u"\n".join(lines)
        except Exception as ex:
            return u"Hybrid+Rerank score breakdown unavailable: {}".format(ex)

    def _format_bm25f_tooltip(self, ex, mode):
        """Render the dict returned by `search_helpers.explain_score` into
        the multi-line tooltip body shown on hover in the Score column."""
        p = ex["params"]
        lines = []
        lines.append(u"Score: {0:.0f}%   ({1})".format(
            ex["final"] * 100.0, ex["calibration"]))
        lines.append(u"Raw BM25F = {0:.3f}".format(ex["raw"]))
        if ex.get("final_calibrated") is not None:
            lines.append(
                u"Calibrated P(correct | filter pass) = {0:.1f}%   "
                u"(population frequency at this raw score; meaningful for "
                u"auto-accept thresholds, NOT a per-query relative confidence)".format(
                    ex["final_calibrated"] * 100.0))
        if mode == u"regex":
            lines.append(u"Mode: Regex — binary filter passed; ranker is BM25F.")
        lines.append(u"Params: k1={k1}, b_name={bn}, b_class={bc}, "
                     u"w_name={wn}, w_class={wc}, alpha_3g={a}".format(
                         k1=p["k1"], bn=p["b_name"], bc=p["b_class"],
                         wn=p["w_name"], wc=p["w_class"], a=p["alpha_3g"]))
        lines.append(u"")
        lines.append(u"Per-term contributions (descending):")
        terms = sorted(ex["per_term"], key=lambda t: t["contrib"], reverse=True)
        for t in terms:
            tag = u""
            if u"via_typo_of" in t:
                tag = u"  (typo of '{0}', d={1})".format(
                    t[u"via_typo_of"], t[u"distance"])
            lines.append(
                u"  {term}{tag}: w={w:.2f}, idf={idf:.2f}, "
                u"tf_name={tn}, tf_class={tc}, contrib={c:.3f}".format(
                    term=t["term"], tag=tag, w=t["weight"], idf=t["idf"],
                    tn=t["tf_name"], tc=t["tf_class"], c=t["contrib"]))
        if ex["trigram_contrib"] > 0:
            lines.append(u"")
            lines.append(u"Trigram contribution: {0:.3f}".format(ex["trigram_contrib"]))
        return u"\n".join(lines)

    def _metric_cells_for(self, mode):
        """Return [(pct_textblock, frac_textblock, key)] for the 6 R@K cells of one mode."""
        if mode == u"regex":
            return [
                (self.text_regex_r1_pct,   self.text_regex_r1_frac,   "r1"),
                (self.text_regex_r3_pct,   self.text_regex_r3_frac,   "r3"),
                (self.text_regex_r5_pct,   self.text_regex_r5_frac,   "r5"),
                (self.text_regex_r10_pct,  self.text_regex_r10_frac,  "r10"),
                (self.text_regex_r20_pct,  self.text_regex_r20_frac,  "r20"),
                (self.text_regex_rall_pct, self.text_regex_rall_frac, "rall"),
            ]
        if mode == u"semantic":
            return [
                (self.text_semantic_r1_pct,   self.text_semantic_r1_frac,   "r1"),
                (self.text_semantic_r3_pct,   self.text_semantic_r3_frac,   "r3"),
                (self.text_semantic_r5_pct,   self.text_semantic_r5_frac,   "r5"),
                (self.text_semantic_r10_pct,  self.text_semantic_r10_frac,  "r10"),
                (self.text_semantic_r20_pct,  self.text_semantic_r20_frac,  "r20"),
                (self.text_semantic_rall_pct, self.text_semantic_rall_frac, "rall"),
            ]
        if mode == u"hybrid":
            return [
                (self.text_hybrid_r1_pct,   self.text_hybrid_r1_frac,   "r1"),
                (self.text_hybrid_r3_pct,   self.text_hybrid_r3_frac,   "r3"),
                (self.text_hybrid_r5_pct,   self.text_hybrid_r5_frac,   "r5"),
                (self.text_hybrid_r10_pct,  self.text_hybrid_r10_frac,  "r10"),
                (self.text_hybrid_r20_pct,  self.text_hybrid_r20_frac,  "r20"),
                (self.text_hybrid_rall_pct, self.text_hybrid_rall_frac, "rall"),
            ]
        if mode == u"hybrid_reranked":
            return [
                (self.text_hybrid_reranked_r1_pct,   self.text_hybrid_reranked_r1_frac,   "r1"),
                (self.text_hybrid_reranked_r3_pct,   self.text_hybrid_reranked_r3_frac,   "r3"),
                (self.text_hybrid_reranked_r5_pct,   self.text_hybrid_reranked_r5_frac,   "r5"),
                (self.text_hybrid_reranked_r10_pct,  self.text_hybrid_reranked_r10_frac,  "r10"),
                (self.text_hybrid_reranked_r20_pct,  self.text_hybrid_reranked_r20_frac,  "r20"),
                (self.text_hybrid_reranked_rall_pct, self.text_hybrid_reranked_rall_frac, "rall"),
            ]
        if mode == u"semantic_reranked":
            return [
                (self.text_semantic_reranked_r1_pct,   self.text_semantic_reranked_r1_frac,   "r1"),
                (self.text_semantic_reranked_r3_pct,   self.text_semantic_reranked_r3_frac,   "r3"),
                (self.text_semantic_reranked_r5_pct,   self.text_semantic_reranked_r5_frac,   "r5"),
                (self.text_semantic_reranked_r10_pct,  self.text_semantic_reranked_r10_frac,  "r10"),
                (self.text_semantic_reranked_r20_pct,  self.text_semantic_reranked_r20_frac,  "r20"),
                (self.text_semantic_reranked_rall_pct, self.text_semantic_reranked_rall_frac, "rall"),
            ]
        return [
            (self.text_fuzzy_r1_pct,   self.text_fuzzy_r1_frac,   "r1"),
            (self.text_fuzzy_r3_pct,   self.text_fuzzy_r3_frac,   "r3"),
            (self.text_fuzzy_r5_pct,   self.text_fuzzy_r5_frac,   "r5"),
            (self.text_fuzzy_r10_pct,  self.text_fuzzy_r10_frac,  "r10"),
            (self.text_fuzzy_r20_pct,  self.text_fuzzy_r20_frac,  "r20"),
            (self.text_fuzzy_rall_pct, self.text_fuzzy_rall_frac, "rall"),
        ]

    def _quality_cells_for(self, mode):
        """Return [(point_tb, ci_tb, summary_key)] for the MRR and nDCG@10
        cells of one mode. Uses getattr because the cell x:Names are
        uniform (`text_{mode}_mrr_pt`/`_mrr_ci`/`_ndcg_pt`/`_ndcg_ci`) and
        a cell may be absent on an older XAML (yields None, skipped)."""
        return [
            (getattr(self, "text_{0}_mrr_pt".format(mode),  None),
             getattr(self, "text_{0}_mrr_ci".format(mode),  None), u"mrr"),
        ]

    _RK_LABEL = {u"r1": u"1", u"r3": u"3", u"r5": u"5",
                 u"r10": u"10", u"r20": u"20", u"rall": u"All"}

    def _recall_cell_tooltip(self, summary, key, hits, corr, denom, base=None):
        """Full numeric detail for one dual-value Recall@K cell, shown on
        hover: both relevance definitions (any-acceptable and
        canonical-correct) with their counts and 95% Wilson CIs. The cell
        face itself shows only the two percentages to stay compact. When a
        name-only `base` summary is present (BIM context ON), the dropped
        before-context detail is appended here so the cell face can stay as
        the compact "before | after" pair."""
        cut = self._RK_LABEL.get(key, key)

        def _ci(triple):
            if not triple:
                return u""
            _p, lo, hi = triple
            return u"  95% CI [{0:.0f}%, {1:.0f}%]".format(100.0 * lo, 100.0 * hi)

        accp = int(round(100.0 * hits / denom)) if denom else 0
        crp  = int(round(100.0 * corr / denom)) if denom else 0
        text = (
            u"Recall@{cut}\n"
            u"any-acceptable:    {h}/{d} = {ap}%{aci}\n"
            u"canonical-correct: {c}/{d} = {cp}%{cci}\n\n"
            u"top = any acceptable EPD appears in the top {cut}\n"
            u"bottom = the single canonical correct EPD only"
        ).format(
            cut=cut, h=hits, d=denom, c=corr, ap=accp, cp=crp,
            aci=_ci(summary.get(key + "_ci")),
            cci=_ci(summary.get(key + "_corr_ci")))
        if base and base.get("denom"):
            bd = base["denom"]
            b_acc = int(round(100.0 * base.get(key, 0) / bd))
            text += (
                u"\n\ncell face = name-only | +BIM context\n"
                u"  name-only:    {0}/{1} = {2}%{3}\n"
                u"  +BIM context: {4}/{5} = {6}%   ({7:+d} pp)"
            ).format(base.get(key, 0), bd, b_acc, _ci(base.get(key + "_ci")),
                     hits, denom, accp, accp - b_acc)
        return text

    def _quality_cell_tooltip(self, summary, qkey, pt, lo, hi, base=None):
        """Hover detail for an MRR / nDCG@10 cell (point + 95% bootstrap CI
        + a one-line definition). When a name-only `base` summary is present
        (BIM context ON), the cell face reads "before | after" and the
        baseline point + CI + delta are appended here."""
        if qkey == u"mrr":
            lines = [u"MRR (any-acceptable): {0:.3f}  95% CI [{1:.3f}, {2:.3f}]".format(
                pt, lo, hi)]
            mc = summary.get("mrr_corr")
            if mc:
                lines.append(
                    u"MRR (canonical-correct): {0:.3f}  95% CI [{1:.3f}, {2:.3f}]".format(
                        mc[0], mc[1], mc[2]))
            lines.append(u"")
            lines.append(
                u"Mean Reciprocal Rank = mean of 1 / (rank of first hit) over all "
                u"queries. Sensitive to rank position, not just presence. "
                u"1.000 = every query's hit is at rank 1.")
            text = u"\n".join(lines)
        else:
            text = (
                u"nDCG@10: {0:.3f}  95% CI [{1:.3f}, {2:.3f}]\n\n"
                u"Normalised discounted cumulative gain over the top 10, graded "
                u"relevance (canonical correct = 2, acceptable = 1). Rewards ranking "
                u"the canonical EPD first; far less inflated by broad acceptable sets "
                u"than any-acceptable Recall. 1.000 = ideal ordering."
            ).format(pt, lo, hi)
        if base:
            bt = base.get(qkey)
            if bt:
                text += (
                    u"\n\ncell face = name-only | +BIM context\n"
                    u"  name-only:    {0:.3f}  95% CI [{1:.3f}, {2:.3f}]\n"
                    u"  +BIM context: {3:.3f}   ({4:+.3f})"
                ).format(bt[0], bt[1], bt[2], pt, pt - bt[0])
        return text

    def _duration_tooltip(self, summary, base):
        """Explain what the Time cell measures so it can't be mistaken for the
        click-to-result wall-clock. It is the per-query SEARCH LOOP of a single
        pass (`t0..duration` in `_run_pass`), measured with the model + reranker
        already warm. It deliberately excludes one-time model/sidecar warm-up
        and the post-run rendering (bootstrap CIs, McNemar, results grid) - and,
        when BIM context is on, the separate name-only baseline pass that also
        runs (only the enriched pass is shown). So the real wall time is
        markedly longer; this warm, single-pass number is the per-query latency
        you would actually see in production (one pass, no double-run)."""
        try:
            enr = float(summary.get("duration", 0.0) or 0.0)
            n = summary.get("n_scope") or summary.get("denom") or 0
            per_q = (enr / n) if n else 0.0
            lines = [
                u"{0:.1f} s = the per-query search loop of one pass over {1} "
                u"queries (~{2:.2f} s/query), model + reranker already warm.".format(
                    enr, n, per_q)]
            if base and base.get("denom"):
                bdur = float(base.get("duration", 0.0) or 0.0)
                lines.append(
                    u"This is the +BIM-context pass. With BIM context on a "
                    u"name-only baseline pass ALSO runs ({0:.1f} s, not shown), "
                    u"so the two passes alone take ~{1:.0f} s.".format(
                        bdur, enr + bdur))
            lines.append(
                u"Excludes one-time model/sidecar warm-up and post-run "
                u"rendering (CIs, McNemar, grid) — the click-to-result "
                u"wall-clock is longer than this. This warm, single-pass "
                u"number is the per-query latency you'd see in production.")
            return u"\n".join(lines)
        except Exception:
            return None

    def _update_metric_cells(self):
        """Refresh all metric cells + per-mode durations + scope line from
        self._summary.

        Three metric groups per row (R@K | MRR | nDCG@10):
          * Recall@K cells are dual-value - top = any-acceptable %,
            bottom = canonical-correct % (only `correct_uuid`). Every
            number is coloured on the shared 5-tier value scale.
          * MRR and nDCG@10 show the point estimate over a 95% bootstrap CI.
        Full n/N + CIs live in each cell's ToolTip so the grid stays
        compact. Nothing is computed here - every value is read from
        self._summary, the same dict the HTML/Markdown exports consume."""
        duration_lookup = {
            u"regex":           self.text_regex_duration,
            u"fuzzy":           self.text_fuzzy_duration,
            u"semantic":          getattr(self, "text_semantic_duration", None),
            u"semantic_reranked": getattr(self, "text_semantic_reranked_duration", None),
            u"hybrid":            getattr(self, "text_hybrid_duration", None),
            u"hybrid_reranked":   getattr(self, "text_hybrid_reranked_duration", None),
        }
        subtle = self.Resources["SubtleFg"]
        for mode in (u"regex", u"fuzzy", u"semantic", u"hybrid",
                     u"hybrid_reranked", u"semantic_reranked"):
            summary = self._summary.get(mode)
            base    = self._summary_baseline.get(mode)
            base_ok = bool(base and base.get("denom"))
            duration_tb = duration_lookup.get(mode)
            if duration_tb is None:
                continue
            if not summary or summary.get("denom", 0) == 0:
                for pct_tb, frac_tb, _key in self._metric_cells_for(mode):
                    pct_tb.Text       = u"—"
                    pct_tb.Foreground = subtle
                    pct_tb.ToolTip    = None
                    frac_tb.Text      = u""
                    frac_tb.ToolTip   = None
                    # Collapse (not just blank) the empty bottom line so the
                    # cell is a single centered line that aligns with the
                    # vertically-centred mode label - an empty TextBlock still
                    # reserves a line, which floated the "-" above the label.
                    frac_tb.Visibility = Visibility.Collapsed
                for pt_tb, ci_tb, _qk in self._quality_cells_for(mode):
                    if pt_tb is not None:
                        pt_tb.Text = u"—"; pt_tb.Foreground = subtle; pt_tb.ToolTip = None
                    if ci_tb is not None:
                        ci_tb.Text = u""; ci_tb.ToolTip = None
                        ci_tb.Visibility = Visibility.Collapsed
                if summary:
                    duration_tb.Text = u"{:.1f} s".format(summary.get("duration", 0.0))
                    duration_tb.ToolTip = self._duration_tooltip(summary, None)
                else:
                    duration_tb.Text = u"—"
                    duration_tb.ToolTip = None
                continue
            denom = summary["denom"]
            # Group 1 - dual-value Recall@K (acceptable % top, correct % bottom).
            # With a name-only baseline present (dense mode + BIM context ON),
            # each line reads "before | after".
            for pct_tb, frac_tb, key in self._metric_cells_for(mode):
                hits = summary[key]
                pct  = (100.0 * hits / denom) if denom else 0.0
                corr     = summary.get(key + "_corr", 0)
                corr_pct = (100.0 * corr / denom) if denom else 0.0
                if base_ok:
                    bd = base["denom"]
                    b_pct  = 100.0 * base.get(key, 0) / bd
                    b_corr = 100.0 * base.get(key + "_corr", 0) / bd
                    # One colour system: each half (before AND after) is
                    # coloured by its own value tier; the separator stays grey.
                    self._set_split_cell(
                        pct_tb, u"{}%".format(int(round(b_pct))),
                        u"{}%".format(int(round(pct))),
                        self._pct_brush(b_pct), self._pct_brush(pct), subtle)
                    self._set_split_cell(
                        frac_tb, u"{}%".format(int(round(b_corr))),
                        u"{}%".format(int(round(corr_pct))),
                        self._pct_brush(b_corr), self._pct_brush(corr_pct), subtle)
                else:
                    pct_tb.Text  = u"{}%".format(int(round(pct)))
                    pct_tb.Foreground = self._pct_brush(pct)
                    frac_tb.Text = u"{}%".format(int(round(corr_pct)))
                    frac_tb.Foreground = self._pct_brush(corr_pct)
                frac_tb.Visibility = Visibility.Visible
                tip = self._recall_cell_tooltip(
                    summary, key, hits, corr, denom,
                    base=(base if base_ok else None))
                pct_tb.ToolTip  = tip
                frac_tb.ToolTip = tip
            # Groups 2 & 3 - MRR and nDCG@10 (point estimate + 95% bootstrap CI)
            for pt_tb, ci_tb, qkey in self._quality_cells_for(mode):
                if pt_tb is None:
                    continue
                triple = summary.get(qkey)
                if not triple:
                    pt_tb.Text = u"—"; pt_tb.Foreground = subtle; pt_tb.ToolTip = None
                    if ci_tb is not None:
                        ci_tb.Text = u""; ci_tb.ToolTip = None
                        ci_tb.Visibility = Visibility.Collapsed
                    continue
                pt, lo, hi = triple
                b_triple = base.get(qkey) if base_ok else None
                if b_triple:
                    self._set_split_cell(
                        pt_tb, u"{:.3f}".format(b_triple[0]),
                        u"{:.3f}".format(pt),
                        self._pct_brush(100.0 * b_triple[0]),
                        self._pct_brush(100.0 * pt), subtle)
                else:
                    pt_tb.Text = u"{:.3f}".format(pt)
                    pt_tb.Foreground = self._pct_brush(100.0 * pt)
                if ci_tb is not None:
                    ci_tb.Text = u"[{:.2f}, {:.2f}]".format(lo, hi)
                    ci_tb.Visibility = Visibility.Visible
                tip = self._quality_cell_tooltip(
                    summary, qkey, pt, lo, hi,
                    base=(base if base_ok else None))
                pt_tb.ToolTip = tip
                if ci_tb is not None:
                    ci_tb.ToolTip = tip
            duration_tb.Text = u"{:.1f} s".format(summary["duration"])
            duration_tb.ToolTip = self._duration_tooltip(
                summary, base if base_ok else None)

        # Scope / status line - use the most-recent run's denom/n_scope
        latest = (self._summary.get(self._last_run_mode)
                  if self._last_run_mode else None)
        if latest:
            self.text_scope_summary.Text = (
                u"Scope: {denom} of {n} in scope matched a ground-truth entry  ·  "
                u"{n} material(s) total"
            ).format(denom=latest["denom"], n=latest["n_scope"])
        else:
            self.text_scope_summary.Text = u""
        self._update_significance_panel()

    def _mcnemar_pair_live(self, mode_a, mode_b, k):
        """Two-sided exact McNemar p-value for mode_a vs mode_b on the
        paired per-query hit/miss vectors at R@k (any-acceptable). Same
        computation as the HTML/MD export's Section C4 - reads the
        `hits_per_q_acceptable` vectors stashed on each mode's summary.
        Returns None when either mode lacks a comparable vector."""
        sa = self._summary.get(mode_a) or {}
        sb = self._summary.get(mode_b) or {}
        ha = (sa.get("hits_per_q_acceptable") or {}).get(k) or []
        hb = (sb.get("hits_per_q_acceptable") or {}).get(k) or []
        if not ha or not hb or len(ha) != len(hb):
            return None
        b = sum(1 for i in range(len(ha)) if ha[i] == 1 and hb[i] == 0)
        c = sum(1 for i in range(len(ha)) if ha[i] == 0 and hb[i] == 1)
        return mcnemar_pvalue(b, c)

    _SIG_ORDER = [u"regex", u"fuzzy", u"semantic", u"hybrid",
                  u"hybrid_reranked", u"semantic_reranked"]
    _SIG_SHORT = {u"regex": u"Regex", u"fuzzy": u"Fuzzy", u"semantic": u"Semantic",
                  u"hybrid": u"Hybrid", u"hybrid_reranked": u"Hyb+Rer",
                  u"semantic_reranked": u"Sem+Rer"}

    def _context_sig_lines(self):
        """When context-bearing modes ran with BIM context ON (the dense modes
        plus fuzzy), summarise the name-only → +context effect per mode with a
        paired McNemar p-value at R@1 (the before/after vectors iterate the
        same queries in the same order, so they are correctly paired). Fuzzy is
        included to document that enrichment hurts the lexical path."""
        out = []
        for mode in (u"semantic", u"semantic_reranked", u"hybrid",
                     u"hybrid_reranked", u"fuzzy"):
            base = self._summary_baseline.get(mode)
            enr  = self._summary.get(mode)
            if not base or not enr or not base.get("denom") or not enr.get("denom"):
                continue
            b1 = 100.0 * base.get("r1", 0) / base["denom"]
            e1 = 100.0 * enr.get("r1", 0) / enr["denom"]
            hb = (base.get("hits_per_q_acceptable") or {}).get(1) or []
            he = (enr.get("hits_per_q_acceptable") or {}).get(1) or []
            p = None
            if hb and he and len(hb) == len(he):
                b = sum(1 for i in range(len(hb)) if he[i] == 1 and hb[i] == 0)
                c = sum(1 for i in range(len(hb)) if he[i] == 0 and hb[i] == 1)
                p = mcnemar_pvalue(b, c)
            ps = (u"n/a" if p is None else
                  (u"<.001" if p < 0.001 else u"{:.3f}".format(p)))
            star = u"*" if (p is not None and p < 0.05) else u""
            out.append(u"  {0:<11}{1:>4.0f}% → {2:>3.0f}%   McNemar p={3}{4}".format(
                self._SIG_SHORT.get(mode, mode), b1, e1, ps, star))
        if out:
            out.insert(0, u"BIM context effect (name-only → +context, R@1, "
                          u"any-acceptable):")
        return out

    def _update_significance_panel(self):
        """Render the in-tool significance panel: (1) the BIM-context name-only
        → +context effect per dense mode (when context is ON), and (2) the
        McNemar pairwise p-value matrix (R@1, any-acceptable) across whatever
        modes have been run. Upper triangle only (the test is symmetric);
        '*' flags p < 0.05. Monospaced (Consolas) so the columns align."""
        mtb = getattr(self, "text_mcnemar_matrix", None)
        ntb = getattr(self, "text_mcnemar_note", None)
        if mtb is None:
            return
        short = self._SIG_SHORT
        ctx_lines = self._context_sig_lines()
        modes = [m for m in self._SIG_ORDER
                 if (self._summary.get(m) or {}).get("denom")]
        blocks = []
        if ctx_lines:
            blocks.append(u"\n".join(ctx_lines))
        sig_pairs = []
        n = 0
        if len(modes) >= 2:
            n = (self._summary.get(modes[0]) or {}).get("denom", 0)
            COLW, LBLW = 10, 13
            lines = [u"Cross-mode (R@1):",
                     u" " * LBLW + u"".join(short[m].rjust(COLW) for m in modes)]
            for i, rm in enumerate(modes):
                row = short[rm].ljust(LBLW)
                for j, cm in enumerate(modes):
                    if j < i:
                        cell = u""                   # lower triangle (symmetric)
                    elif i == j:
                        cell = u"·"             # middle dot on the diagonal
                    else:
                        p = self._mcnemar_pair_live(rm, cm, 1)
                        if p is None:
                            cell = u"n/a"
                        else:
                            s = u"<.001" if p < 0.001 else u"{:.3f}".format(p)
                            if p < 0.05:
                                s += u"*"
                                sig_pairs.append((rm, cm, p))
                            cell = s
                    row += cell.rjust(COLW)
                lines.append(row)
            blocks.append(u"\n".join(lines))
        mtb.Text = u"\n\n".join(blocks)
        if ntb is not None:
            if not blocks:
                ntb.Text = u"Run at least two modes to compute pairwise significance."
            elif sig_pairs:
                parts = [u"{0} vs {1} (p={2})".format(
                    short[a], short[b],
                    u"<.001" if p < 0.001 else u"{:.3f}".format(p))
                    for (a, b, p) in sig_pairs]
                ntb.Text = (
                    u"* p < 0.05.  Significant: " + u"; ".join(parts) +
                    u".  Exact test, n = {0} — underpowered; non-significance "
                    u"is not evidence of equivalence.".format(n))
            elif len(modes) >= 2:
                ntb.Text = (
                    u"* p < 0.05.  No pair reaches significance at n = {0} — the "
                    u"test is underpowered; expand the ground truth toward "
                    u"N ≈ 50+ for reliable mode discrimination.".format(n))
            else:
                ntb.Text = (
                    u"* p < 0.05.  Run a second mode for the cross-mode matrix.")

    # ── Render results ──────────────────────────────────────────────

    def _refresh_cards(self):
        mode    = self._show_mode()
        results = self._results.get(mode)
        if not results:
            self.datagrid_results.Visibility   = Visibility.Collapsed
            self.panel_placeholder.Visibility  = Visibility.Visible
            return

        topk = self._current_topk()
        thr  = self._current_threshold()

        rows = []
        for entry in results:
            # Optionally drop the skipped / not-in-GT groups (display only -
            # metrics above are unaffected). is_match_entry is True only for
            # ground-truth materials labelled "match".
            if not self._show_skipped and not entry.get("is_match_entry"):
                continue
            query = entry["query"]
            gt    = entry["gt"]
            qclass = (gt.get("class", u"") if gt else u"")
            header_subtitle = u"[Class: {}]".format(qclass) if qclass else u""
            if gt is None:
                header_hit = u"(not in ground-truth file)"
            elif gt.get("label") != u"match":
                header_hit = u"(skipped: label={})".format(gt.get("label", u"?"))
            elif entry["hit_rank"] is None:
                header_hit = u"Miss — not found in ranked list"
            elif entry["hit_rank"] <= topk:
                header_hit = u"Hit ✓  Rank {}".format(entry["hit_rank"])
            else:
                header_hit = u"Hit ✗  Rank {} (beyond Top-{})".format(entry["hit_rank"], topk)

            # Filter by min score, then trim to Top-K
            filtered = [(it, sc) for (it, sc) in entry["ranked_all"] if sc is None or sc >= thr]
            filtered = filtered[:topk]

            if not filtered:
                rows.append(EmptyRow(query, header_subtitle, header_hit))
                continue

            accept = entry.get("acceptable", set())
            mode_for_tooltip = self._show_mode()
            ui_lang = self._current_lang()
            for i, (it, sc) in enumerate(filtered):
                # Reconstruct the haystack the same way the search code did,
                # so the tooltip's lengths / branch detection are accurate.
                # Use the same lang so the tooltip reflects the EN-translated
                # classification when EN is active.
                try:
                    haystack_text = build_searchable(it, lang=ui_lang)
                except Exception:
                    haystack_text = u""
                # Localise the classification for display (EN translation,
                # German fallback for any unmapped segment).
                raw_classification = it.get("classification", u"")
                display_classification = translate_path(raw_classification, ui_lang)
                rows.append(CandidateRow(
                    query           = query,
                    header_subtitle = header_subtitle,
                    header_hit      = header_hit,
                    rank            = i + 1,
                    uuid            = it.get("uuid", u""),
                    match_name      = it.get("name", u""),
                    classification  = display_classification,
                    score           = sc,
                    is_hit          = (it.get("uuid", u"") in accept) if accept else False,
                    score_tooltip   = self._explain_score(
                        query, haystack_text, sc, mode_for_tooltip,
                        entry=entry, uuid=it.get("uuid", u"")),
                ))

        # Build a grouped CollectionViewSource so the Expander-per-query layout works.
        cvs = CollectionViewSource()
        cvs.Source = rows
        cvs.GroupDescriptions.Add(PropertyGroupDescription("GroupKey"))
        self.datagrid_results.ItemsSource = cvs.View
        self.datagrid_results.Visibility  = Visibility.Visible
        self.panel_placeholder.Visibility = Visibility.Collapsed

    # ── Event handlers ──────────────────────────────────────────────

    def on_database_changed(self, sender, e):
        if not self.IsLoaded:
            return
        self._load_datasets()
        # Clear results (numbers were against the previous datastock)
        self._results = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._summary = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._summary_baseline = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._last_run_mode = None
        self._update_metric_cells()
        self._refresh_cards()
        self._save_settings_file()

    def on_language_changed(self, sender, e):
        if not self.IsLoaded:
            return
        self._load_datasets()
        self._results = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._summary = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._summary_baseline = {"regex": None, "fuzzy": None, "semantic": None, "semantic_reranked": None, "hybrid": None, "hybrid_reranked": None}
        self._last_run_mode = None
        self._update_metric_cells()
        self._refresh_cards()
        self._save_settings_file()

    def on_scope_changed(self, sender, e):
        if not self.IsLoaded:
            return
        scope = self._current_scope_key()
        if scope in (u"pick_elements", u"pick_materials"):
            # Re-launch the window via the entry-point loop so the user
            # can pick from the Revit canvas / material picker. The current
            # window closes, the script's main loop runs the picker, then
            # re-opens us with `pre_materials` populated.
            self._save_settings_file()
            self._pending_pick = scope
            self.Close()
            return
        self._refresh_scope_materials()
        self._save_settings_file()

    def on_topn_changed(self, sender, e):
        if not self.IsLoaded:
            return
        self._refresh_cards()
        self._save_settings_file()

    def on_threshold_changed(self, sender, e):
        if not self.IsLoaded:
            return
        try:
            v = int(round(float(self.slider_threshold.Value)))
            self.text_threshold.Text = u"{}%".format(v)
        except Exception:
            pass
        self._refresh_cards()
        self._save_settings_file()

    def on_threshold_text_changed(self, sender, e):
        if not self.IsLoaded:
            return
        try:
            t = (self.text_threshold.Text or u"").strip().rstrip(u"%").strip()
            v = max(0, min(100, int(round(float(t)))))
            if abs(v - float(self.slider_threshold.Value)) > 0.5:
                self.slider_threshold.Value = v
        except Exception:
            pass

    def on_show_mode_changed(self, sender, e):
        if not self.IsLoaded:
            return
        self._refresh_cards()
        # Keep the single Run button's label aligned with the active mode.
        self._update_run_button_label()

    def on_browse_groundtruth(self, sender, e):
        try:
            from Microsoft.Win32 import OpenFileDialog
            dlg = OpenFileDialog()
            dlg.Filter = "Ground truth JSON (*.json)|*.json|All files (*.*)|*.*"
            dlg.Title  = "Choose Phase 0 ground truth JSON"
            if self._gt_path and os.path.isfile(self._gt_path):
                dlg.InitialDirectory = os.path.dirname(self._gt_path)
                dlg.FileName         = os.path.basename(self._gt_path)
            if dlg.ShowDialog():
                self._gt_path = dlg.FileName
                self._refresh_groundtruth_label()
                # Pre-load to surface format errors immediately
                self._load_ground_truth()
                self._save_settings_file()
        except Exception as ex:
            logger.error(u"Browse error: {}".format(ex))

    # ── Expand / Collapse / Export ──────────────────────────────────

    def _find_expanders(self, parent):
        out = []
        try:
            n = VisualTreeHelper.GetChildrenCount(parent)
        except Exception:
            return out
        for i in range(n):
            child = VisualTreeHelper.GetChild(parent, i)
            if isinstance(child, Expander):
                out.append(child)
            out.extend(self._find_expanders(child))
        return out

    def on_expand_all(self, sender, e):
        for exp in self._find_expanders(self.datagrid_results):
            exp.IsExpanded = True

    def on_collapse_all(self, sender, e):
        for exp in self._find_expanders(self.datagrid_results):
            exp.IsExpanded = False

    def on_toggle_skipped(self, sender, e):
        """Flip the results-grid skipped-group visibility and re-render. The
        button label reflects the action it will perform next."""
        self._show_skipped = not self._show_skipped
        btn = getattr(self, "btn_toggle_skipped", None)
        if btn is not None:
            btn.Content = u"Hide skipped" if self._show_skipped else u"Show skipped"
        self._refresh_cards()

    def on_export_results(self, sender, e):
        mode = self._show_mode()
        results = self._results.get(mode)
        if not results:
            forms.alert(u"Nothing to export. Run a benchmark first.",
                        title=u"Validation")
            return
        try:
            from Microsoft.Win32 import SaveFileDialog
            dlg = SaveFileDialog()
            dlg.Filter      = "HTML report (*.html)|*.html|Markdown (*.md)|*.md|All files (*.*)|*.*"
            dlg.FilterIndex = 1
            dlg.Title       = "Export validation results"
            dlg.FileName    = "validation_report_{}.html".format(
                time.strftime("%Y%m%d_%H%M%S"))
            if not dlg.ShowDialog():
                return
            out_path = dlg.FileName
            ext      = os.path.splitext(out_path)[1].lower()
            if ext == u".md" or ext == u".markdown":
                content = self._build_markdown_report(mode, results)
            else:
                content = self._build_html_report(mode, results)
            with io.open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.text_status.Text = u"Exported to {}".format(out_path)
        except Exception as ex:
            logger.error(u"Export error: {}\n{}".format(ex, traceback.format_exc()))
            forms.alert(u"Export failed: {}".format(ex), title=u"Validation")

    # ── Report helpers (shared by HTML + Markdown) ──────────────────

    @staticmethod
    def _html_escape(s):
        if s is None:
            return u""
        out = unicode(s)
        out = out.replace(u"&", u"&amp;")
        out = out.replace(u"<", u"&lt;")
        out = out.replace(u">", u"&gt;")
        out = out.replace(u"\"", u"&quot;")
        return out

    @staticmethod
    def _md_escape_cell(s):
        """Escape a value for safe rendering inside a Markdown table cell."""
        if s is None:
            return u""
        out = unicode(s).replace(u"|", u"\\|")
        out = out.replace(u"\r", u" ").replace(u"\n", u" ")
        return out

    # Canonical mode order in the report -- mirrors the thesis hypothesis
    # progression (keyword baselines first, then learned signals).
    _REPORT_MODES = (u"regex", u"fuzzy", u"semantic", u"hybrid",
                     u"hybrid_reranked", u"semantic_reranked")

    @staticmethod
    def _mode_label(mode):
        """Human-readable label for a benchmark mode key. `mode.capitalize()`
        would render `hybrid_reranked` as `Hybrid_reranked`; use this
        helper everywhere the report displays a mode name."""
        return {
            u"regex":             u"Regex",
            u"fuzzy":             u"Fuzzy",
            u"semantic":          u"Semantic",
            u"semantic_reranked": u"Semantic + Rerank",
            u"hybrid":            u"Hybrid",
            u"hybrid_reranked":   u"Hybrid + Rerank",
        }.get(mode, mode.capitalize())

    # ── BIM-context impact (report Section C3b) ─────────────────────
    _NOISE_STEP_LABELS = set([u"+ density", u"+ λ thermal", u"+ host types"])

    def _load_reference_ladder(self):
        """Load the shipped offline ladder reference for the current language
        (tools/ladder_modes_results.json under the Connector). Returns the
        per-language record dict, or None if absent/unreadable."""
        try:
            path = os.path.join(CONNECTOR, u"tools", u"ladder_modes_results.json")
            if not os.path.exists(path):
                return None
            with io.open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            lang = self._current_lang()
            for rec in data:
                if rec.get("lang") == lang:
                    return rec
            return data[0] if data else None
        except Exception:
            return None

    def _classify_field_steps(self, modewise):
        """From {mode: [rows...]} (cumulative ladder; each row has 'label' and
        'mrr'), compute each step's mean marginal ΔMRR across the available
        modes and a helps / neutral / hurts / noise verdict. Returns a list of
        {field, verdict, evidence}."""
        # The per-field verdict is a recommendation for what to enrich the
        # DENSE query with, so it averages over the dense modes only. Fuzzy is
        # deliberately excluded here - every field hurts it, so folding it in
        # would bias the dense recommendation. Fuzzy's noise is documented
        # separately (its row in the before/after comparison + its prose line).
        modes = [m for m in modewise if modewise.get(m) and m != u"fuzzy"]
        if not modes:
            return []
        L = min(len(modewise[m]) for m in modes)
        out = []
        for i in range(1, L):
            label = modewise[modes[0]][i].get("label", u"")
            deltas = [modewise[m][i].get("mrr", 0.0) - modewise[m][i - 1].get("mrr", 0.0)
                      for m in modes]
            mean_d = sum(deltas) / len(deltas)
            pos = sum(1 for d in deltas if d > 0)
            if label in self._NOISE_STEP_LABELS:
                verdict = u"noise — use a filter"
            elif mean_d <= -0.015:
                verdict = (u"hurts (helps EN-semantic only)"
                           if label == u"+ categories" else u"hurts")
            elif mean_d >= 0.015 and pos >= (len(modes) + 1) // 2:
                verdict = u"helps"
            else:
                verdict = u"~ neutral — safe to keep"
            out.append({
                "field": label,
                "verdict": verdict,
                "evidence": u"{0:+.3f} mean ΔMRR · helps in {1}/{2} mode(s)".format(
                    mean_d, pos, len(modes)),
            })
        return out

    def _bim_context_section_data(self):
        """Assemble the BIM-context impact data for the export report: the
        per-mode name-only vs +context comparison (from this run's baseline +
        enriched summaries), a per-field verdict table (from this session's
        Measure ladder if present, else the shipped offline reference), and a
        short plain-English summary per dense mode."""
        # Modes that carry a name-only baseline when context is ON (dense modes
        # + fuzzy). Fuzzy is included so the comparison documents that
        # enrichment adds noise to the lexical path - the comprehensive view.
        ctx_modes = (u"semantic", u"semantic_reranked", u"hybrid",
                     u"hybrid_reranked", u"fuzzy")

        def _pt(s, key):
            t = s.get(key)
            return t[0] if t else 0.0

        comparison = []
        for mode in ctx_modes:
            base = self._summary_baseline.get(mode)
            enr  = self._summary.get(mode)
            if not base or not enr or not base.get("denom") or not enr.get("denom"):
                continue
            bd, ed = float(base["denom"]), float(enr["denom"])
            comparison.append({
                "mode": mode, "mode_label": self._mode_label(mode),
                "base_mrr": _pt(base, "mrr"),   "enr_mrr": _pt(enr, "mrr"),
                "base_ndcg": _pt(base, "ndcg10"), "enr_ndcg": _pt(enr, "ndcg10"),
                "base_r1": base.get("r1", 0) / bd,  "enr_r1": enr.get("r1", 0) / ed,
                "base_r5": base.get("r5", 0) / bd,  "enr_r5": enr.get("r5", 0) / ed,
                "base_r10": base.get("r10", 0) / bd, "enr_r10": enr.get("r10", 0) / ed,
            })

        verdicts, source, modewise = [], u"", None
        lr = self._ladder_results
        if lr and lr.get("by_mode"):
            modewise = dict((m, lr["by_mode"][m]["rows"])
                            for m in lr.get("modes", []) if lr["by_mode"].get(m))
            source = u"this session's Measure-field-value ladder"
        else:
            ref = self._load_reference_ladder()
            if ref and ref.get("out"):
                modewise = ref["out"]
                source = u"shipped offline reference (tools/ladder_modes.md, {0})".format(
                    (ref.get("lang") or u"").upper())
        if modewise:
            verdicts = self._classify_field_steps(modewise)

        prose = []
        for c in comparison:
            dm = c["enr_mrr"] - c["base_mrr"]
            verb = (u"lifted" if dm > 0.002 else
                    (u"left ~flat" if abs(dm) <= 0.002 else u"lowered"))
            prose.append(
                u"{0}: BIM context {1} MRR {2:.3f} → {3:.3f} ({4:+.3f}) and "
                u"R@5 {5:.0f}% → {6:.0f}%.".format(
                    c["mode_label"], verb, c["base_mrr"], c["enr_mrr"], dm,
                    c["base_r5"] * 100, c["enr_r5"] * 100))

        return {
            "has_comparison": bool(comparison),
            "comparison": comparison,
            "verdicts": verdicts,
            "verdict_source": source,
            "fields_used": [self._FIELD_LABEL.get(k, k)
                            for k in self._BEST_CONTEXT_FIELDS],
            "prose": prose,
        }

    @staticmethod
    def _delta_cell_html(base, enr, pp=False):
        """A 'base → enr (Δ)' HTML cell, Δ green/red by sign. `pp` renders the
        values as percentages and the delta in percentage points."""
        d = enr - base
        color = u"#16A34A" if d > 1e-9 else (u"#DC2626" if d < -1e-9 else u"#6B7280")
        if pp:
            return (u"<span class=\"pt\">{0:.0f}% → {1:.0f}%</span>"
                    u"<span class=\"ci\" style=\"color:{2}\">({3:+.0f} pp)</span>"
                    ).format(base * 100, enr * 100, color, d * 100)
        return (u"<span class=\"pt\">{0:.3f} → {1:.3f}</span>"
                u"<span class=\"ci\" style=\"color:{2}\">({3:+.3f})</span>"
                ).format(base, enr, color, d)

    def _emit_bim_context_section_html(self, html, e, summaries_by_mode):
        """Append the BIM-context impact section (Section C3b) to the HTML
        report. No-op when there is neither a before/after comparison nor a
        loadable verdict source."""
        data = self._bim_context_section_data()
        if not data["has_comparison"] and not data["verdicts"]:
            return
        html.append(u"<section class=\"ranking-quality-wrap\">")
        html.append(u"<h2>BIM-context impact — name-only vs +BIM context</h2>")
        html.append(u"<p>Query enriched with <strong>{0}</strong> (the universal "
                    u"best combo). The BM25F leg always ranks on the bare name, "
                    u"and <strong>Regex</strong> (a pattern mode) is never "
                    u"enriched. <strong>Fuzzy</strong> is shown enriched too — "
                    u"not because it helps (it does not), but to document "
                    u"comprehensively that the lexical path degrades under "
                    u"enrichment, leaving no open question.</p>".format(
                        e(u" + ".join(data["fields_used"]))))
        if data["has_comparison"]:
            html.append(u"<table class=\"ranking-quality\">")
            html.append(u"<thead><tr><th>Mode</th>"
                        u"<th>MRR base → +ctx</th>"
                        u"<th>nDCG@10 base → +ctx</th><th>R@1</th><th>R@5</th>"
                        u"<th>R@10</th></tr></thead><tbody>")
            for c in data["comparison"]:
                html.append(u"<tr><td class=\"mode-label\">{0}</td>"
                            u"<td class=\"value\">{1}</td>"
                            u"<td class=\"value\">{2}</td>"
                            u"<td class=\"value\">{3}</td>"
                            u"<td class=\"value\">{4}</td>"
                            u"<td class=\"value\">{5}</td></tr>".format(
                                e(c["mode_label"]),
                                self._delta_cell_html(c["base_mrr"], c["enr_mrr"]),
                                self._delta_cell_html(c["base_ndcg"], c["enr_ndcg"]),
                                self._delta_cell_html(c["base_r1"], c["enr_r1"], pp=True),
                                self._delta_cell_html(c["base_r5"], c["enr_r5"], pp=True),
                                self._delta_cell_html(c["base_r10"], c["enr_r10"], pp=True)))
            html.append(u"</tbody></table>")
        if data["verdicts"]:
            html.append(u"<h3>Per-field verdict <small>(source: {0})</small></h3>".format(
                e(data["verdict_source"])))
            html.append(u"<table class=\"ranking-quality\">")
            html.append(u"<thead><tr><th>Field added</th><th>Verdict</th>"
                        u"<th>Evidence</th></tr></thead><tbody>")
            for v in data["verdicts"]:
                html.append(u"<tr><td class=\"mode-label\">{0}</td>"
                            u"<td class=\"value\">{1}</td>"
                            u"<td class=\"value\">{2}</td></tr>".format(
                                e(v["field"]), e(v["verdict"]), e(v["evidence"])))
            html.append(u"</tbody></table>")
        if data["prose"]:
            html.append(u"<ul>")
            for line in data["prose"]:
                html.append(u"<li>{0}</li>".format(e(line)))
            html.append(u"</ul>")
        html.append(u"</section>")

    def _emit_bim_context_section_md(self, w):
        """Append the BIM-context impact section to the Markdown report."""
        data = self._bim_context_section_data()
        if not data["has_comparison"] and not data["verdicts"]:
            return
        esc = self._md_escape_cell
        w(u"## BIM-context impact — name-only vs +BIM context")
        w()
        w(u"Query enriched with **{0}** (the universal best combo). The BM25F "
          u"leg always ranks on the bare name, and **Regex** (a pattern mode) "
          u"is never enriched. **Fuzzy** is shown enriched too — not because it "
          u"helps (it does not), but to document comprehensively that the "
          u"lexical path degrades under enrichment, leaving no open "
          u"question.".format(u" + ".join(data["fields_used"])))
        w()
        if data["has_comparison"]:
            w(u"| Mode | MRR base → +ctx | nDCG@10 base → +ctx | R@1 | R@5 | R@10 |")
            w(u"|---|---|---|---|---|---|")

            def _d(base, enr, pp=False):
                dd = enr - base
                if pp:
                    return u"{0:.0f}% → {1:.0f}% ({2:+.0f} pp)".format(
                        base * 100, enr * 100, dd * 100)
                return u"{0:.3f} → {1:.3f} ({2:+.3f})".format(base, enr, dd)

            for c in data["comparison"]:
                w(u"| **{0}** | {1} | {2} | {3} | {4} | {5} |".format(
                    esc(c["mode_label"]),
                    _d(c["base_mrr"], c["enr_mrr"]),
                    _d(c["base_ndcg"], c["enr_ndcg"]),
                    _d(c["base_r1"], c["enr_r1"], pp=True),
                    _d(c["base_r5"], c["enr_r5"], pp=True),
                    _d(c["base_r10"], c["enr_r10"], pp=True)))
            w()
        if data["verdicts"]:
            w(u"### Per-field verdict ({0})".format(data["verdict_source"]))
            w()
            w(u"| Field added | Verdict | Evidence |")
            w(u"|---|---|---|")
            for v in data["verdicts"]:
                w(u"| {0} | {1} | {2} |".format(
                    esc(v["field"]), esc(v["verdict"]), esc(v["evidence"])))
            w()
        if data["prose"]:
            w(u"### How BIM context changed each mode")
            w()
            for line in data["prose"]:
                w(u"- {0}".format(line))
            w()

    def _hit_status(self, entry, topk):
        """Return (status_label, css_class) - e.g. ('Hit @ Rank 7', 'hit')."""
        gt = entry["gt"]
        if gt is None:
            return (u"(not in ground-truth file)", u"skip")
        if gt.get("label") != u"match":
            return (u"(skipped: label={})".format(gt.get("label", u"?")), u"skip")
        rk = entry["hit_rank"]
        if rk is None:
            return (u"Miss — not found in ranked list", u"miss")
        if rk <= topk:
            return (u"Hit @ Rank {}".format(rk), u"hit")
        return (u"Hit beyond Top-{} (Rank {})".format(topk, rk), u"miss")

    def _rank_distribution(self, results):
        """Return dict: bin_label -> count, ordered as below.
        Only counts entries with label='match' (the recall denominator)."""
        bins = [
            (u"Rank 1",           lambda r: r == 1),
            (u"Rank 2-3",         lambda r: r is not None and 2 <= r <= 3),
            (u"Rank 4-5",         lambda r: r is not None and 4 <= r <= 5),
            (u"Rank 6-10",        lambda r: r is not None and 6 <= r <= 10),
            (u"Beyond Top-10",    lambda r: r is not None and r > 10),
            (u"Miss (not found)", lambda r: r is None),
        ]
        counts = [(label, 0) for (label, _) in bins]
        out = []
        for label, pred in bins:
            n = 0
            for entry in results:
                if not entry.get("is_match_entry"):
                    continue
                if pred(entry["hit_rank"]):
                    n += 1
            out.append((label, n))
        return out

    def _class_breakdown(self, results, topk):
        """Group match-label entries by GT 'class' field. Returns ordered list:
        [(class_name, {n, r1, r10, rall}), ...] sorted by n descending."""
        by_class = {}
        for entry in results:
            if not entry.get("is_match_entry"):
                continue
            cls = (entry["gt"] or {}).get("class", u"(unknown)") or u"(unknown)"
            bucket = by_class.setdefault(cls, {"n": 0, "r1": 0, "r10": 0, "rall": 0})
            bucket["n"] += 1
            rk = entry["hit_rank"]
            if rk is not None:
                bucket["rall"] += 1
                if rk <= 1:  bucket["r1"]  += 1
                if rk <= 10: bucket["r10"] += 1
        items = list(by_class.items())
        items.sort(key=lambda kv: (-kv[1]["n"], kv[0]))
        return items

    def _hits_and_misses(self, results, topk):
        """Return (hits, misses, skipped). Each is a list of (query, entry)."""
        hits, misses, skipped = [], [], []
        for entry in results:
            gt = entry["gt"]
            if gt is None or gt.get("label") != u"match":
                skipped.append(entry)
                continue
            if entry["hit_rank"] is not None and entry["hit_rank"] <= topk:
                hits.append(entry)
            else:
                misses.append(entry)
        hits.sort(key=lambda e: e["hit_rank"] or 9999)
        return hits, misses, skipped

    def _executive_summary(self, results_by_mode):
        """Plain-English 1-2 sentence summary covering whichever modes have data."""
        parts = []
        for mode in (u"hybrid_reranked", u"semantic_reranked", u"hybrid", u"semantic", u"fuzzy", u"regex"):   # full stack first - best ceiling
            summary = self._summary.get(mode)
            entries = results_by_mode.get(mode)
            if not summary or not entries:
                continue
            denom = summary["denom"]
            if denom == 0:
                parts.append(u"{} run found 0 ground-truth matches in scope.".format(
                    self._mode_label(mode)))
                continue
            rall = summary["rall"]
            r1   = summary["r1"]
            pct_all = (100.0 * rall / denom)
            parts.append(
                u"{mode} search found an acceptable EPD anywhere in the ranked list "
                u"for {rall} of {denom} ground-truth materials ({pct:.0f}% Recall@All); "
                u"{r1} of those landed at Rank 1.".format(
                    mode=self._mode_label(mode), rall=rall, denom=denom,
                    pct=pct_all, r1=r1)
            )
        if not parts:
            return u"No results to summarise."
        return u" ".join(parts)

    # ── HTML report ─────────────────────────────────────────────────

    def _cache_provenance(self):
        """One-line provenance of the active datastock cache file (name +
        modification date), so the exported report itself proves which
        corpus snapshot the benchmark ran on. Added 2026-07-05."""
        try:
            fn = u"ds_cache_v2_{0}_{1}.json".format(
                self._current_datastock(), self._current_lang())
            mtime = os.path.getmtime(os.path.join(CACHE_DIR, fn))
            return u"{0} · modified {1}".format(
                fn, time.strftime(u"%Y-%m-%d %H:%M", time.localtime(mtime)))
        except Exception:
            return u"(unavailable)"

    def _build_html_report(self, mode, results):
        e   = self._html_escape
        topk = self._current_topk()
        thr  = self._current_threshold()
        ui_lang = self._current_lang()
        # Localise classification paths the same way the on-screen grid does.
        tx = lambda path: translate_path(path or u"", ui_lang)
        run_ts = time.strftime("%Y-%m-%d %H:%M:%S")

        # Pre-compute things shared across sections. results_by_mode and
        # summaries cover every benchmarked mode (regex / fuzzy / semantic
        # / hybrid / hybrid_reranked) so the headline metrics, rank
        # distribution, and executive summary all reflect the full stack.
        results_by_mode = dict(
            (m, self._results.get(m)) for m in self._REPORT_MODES)
        summaries_by_mode = dict(
            (m, self._summary.get(m)) for m in self._REPORT_MODES)

        topk_label = unicode(topk)

        # --- CSS ----------------------------------------------------
        css = u"""
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
       font-size: 13.5px; line-height: 1.55; color: #1F2937;
       max-width: 1100px; margin: 0 auto; padding: 32px 28px 56px; background: #FFFFFF; }
header.report-head { border-bottom: 2px solid #0078D4; margin-bottom: 22px; padding-bottom: 14px; }
header.report-head h1 { margin: 0 0 4px; font-size: 22px; color: #111827; font-weight: 600; }
header.report-head .sub { color: #6B7280; font-size: 12px; }
section { margin-top: 28px; }
section h2 { color: #0078D4; font-size: 13px; font-weight: 700; margin: 0 0 12px;
             padding-bottom: 5px; border-bottom: 1px solid #E5E7EB;
             text-transform: uppercase; letter-spacing: 0.6px; }
section h3 { font-size: 13.5px; font-weight: 600; margin: 14px 0 6px; color: #374151; }

/* Meta table */
.meta-box table { border-collapse: collapse; width: 100%; }
.meta-box th { text-align: left; padding: 6px 12px; background: #F5F6F7; width: 180px;
               font-weight: 600; color: #374151; border: 1px solid #E5E7EB; font-size: 12px;
               vertical-align: top; }
.meta-box td { padding: 6px 12px; border: 1px solid #E5E7EB; font-size: 12px;
               word-break: break-word; vertical-align: top; }
.meta-box td.path { font-family: SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px;
                    color: #4B5563; }

/* Executive summary */
.exec p { margin: 0; padding: 14px 18px; background: #F0F7FF;
          border-left: 3px solid #0078D4; font-size: 13px; }

/* Headline metrics */
table.metrics { border-collapse: collapse; width: 100%; }
table.metrics th, table.metrics td { padding: 10px 8px; text-align: center;
                                     border: 1px solid #E5E7EB; }
table.metrics thead th { background: #F0F4F8; font-weight: 600; font-size: 11.5px; color: #374151; }
table.metrics tbody td { font-size: 15px; font-weight: 600; }
table.metrics td .frac { display: block; font-size: 10px; color: #6B7280;
                         font-weight: 400; margin-top: 2px; }
table.metrics td.mode-label { text-align: left; padding-left: 14px; background: #F9FAFB;
                              font-weight: 600; font-size: 12.5px; }
table.metrics tr.row-delta td { background: #FFFBEB; color: #92400E; font-style: italic; font-size: 13px; }
table.metrics tr.row-delta td.mode-label { background: #FEF3C7; }
/* Shared 5-tier value scale (global so the metrics, exact-correct and
   ranking-quality tables all use one colour system — matches the live
   dashboard's _pct_brush). */
.pct-red    { color: #DC2626; }
.pct-orange { color: #EA580C; }
.pct-amber  { color: #CA8A04; }
.pct-green  { color: #16A34A; }
.pct-top    { color: #047857; }
.pct-zero   { color: #6B7280; }

/* Rank distribution */
.bar-group { margin: 8px 0 18px; }
.bar-row { display: flex; align-items: center; margin-bottom: 5px; }
.bar-label { width: 130px; font-size: 11.5px; color: #374151; }
.bar { flex: 1; height: 18px; background: #F3F4F6; border-radius: 2px; overflow: hidden; }
.bar-fill { display: block; height: 100%; background: #0078D4; }
.bar-fill.miss { background: #DC2626; }
.bar-fill.beyond { background: #F59E0B; }
.bar-count { width: 50px; text-align: right; font-size: 11.5px; color: #374151;
             margin-left: 10px; font-weight: 500; }

/* Per-class breakdown */
table.classes { border-collapse: collapse; width: 100%; }
table.classes th, table.classes td { padding: 7px 10px; border: 1px solid #E5E7EB;
                                     font-size: 12px; }
table.classes th { background: #F0F4F8; font-weight: 600; text-align: left; }
table.classes td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.classes tbody tr:nth-child(even) { background: #F9FAFB; }

/* Hits & Misses */
.hits-misses .cols { display: flex; gap: 28px; }
.hits-misses .col { flex: 1; min-width: 0; }
.hits-misses ul { margin: 6px 0; padding-left: 4px; list-style: none; }
.hits-misses li { font-size: 11.5px; margin-bottom: 4px; line-height: 1.5;
                  padding-left: 14px; text-indent: -14px; }
.hits-misses .ok  { color: #16A34A; font-weight: 700; }
.hits-misses .bad { color: #DC2626; font-weight: 700; }
.hits-misses .extra { color: #6B7280; font-size: 11px; }

/* Per-query cards */
.query-card { border: 1px solid #E5E7EB; border-radius: 4px; margin-bottom: 14px;
              padding: 13px 16px 14px; background: #FFFFFF; page-break-inside: avoid; }
.query-card h3 { margin: 0 0 6px; font-size: 14px; }
.query-card .class-tag { display: inline-block; background: #E8F2FC; color: #0078D4;
                         font-size: 10.5px; padding: 2px 8px; border-radius: 10px;
                         margin-left: 6px; font-weight: 500; vertical-align: middle; }
.query-card .hit-tag { font-size: 12px; margin-left: 8px; font-weight: 600;
                       vertical-align: middle; }
.query-card .hit-tag.hit  { color: #16A34A; }
.query-card .hit-tag.miss { color: #DC2626; }
.query-card .hit-tag.skip { color: #6B7280; font-style: italic; font-weight: 500; }
.query-card .gt-context { margin: 8px 0 12px; padding: 8px 12px; background: #F9FAFB;
                          border-radius: 3px; font-size: 11.5px; line-height: 1.6; }
.query-card .gt-context dl { margin: 0; }
.query-card .gt-context dt { color: #6B7280; font-weight: 600; display: inline; }
.query-card .gt-context dd { display: inline; margin: 0 0 0 4px; }
.query-card .gt-context dd:after { content: ""; display: block; height: 2px; }
.query-card table { border-collapse: collapse; width: 100%; margin-top: 4px; }
.query-card th, .query-card td { padding: 5px 8px; border: 1px solid #E5E7EB;
                                  font-size: 11.5px; text-align: left; vertical-align: top; }
.query-card th { background: #F5F6F7; font-weight: 600; }
.query-card td.center { text-align: center; }
.query-card td.right  { text-align: right; }
.query-card tr.is-hit td      { background: #F0FDF4; }
.query-card tr.is-correct td  { background: #DCFCE7; }
.query-card .star    { color: #16A34A; font-weight: bold; }
.query-card .uuid    { font-family: SFMono-Regular, Menlo, Consolas, monospace;
                       font-size: 10.5px; color: #4B5563; }
.query-card .empty   { padding: 8px 12px; background: #FEF2F2; color: #991B1B;
                       border-radius: 3px; font-size: 11.5px; font-style: italic; }
.query-card .expected { margin-top: 8px; padding: 8px 12px; background: #FFFBEB;
                        border-left: 3px solid #F59E0B; font-size: 11.5px; }
.query-card .expected .label { color: #92400E; font-weight: 600; }
.query-card .expected .name  { font-weight: 600; color: #374151; }
.query-card .expected .rationale { color: #4B5563; margin-top: 2px; }

/* Ranking quality table (Section: MRR + nDCG@10 with bootstrap CIs) */
table.ranking-quality { border-collapse: collapse; width: 100%; }
table.ranking-quality th, table.ranking-quality td {
    padding: 8px 10px; border: 1px solid #E5E7EB; font-size: 12.5px; }
table.ranking-quality thead th { background: #F0F4F8; font-weight: 600; color: #374151; }
table.ranking-quality td.mode-label { text-align: left; background: #F9FAFB;
                                      font-weight: 600; padding-left: 14px; }
table.ranking-quality td.value { text-align: center; font-variant-numeric: tabular-nums; }
table.ranking-quality td.value .pt { font-weight: 700; font-size: 14px; }
table.ranking-quality td.value .ci { display: block; font-size: 10.5px;
                                     color: #6B7280; font-weight: 400; margin-top: 1px; }

/* McNemar pairwise p-value matrix */
table.mcnemar { border-collapse: collapse; width: 100%; margin-bottom: 10px; }
table.mcnemar th, table.mcnemar td {
    padding: 7px 9px; border: 1px solid #E5E7EB; font-size: 12px;
    text-align: center; font-variant-numeric: tabular-nums; }
table.mcnemar thead th { background: #F0F4F8; font-weight: 600; }
table.mcnemar td.row-label { background: #F9FAFB; font-weight: 600; text-align: left; }
table.mcnemar td.dash { background: #F3F4F6; color: #9CA3AF; }
table.mcnemar td.sig  { background: #FEF2F2; color: #991B1B; font-weight: 700; }
table.mcnemar td.ns   { color: #4B5563; }
.mcnemar-note { font-size: 11.5px; color: #4B5563; margin: 4px 0 14px; font-style: italic; }

/* Latency vs quality table (Pareto-style) */
table.pareto { border-collapse: collapse; width: 100%; }
table.pareto th, table.pareto td { padding: 7px 10px; border: 1px solid #E5E7EB;
                                   font-size: 12px; text-align: center; }
table.pareto thead th { background: #F0F4F8; font-weight: 600; }
table.pareto td.mode-label { text-align: left; background: #F9FAFB;
                             font-weight: 600; padding-left: 14px; }
table.pareto td.num { text-align: right; font-variant-numeric: tabular-nums; }

/* Methodology */
.methodology ul { margin: 6px 0 12px; padding-left: 22px; }
.methodology li { font-size: 12px; margin-bottom: 4px; }
.methodology blockquote { margin: 12px 0 0; padding: 10px 16px; background: #F9FAFB;
                          border-left: 3px solid #6B7280; font-size: 11.5px; color: #374151; }
.methodology blockquote p { margin: 0 0 6px; }
.methodology blockquote ul { padding-left: 18px; margin: 0; }

/* Print */
@media print {
  body { max-width: none; padding: 10px 14px; font-size: 11.5px; }
  section { page-break-inside: avoid; margin-top: 18px; }
  .query-card { page-break-inside: avoid; }
  header.report-head { border-bottom-width: 1px; }
  section h2 { font-size: 11.5px; }
}
"""

        # --- HTML head ---------------------------------------------
        html = []
        html.append(u"<!DOCTYPE html>")
        html.append(u"<html lang=\"en\">")
        html.append(u"<head>")
        html.append(u"<meta charset=\"UTF-8\">")
        html.append(u"<title>{}</title>".format(e(u"Validation Report — Phase 0 Recall@K Benchmark")))
        html.append(u"<style>{}</style>".format(css))
        html.append(u"</head>")
        html.append(u"<body>")

        # --- Header ------------------------------------------------
        ds_label = u"{} ({})".format(self._current_datastock(), self._current_lang().upper())
        sub_pieces = [
            u"Generated " + run_ts,
            u"ÖKOBAUDAT " + ds_label,
        ]
        if self._gt_meta.get("version"):
            sub_pieces.append(u"GT v" + unicode(self._gt_meta["version"]))
        html.append(u"<header class=\"report-head\">")
        html.append(u"<h1>{}</h1>".format(e(u"Validation Report — Phase 0 Recall@K Benchmark")))
        html.append(u"<div class=\"sub\">{}</div>".format(e(u" · ".join(sub_pieces))))
        html.append(u"</header>")

        # --- Section A: Run configuration --------------------------
        html.append(u"<section class=\"meta-box\">")
        html.append(u"<h2>Run configuration</h2>")
        html.append(u"<table>")
        meta_rows = [
            (u"Datastock",      ds_label),
            (u"Scope mode",     self._current_scope_key().replace(u"_", u" ")),
            (u"Scope size",     u"{} material(s)".format(len(self._scope_names))),
            (u"Ground truth",   os.path.basename(self._gt_path or u"")),
            (u"Dataset cache",  u"{:,} EPDs".format(len(self._datasets))),
            (u"Cache snapshot", self._cache_provenance()),
            (u"Top-K cutoff",   topk_label),
            (u"Min score",      u"{:.0f}%".format(thr * 100.0)),
            (u"Showing (cards)", self._mode_label(mode)),
        ]
        if self._gt_meta.get("dataset_count"):
            meta_rows.append((u"GT dataset_count", unicode(self._gt_meta["dataset_count"])))
        if self._gt_meta.get("matched_count") is not None:
            meta_rows.append((u"GT matched / skipped",
                              u"{} / {}".format(self._gt_meta.get("matched_count", u"?"),
                                                self._gt_meta.get("skipped_count", u"?"))))
        if self._gt_meta.get("version"):
            meta_rows.append((u"GT version", unicode(self._gt_meta["version"])))
        if self._gt_meta.get("source_revit_model"):
            meta_rows.append((u"Source model", unicode(self._gt_meta["source_revit_model"])))
        if self._gt_meta.get("validation_status"):
            meta_rows.append((u"GT status", unicode(self._gt_meta["validation_status"])))
        for k, v in meta_rows:
            css_cls = u" class=\"path\"" if k == u"Ground truth" else u""
            html.append(u"<tr><th>{}</th><td{}>{}</td></tr>".format(
                e(k), css_cls, e(v)))
        html.append(u"</table>")
        html.append(u"</section>")

        # --- Section B: Executive summary - removed 2026-07-05 (thesis
        # decision: the report shows data, the thesis carries the narrative;
        # a prose duplicate of the grid invites inconsistency). -------

        # --- Section C: Headline metrics table ---------------------
        html.append(u"<section class=\"headline\">")
        html.append(u"<h2>Headline metrics — acceptable set (R@K with 95% Wilson CI)</h2>")
        html.append(u"<table class=\"metrics\">")
        html.append(u"<thead><tr>"
                    u"<th>Mode</th><th>R@1</th><th>R@3</th><th>R@5</th>"
                    u"<th>R@10</th><th>R@20</th><th>R@All</th><th>Time</th><th>Scope · GT</th>"
                    u"</tr></thead><tbody>")

        def _pct_css(pct):
            # Same 5-tier value scale as the live dashboard's _pct_brush
            # (None = no data → grey; a real 0% maps to red like the dashboard).
            if pct is None: return u"pct-zero"
            if pct < 15: return u"pct-red"
            if pct < 30: return u"pct-orange"
            if pct < 45: return u"pct-amber"
            if pct < 70: return u"pct-green"
            return u"pct-top"

        def _cell(summary, denom, key, ci_suffix=u"_ci"):
            """Render a single R@K cell with Wilson CI in the fraction line.
            `key`         is e.g. "r1"; the CI is looked up at key + ci_suffix.
            Pass ci_suffix=None to suppress the CI (used in Δ rows).
            """
            if not summary or denom == 0:
                return u"<td>—</td>"
            hits = summary.get(key, 0)
            pct  = 100.0 * hits / denom
            ci_str = u""
            if ci_suffix:
                ci = summary.get(key + ci_suffix)
                if ci and (ci[1] > 0 or ci[2] > 0):
                    ci_str = u" · [{:.0f}-{:.0f}%]".format(
                        ci[1] * 100.0, ci[2] * 100.0)
            return (u"<td class=\"{}\">{:d}%"
                    u"<span class=\"frac\">{}/{}{}</span></td>"
                    .format(_pct_css(pct), int(round(pct)),
                            hits, denom, ci_str))

        # Iterate every mode that ran, in canonical thesis-progression
        # order. After each non-baseline mode, emit a Δ row showing its
        # lift over the previous mode that ran -- the table thus reads
        # top-to-bottom as the upgrade story (regex -> fuzzy -> semantic
        # -> hybrid -> +rerank), with the per-stage improvement quoted
        # immediately under each step.
        prev_mode_key = None
        prev_summary  = None
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary:
                continue
            denom = summary.get("denom", 0)
            html.append(u"<tr>")
            html.append(u"<td class=\"mode-label\">{}</td>".format(
                e(self._mode_label(mode_key))))
            for key in (u"r1", u"r3", u"r5", u"r10", u"r20", u"rall"):
                html.append(_cell(summary, denom, key))
            html.append(u"<td>{:.1f} s</td>".format(summary.get("duration", 0.0)))
            html.append(u"<td>{} / {}</td>".format(denom, summary.get("n_scope", 0)))
            html.append(u"</tr>")
            # Δ row from prev mode to this one
            if (prev_summary is not None
                    and prev_summary.get("denom")
                    and summary.get("denom")):
                html.append(u"<tr class=\"row-delta\">")
                html.append(u"<td class=\"mode-label\">Δ ({} − {})</td>".format(
                    e(self._mode_label(mode_key)),
                    e(self._mode_label(prev_mode_key))))
                for key in (u"r1", u"r3", u"r5", u"r10", u"r20", u"rall"):
                    pd = prev_summary["denom"]
                    cd = summary["denom"]
                    pp = 100.0 * prev_summary[key] / pd if pd else 0.0
                    cp = 100.0 * summary[key]      / cd if cd else 0.0
                    d  = cp - pp
                    sign = u"+" if d >= 0 else u"−"
                    html.append(u"<td>{}{:.0f} pp</td>".format(sign, abs(d)))
                html.append(u"<td>{:+.1f} s</td>".format(
                    summary["duration"] - prev_summary["duration"]))
                html.append(u"<td>—</td>")
                html.append(u"</tr>")
            prev_mode_key = mode_key
            prev_summary  = summary

        html.append(u"</tbody></table>")
        html.append(u"</section>")

        # --- Section C2: Exact-correct R@K (canonical correct_uuid only) ---
        # The reranker's purpose is to surface the canonical EPD, not just
        # any acceptable one. R@K against the acceptable_uuids set (which
        # may carry up to 10 candidates per query) can't see reordering
        # within that set - this section can.
        html.append(u"<section class=\"headline\">")
        html.append(u"<h2>Headline metrics — exact correct only "
                    u"(correct_uuid hit, 95% Wilson CI)</h2>")
        html.append(u"<table class=\"metrics\">")
        html.append(u"<thead><tr>"
                    u"<th>Mode</th><th>R@1</th><th>R@3</th><th>R@5</th>"
                    u"<th>R@10</th><th>R@20</th><th>R@All</th><th>Time</th><th>Scope · GT</th>"
                    u"</tr></thead><tbody>")
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            denom = summary.get("denom", 0)
            html.append(u"<tr>")
            html.append(u"<td class=\"mode-label\">{}</td>".format(
                e(self._mode_label(mode_key))))
            for key in (u"r1_corr", u"r3_corr", u"r5_corr",
                        u"r10_corr", u"r20_corr", u"rall_corr"):
                html.append(_cell(summary, denom, key))
            html.append(u"<td>{:.1f} s</td>".format(summary.get("duration", 0.0)))
            html.append(u"<td>{} / {}</td>".format(denom, summary.get("n_scope", 0)))
            html.append(u"</tr>")
        html.append(u"</tbody></table>")
        html.append(u"</section>")

        # --- Section C3: Ranking quality (MRR + nDCG@10) ---------------
        html.append(u"<section class=\"ranking-quality-wrap\">")
        html.append(u"<h2>Ranking quality — MRR and nDCG@10 "
                    u"(95% bootstrap CI, B = 1000)</h2>")
        html.append(u"<table class=\"ranking-quality\">")
        html.append(u"<thead><tr><th>Mode</th>"
                    u"<th>MRR<br><small>(acceptable)</small></th>"
                    u"<th>MRR<br><small>(correct only)</small></th>"
                    u"<th>nDCG@10<br><small>(graded 3/1/0)</small></th>"
                    u"</tr></thead><tbody>")

        def _quality_cell(triple):
            if not triple:
                return u"<td class=\"value\">—</td>"
            pt, lo, hi = triple
            # Colour MRR/nDCG on the same 5-tier value scale (×100) so the
            # ranking-quality table matches the recall tables + dashboard.
            return (u"<td class=\"value\">"
                    u"<span class=\"pt {0}\">{1:.3f}</span>"
                    u"<span class=\"ci\">[{2:.3f}, {3:.3f}]</span></td>".format(
                        _pct_css(100.0 * pt), pt, lo, hi))

        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            html.append(u"<tr>")
            html.append(u"<td class=\"mode-label\">{}</td>".format(
                e(self._mode_label(mode_key))))
            html.append(_quality_cell(summary.get("mrr")))
            html.append(_quality_cell(summary.get("mrr_corr")))
            html.append(_quality_cell(summary.get("ndcg10")))
            html.append(u"</tr>")
        html.append(u"</tbody></table>")
        html.append(u"</section>")

        # --- Section C3b: BIM-context impact (name-only vs +BIM context) ---
        self._emit_bim_context_section_html(html, e, summaries_by_mode)

        # --- Section C4: Pairwise significance (McNemar exact) ---------
        # Two N×N matrices: one at R@1, one at R@10. Cell = two-sided
        # McNemar p-value for "mode-row vs mode-col" on the per-query
        # hit/miss vectors. Highlighted red when p < 0.05.
        modes_present = [m for m in self._REPORT_MODES
                         if summaries_by_mode.get(m) and
                            summaries_by_mode[m].get("denom")]

        def _mcnemar_pair(mode_a, mode_b, k_):
            """p-value for the paired hit/miss vectors at R@k_."""
            sa = summaries_by_mode.get(mode_a) or {}
            sb = summaries_by_mode.get(mode_b) or {}
            ha = (sa.get("hits_per_q_acceptable") or {}).get(k_) or []
            hb = (sb.get("hits_per_q_acceptable") or {}).get(k_) or []
            if not ha or not hb or len(ha) != len(hb):
                return None
            b = sum(1 for i in range(len(ha)) if ha[i] == 1 and hb[i] == 0)
            c = sum(1 for i in range(len(ha)) if ha[i] == 0 and hb[i] == 1)
            return mcnemar_pvalue(b, c)

        def _emit_mcnemar_table(k_):
            html.append(u"<h3>At R@{}</h3>".format(k_))
            html.append(u"<table class=\"mcnemar\">")
            html.append(u"<thead><tr><th>&nbsp;</th>")
            for m in modes_present:
                html.append(u"<th>{}</th>".format(e(self._mode_label(m))))
            html.append(u"</tr></thead><tbody>")
            sig_pairs = []
            for i, row_mode in enumerate(modes_present):
                html.append(u"<tr>")
                html.append(u"<td class=\"row-label\">{}</td>".format(
                    e(self._mode_label(row_mode))))
                for j, col_mode in enumerate(modes_present):
                    if i == j:
                        html.append(u"<td class=\"dash\">—</td>")
                        continue
                    p = _mcnemar_pair(row_mode, col_mode, k_)
                    if p is None:
                        html.append(u"<td class=\"dash\">—</td>")
                        continue
                    cls = u"sig" if p < 0.05 else u"ns"
                    if p < 0.001:
                        p_str = u"&lt;0.001"
                    else:
                        p_str = u"{:.3f}".format(p)
                    html.append(u"<td class=\"{}\">{}</td>".format(cls, p_str))
                    if p < 0.05 and i < j:
                        sig_pairs.append((row_mode, col_mode, p))
                html.append(u"</tr>")
            html.append(u"</tbody></table>")
            if sig_pairs:
                parts = [u"{} vs {} (p = {:.3f})".format(
                    self._mode_label(a), self._mode_label(b), p)
                    for (a, b, p) in sig_pairs]
                html.append(u"<p class=\"mcnemar-note\">Significant pairs "
                            u"(p &lt; 0.05): {}.</p>".format(
                                e(u"; ".join(parts))))
            else:
                html.append(u"<p class=\"mcnemar-note\">No pairwise difference "
                            u"is significant at p &lt; 0.05 — likely a "
                            u"sample-size limitation; ground truth must be "
                            u"expanded to N ≈ 50+ for reliable mode "
                            u"discrimination.</p>")

        html.append(u"<section class=\"pairwise\">")
        html.append(u"<h2>Pairwise mode comparison — McNemar exact two-sided "
                    u"p-values</h2>")
        if len(modes_present) >= 2:
            _emit_mcnemar_table(1)
            _emit_mcnemar_table(10)
        else:
            html.append(u"<p class=\"mcnemar-note\">Need at least two modes "
                        u"with results to compare.</p>")
        html.append(u"</section>")

        # --- Section C5: Latency vs quality ----------------------------
        html.append(u"<section class=\"pareto-wrap\">")
        html.append(u"<h2>Latency vs quality</h2>")
        html.append(u"<table class=\"pareto\">")
        html.append(u"<thead><tr><th>Mode</th><th>Time</th>"
                    u"<th>R@10 (acceptable)</th><th>MRR (acceptable)</th>"
                    u"</tr></thead><tbody>")
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            denom = summary.get("denom", 0)
            r10  = 100.0 * summary.get("r10", 0) / denom if denom else 0.0
            mrr_t  = summary.get("mrr") or (0.0, 0.0, 0.0)
            dur = summary.get("duration", 0.0) or 0.0
            html.append(u"<tr>")
            html.append(u"<td class=\"mode-label\">{}</td>".format(
                e(self._mode_label(mode_key))))
            html.append(u"<td class=\"num\">{:.1f} s</td>".format(dur))
            html.append(u"<td class=\"num\">{:.0f}%</td>".format(r10))
            html.append(u"<td class=\"num\">{:.3f}</td>".format(mrr_t[0]))
            html.append(u"</tr>")
        html.append(u"</tbody></table>")
        html.append(u"</section>")

        # --- Section D: Rank distribution --------------------------
        html.append(u"<section class=\"rank-dist\">")
        html.append(u"<h2>Rank distribution</h2>")
        for run_mode_key in self._REPORT_MODES:
            entries = results_by_mode.get(run_mode_key)
            summary = summaries_by_mode.get(run_mode_key)
            if not entries or not summary or summary["denom"] == 0:
                continue
            dist = self._rank_distribution(entries)
            total = summary["denom"]
            html.append(u"<div class=\"bar-group\">")
            html.append(u"<h3>{}</h3>".format(e(self._mode_label(run_mode_key))))
            for label, count in dist:
                width = (100.0 * count / total) if total else 0.0
                bar_cls = u""
                if label.startswith(u"Miss"):           bar_cls = u" miss"
                elif label.startswith(u"Beyond"):       bar_cls = u" beyond"
                html.append(u"<div class=\"bar-row\">")
                html.append(u"<span class=\"bar-label\">{}</span>".format(e(label)))
                html.append(u"<span class=\"bar\"><span class=\"bar-fill{}\" "
                            u"style=\"width: {:.1f}%;\"></span></span>".format(bar_cls, width))
                html.append(u"<span class=\"bar-count\">{} ({:.0f}%)</span>".format(
                    count, (100.0 * count / total) if total else 0.0))
                html.append(u"</div>")
            html.append(u"</div>")
        html.append(u"</section>")

        # --- Section E: Per-class breakdown ------------------------
        breakdown = self._class_breakdown(results, topk)
        if breakdown:
            html.append(u"<section class=\"class-breakdown\">")
            html.append(u"<h2>Per-class breakdown (showing: {})</h2>".format(e(self._mode_label(mode))))
            html.append(u"<table class=\"classes\">")
            html.append(u"<thead><tr><th>Class</th><th>N</th><th>R@1</th>"
                        u"<th>R@10</th><th>R@All</th></tr></thead><tbody>")
            for cls, b in breakdown:
                def _pc(numer, denom):
                    if denom == 0: return u"—"
                    return u"{}/{} ({:.0f}%)".format(numer, denom, 100.0 * numer / denom)
                html.append(u"<tr><td>{}</td><td class=\"num\">{}</td>"
                            u"<td class=\"num\">{}</td><td class=\"num\">{}</td>"
                            u"<td class=\"num\">{}</td></tr>".format(
                                e(cls), b["n"],
                                _pc(b["r1"], b["n"]),
                                _pc(b["r10"], b["n"]),
                                _pc(b["rall"], b["n"]),
                            ))
            html.append(u"</tbody></table>")
            html.append(u"</section>")

        # --- Section F: Hits / Misses lists ------------------------
        hits, misses, _skipped = self._hits_and_misses(results, topk)
        html.append(u"<section class=\"hits-misses\">")
        html.append(u"<h2>Hits &amp; Misses ({})</h2>".format(e(self._mode_label(mode))))
        html.append(u"<div class=\"cols\"><div class=\"col\">")
        html.append(u"<h3>Hits ({})</h3>".format(len(hits)))
        html.append(u"<ul>")
        if not hits:
            html.append(u"<li class=\"extra\">— no hits within Top-{}</li>".format(topk_label))
        for entry in hits:
            score = entry.get("best_score")
            score_str = u" · score {:.0f}%".format(score * 100.0) if isinstance(score, float) else u""
            html.append(u"<li><span class=\"ok\">✓</span> {name} — Rank {rk}{sc}</li>".format(
                name=e(entry["query"]), rk=entry["hit_rank"], sc=score_str))
        html.append(u"</ul>")
        html.append(u"</div><div class=\"col\">")
        html.append(u"<h3>Misses ({})</h3>".format(len(misses)))
        html.append(u"<ul>")
        if not misses:
            html.append(u"<li class=\"extra\">— no misses</li>")
        for entry in misses:
            best_name  = entry.get("best_name") or u"(no result)"
            best_score = entry.get("best_score")
            sc_str = u"score {:.0f}%".format(best_score * 100.0) if isinstance(best_score, float) else u"n/a"
            html.append(u"<li><span class=\"bad\">✗</span> {q} "
                        u"<span class=\"extra\">— best returned: {bn} ({sc})</span></li>".format(
                            q=e(entry["query"]),
                            bn=e(best_name[:55] + (u"…" if len(best_name) > 55 else u"")),
                            sc=sc_str))
        html.append(u"</ul>")
        html.append(u"</div></div>")
        html.append(u"</section>")

        # --- Section G: Per-query detail cards ---------------------
        html.append(u"<section class=\"details\">")
        html.append(u"<h2>Per-query details ({})</h2>".format(e(self._mode_label(mode))))
        for entry in results:
            query  = entry["query"]
            gt     = entry["gt"]
            qclass = (gt.get("class", u"") if gt else u"")
            label, hit_cls = self._hit_status(entry, topk)

            html.append(u"<div class=\"query-card\">")
            class_tag = (u"<span class=\"class-tag\">{}</span>".format(e(qclass))) if qclass else u""
            html.append(u"<h3>{q}{ct} <span class=\"hit-tag {hc}\">{lbl}</span></h3>".format(
                q=e(query), ct=class_tag, hc=hit_cls, lbl=e(label)))

            # GT context block (only if GT exists)
            if gt is not None:
                ctx_pieces = []
                if gt.get("description"):
                    ctx_pieces.append((u"Description", gt["description"]))
                hosts = gt.get("host_categories") or []
                htypes = gt.get("host_types") or []
                if hosts or htypes:
                    h_str = u", ".join(hosts) if hosts else u""
                    t_str = u", ".join(htypes) if htypes else u""
                    combined = (u"{} ({})".format(h_str, t_str) if (h_str and t_str)
                                else (h_str or t_str))
                    ctx_pieces.append((u"Host", combined))
                if gt.get("expected_classification_path"):
                    ctx_pieces.append((u"Expected classification", tx(gt["expected_classification_path"])))
                if gt.get("lca_reasoning"):
                    ctx_pieces.append((u"LCA reasoning", gt["lca_reasoning"]))

                if ctx_pieces:
                    html.append(u"<div class=\"gt-context\"><dl>")
                    for lbl, val in ctx_pieces:
                        html.append(u"<dt>{}:</dt> <dd>{}</dd>".format(e(lbl), e(val)))
                    html.append(u"</dl></div>")

            # Candidate table
            accept = entry.get("acceptable", set())
            correct_uuid = (gt or {}).get("correct_uuid") or u""
            filtered = [(it, sc) for (it, sc) in entry["ranked_all"]
                        if sc is None or sc >= thr][:topk]
            if not filtered:
                html.append(u"<div class=\"empty\">no candidates above min score "
                            u"({:.0f}%)</div>".format(thr * 100.0))
            else:
                html.append(u"<table>")
                html.append(u"<thead><tr><th style=\"width:34px;\">★</th>"
                            u"<th style=\"width:50px;\">Rank</th>"
                            u"<th style=\"width:60px;\">Score</th>"
                            u"<th>Name</th><th>Classification</th>"
                            u"<th style=\"width:260px;\">UUID</th></tr></thead><tbody>")
                for i, (it, sc) in enumerate(filtered):
                    uuid = it.get("uuid", u"")
                    is_correct = (uuid == correct_uuid)
                    is_hit     = (uuid in accept) if accept else False
                    tr_cls = u""
                    if is_correct: tr_cls = u" class=\"is-correct\""
                    elif is_hit:   tr_cls = u" class=\"is-hit\""
                    star = u"★★" if is_correct else (u"★" if is_hit else u"")
                    html.append(u"<tr{tr}><td class=\"center star\">{st}</td>"
                                u"<td class=\"center\">{rk}</td>"
                                u"<td class=\"right\">{sc:.0f}%</td>"
                                u"<td>{nm}</td><td>{cl}</td>"
                                u"<td class=\"uuid\">{uu}</td></tr>".format(
                                    tr=tr_cls, st=e(star), rk=i + 1, sc=sc * 100.0,
                                    nm=e(it.get("name", u"") or u""),
                                    cl=e(tx(it.get("classification", u""))),
                                    uu=e(uuid)))
                html.append(u"</tbody></table>")

            # "What was expected" panel for misses
            if hit_cls == u"miss" and gt and (gt.get("top_10") or []):
                top1 = gt["top_10"][0]
                html.append(u"<div class=\"expected\">")
                html.append(u"<div class=\"label\">What was expected (canonical Rank 1):</div>")
                html.append(u"<div class=\"name\">{} <span class=\"uuid\">{}</span></div>".format(
                    e(top1.get("name", u"")), e(top1.get("uuid", u""))))
                if top1.get("classification"):
                    html.append(u"<div class=\"extra\">{}</div>".format(e(tx(top1["classification"]))))
                if top1.get("rationale"):
                    html.append(u"<div class=\"rationale\">{}</div>".format(e(top1["rationale"])))
                html.append(u"</div>")

            html.append(u"</div>")  # /query-card
        html.append(u"</section>")

        # --- Section H: Methodology footer -------------------------
        html.append(u"<section class=\"methodology\">")
        html.append(u"<h2>Methodology</h2>")
        html.append(u"<ul>")
        html.append(u"<li><strong>Recall@K (acceptable set)</strong>: fraction of "
                    u"ground-truth-match queries whose first acceptable UUID lands "
                    u"within the top-K returned candidates. R@All counts a hit "
                    u"anywhere in the full ranked list.</li>")
        html.append(u"<li><strong>Recall@K (exact correct)</strong>: same, but a "
                    u"hit means the row's UUID equals the canonical "
                    u"<code>correct_uuid</code>. Distinguishes \"the right EPD\" "
                    u"from \"any acceptable EPD\" — the reranker's job is most "
                    u"visible here.</li>")
        html.append(u"<li><strong>Wilson 95% CI</strong> on every R@K cell: "
                    u"binomial confidence interval after Wilson 1927. Far less "
                    u"degenerate than the normal approximation at small N or "
                    u"near 0%/100%.</li>")
        html.append(u"<li><strong>MRR</strong> (mean reciprocal rank): mean of "
                    u"1/rank of the first hit per query (0 if missed). Reported "
                    u"separately for the acceptable set and the correct-only "
                    u"hit.</li>")
        html.append(u"<li><strong>Bootstrap CI</strong> on MRR: "
                    u"1000-replicate percentile interval (Efron &amp; Tibshirani "
                    u"1993; seed = 42 for reproducibility).</li>")
        html.append(u"<li><strong>McNemar exact</strong> (1947): two-sided "
                    u"binomial p-value on the discordant pairs of paired hit/miss "
                    u"vectors. Computed for every mode-pair at R@1 and R@10. "
                    u"Exact (not chi-squared) — stays valid on the small "
                    u"ground-truth sizes typical of construction LCA.</li>")
        html.append(u"<li><strong>Denominator</strong>: only queries whose GT entry "
                    u"has <code>label: \"match\"</code> are counted. Entries "
                    u"labeled <code>skip</code> or with no GT match are still "
                    u"shown in the per-query details but excluded from every "
                    u"aggregate.</li>")
        html.append(u"<li><strong>Acceptable set</strong>: union of "
                    u"<code>correct_uuid</code> and <code>acceptable_uuids</code> "
                    u"from each GT entry. The <code>correct_uuid</code> is "
                    u"highlighted in the per-query tables with a double star "
                    u"(★★).</li>")
        html.append(u"<li><strong>Search backends</strong>: <em>Regex</em> "
                    u"(template scoring) and <em>Fuzzy</em> (3-tier Levenshtein) "
                    u"— both BM25F-ranked (Robertson &amp; Spärck-Jones 1976; "
                    u"Zaragoza et al. 2004). <em>Semantic</em> (cosine over "
                    u"bge-m3 embeddings via Ollama). <em>Hybrid</em> (Reciprocal "
                    u"Rank Fusion of BM25F + cosine, k=60, Cormack et al. 2009). "
                    u"<em>Hybrid+Rerank</em> (top-K from Hybrid passed through "
                    u"BAAI/bge-reranker-v2-m3 cross-encoder for joint-attention "
                    u"re-scoring). All modes share the same calibrated "
                    u"<code>Score%</code> via <code>ConfidenceCalibrator</code> "
                    u"(<code>MAX_CONFIDENCE = 0.97</code> cap).</li>")
        html.append(u"</ul>")

        # Methodology blockquote pulled from the GT JSON
        if self._gt_meta.get("methodology") or self._gt_meta.get("ranking_basis"):
            html.append(u"<blockquote>")
            html.append(u"<p><strong>Ground-truth construction (from GT JSON):</strong></p>")
            if self._gt_meta.get("methodology"):
                html.append(u"<p>{}</p>".format(e(self._gt_meta["methodology"])))
            if self._gt_meta.get("ranking_basis"):
                html.append(u"<ul>")
                for item in self._gt_meta["ranking_basis"]:
                    html.append(u"<li>{}</li>".format(e(item)))
                html.append(u"</ul>")
            html.append(u"</blockquote>")
        html.append(u"</section>")

        html.append(u"</body></html>")
        return u"\n".join(html) + u"\n"

    # ── Markdown report ─────────────────────────────────────────────

    def _build_markdown_report(self, mode, results):
        topk = self._current_topk()
        thr  = self._current_threshold()
        ui_lang = self._current_lang()
        # Localise classification paths the same way the on-screen grid does.
        tx = lambda path: translate_path(path or u"", ui_lang)
        run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        topk_label = unicode(topk)

        # Cover all benchmarked modes (regex / fuzzy / semantic / hybrid /
        # hybrid_reranked) so the headline metrics + rank distribution +
        # executive summary all reflect the full stack.
        results_by_mode = dict(
            (m, self._results.get(m)) for m in self._REPORT_MODES)
        summaries_by_mode = dict(
            (m, self._summary.get(m)) for m in self._REPORT_MODES)

        L = []
        def w(line=u""):
            L.append(line)

        w(u"# Validation Report — Phase 0 Recall@K Benchmark")
        w()
        w(u"_Generated {} · ÖKOBAUDAT {} ({})_".format(
            run_ts, self._current_datastock(), self._current_lang().upper()))
        w()

        # --- A. Run configuration ---
        w(u"## Run configuration")
        w()
        w(u"| Field | Value |")
        w(u"|---|---|")
        rows = [
            (u"Datastock",      u"{} ({})".format(self._current_datastock(), self._current_lang().upper())),
            (u"Scope mode",     self._current_scope_key().replace(u"_", u" ")),
            (u"Scope size",     u"{} material(s)".format(len(self._scope_names))),
            (u"Ground truth",   u"`{}`".format(os.path.basename(self._gt_path or u""))),
            (u"Dataset cache",  u"{:,} EPDs".format(len(self._datasets))),
            (u"Cache snapshot", self._cache_provenance()),
            (u"Top-K cutoff",   topk_label),
            (u"Min score",      u"{:.0f}%".format(thr * 100.0)),
            (u"Showing (cards)", self._mode_label(mode)),
        ]
        if self._gt_meta.get("dataset_count"):
            rows.append((u"GT dataset_count", unicode(self._gt_meta["dataset_count"])))
        if self._gt_meta.get("matched_count") is not None:
            rows.append((u"GT matched / skipped",
                         u"{} / {}".format(self._gt_meta.get("matched_count", u"?"),
                                           self._gt_meta.get("skipped_count", u"?"))))
        if self._gt_meta.get("version"):
            rows.append((u"GT version", unicode(self._gt_meta["version"])))
        if self._gt_meta.get("source_revit_model"):
            rows.append((u"Source model", unicode(self._gt_meta["source_revit_model"])))
        if self._gt_meta.get("validation_status"):
            rows.append((u"GT status", unicode(self._gt_meta["validation_status"])))
        for k, v in rows:
            w(u"| **{}** | {} |".format(self._md_escape_cell(k), self._md_escape_cell(v)))
        w()

        # --- B. Executive summary - removed 2026-07-05 (see HTML builder) ---

        # --- C. Headline metrics (acceptable set) ---
        w(u"## Headline metrics — acceptable set (R@K with 95% Wilson CI)")
        w()
        w(u"| Mode | R@1 | R@3 | R@5 | R@10 | R@20 | R@All | Time | Scope · GT |")
        w(u"|---|---:|---:|---:|---:|---:|---:|---:|---|")

        def _md_cell(summary, denom, key, ci_suffix=u"_ci"):
            """Render a R@K cell as `pct% (hits/denom) [lo-hi]%`.
            Pass ci_suffix=None to suppress the CI (used in Δ rows)."""
            if not summary or denom == 0:
                return u"—"
            hits = summary.get(key, 0)
            pct  = 100.0 * hits / denom
            ci_str = u""
            if ci_suffix:
                ci = summary.get(key + ci_suffix)
                if ci and (ci[1] > 0 or ci[2] > 0):
                    ci_str = u" [{:.0f}-{:.0f}%]".format(
                        ci[1] * 100.0, ci[2] * 100.0)
            return u"{:d}% ({}/{}){}".format(
                int(round(pct)), hits, denom, ci_str)

        # Iterate every mode that ran, in thesis-progression order, and
        # emit a Δ row after each non-baseline mode showing its lift over
        # the previous mode that ran.
        prev_mode_key = None
        prev_summary  = None
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary:
                continue
            denom = summary.get("denom", 0)
            cells = [_md_cell(summary, denom, k)
                     for k in (u"r1", u"r3", u"r5", u"r10", u"r20", u"rall")]
            w(u"| **{}** | {} | {} | {} | {} | {} | {} | {:.1f} s | {} / {} |".format(
                self._mode_label(mode_key),
                cells[0], cells[1], cells[2], cells[3], cells[4], cells[5],
                summary["duration"], denom, summary.get("n_scope", 0)))
            if (prev_summary is not None
                    and prev_summary.get("denom")
                    and summary.get("denom")):
                d_cells = []
                for key in (u"r1", u"r3", u"r5", u"r10", u"r20", u"rall"):
                    pd = prev_summary["denom"]
                    cd = summary["denom"]
                    pp = 100.0 * prev_summary[key] / pd if pd else 0.0
                    cp = 100.0 * summary[key]      / cd if cd else 0.0
                    d  = cp - pp
                    d_cells.append(u"{:+.0f} pp".format(d))
                w(u"| _Δ ({} − {})_ | {} | {} | {} | {} | {} | {} | {:+.1f} s | — |".format(
                    self._mode_label(mode_key), self._mode_label(prev_mode_key),
                    d_cells[0], d_cells[1], d_cells[2], d_cells[3], d_cells[4], d_cells[5],
                    summary["duration"] - prev_summary["duration"]))
            prev_mode_key = mode_key
            prev_summary  = summary
        w()

        # --- C2. Exact-correct R@K ---
        w(u"## Headline metrics — exact correct only (correct_uuid hit, 95% Wilson CI)")
        w()
        w(u"| Mode | R@1 | R@3 | R@5 | R@10 | R@20 | R@All | Time | Scope · GT |")
        w(u"|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            denom = summary.get("denom", 0)
            cells = [_md_cell(summary, denom, k)
                     for k in (u"r1_corr", u"r3_corr", u"r5_corr",
                               u"r10_corr", u"r20_corr", u"rall_corr")]
            w(u"| **{}** | {} | {} | {} | {} | {} | {} | {:.1f} s | {} / {} |".format(
                self._mode_label(mode_key),
                cells[0], cells[1], cells[2], cells[3], cells[4], cells[5],
                summary["duration"], denom, summary.get("n_scope", 0)))
        w()

        # --- C3. Ranking quality ---
        w(u"## Ranking quality — MRR and nDCG@10 (95% bootstrap CI, B = 1000)")
        w()
        w(u"| Mode | MRR (acceptable) | MRR (correct only) | nDCG@10 (graded 3/1/0) |")
        w(u"|---|---:|---:|---:|")

        def _quality_md(triple):
            if not triple:
                return u"—"
            pt, lo, hi = triple
            return u"**{:.3f}** [{:.3f}, {:.3f}]".format(pt, lo, hi)

        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            w(u"| **{}** | {} | {} | {} |".format(
                self._mode_label(mode_key),
                _quality_md(summary.get("mrr")),
                _quality_md(summary.get("mrr_corr")),
                _quality_md(summary.get("ndcg10"))))
        w()

        # --- C3b. BIM-context impact (name-only vs +BIM context) ---
        self._emit_bim_context_section_md(w)

        # --- C4. Pairwise McNemar significance ---
        w(u"## Pairwise mode comparison — McNemar exact two-sided p-values")
        w()
        modes_present_md = [m for m in self._REPORT_MODES
                            if summaries_by_mode.get(m) and
                               summaries_by_mode[m].get("denom")]

        def _mcnemar_pair_md(mode_a, mode_b, k_):
            sa = summaries_by_mode.get(mode_a) or {}
            sb = summaries_by_mode.get(mode_b) or {}
            ha = (sa.get("hits_per_q_acceptable") or {}).get(k_) or []
            hb = (sb.get("hits_per_q_acceptable") or {}).get(k_) or []
            if not ha or not hb or len(ha) != len(hb):
                return None
            b = sum(1 for i in range(len(ha)) if ha[i] == 1 and hb[i] == 0)
            c = sum(1 for i in range(len(ha)) if ha[i] == 0 and hb[i] == 1)
            return mcnemar_pvalue(b, c)

        def _emit_mcnemar_md(k_):
            w(u"### At R@{}".format(k_))
            w()
            header = u"| | " + u" | ".join(
                self._mode_label(m) for m in modes_present_md) + u" |"
            w(header)
            w(u"|---|" + u"|".join(u"---:" for _ in modes_present_md) + u"|")
            sig_pairs = []
            for i, row_mode in enumerate(modes_present_md):
                cells = [u"**{}**".format(self._mode_label(row_mode))]
                for j, col_mode in enumerate(modes_present_md):
                    if i == j:
                        cells.append(u"—")
                        continue
                    p = _mcnemar_pair_md(row_mode, col_mode, k_)
                    if p is None:
                        cells.append(u"—")
                        continue
                    if p < 0.001:
                        p_str = u"<0.001"
                    else:
                        p_str = u"{:.3f}".format(p)
                    if p < 0.05:
                        cells.append(u"**{}** ⚠".format(p_str))
                        if i < j:
                            sig_pairs.append((row_mode, col_mode, p))
                    else:
                        cells.append(p_str)
                w(u"| " + u" | ".join(cells) + u" |")
            w()
            if sig_pairs:
                parts = [u"{} vs {} (p = {:.3f})".format(
                    self._mode_label(a), self._mode_label(b), p)
                    for (a, b, p) in sig_pairs]
                w(u"_Significant pairs (p < 0.05): {}._".format(
                    u"; ".join(parts)))
            else:
                w(u"_No pairwise difference is significant at p < 0.05 — "
                  u"likely a sample-size limitation; ground truth must be "
                  u"expanded to N ≈ 50+ for reliable mode "
                  u"discrimination._")
            w()

        if len(modes_present_md) >= 2:
            _emit_mcnemar_md(1)
            _emit_mcnemar_md(10)
        else:
            w(u"_Need at least two modes with results to compare._")
            w()

        # --- C5. Latency vs quality ---
        w(u"## Latency vs quality")
        w()
        w(u"| Mode | Time | R@10 (acceptable) | MRR (acceptable) |")
        w(u"|---|---:|---:|---:|")
        for mode_key in self._REPORT_MODES:
            summary = summaries_by_mode.get(mode_key)
            if not summary or not summary.get("denom"):
                continue
            denom = summary.get("denom", 0)
            r10  = 100.0 * summary.get("r10", 0) / denom if denom else 0.0
            mrr_t  = summary.get("mrr") or (0.0, 0.0, 0.0)
            dur = summary.get("duration", 0.0) or 0.0
            w(u"| **{}** | {:.1f} s | {:.0f}% | {:.3f} |".format(
                self._mode_label(mode_key), dur, r10,
                mrr_t[0]))
        w()

        # --- D. Rank distribution ---
        w(u"## Rank distribution")
        w()
        for run_mode_key in self._REPORT_MODES:
            entries = results_by_mode.get(run_mode_key)
            summary = summaries_by_mode.get(run_mode_key)
            if not entries or not summary or summary["denom"] == 0:
                continue
            w(u"### {}".format(self._mode_label(run_mode_key)))
            w()
            w(u"| Bin | Count | % | Bar |")
            w(u"|---|---:|---:|---|")
            dist = self._rank_distribution(entries)
            total = summary["denom"]
            for label, count in dist:
                pct = (100.0 * count / total) if total else 0.0
                bar_n = int(round(pct / 5.0))   # 1 block per 5%
                bar = (u"█" * bar_n) if bar_n > 0 else u""
                w(u"| {} | {} | {:.0f}% | `{}` |".format(label, count, pct, bar))
            w()

        # --- E. Per-class breakdown ---
        breakdown = self._class_breakdown(results, topk)
        if breakdown:
            w(u"## Per-class breakdown ({})".format(self._mode_label(mode)))
            w()
            w(u"| Class | N | R@1 | R@10 | R@All |")
            w(u"|---|---:|---:|---:|---:|")
            for cls, b in breakdown:
                def _pc(numer, denom):
                    if denom == 0: return u"—"
                    return u"{}/{} ({:.0f}%)".format(numer, denom, 100.0 * numer / denom)
                w(u"| {} | {} | {} | {} | {} |".format(
                    self._md_escape_cell(cls), b["n"],
                    _pc(b["r1"], b["n"]),
                    _pc(b["r10"], b["n"]),
                    _pc(b["rall"], b["n"])))
            w()

        # --- F. Hits / Misses ---
        hits, misses, _skipped = self._hits_and_misses(results, topk)
        w(u"## Hits & Misses ({})".format(self._mode_label(mode)))
        w()
        w(u"### Hits ({})".format(len(hits)))
        w()
        if not hits:
            w(u"_— no hits within Top-{}_".format(topk_label))
        else:
            for entry in hits:
                score = entry.get("best_score")
                sc_str = u" · score {:.0f}%".format(score * 100.0) if isinstance(score, float) else u""
                w(u"- ✓ **{}** — Rank {}{}".format(
                    self._md_escape_cell(entry["query"]), entry["hit_rank"], sc_str))
        w()
        w(u"### Misses ({})".format(len(misses)))
        w()
        if not misses:
            w(u"_— no misses_")
        else:
            for entry in misses:
                best_name  = entry.get("best_name") or u"(no result)"
                best_score = entry.get("best_score")
                sc_str = u"score {:.0f}%".format(best_score * 100.0) if isinstance(best_score, float) else u"n/a"
                w(u"- ✗ **{}** — best returned: {} ({})".format(
                    self._md_escape_cell(entry["query"]),
                    self._md_escape_cell(best_name[:55] + (u"…" if len(best_name) > 55 else u"")),
                    sc_str))
        w()

        # --- G. Per-query details ---
        w(u"## Per-query details ({})".format(self._mode_label(mode)))
        w()
        for entry in results:
            query  = entry["query"]
            gt     = entry["gt"]
            qclass = (gt.get("class", u"") if gt else u"")
            label, _hit_cls = self._hit_status(entry, topk)

            w(u"### {} — {}{}".format(
                query, label,
                u" _(class: {})_".format(qclass) if qclass else u""))
            w()

            # GT context
            if gt is not None:
                if gt.get("description"):
                    w(u"- **Description**: {}".format(gt["description"]))
                hosts  = gt.get("host_categories") or []
                htypes = gt.get("host_types") or []
                if hosts or htypes:
                    h_str = u", ".join(hosts) if hosts else u""
                    t_str = u", ".join(htypes) if htypes else u""
                    combined = (u"{} ({})".format(h_str, t_str) if (h_str and t_str)
                                else (h_str or t_str))
                    w(u"- **Host**: {}".format(combined))
                if gt.get("expected_classification_path"):
                    w(u"- **Expected classification**: {}".format(tx(gt["expected_classification_path"])))
                if gt.get("lca_reasoning"):
                    w(u"- **LCA reasoning**: {}".format(gt["lca_reasoning"]))
                if any([gt.get("description"), hosts, htypes,
                        gt.get("expected_classification_path"), gt.get("lca_reasoning")]):
                    w()

            # Candidates table
            accept = entry.get("acceptable", set())
            correct_uuid = (gt or {}).get("correct_uuid") or u""
            filtered = [(it, sc) for (it, sc) in entry["ranked_all"]
                        if sc is None or sc >= thr][:topk]
            if not filtered:
                w(u"_no candidates above min score ({:.0f}%)_".format(thr * 100.0))
                w()
                continue
            w(u"| ★ | Rank | Score | Name | Classification | UUID |")
            w(u"|---|---:|---:|---|---|---|")
            for i, (it, sc) in enumerate(filtered):
                uuid = it.get("uuid", u"")
                is_correct = (uuid == correct_uuid)
                is_hit     = (uuid in accept) if accept else False
                star = u"★★" if is_correct else (u"★" if is_hit else u"")
                w(u"| {} | {} | {:.0f}% | {} | {} | `{}` |".format(
                    star, i + 1, sc * 100.0,
                    self._md_escape_cell((it.get("name", u"") or u"")[:70]),
                    self._md_escape_cell(tx(it.get("classification", u""))[:60]),
                    uuid))
            w()

            # Expected (for misses)
            if _hit_cls == u"miss" and gt and (gt.get("top_10") or []):
                top1 = gt["top_10"][0]
                w(u"> **What was expected (canonical Rank 1):** "
                  u"{} `{}`".format(top1.get("name", u""), top1.get("uuid", u"")))
                if top1.get("rationale"):
                    w(u">")
                    w(u"> _{}_".format(top1["rationale"]))
                w()

        # --- H. Methodology footer ---
        w(u"## Methodology")
        w()
        w(u"- **Recall@K (acceptable set)**: fraction of ground-truth-match queries "
          u"whose first acceptable UUID lands within the top-K returned candidates. "
          u"R@All counts a hit anywhere in the full ranked list.")
        w(u"- **Recall@K (exact correct)**: same, but a hit means the row's UUID "
          u"equals the canonical `correct_uuid`. Distinguishes \"the right EPD\" from "
          u"\"any acceptable EPD\" — the reranker's job is most visible here.")
        w(u"- **Wilson 95% CI** on every R@K cell: binomial CI after Wilson 1927. "
          u"Far less degenerate than the normal approximation at small N or near 0%/100%.")
        w(u"- **MRR** (mean reciprocal rank): mean of 1/rank of the first hit per query "
          u"(0 if missed). Reported separately for the acceptable set and the "
          u"correct-only hit.")
        w(u"- **Bootstrap CI** on MRR: 1000-replicate percentile interval "
          u"(Efron & Tibshirani 1993; seed = 42 for reproducibility).")
        w(u"- **McNemar exact** (1947): two-sided binomial p-value on the discordant "
          u"pairs of paired hit/miss vectors. Computed for every mode-pair at R@1 and "
          u"R@10. Exact (not chi-squared) — stays valid on the small ground-truth "
          u"sizes typical of construction LCA.")
        w(u"- **Denominator**: only queries with GT entry `label: \"match\"` are counted; "
          u"`skip` entries and queries with no GT match are excluded from every "
          u"aggregate.")
        w(u"- **Acceptable set**: union of `correct_uuid` and `acceptable_uuids` from "
          u"each GT entry. The `correct_uuid` is marked with ★★ in the per-query "
          u"tables.")
        w(u"- **Search backends**: _Regex_ (template scoring) and _Fuzzy_ (3-tier "
          u"Levenshtein) — both BM25F-ranked (Robertson & Spärck-Jones 1976; Zaragoza "
          u"et al. 2004). _Semantic_ (cosine over bge-m3 embeddings via Ollama). "
          u"_Hybrid_ (Reciprocal Rank Fusion of BM25F + cosine, k=60, Cormack et al. "
          u"2009). _Hybrid+Rerank_ (top-K from Hybrid passed through "
          u"BAAI/bge-reranker-v2-m3 cross-encoder). All modes share the same "
          u"calibrated `Score%` via `ConfidenceCalibrator` (`MAX_CONFIDENCE = 0.97` "
          u"cap).")
        w()

        if self._gt_meta.get("methodology") or self._gt_meta.get("ranking_basis"):
            w(u"### Ground-truth construction (from GT JSON)")
            w()
            if self._gt_meta.get("methodology"):
                w(u"> {}".format(self._gt_meta["methodology"]))
                w(u">")
            if self._gt_meta.get("ranking_basis"):
                for item in self._gt_meta["ranking_basis"]:
                    w(u"> - {}".format(item))
            w()

        return u"\n".join(L) + u"\n"


# ──────────────────────────────────────────────────────────────────
# Entry point (with Pick-Elements / Pick-Materials re-launch loop)
# ──────────────────────────────────────────────────────────────────

def _pick_revit_elements():
    """Let the user pick elements on the canvas; return {mat_name: Material}.

    Returns None if the user cancelled (Esc or no selection), {} on a real
    error. Cancellation is silent; real errors are logged and re-raised so
    they don't disappear into a silent script exit.
    """
    try:
        sel = revit.uidoc.Selection
        picked = sel.PickObjects(
            revit.UI.Selection.ObjectType.Element,
            "Pick elements whose materials should be validated. Click Finish on the ribbon when done."
        )
    except Exception as ex:
        # The Revit API throws OperationCanceledException when the user
        # presses Esc - that's a normal cancel, not a real error.
        msg = (unicode(ex) if ex else u"").lower()
        type_str = unicode(type(ex).__name__).lower()
        if (u"cancel" in msg or u"abort" in msg or
                u"operationcanceledexception" in type_str):
            return None
        logger.error(u"PickObjects failed: {}\n{}".format(ex, traceback.format_exc()))
        raise
    if not picked:
        return None
    doc = revit.doc
    mats = {}
    for ref in picked:
        try:
            el = doc.GetElement(ref.ElementId)
            mat_ids = el.GetMaterialIds(False)
        except Exception:
            continue
        for mid in mat_ids:
            m = doc.GetElement(mid)
            if m is not None and m.Name and m.Name not in mats:
                mats[m.Name] = m
    return mats


def _pick_revit_materials():
    """Show pyRevit's material picker; return {mat_name: Material} or None on cancel."""
    try:
        doc = revit.doc
        all_mats = list(DB.FilteredElementCollector(doc).OfClass(DB.Material).ToElements())
        all_mats.sort(key=lambda m: m.Name)
        names = [m.Name for m in all_mats]
        sel = forms.SelectFromList.show(
            names, multiselect=True, title="Pick Materials to Validate",
            button_name="Use Selection"
        )
        if not sel:
            return None
        sel_set = set(sel)
        return {m.Name: m for m in all_mats if m.Name in sel_set}
    except Exception as ex:
        logger.error(u"Material picker failed: {}\n{}".format(ex, traceback.format_exc()))
        return None


def main():
    settings = None
    pre_materials = None
    while True:
        # __init__ calls ShowDialog internally - blocks until the user
        # closes the window or picks a Pick-scope option.
        win = ValidationWindow(pre_materials=pre_materials, settings=settings)

        pending = getattr(win, "_pending_pick", None)
        if not pending:
            break

        # Snapshot settings before re-opening.
        settings = win._load_settings_file()

        if pending == u"pick_elements":
            picked = _pick_revit_elements()
        else:
            picked = _pick_revit_materials()

        # Picker cancelled - drop back to a fresh window with no pre-materials
        # but the previous scope reverted to a non-pick option so we don't
        # immediately re-enter the pick flow.
        if picked is None:
            pre_materials = None
            settings["scope"] = u"elements_view"
            continue

        # Empty result (no materials in picked elements) - same fallback.
        if not picked:
            forms.alert(u"No materials found on the picked element(s).",
                        title=u"Validation")
            pre_materials = None
            settings["scope"] = u"elements_view"
            continue

        pre_materials = picked


if __name__ == "__main__":
    main()
