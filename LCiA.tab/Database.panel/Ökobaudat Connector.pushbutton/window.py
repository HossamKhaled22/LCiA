# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: GPL-3.0-or-later
"""
AssignMaterialWindow -- WPF UI for the MaterialsDatasets tool.

Orchestrates OekobaudatClient (API + cache) and RevitMaterialMapper (Revit writes).
All UI event handlers live here; business logic is delegated to the other modules.
"""
#pylint: disable=import-error,invalid-name,broad-except
import os
import re
import json
import time
import traceback

import clr
clr.AddReference("System")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Windows.Forms")
from System.Windows.Controls import (DataGridTextColumn, DataGridLength, DataGridLengthUnitType, TextBlock,
                                     Button as WpfButton, Border as WpfBorder,
                                     Orientation, StackPanel as WpfStackPanel,
                                     WrapPanel as WpfWrapPanel,
                                     Grid as WpfGrid, ColumnDefinition, RowDefinition,
                                     ScrollViewer as WpfScrollViewer, ComboBoxItem)
from System.Windows.Data import Binding
from System.Windows import Visibility, Style, Setter, TextWrapping, VerticalAlignment
from System.Windows import MessageBox, MessageBoxButton, MessageBoxResult
from System.Windows import Thickness, HorizontalAlignment, FontWeights, FontStyles, GridLength
from System.Windows import GridUnitType
from System.Windows.Media import SolidColorBrush, Color
from System.Windows.Input import Cursors
from System.Windows.Documents import Run, Bold, Italic
from System.Threading import Thread, ThreadStart
from System.Windows import DragDrop, DragDropEffects, DataObject, SystemParameters
from System.Windows.Input import MouseButtonState
import System

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.DB import FilteredElementCollector

import constants
from models import MaterialDataset, IndicatorRow
from System.Windows.Input import Key
from System.Windows.Threading import DispatcherTimer
from System import TimeSpan
from search_helpers import (
    _match, _fuzzy_match_inner, _max_distance, _fuzzy_score, REGEX_TEMPLATES,
    # Phase 0 v7 - BM25F ranking + mode-agnostic confidence layer
    build_searchable, CorpusStats, IsotonicCalibrator,
    bm25f_score, explain_score, expand_query_with_typos, soft_bound,
    ConfidenceCalibrator,
    # Phase 1 - dense semantic retrieval
    EmbeddingIndex, OllamaEmbeddingClient,
    cosine_similarity, build_semantic_haystack,
    # Phase 1+ - Reciprocal Rank Fusion (hybrid BM25F + semantic)
    reciprocal_rank_fusion, rrf_max_score, bm25f_ranking,
    # Phase 1+ - Cross-encoder reranker (local Python sidecar)
    LocalRerankerClient,
)
from api_client import OekobaudatClient
from revit_mapper import RevitMaterialMapper
import attribute_extractor
# Phase 1+ T5 - shared BIM-context query builder (same module the offline
# ablation used; IronPython-2.7 safe, no project imports). Enriches the
# SEMANTIC / reranker query with the picked material's textual context.
import query_context

logger = script.get_logger()


# Materials Classification - German -> English label translations are
# now in `classification_labels.py` (shared with the Validation pushbutton
# and the search_helpers.build_searchable haystack translator). Importing
# the dict by its original name keeps every existing reference in this
# file working unchanged.
from classification_labels import _DE_TO_EN_LABELS, translate_path

PATH_SCRIPT       = os.path.dirname(__file__)
XAML_FILE         = "OkobaudatConnector.xaml"

uidoc = __revit__.ActiveUIDocument
doc   = uidoc.Document


# Holds the item currently being dragged (dataset or indicator row).
# Module-level dict so handlers can mutate it without closure issues.
_DRAG_ITEM = {"type": None, "data": None}



class RegexTemplateItem(object):
    def __init__(self, label, description, template, placeholder):
        self.Label       = label
        self.Description = description
        self.Template    = template
        self.Placeholder = placeholder


# ── BIM-context field registry - mirrors the Validation pushbutton's field
#    picker (Validation script.py:512-528) so the live Connector enriches the
#    SAME field sets the offline ladder / Validation tools measure. ──
_CTX_FIELD_ORDER = ["class", "description", "concrete_grade",
                    "structural_class", "host_categories", "density",
                    "thermal_conductivity", "host_types"]
_CTX_FIELD_LABEL = {
    "class":                u"class",
    "description":          u"description",
    "concrete_grade":       u"concrete grade",
    "structural_class":     u"struct class",
    "host_categories":      u"categories",
    "density":              u"density",
    "thermal_conductivity": u"λ thermal",
    "host_types":           u"host types",
}
_CTX_NOISE_FIELDS = set(["density", "thermal_conductivity", "host_types"])
# Default tick set in the picker = the shipped best combo minus 'name'.
_CTX_DEFAULT_FIELDS = set(["class", "description",
                           "concrete_grade", "structural_class"])


class AssignMaterialWindow(forms.WPFWindow):

    def __init__(self):
        forms.WPFWindow.__init__(self, os.path.join(PATH_SCRIPT, XAML_FILE))
        self.Title = constants.TOOL_TITLE
        self.footer_version.Text = constants.TOOL_VERSION

        # Collaborators
        self._client = OekobaudatClient(
            cache_dir=os.path.join(PATH_SCRIPT, "LCiA_Extension_Cache")
        )
        self._mapper = RevitMaterialMapper(doc, logger)

        # UI state
        self.datasets          = []
        self.datasets_sorted   = []
        self.selected_dataset  = None
        self.search_mode_materials = 'fuzzy'
        self._current_source   = "a2_sphera"
        self._compliance       = "A2"
        self._lang             = "de"
        self._sort_mode        = 0          # 0=Default, 1=A-Z, 2=Z-A
        self._datasets_original = []
        self._grid_filters     = {}         # {column_tag: filter_value}
        self._filtered_datasets = []
        self._filter_controls  = {}         # {tag: WPF control}
        self._indicator_cache  = {}         # {uuid: {prop: val, attributes: {}}}

        # BIM-context live enrichment state - stateful so the field chips in the
        # always-visible bar can rebuild the search query without re-opening the
        # material picker.
        self._ctx_material    = None   # picked active-view material dict, or None
        self._ctx_field_chips = {}     # field_key -> ToggleButton (built lazily)
        self._ctx_last_query  = None   # last query WE wrote (safe toggle-off clear)

        # Phase 0 v7 - BM25F ranking state (rebuilt on every dataset reload).
        # `_corpus_stats` is a CorpusStats; `_calibrator` an IsotonicCalibrator
        # (or None) loaded from LCiA_Extension_Cache/calibration_*.json;
        # `_bm25_params` overrides the module defaults when a tuned
        # bm25_params_v1.json is present. All three are optional - the live
        # UI falls back to legacy `_fuzzy_score` if `_corpus_stats` is None.
        self._corpus_stats      = None
        self._calibrator        = None
        self._bm25_params       = None
        # Phase 1+ T5 - tuned hybrid-fusion params (symmetric pool +
        # per-ranker weights from tools/tune_fusion.py). Loaded per
        # (source, lang) in _build_corpus_stats; None ⇒ live default
        # (asymmetric pool, k=HYBRID_RRF_K, equal weights).
        self._hybrid_params     = None
        # Phase 0 → 1 bridge: per-mode confidence calibrators producing
        # the displayed Score% and driving the Min Score filter. Keyed by
        # search mode ("bm25f" today; "semantic" / "hybrid" in Phase 1+).
        # Sigmoid defaults take over when no `confidence_<mode>_<src>_<lang>.json`
        # is present in the cache dir, so the UI works without any fit step.
        self._confidence_cals   = {}
        # Phase 1 - semantic search backing state. Loaded per
        # (source, lang) in _load_confidence_calibrators alongside the
        # confidence calibrators. `_semantic_available` is the gate the
        # mode toggle and `_apply_grid_filters` check before scoring
        # via cosine.
        self._embedding_index    = None
        self._ollama_client      = None
        self._semantic_available = False
        # Phase 1.5 - auto-build subprocess state. `_prefetch_procs` maps
        # (source, lang) → System.Diagnostics.Process for builds that
        # are currently running in the background. The DispatcherTimer
        # polls these every 3 s; on exit-code 0 we reload the .bin and
        # turn semantic mode on without a restart. `_python_exe_cache`
        # caches the discovered CPython 3 path (or the "NOT_FOUND"
        # sentinel) so we don't probe `python --version` on every
        # datastock switch.
        self._prefetch_procs     = {}
        self._prefetch_timer     = None
        self._python_exe_cache   = None
        # Materials Classification chip data - initialized here (not in
        # _populate_advanced_combos) so that _refresh_classification_chip_labels
        # works on the very first window open, before the user has expanded
        # the Filters panel. Only the second tuple element (German string)
        # matters; the third is a leftover hardcoded English label that's
        # now ignored - _chip_label looks up _DE_TO_EN_LABELS instead.
        self._classification_roots = [
            ('tgl_cls_mineral',    u'Mineralische Baustoffe',                       u'Mineral Construction'),
            ('tgl_cls_wood',       u'Holz',                                          u'Wood'),
            ('tgl_cls_metals',     u'Metalle',                                       u'Metals'),
            ('tgl_cls_plastics',   u'Kunststoffe',                                   u'Plastics'),
            ('tgl_cls_insulation', u'Dämmstoffe',                                    u'Insulation'),
            ('tgl_cls_coatings',   u'Beschichtungen',                                u'Coatings'),
            ('tgl_cls_windows',    u'Komponenten von Fenstern und Vorhangfassaden', u'Windows & Façades'),
            ('tgl_cls_bldg_svcs',  u'Gebäudetechnik',                                u'Building Services'),
            ('tgl_cls_composites', u'Komposite',                                     u'Composites'),
            ('tgl_cls_other',      u'Sonstige',                                      u'Other'),
            ('tgl_cls_eol',        u'End of Life',                                   u'End of Life'),
        ]
        self._classification_subs = {
            u'Mineralische Baustoffe': [
                (u'Asphalt',                                  u'Asphalt'),
                (u'Bindemittel',                              u'Binders'),
                (u'Mörtel und Beton',                         u'Mortar & Concrete'),
                (u'Pigmente',                                 u'Pigments'),
                (u'Steine und Elemente',                      u'Stones & Elements'),
                (u'Zuschläge',                                u'Aggregates'),
            ],
            u'Holz': [
                (u'Holzböden',                                u'Wood Flooring'),
                (u'Holzwerkstoffe',                           u'Engineered Wood'),
                (u'Modifiziertes Holz',                       u'Modified Wood'),
                (u'Vollholz',                                 u'Solid Wood'),
            ],
            u'Metalle': [
                (u'Aluminium',                                u'Aluminium'),
                (u'Blei',                                     u'Lead'),
                (u'Edelstahl',                                u'Stainless Steel'),
                (u'Kupfer',                                   u'Copper'),
                (u'Oberflächenbehandlung und Beschichtung von Metallen', u'Surface Treatment'),
                (u'Stahl und Eisen',                          u'Steel & Iron'),
                (u'Zink',                                     u'Zinc'),
            ],
            u'Kunststoffe': [
                (u'Bodenbeläge',                              u'Floor Coverings'),
                (u'Dachbahnen',                               u'Roofing Membranes'),
                (u'Dichtmassen',                              u'Sealants'),
                (u'Folien und Vliese',                        u'Films & Nonwovens'),
                (u'Kunststoffprofile elastisch',              u'Elastic Profiles'),
                (u'Profile',                                  u'Profiles'),
                (u'Rohre',                                    u'Pipes'),
            ],
            u'Dämmstoffe': [
                (u'Baumwolle',                                u'Cotton'),
                (u'Blähperlit',                               u'Expanded Perlite'),
                (u'Calciumsilikat',                           u'Calcium Silicate'),
                (u'Dämmelemente',                             u'Insulation Elements'),
                (u'Expandierter Kork',                        u'Expanded Cork'),
                (u'Expandiertes Polystyrol (EPS)',            u'EPS'),
                (u'Extrudiertes Polystyrol (XPS)',            u'XPS'),
                (u'Flachsfaser',                              u'Flax Fibre'),
                (u'Hanffaser',                                u'Hemp Fibre'),
                (u'Harnstoff-Formaldehydharz',                u'Urea-Formaldehyde'),
                (u'Holzfasern',                               u'Wood Fibres'),
                (u'Holzwolleplatten',                         u'Wood-Wool Boards'),
                (u'Kautschuk',                                u'Rubber'),
                (u'Melaminharz',                              u'Melamine'),
                (u'Mineralwolle',                             u'Mineral Wool'),
                (u'Ortschaum',                                u'In-situ Foam'),
                (u'Phenolharz-Hartschaum (PF)',               u'PF Rigid Foam'),
                (u'Polyethylen',                              u'Polyethylene'),
                (u'Polyurethan-Hartschaum (PU)',              u'PU Rigid Foam'),
                (u'Schafswolle',                              u'Sheep Wool'),
                (u'Schaumbeton',                              u'Foamed Concrete'),
                (u'Schaumglas',                               u'Foam Glass'),
                (u'Siliziumdioxid-basiert',                   u'Silica-Based'),
                (u'Stroh',                                    u'Straw'),
                (u'Wärmedämmverbundsystem',                   u'ETICS'),
                (u'Zellulosefaser',                           u'Cellulose Fibre'),
            ],
            u'Beschichtungen': [
                (u'Bituminöse Anstriche',                     u'Bituminous Coatings'),
                (u'Brandschutz',                              u'Fire Protection'),
                (u'Fassadenfarben',                           u'Façade Paints'),
                (u'Grundierungen',                            u'Primers'),
                (u'Innenbeschichtungen',                      u'Interior Coatings'),
                (u'Lacke und Lasuren',                        u'Lacquers & Stains'),
                (u'Reaktionsharze',                           u'Reactive Resins'),
            ],
            u'Komponenten von Fenstern und Vorhangfassaden': [
                (u'Antriebssysteme',                          u'Drive Systems'),
                (u'Beschläge',                                u'Fittings'),
                (u'Dichtungskomponenten / -materialien',      u'Sealing Components'),
                (u'Fassaden',                                 u'Façades'),
                (u'Fenster',                                  u'Windows'),
                (u'Füllungen',                                u'Infill Panels'),
                (u'Rahmen',                                   u'Frames'),
                (u'Tageslichtsysteme und Rauch- / Wärmeabzugsanlagen', u'Daylight & Smoke Vents'),
                (u'Türen und Tore',                           u'Doors & Gates'),
                (u'Zubehör für Fenster, Fassaden, Türen und Tore', u'Accessories'),
            ],
            u'Gebäudetechnik': [
                (u'Beförderung',                              u'Vertical Transport'),
                (u'Brandschutz',                              u'Fire Protection'),
                (u'Elektro',                                  u'Electrical'),
                (u'Heizung',                                  u'Heating'),
                (u'Klimatisierung und Lüftung',               u'HVAC'),
                (u'Nutzung',                                  u'Use-Phase Energy'),
                (u'Sanitär',                                  u'Sanitary'),
            ],
            u'Komposite': [
                (u'Systembauteile',                           u'System Components'),
            ],
            u'Sonstige': [
                (u'Baustellenprozesse',                       u'Construction Site'),
                (u'Energieträger - Bereitstellung frei Verbraucher', u'Energy Carriers'),
                (u'Güter - Transporte[t km]',                 u'Freight Transport'),
                (u'Personen - Transporte[Personen km]',       u'Passenger Transport'),
            ],
            u'End of Life': [
                (u'Generisch',                                u'Generic'),
            ],
        }
        # Lazy cache for the L3/L4 tree built from self.datasets.
        # Built on first call to _rebuild_classification_tree and
        # invalidated on every dataset reload (in _compute_indicator_ranges).
        self._classification_tree_l34 = None
        self._indicator_ranges = {}         # {prop: (min, max)} computed from loaded cache
        self._advanced_active  = False      # whether advanced filters are applied
        self._advanced_filtered = []        # datasets after advanced filtering
        self._min_score_pct = 50            # Min Score slider threshold (0-100; 0 = no filter)

        # Slot-command popup state
        self._selected_indicator = None  # IndicatorRow currently selected in env/resource grid
        self._drag_start_point   = None  # System.Windows.Point for drag threshold detection

        # Slash-command popup state
        self._suppress_popup = False
        self._popup_items    = [
            RegexTemplateItem(l, d, t, p) for l, d, t, p in REGEX_TEMPLATES
        ]

        # Debounce timer for search input (avoids filtering on every keystroke)
        self._search_timer = DispatcherTimer()
        self._search_timer.Interval = TimeSpan.FromMilliseconds(250)
        self._search_timer.Tick += self._on_search_debounce

        self._init_ui()
        self.ShowDialog()
    def _init_ui(self):
        self._update_search_mode_button()
        self._update_source_ui()
        self._update_language_labels()
        self._cache_all_datasets()
        self._do_api_search("")
    def _update_source_ui(self):
        """Show/hide EPD No and Compliance columns based on the active source."""
        try:
            if hasattr(self, "col_epd_no"):
                self.col_epd_no.Visibility = (
                    Visibility.Visible
                    if self._current_source == "project_epds"
                    else Visibility.Collapsed
                )
            if hasattr(self, "col_compliance"):
                self.col_compliance.Visibility = (
                    Visibility.Collapsed
                    if self._current_source == "project_epds"
                    else Visibility.Visible
                )
        except Exception:
            pass

        ds_info = constants.DATASTOCKS.get(self._current_source, {})
        self.footer_source.Text = "Source: {}".format(
            ds_info.get("label", u"ÖKOBAUDAT")
        )
    def _apply_details_columns(self, modules):
        """Rebuild Indicator / Unit + one column per module on both detail grids."""
        wrap_style = Style(TextBlock)
        wrap_style.Setters.Add(Setter(TextBlock.TextWrappingProperty, TextWrapping.Wrap))
        wrap_style.Setters.Add(Setter(TextBlock.VerticalAlignmentProperty, VerticalAlignment.Center))

        for grid in (self.datagrid_env, self.datagrid_resource):
            grid.Columns.Clear()

            ind_col = DataGridTextColumn()
            ind_col.Header = "Indicator"
            ind_col.Binding = Binding("Indicator")
            ind_col.Width = DataGridLength(2.5, DataGridLengthUnitType.Star)
            ind_col.MinWidth = 140
            ind_col.IsReadOnly = True
            ind_col.ElementStyle = wrap_style
            grid.Columns.Add(ind_col)

            unit_col = DataGridTextColumn()
            unit_col.Header = "Unit"
            unit_col.Binding = Binding("Unit")
            unit_col.Width = DataGridLength(0.9, DataGridLengthUnitType.Star)
            unit_col.MinWidth = 60
            unit_col.IsReadOnly = True
            grid.Columns.Add(unit_col)

            for module in modules:
                col = DataGridTextColumn()
                col.Header = module
                col.Binding = Binding(constants._module_to_col_key(module))
                col.Width = DataGridLength(1.0, DataGridLengthUnitType.Star)
                col.MinWidth = 45
                col.IsReadOnly = True
                grid.Columns.Add(col)
    def radio_datasource_changed(self, sender, e):
        try:
            if self.radio_a2_sphera.IsChecked:
                self._current_source = "a2_sphera"
            elif self.radio_a1_sphera.IsChecked:
                self._current_source = "a1_sphera"
            elif self.radio_a2_ecoinvent.IsChecked:
                self._current_source = "a2_ecoinvent"
            elif self.radio_project_epds.IsChecked:
                self._current_source = "project_epds"
        except Exception:
            pass

        ds_info = constants.DATASTOCKS.get(self._current_source, {})
        self._compliance = ds_info.get("compliance", "A2")

        self._update_source_ui()
        self.datasets = []
        self.datasets_sorted = []
        self.selected_dataset = None
        self.datagrid_datasets.ItemsSource = []
        self.datagrid_env.ItemsSource = []
        self.datagrid_resource.ItemsSource = []
        self._clear_filter_inputs()
        self._set_status("")
        self._do_api_search("")
    def radio_language_changed(self, sender, e):
        try:
            new_lang = "en" if self.radio_lang_en.IsChecked else "de"
            if hasattr(self, "_lang") and self._lang == new_lang:
                return
            self._lang = new_lang
        except Exception:
            self._lang = "de"

        if hasattr(self, "_current_source"):
            # Clear selection BEFORE updating language labels so the tech
            # description label resets to the localised placeholder instead
            # of re-rendering the previous language's description text.
            self.datasets = []
            self.datasets_sorted = []
            self.selected_dataset = None
            self.datagrid_datasets.ItemsSource = []
            self.datagrid_env.ItemsSource = []
            self.datagrid_resource.ItemsSource = []
            self._update_language_labels()
            self._clear_filter_inputs()
            self._do_api_search("")
    def _apply_sort(self):
        if self._sort_mode == 1:
            self.datasets_sorted = sorted(
                self.datasets, key=lambda d: (d.DisplayName or "").lower()
            )
        elif self._sort_mode == 2:
            self.datasets_sorted = sorted(
                self.datasets, key=lambda d: (d.DisplayName or "").lower(), reverse=True
            )
        else:
            self.datasets_sorted = (
                list(self._datasets_original) if self._datasets_original
                else list(self.datasets)
            )
    def combo_sort_changed(self, sender, e):
        try:
            self._sort_mode = self.combo_sort.SelectedIndex
            self._apply_sort()
            self._apply_grid_filters()
        except Exception:
            pass
    def _cache_all_datasets(self):
        """Pre-populate the local cache for all sources and languages.
        Shows a progress bar only when at least one cache file is stale."""
        stale = []
        for src_key, ds_info in constants.DATASTOCKS.items():
            for lang in ["de", "en"]:
                cache_path = self._client.get_cache_path(src_key, lang)
                if not self._client.is_cache_fresh(cache_path):
                    stale.append((src_key, ds_info, lang))

        if not stale:
            return

        with forms.ProgressBar(
            title="Caching Materials Datasets ({value} of {max_value})",
            cancellable=False
        ) as pb:
            pb.update_progress(0, len(stale))
            for idx, (src_key, ds_info, lang) in enumerate(stale):
                try:
                    extra = ds_info.get("extra_params", "")
                    results, _ = self._client.search_processes(
                        ds_info["uuid"], "", extra_params=extra, lang=lang
                    )
                    cache_path = self._client.get_cache_path(src_key, lang)
                    self._client.save_cache(cache_path, results)
                except Exception as ex:
                    logger.error("Silent cache prefetch failed for {} ({}): {}".format(
                        src_key, lang, ex))
                pb.update_progress(idx + 1, len(stale))
    def _load_indicator_cache(self):
        """Load pre-fetched indicator data for the current source/language.
        Populates self._indicator_cache = {uuid: {prop: val, attributes: {}}}."""
        cache_path = os.path.join(
            self._client._cache_dir,
            "indicators_{}_{}.json".format(self._current_source, self._lang)
        )
        self._indicator_cache = {}
        if not os.path.exists(cache_path):
            return
        try:
            with open(cache_path, "rb") as f:
                raw = f.read()
                data = json.loads(raw.decode("utf-8"))
            self._indicator_cache = data.get("datasets", {})
        except Exception:
            self._indicator_cache = {}
    def _format_score_tooltip(self, query_lower, searchable, expanded_query):
        """Render a multi-line tooltip explaining the BM25F score of one
        (query, dataset) pair. Calls `search_helpers.explain_score` (the
        single source of truth - never re-implement the formula here)
        and formats the breakdown for the WPF ToolTip.
        """
        if not self._corpus_stats or not query_lower:
            return u""
        try:
            ex = explain_score(
                query_lower, searchable, self._corpus_stats,
                params=self._bm25_params,
                calibrator=self._calibrator,
                expanded_query=expanded_query,
                confidence=self._confidence_cals.get("bm25f"),
            )
        except Exception as e:
            return u"Score breakdown unavailable: {}".format(e)
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
        p = ex["params"]
        lines.append(u"Params: k1={k1}, b_name={bn}, b_class={bc}, "
                     u"w_name={wn}, w_class={wc}, alpha_3g={a}".format(
                         k1=p["k1"], bn=p["b_name"], bc=p["b_class"],
                         wn=p["w_name"], wc=p["w_class"], a=p["alpha_3g"]))
        lines.append(u"")
        lines.append(u"Per-term contributions:")
        # Sort terms by contribution descending so the dominant signals appear first.
        terms = sorted(ex["per_term"], key=lambda t: t["contrib"], reverse=True)
        for t in terms:
            tag = u""
            if "via_typo_of" in t:
                tag = u"  (typo of '{0}', d={1})".format(
                    t["via_typo_of"], t["distance"])
            lines.append(
                u"  {term}{tag}: w={w:.2f}, idf={idf:.2f}, "
                u"tf_name={tn}, tf_class={tc}, contrib={c:.3f}".format(
                    term=t["term"], tag=tag, w=t["weight"], idf=t["idf"],
                    tn=t["tf_name"], tc=t["tf_class"], c=t["contrib"]))
        if ex["trigram_contrib"] > 0:
            lines.append(u"")
            lines.append(u"Trigram contribution: {0:.3f}".format(ex["trigram_contrib"]))
        return u"\n".join(lines)
    def _format_semantic_tooltip(self, raw_cosine, cc, hay=None, query=None,
                                  ctx_note=None):
        """Render the Score% tooltip for a semantic-mode row.

        Mirrors the pedagogy of `_format_score_tooltip` (BM25F) -
        formula, inputs, knobs, scale reference - adapted to dense
        retrieval. There is no per-term decomposition (each of the
        1024 bge-m3 dimensions is a learned latent feature with no
        human-readable meaning), so the tooltip stops at the
        confidence-mapping table.

        The "Confidence mapping" anchors are evaluated live via
        `cc.apply(x)` so the table tracks the *active* calibrator
        (sigmoid default OR an isotonic produced by
        `tools/fit_confidence.py --mode semantic`). When the
        calibrator changes, the displayed anchors change - no
        hardcoded drift.
        """
        try:
            anchors = [0.40, 0.50, 0.60, 0.70, 0.80]
            pct = {a: cc.apply(a) * 100.0 for a in anchors}

            lines = []
            lines.append(u"Score: {0:.0f}%   ({1})".format(
                cc.apply(raw_cosine) * 100.0, cc.source()))
            lines.append(
                u"Raw cosine = {0:.4f}   "
                u"(range -1 .. 1; vectors are L2-normalised, "
                u"so cosine = dot product)".format(raw_cosine))

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
            if ctx_note:
                lines.append(u"    {0}".format(ctx_note))
            if hay:
                hay_text = build_semantic_haystack(hay) or u""
                if hay_text:
                    if len(hay_text) > 240:
                        hay_text = hay_text[:240] + u"..."
                    lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            lines.append(u"")
            lines.append(u"Model")
            if self._embedding_index is not None:
                lines.append(u"    {0} (dim={1})".format(
                    self._embedding_index.model or u"?",
                    self._embedding_index.dim))
            else:
                lines.append(u"    bge-m3 (1024-dim multilingual, BAAI 2024)")
            base_url = (getattr(self._ollama_client, "base_url", None)
                        or u"http://localhost:11434")
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
    def _compute_rrf_scores(self, datasets, nl_query, query_vector, expanded_query):
        """Pre-compute BM25F + semantic rankings of `datasets` and fuse via
        Reciprocal Rank Fusion (Cormack, Clarke & Buttcher, SIGIR 2009).

        Called once per `_apply_grid_filters` pass when the user has opted
        into Hybrid mode via the `chk_hybrid_rrf` checkbox. Cost is
        ~O(2N): one pass building each ranking, plus a constant-time
        fusion step. Negligible vs the existing per-row BM25F scoring.

        Honours the tuned fusion params from
        `hybrid_params_<source>_<lang>.json` when present: a **symmetric**
        BM25F pool (the full candidate set, scored by
        `search_helpers.bm25f_ranking` - the same helper the offline
        `tools/tune_fusion.py` uses, so the tuned config reproduces live) and
        per-ranker weights `w_bm25` / `w_sem`. Absent ⇒ the live default:
        asymmetric pool (fuzzy-filtered) + equal weights + k=HYBRID_RRF_K.

        NOTE: BM25F always ranks `nl_query` (the typed name). The semantic
        ranker uses `query_vector`, which IS the BIM-context-enriched
        embedding when the Context option is on - enrichment flows into the
        hybrid result via the cosine side only, exactly as designed.

        Returns a 4-tuple:
          (rrf_scores, bm25f_ranks, sem_ranks, max_rrf).
        """
        hp = self._hybrid_params or {}
        k = int(hp.get("k", constants.HYBRID_RRF_K))
        pool = hp.get("pool", "asymmetric")
        w_bm25 = float(hp.get("w_bm25", 1.0))
        w_sem  = float(hp.get("w_sem", 1.0))

        # BM25F ranking.
        bm25f_uuids = []
        bm25f_ranks = {}
        if self._corpus_stats is not None and nl_query:
            if pool == "symmetric":
                # Full-corpus pool (no fuzzy pre-filter) - the tuned config.
                items = []
                for ds in datasets:
                    try:
                        items.append(
                            (ds.uuid, build_searchable(ds, lang=self._lang)))
                    except Exception:
                        continue
                bm25f_uuids = bm25f_ranking(
                    nl_query, items, self._corpus_stats,
                    params=self._bm25_params, expanded_query=expanded_query)
            else:
                # Legacy asymmetric pool: fuzzy-filter survivors only.
                fuzzy_tokens = nl_query.split()
                bm25f_scored = []
                for ds in datasets:
                    try:
                        hay = build_searchable(ds, lang=self._lang)
                        if not _fuzzy_match_inner(nl_query, fuzzy_tokens, hay):
                            continue
                        raw = bm25f_score(
                            nl_query, hay, self._corpus_stats,
                            params=self._bm25_params,
                            expanded_query=expanded_query)
                    except Exception:
                        continue
                    bm25f_scored.append((ds.uuid, raw))
                bm25f_scored.sort(key=lambda t: t[1], reverse=True)
                bm25f_uuids = [u for (u, _) in bm25f_scored]
            bm25f_ranks = {u: i + 1 for i, u in enumerate(bm25f_uuids)}

        # Semantic ranking - every dataset with a cached embedding,
        # ranked by cosine. No binary filter (pure dense ranker).
        sem_uuids = []
        sem_ranks = {}
        if (self._embedding_index is not None and query_vector is not None):
            sem_scored = []
            for ds in datasets:
                v = self._embedding_index.get(ds.uuid)
                if v is None:
                    continue
                try:
                    raw = cosine_similarity(query_vector, v)
                except Exception:
                    continue
                sem_scored.append((ds.uuid, raw))
            sem_scored.sort(key=lambda t: t[1], reverse=True)
            sem_uuids = [u for (u, _) in sem_scored]
            sem_ranks = {u: i + 1 for i, u in enumerate(sem_uuids)}

        weights = [w_bm25, w_sem]
        rrf_scores = reciprocal_rank_fusion(
            [bm25f_uuids, sem_uuids], k=k, weights=weights)
        max_rrf    = rrf_max_score(2, k=k, weights=weights)
        return rrf_scores, bm25f_ranks, sem_ranks, max_rrf
    def _format_hybrid_tooltip(self, raw_rrf, norm_rrf, bm25f_rank, sem_rank,
                                cc, hay=None, query=None, ctx_note=None):
        """Render the Score% tooltip for a hybrid-mode (RRF-fused) row.

        Reflects the tuned fusion params (symmetric pool + per-ranker
        weights from tools/tune_fusion.py) when a hybrid_params_*.json is
        loaded; otherwise the live default (asymmetric pool, equal weights,
        k=HYBRID_RRF_K). The Embedded-text section shows the (possibly BIM-
        context-enriched) dense query the semantic side actually used.
        """
        try:
            hp = self._hybrid_params or {}
            k = int(hp.get("k", constants.HYBRID_RRF_K))
            w_bm25 = float(hp.get("w_bm25", 1.0))
            w_sem  = float(hp.get("w_sem", 1.0))
            pool   = hp.get("pool", "asymmetric")
            tuned  = bool(self._hybrid_params)
            bm25f_contrib = (w_bm25 / (k + bm25f_rank)) if bm25f_rank else 0.0
            sem_contrib   = (w_sem  / (k + sem_rank))   if sem_rank   else 0.0

            def rank_str(r):
                return u"#{0}".format(r) if r else u"(absent)"

            lines = []
            lines.append(u"Score: {0:.0f}%   ({1})".format(
                cc.apply(norm_rrf) * 100.0, cc.source()))
            lines.append(
                u"Raw RRF = {0:.4f}     Normalised = {1:.4f}"
                u"   (1.0 = rank-1 in both rankers)".format(raw_rrf, norm_rrf))

            lines.append(u"")
            lines.append(u"Per-ranker contributions")
            lines.append(u"    BM25F:     {0:<10} -> {1:g}/(k+r) = {2:.4f}".format(
                rank_str(bm25f_rank), w_bm25, bm25f_contrib))
            lines.append(u"    Semantic:  {0:<10} -> {1:g}/(k+r) = {2:.4f}".format(
                rank_str(sem_rank), w_sem, sem_contrib))

            lines.append(u"")
            lines.append(u"Formula   (Cormack, Clarke & Buttcher, SIGIR 2009)")
            lines.append(
                u"    rrf(d)  = sum_r  w_r / (k + rank_r(d))"
                u"     # k = {0}, r = ranker".format(k))
            lines.append(
                u"    norm(d) = rrf(d) / ((w_bm25 + w_sem) / (k + 1))")
            lines.append(
                u"    score   = ConfidenceCalibrator[hybrid].apply(norm)")
            if tuned:
                lines.append(
                    u"    tuned: pool={0}, w_bm25={1:g}, w_sem={2:g} "
                    u"(tune_fusion.py)".format(pool, w_bm25, w_sem))

            if query or hay or ctx_note:
                lines.append(u"")
                lines.append(u"Embedded text")
                if query:
                    lines.append(u"    Query:    \"{0}\"".format(query))
                if ctx_note:
                    lines.append(u"    {0}".format(ctx_note))
                if hay:
                    hay_text = build_semantic_haystack(hay) or u""
                    if hay_text:
                        if len(hay_text) > 240:
                            hay_text = hay_text[:240] + u"..."
                        lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            return u"\n".join(lines)
        except Exception as ex:
            return u"Hybrid score breakdown unavailable: {}".format(ex)
    def _apply_reranker_post_pass(self, result_pairs, nl_query, top_k,
                                   ctx_note=None):
        """Take the already-scored list of `(ds, score)` pairs from
        the first-pass ranker (cosine or RRF) and re-score the top-K
        via the local cross-encoder sidecar.

        Mutates `ds.MatchScore` / `MatchScoreRaw` / `MatchScorePct` /
        `MatchScoreTooltip` on the top-K rows; leaves the tail untouched
        (so it stays sorted under the first-pass ordering AND under the
        reranked head, since rerank scores are typically higher
        confidence than tail cosines). Returns the resorted list.

        Bails (returns the input unchanged) if the reranker service is
        unreachable mid-query - falls back gracefully to the base
        ranker without dropping rows."""
        if (not result_pairs or
                not nl_query or
                self._reranker_client is None or
                not self._reranker_available):
            return result_pairs
        top_k = max(1, int(top_k or constants.RERANKER_TOP_K))
        # Sort once to know the top-K head; we'll resort again after.
        sorted_pairs = sorted(
            result_pairs,
            key=lambda p: (p[0].MatchScore if p[0].MatchScore is not None else -1),
            reverse=True)
        head = sorted_pairs[:top_k]
        tail = sorted_pairs[top_k:]
        if not head:
            return sorted_pairs
        documents = []
        for ds, _sc in head:
            hay = build_searchable(ds, lang=self._lang)
            documents.append(build_semantic_haystack(hay) or
                             (ds.DisplayName or u""))
        scores = self._reranker_client.rerank(nl_query, documents)
        if not scores or len(scores) != len(head):
            logger.warning(
                "Reranker post-pass aborted: {}".format(
                    self._reranker_client.last_error or "score-length mismatch"))
            return sorted_pairs
        cc = self._confidence_cals.get("reranker")
        if cc is None:
            cc = ConfidenceCalibrator(mode="reranker")
        # Capture each ds's first-pass rank for the tooltip.
        before_rank = {head[i][0].uuid: i + 1 for i in range(len(head))}
        for i, (ds, _sc_before) in enumerate(head):
            logit = scores[i]
            ds.MatchScore        = cc.apply(logit)
            ds.MatchScoreRaw     = logit
            ds.MatchScorePct     = u"{0:.0f}%".format(ds.MatchScore * 100.0)
            ds.MatchScoreTooltip = self._format_reranker_tooltip(
                logit, cc,
                rank_before=before_rank.get(ds.uuid),
                rank_after=None,   # set after the resort below
                hay=build_searchable(ds, lang=self._lang),
                query=nl_query, ctx_note=ctx_note)
        # Resort head by the RAW cross-encoder logit (MatchScoreRaw), NOT
        # the calibrated MatchScore. Ranking must use the model's raw
        # relevance signal so the ordering is independent of the
        # calibrator's shape -- a flat/degenerate calibrator must never be
        # able to collapse the rerank ordering (which is exactly what a
        # mid-range isotonic plateau would do). Matches the offline
        # tools/phase1_benchmark.py, which already ranks by raw logit.
        # Tail stays in its first-pass order (it wasn't re-scored).
        head_sorted = sorted(
            head,
            key=lambda p: (p[0].MatchScoreRaw
                           if p[0].MatchScoreRaw is not None else -1e9),
            reverse=True)
        # Backfill the "rank_after" line in each head row's tooltip.
        for i, (ds, _sc) in enumerate(head_sorted):
            try:
                ds.MatchScoreTooltip = self._format_reranker_tooltip(
                    ds.MatchScoreRaw, cc,
                    rank_before=before_rank.get(ds.uuid),
                    rank_after=i + 1,
                    hay=build_searchable(ds, lang=self._lang),
                    query=nl_query, ctx_note=ctx_note)
            except Exception:
                pass
        return head_sorted + tail
    def _format_reranker_tooltip(self, logit, cc, rank_before=None,
                                  rank_after=None, hay=None, query=None,
                                  ctx_note=None):
        """Render the Score% tooltip for a reranker-mode row.

        Mirrors the BM25F / hybrid tooltip layout:
          • Score % + calibrator source
          • Raw cross-encoder logit + rank-before/after
          • Formula
          • Model / runtime line
          • Embedded text (query + dataset haystack)"""
        try:
            lines = []
            lines.append(u"Score: {0:.0f}%   ({1})".format(
                cc.apply(logit) * 100.0, cc.source()))
            rank_str = u""
            if rank_after is not None and rank_before is not None:
                if rank_after == rank_before:
                    rank_str = u"  Rank: #{0} (unchanged by reranker)".format(rank_after)
                else:
                    delta = rank_before - rank_after
                    arrow = u"↑" if delta > 0 else u"↓"
                    rank_str = u"  Rank: #{0} (was #{1}, {2}{3})".format(
                        rank_after, rank_before, arrow, abs(delta))
            elif rank_before is not None:
                rank_str = u"  First-pass rank: #{0}".format(rank_before)
            lines.append(
                u"Raw logit = {0:+.3f}{1}".format(logit, rank_str))

            lines.append(u"")
            lines.append(u"Formula   (cross-encoder reranking)")
            lines.append(
                u"    logit  = CrossEncoder(query, dataset)"
                u"     # attention over the (query, doc) pair")
            lines.append(
                u"    score  = ConfidenceCalibrator[reranker].apply(logit)")

            lines.append(u"")
            lines.append(u"Model")
            model_name = constants.RERANKER_MODEL
            if self._reranker_client is not None and self._reranker_client.model:
                model_name = self._reranker_client.model
            lines.append(u"    {0}".format(model_name))
            lines.append(
                u"    Served by local sidecar @ {0}".format(
                    constants.RERANKER_BASE_URL))

            if query or hay or ctx_note:
                lines.append(u"")
                lines.append(u"Embedded text")
                if query:
                    lines.append(u"    Query:    \"{0}\"".format(query))
                if ctx_note:
                    lines.append(u"    {0}".format(ctx_note))
                if hay:
                    hay_text = build_semantic_haystack(hay) or u""
                    if hay_text:
                        if len(hay_text) > 240:
                            hay_text = hay_text[:240] + u"..."
                        lines.append(u"    Dataset:  \"{0}\"".format(hay_text))

            lines.append(u"")
            lines.append(u"Cross-encoder vs cosine")
            lines.append(
                u"    Cross-encoder sees the (query, doc) pair together")
            lines.append(
                u"    and attends across both. Cosine sees only two pre-")
            lines.append(
                u"    computed vectors. Reranking is slower but typically")
            lines.append(
                u"    sharpens top-10 ordering by 5-15 % MRR.")

            return u"\n".join(lines)
        except Exception as ex:
            return u"Reranker score breakdown unavailable: {}".format(ex)
    def _build_corpus_stats(self):
        """Build BM25F corpus statistics from the loaded datasets, and load
        the optional isotonic calibrator + tuned params from the cache dir.

        Called from `_do_api_search` after `self.datasets` is populated. Cheap
        (~50ms for 2400 datasets). The resulting `self._corpus_stats`,
        `self._calibrator`, and `self._bm25_params` are read by
        `_apply_grid_filters` to rank survivors by BM25F (Robertson 2009 /
        Zaragoza 2004) - the same scorer the offline `tools/phase0_benchmark.py`
        and the Validation pushbutton tooltip use. Single source of truth.

        On failure (e.g. degenerate dataset list, malformed cache JSON) the
        method leaves the three attributes set to None; `_apply_grid_filters`
        then falls back to the legacy `_fuzzy_score`-style ordering.
        """
        try:
            searchables = [build_searchable(ds, lang=self._lang)
                           for ds in self.datasets]
            self._corpus_stats = CorpusStats.build(searchables)
        except Exception as ex:
            logger.error("CorpusStats build failed: {}".format(ex))
            self._corpus_stats = None
        # Best-effort calibrator load
        try:
            cal_path = os.path.join(
                self._client._cache_dir,
                "calibration_{}_{}.json".format(self._current_source, self._lang),
            )
            if os.path.exists(cal_path):
                with open(cal_path, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                self._calibrator = IsotonicCalibrator.from_dict(d)
            else:
                self._calibrator = None
        except Exception as ex:
            logger.error("Calibrator load failed: {}".format(ex))
            self._calibrator = None
        # Best-effort tuned params load
        try:
            params_path = os.path.join(
                self._client._cache_dir, "bm25_params_v1.json")
            if os.path.exists(params_path):
                with open(params_path, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                self._bm25_params = d.get("params") or None
            else:
                self._bm25_params = None
        except Exception as ex:
            logger.error("BM25 params load failed: {}".format(ex))
            self._bm25_params = None
        # Best-effort tuned hybrid-fusion params (per source+lang). When
        # present, _compute_rrf_scores uses the symmetric BM25F pool and the
        # tuned per-ranker weights; when absent it keeps the live default
        # (asymmetric pool, equal weights). Same load pattern as bm25_params.
        try:
            hp_path = os.path.join(
                self._client._cache_dir,
                "hybrid_params_{}_{}.json".format(
                    self._current_source, self._lang))
            if os.path.exists(hp_path):
                with open(hp_path, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                self._hybrid_params = d.get("params") or None
            else:
                self._hybrid_params = None
        except Exception as ex:
            logger.error("Hybrid params load failed: {}".format(ex))
            self._hybrid_params = None
        # Per-mode confidence calibrators - sigmoid defaults until an
        # isotonic fit appears in the cache directory (produced by
        # tools/fit_confidence.py). The same `apply(raw)` interface is
        # used in every mode, so the live grid and Min Score filter need
        # no changes when Phase 1's semantic mode lands.
        self._confidence_cals = {}
        for mode in ("bm25f", "semantic", "hybrid", "reranker"):
            try:
                p = os.path.join(
                    self._client._cache_dir,
                    "confidence_{}_{}_{}.json".format(
                        mode, self._current_source, self._lang),
                )
                d = None
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        d = json.loads(f.read().decode("utf-8"))
                self._confidence_cals[mode] = ConfidenceCalibrator.from_dict(mode, d)
            except Exception as ex:
                logger.error("Confidence calibrator load failed for {}: {}".format(mode, ex))
                self._confidence_cals[mode] = ConfidenceCalibrator(mode=mode)
        # Phase 1 - semantic backing. Sidecar `.bin` is loaded from the
        # same cache dir; Ollama client is constructed unconditionally
        # (healthcheck happens lazily on the first search). Semantic
        # mode is only enabled when BOTH the sidecar parsed AND Ollama
        # answered - otherwise the toggle still cycles to `semantic`
        # but `_apply_grid_filters` falls back to BM25F with a status
        # message so the user can diagnose.
        self._embedding_index    = None
        self._ollama_client      = None
        self._semantic_available = False
        try:
            bin_path = os.path.join(
                self._client._cache_dir,
                "embeddings_{}_{}.bin".format(self._current_source, self._lang),
            )
            if os.path.exists(bin_path):
                self._embedding_index = EmbeddingIndex.load_bin(bin_path)
                logger.info(
                    "Loaded {} semantic vectors (model={}, dim={}) from {}".format(
                        len(self._embedding_index),
                        self._embedding_index.model,
                        self._embedding_index.dim,
                        os.path.basename(bin_path)))
        except Exception as ex:
            logger.error("Embedding index load failed: {}".format(ex))
            self._embedding_index = None
        try:
            self._ollama_client = OllamaEmbeddingClient(
                base_url=constants.OLLAMA_BASE_URL,
                model=constants.EMBEDDING_MODEL,
                timeout_ms=constants.OLLAMA_TIMEOUT_MS,
            )
            if self._embedding_index is not None and self._ollama_client.is_available():
                self._semantic_available = True
            elif self._embedding_index is None:
                logger.info(
                    "Semantic mode disabled: no embedding sidecar found "
                    "(run embedding_prefetcher.py to enable).")
            else:
                logger.warning(
                    "Semantic mode disabled: Ollama not reachable at {}. "
                    "Last error: {}".format(
                        constants.OLLAMA_BASE_URL,
                        self._ollama_client.last_error))
        except Exception as ex:
            logger.error("Ollama client setup failed: {}".format(ex))
            self._ollama_client = None

        # Phase 1.5 - auto-build hook. When the .bin is missing or
        # older than the dataset cache (TTL refresh case), spawn the
        # CPython 3 prefetcher in the background. The user keeps
        # working in regex/fuzzy; semantic flips on when the build
        # completes - no Revit restart needed. Disabled via
        # constants.EMBEDDING_AUTO_BUILD_ENABLED = False.
        if constants.EMBEDDING_AUTO_BUILD_ENABLED:
            try:
                staleness = self._embedding_cache_is_stale(
                    self._current_source, self._lang)
                if (staleness in ("missing", "stale") and
                        self._ollama_client is not None and
                        self._ollama_client.is_available() and
                        not self._is_prefetch_running(
                            self._current_source, self._lang)):
                    self._spawn_embedding_prefetch(
                        self._current_source, self._lang, reason=staleness)
            except Exception as ex:
                logger.error("Auto-build trigger failed: {}".format(ex))

        # Phase 1+ - Cross-encoder reranker sidecar. The reranker lives
        # in its own CPython 3 process (`rerank_service.py`) because
        # Ollama doesn't expose a true `/api/rerank` endpoint. The
        # checkbox in the search bar stays greyed until the sidecar
        # answers `/health` with ready=True.
        self._reranker_client    = None
        self._reranker_proc      = None
        self._reranker_available = False
        try:
            self._reranker_client = LocalRerankerClient(
                base_url=constants.RERANKER_BASE_URL,
                timeout_ms=constants.RERANKER_TIMEOUT_MS,
            )
            # Spawn the sidecar if (a) auto-spawn is enabled, (b) no
            # sibling process is already serving (filesystem PID lock -
            # same gotcha as Phase 1.5b for the embedding prefetcher),
            # AND (c) the service isn't already reachable.
            if constants.RERANKER_AUTO_SPAWN_ENABLED:
                if self._reranker_client.is_available():
                    self._reranker_available = True
                    logger.info(
                        "Reranker sidecar already running (model={}).".format(
                            self._reranker_client.model or "?"))
                elif not self._is_reranker_running():
                    self._spawn_reranker_service()
                else:
                    logger.info(
                        "Reranker sidecar lock held by a sibling — "
                        "waiting for its /health to come up.")
        except Exception as ex:
            logger.error("Reranker client setup failed: {}".format(ex))
            self._reranker_client = None
    # ──────────────────────────────────────────────────────────
    # Phase 1.5 - semantic-index auto-build (subprocess-based)
    # ──────────────────────────────────────────────────────────
    #
    # The Connector spawns `embedding_prefetcher.py` (CPython 3,
    # lancedb + pyarrow) as a detached background subprocess whenever
    # the live `.bin` sidecar is missing or older than the dataset
    # cache. A DispatcherTimer polls each running subprocess every
    # 3 s and reloads the in-memory `EmbeddingIndex` the moment the
    # process exits with code 0. The user does not have to restart
    # Revit, run a CLI, or even know the feature exists.
    def _embedding_cache_is_stale(self, source, lang):
        """Return 'missing' / 'stale' / 'fresh' for the .bin sidecar
        belonging to (source, lang). 'stale' means the dataset cache
        has been refreshed since the .bin was written - typically a
        7-day TTL miss. Tolerates a 1-second mtime fuzz to account
        for filesystems with second-level resolution."""
        bin_path = os.path.join(
            self._client._cache_dir,
            "embeddings_{}_{}.bin".format(source, lang))
        if not os.path.exists(bin_path):
            return "missing"
        ds_path = os.path.join(
            self._client._cache_dir,
            "ds_cache_v2_{}_{}.json".format(source, lang))
        if not os.path.exists(ds_path):
            return "fresh"
        try:
            bin_mtime = os.path.getmtime(bin_path)
            ds_mtime  = os.path.getmtime(ds_path)
            if ds_mtime > bin_mtime + 1.0:
                return "stale"
        except Exception:
            pass
        return "fresh"

    def _is_prefetch_running(self, source, lang):
        """True iff a prefetch for this (source, lang) is in flight.
        Checks two sources:
          * ``self._prefetch_procs`` - processes this window spawned
          * ``.embedding_lock_{source}_{lang}`` file in the cache dir
            - covers prefetchers spawned by an earlier Revit run that
            crashed or was closed while a build was still running"""
        proc = self._prefetch_procs.get((source, lang))
        try:
            if proc is not None and not proc.HasExited:
                return True
        except Exception:
            pass
        # Cross-process lock check - survives Revit restarts.
        try:
            lock_path = os.path.join(
                self._client._cache_dir,
                ".embedding_lock_{}_{}".format(source, lang))
            if not os.path.exists(lock_path):
                return False
            with open(lock_path, "r") as f:
                pid_str = (f.read() or "").strip()
            if not pid_str:
                # Empty lock - clean it up so the next spawn can proceed.
                try:
                    os.remove(lock_path)
                except Exception:
                    pass
                return False
            pid = int(pid_str)
            if self._is_pid_alive(pid):
                return True
            # Stale lock from a crashed prefetcher.
            try:
                os.remove(lock_path)
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _is_pid_alive(self, pid):
        """Windows-only liveness check via OpenProcess. False positives
        are tolerated - the auto-build will retry next launch."""
        try:
            import clr  # noqa: F401  (forces .NET availability)
            from System.Diagnostics import Process
            try:
                Process.GetProcessById(int(pid))
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _find_python_exe(self):
        """Locate a CPython 3 interpreter with `lancedb` + `pyarrow`.
        Result is cached on `self._python_exe_cache` - either
        ``(exe, prefix_args)`` or the string ``"NOT_FOUND"``. Set
        ``constants.EMBEDDING_PYTHON_EXE`` to bypass auto-detection
        (useful if the wrong interpreter is on PATH)."""
        if self._python_exe_cache == "NOT_FOUND":
            return None, None
        if self._python_exe_cache:
            return self._python_exe_cache
        explicit = (constants.EMBEDDING_PYTHON_EXE or "").strip()
        if explicit:
            if os.path.exists(explicit):
                self._python_exe_cache = (explicit, [])
                return explicit, []
        for exe, prefix in (("python.exe", []), ("py.exe", ["-3"])):
            if self._test_python(exe, prefix):
                self._python_exe_cache = (exe, prefix)
                return exe, prefix
        self._python_exe_cache = "NOT_FOUND"
        return None, None

    def _test_python(self, exe, prefix_args):
        """Spawn ``exe [prefix] --version`` and return True iff it
        exits 0 within 3 seconds. Used only by `_find_python_exe` for
        interpreter discovery."""
        try:
            from System.Diagnostics import Process, ProcessStartInfo
            psi = ProcessStartInfo()
            psi.FileName = exe
            psi.Arguments = " ".join(prefix_args + ["--version"])
            psi.UseShellExecute = False
            psi.RedirectStandardOutput = True
            psi.RedirectStandardError = True
            psi.CreateNoWindow = True
            p = Process()
            p.StartInfo = psi
            if not p.Start():
                return False
            if not p.WaitForExit(3000):
                try:
                    p.Kill()
                except Exception:
                    pass
                return False
            return p.ExitCode == 0
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────
    # Phase 1+ - Cross-encoder reranker sidecar lifecycle
    # ──────────────────────────────────────────────────────────
    def _reranker_lock_path(self):
        return os.path.join(
            self._client._cache_dir, ".reranker_service.lock")

    def _is_reranker_running(self):
        """True iff a reranker sidecar is in flight. Checks BOTH the
        in-window proc handle AND the filesystem PID lock (covers
        sidecars left behind by a previous Revit run that crashed).
        Stale locks (PID dead) are cleaned up here so the next launch
        can spawn cleanly."""
        try:
            if self._reranker_proc is not None and not self._reranker_proc.HasExited:
                return True
        except Exception:
            pass
        try:
            lock_path = self._reranker_lock_path()
            if not os.path.exists(lock_path):
                return False
            with open(lock_path, "r") as f:
                try:
                    pid = int((f.read() or "0").strip())
                except Exception:
                    pid = 0
            if pid > 0 and self._is_pid_alive(pid):
                return True
            try:
                os.remove(lock_path)
            except OSError:
                pass
            return False
        except Exception:
            return False

    def _spawn_reranker_service(self):
        """Spawn `rerank_service.py` in a background CPython 3 process.
        Idempotent: silently returns when a sibling sidecar is already
        running (PID-lock check). On a successful spawn, starts a
        DispatcherTimer polling `GET /health` every ~1 s; flips
        `self._reranker_available = True` and updates the status bar
        the moment the service reports ready."""
        if self._is_reranker_running():
            return
        exe, prefix = self._find_python_exe()
        if exe is None:
            self._set_status(
                "Reranker auto-spawn skipped: no CPython 3 found on PATH. "
                "Set EMBEDDING_PYTHON_EXE in constants.py.")
            return
        script_path = os.path.join(PATH_SCRIPT, "rerank_service.py")
        if not os.path.exists(script_path):
            logger.warning(
                "rerank_service.py not found at {}".format(script_path))
            return
        try:
            from System.Diagnostics import Process, ProcessStartInfo
            psi = ProcessStartInfo()
            psi.FileName = exe
            # `-u` forces unbuffered stdout so we can stream the
            # service's load progress to the status bar.
            psi.Arguments = " ".join(prefix + [
                "-u",
                '"{0}"'.format(script_path),
                "--port",  str(constants.RERANKER_PORT),
                "--model", constants.RERANKER_MODEL,
            ])
            psi.UseShellExecute        = False
            psi.RedirectStandardOutput = True
            psi.RedirectStandardError  = True
            psi.CreateNoWindow         = True
            psi.WorkingDirectory       = PATH_SCRIPT
            proc = Process()
            proc.StartInfo = psi
            proc.Start()
            self._reranker_proc = proc
            self._set_status(
                "Loading cross-encoder reranker ({}) on first launch...".format(
                    constants.RERANKER_MODEL))
            self._start_reranker_health_timer()
        except Exception as ex:
            logger.error("Reranker spawn failed: {}".format(ex))
            self._reranker_proc = None

    def _start_reranker_health_timer(self):
        """Tick every 2 s until the sidecar reports ready=True (then
        stop), or the subprocess exits (then stop and report failure)."""
        try:
            if getattr(self, "_reranker_health_timer", None) is not None:
                return  # already polling
            from System.Windows.Threading import DispatcherTimer
            from System import TimeSpan
            timer = DispatcherTimer()
            timer.Interval = TimeSpan.FromMilliseconds(2000)
            timer.Tick    += self._on_reranker_health_tick
            self._reranker_health_timer = timer
            self._reranker_health_started_at = time.time()
            timer.Start()
        except Exception as ex:
            logger.error("Reranker health timer setup failed: {}".format(ex))

    def _on_reranker_health_tick(self, sender, e):
        """DispatcherTimer callback - polls the sidecar's /health endpoint."""
        try:
            # If the subprocess already exited, give up.
            if self._reranker_proc is not None:
                try:
                    if self._reranker_proc.HasExited:
                        code = self._reranker_proc.ExitCode
                        if code != 0:
                            try:
                                err = self._reranker_proc.StandardError.ReadToEnd()
                            except Exception:
                                err = ""
                            self._set_status(
                                "Reranker sidecar exited (code={}). "
                                "Install deps: pip install sentence-transformers torch. "
                                "{}".format(code, (err or "")[:200]))
                        self._stop_reranker_health_timer()
                        return
                except Exception:
                    pass
            if self._reranker_client is not None and self._reranker_client.is_available():
                self._reranker_available = True
                self._set_status(
                    "Reranker ready ({}). Tick 'Rerank' in semantic mode "
                    "to enable.".format(self._reranker_client.model or constants.RERANKER_MODEL))
                self._stop_reranker_health_timer()
                # Reflect availability in the search-bar checkbox right
                # away in case the user is sitting in semantic mode.
                try:
                    self._update_reranker_checkbox_visibility()
                except Exception:
                    pass
                return
            # Still loading - surface a soft progress message every ~10 ticks
            # so the user doesn't think the Connector is frozen.
            elapsed = time.time() - getattr(self, "_reranker_health_started_at", time.time())
            if int(elapsed) and int(elapsed) % 10 == 0:
                self._set_status(
                    "Loading cross-encoder reranker ({:.0f}s elapsed)...".format(elapsed))
            # Hard timeout - 120 s should be more than enough even on
            # cold CPU; if exceeded, abandon and let the user check the log.
            if elapsed > 120:
                self._set_status(
                    "Reranker health-check timed out after 120s. "
                    "See LCiA_Extension_Cache/reranker_service.log for details.")
                self._stop_reranker_health_timer()
        except Exception as ex:
            logger.error("Reranker health tick error: {}".format(ex))

    def _stop_reranker_health_timer(self):
        try:
            t = getattr(self, "_reranker_health_timer", None)
            if t is not None:
                t.Stop()
                self._reranker_health_timer = None
        except Exception:
            pass

    def _spawn_embedding_prefetch(self, source, lang, reason="missing"):
        """Start ``embedding_prefetcher.py --source X --lang Y`` as a
        background subprocess. Idempotent: silently returns when a
        build for the same (source, lang) is already running. The
        DispatcherTimer is started lazily on the first spawn."""
        if self._is_prefetch_running(source, lang):
            return
        exe, prefix = self._find_python_exe()
        if exe is None:
            self._set_status(
                "Semantic auto-build skipped: no CPython 3 found on PATH. "
                "Set EMBEDDING_PYTHON_EXE in constants.py.")
            return
        script_path = os.path.join(PATH_SCRIPT, "embedding_prefetcher.py")
        if not os.path.exists(script_path):
            logger.warning(
                "embedding_prefetcher.py not found at {}".format(script_path))
            return
        try:
            from System.Diagnostics import Process, ProcessStartInfo
            psi = ProcessStartInfo()
            psi.FileName = exe
            # Quote the script path - the working dir contains 'Ö'
            # plus a space, both of which the command-line parser
            # gets wrong without quotes.
            # `-u` forces unbuffered stdout so the DispatcherTimer's tick
            # callback can read progress lines as they're emitted rather
            # than waiting for a 4KB OS buffer fill.
            psi.Arguments = " ".join(prefix + [
                "-u",
                '"{0}"'.format(script_path),
                "--source", source,
                "--lang",   lang,
                "--workers", str(constants.EMBEDDING_AUTO_BUILD_WORKERS),
            ])
            psi.UseShellExecute        = False
            psi.RedirectStandardOutput = True
            psi.RedirectStandardError  = True
            psi.CreateNoWindow         = True
            psi.WorkingDirectory       = PATH_SCRIPT
            proc = Process()
            proc.StartInfo = psi
            proc.Start()
            self._prefetch_procs[(source, lang)] = proc
            logger.info(
                "Started embedding prefetch subprocess: {}/{} ({})".format(
                    source, lang, reason))
            self._set_status(
                "Building semantic index for {}/{}... (background, ~8 min). "
                "Using BM25F meanwhile.".format(source, lang))
            self._start_prefetch_timer()
        except Exception as ex:
            logger.error("Failed to spawn prefetch subprocess: {}".format(ex))
            self._set_status("Auto-build failed to start: {}".format(ex))

    def _start_prefetch_timer(self):
        """Ensure the prefetch-monitoring DispatcherTimer is running.
        Cheap to call on every spawn - the method short-circuits when
        the timer is already alive."""
        try:
            if self._prefetch_timer is not None and self._prefetch_timer.IsEnabled:
                return
            t = DispatcherTimer()
            t.Interval = TimeSpan.FromSeconds(3)
            t.Tick += self._on_prefetch_tick
            t.Start()
            self._prefetch_timer = t
        except Exception as ex:
            logger.error("Could not start prefetch timer: {}".format(ex))

    def _on_prefetch_tick(self, sender, e):
        """DispatcherTimer callback - fires on the WPF UI thread every
        3 s. For each running subprocess, drains it on exit and either
        reloads the .bin (exit 0) or surfaces a status-bar error
        (non-zero). Stops the timer when no subprocesses remain."""
        finished = []
        for key, proc in list(self._prefetch_procs.items()):
            if proc is None:
                finished.append(key)
                continue
            try:
                has_exited = proc.HasExited
            except Exception:
                continue
            if not has_exited:
                continue
            finished.append(key)
            source, lang = key
            try:
                exit_code = proc.ExitCode
            except Exception:
                exit_code = -1
            if exit_code == 0:
                logger.info(
                    "Semantic prefetch finished cleanly: {}/{}".format(source, lang))
                if (source == self._current_source and lang == self._lang):
                    self._reload_embedding_index_for_current()
            else:
                err = u""
                try:
                    if proc.StandardError is not None:
                        err = proc.StandardError.ReadToEnd() or u""
                except Exception:
                    pass
                short = err.strip().splitlines()
                short_line = (short[-1] if short else u"(no stderr)")[:200]
                logger.warning(
                    u"Semantic prefetch FAILED: {}/{} exit={} stderr={}".format(
                        source, lang, exit_code, short_line))
                if (source == self._current_source and lang == self._lang):
                    self._set_status(
                        u"Semantic auto-build failed (exit {}): {}".format(
                            exit_code, short_line))
            try:
                proc.Dispose()
            except Exception:
                pass
        for k in finished:
            self._prefetch_procs.pop(k, None)
        # Stop the timer if nothing is running anymore.
        if not self._prefetch_procs:
            try:
                if self._prefetch_timer is not None:
                    self._prefetch_timer.Stop()
                    self._prefetch_timer = None
            except Exception:
                pass

    def _reload_embedding_index_for_current(self):
        """Re-load the .bin sidecar after a successful auto-build. The
        timer guarantees this only runs when the freshly-finished
        build was for the currently-selected (source, lang) - building
        for a different combo doesn't touch the UI's index."""
        try:
            bin_path = os.path.join(
                self._client._cache_dir,
                "embeddings_{}_{}.bin".format(
                    self._current_source, self._lang))
            if not os.path.exists(bin_path):
                logger.warning(
                    "Build reported success but .bin missing: {}".format(bin_path))
                return
            self._embedding_index = EmbeddingIndex.load_bin(bin_path)
            if (self._ollama_client is not None
                    and self._ollama_client.is_available()):
                self._semantic_available = True
                self._set_status(
                    u"Semantic index ready ({} vectors, model={}).".format(
                        len(self._embedding_index),
                        self._embedding_index.model or u"?"))
                # If the user happens to be in semantic mode right now,
                # re-rank the grid so the new index shows up immediately.
                if self.search_mode_materials == 'semantic':
                    self._apply_grid_filters()
        except Exception as ex:
            logger.error("Reload after auto-build failed: {}".format(ex))
    def _attach_indicator_data(self):
        """Attach pre-fetched indicator data to all datasets in self.datasets."""
        if not self._indicator_cache:
            return
        for ds in self.datasets:
            entry = self._indicator_cache.get(ds.uuid)
            if entry:
                ds.load_indicator_cache(entry)
            # Upgrade any legacy lowercase value (e.g. "mineral" -> "Concrete")
            if ds.MaterialCategory:
                ds.MaterialCategory = attribute_extractor.upgrade_legacy_category(ds.MaterialCategory)
            # If still missing, fall back to classification-root mapping
            # (kept as a passive cache field; NOT used for any UI behaviour
            # or for the Revit MaterialClass write-back, both of which now
            # consume `ds.Classification` / `ds.RevitMaterialClass` directly).
            if not ds.MaterialCategory and ds.Classification:
                ds.MaterialCategory = attribute_extractor.extract_material_category(
                    ds.Classification
                )
            # Build the underscore-joined classification path that
            # revit_mapper writes to Revit's Material.MaterialClass property.
            # Language-aware:
            #   * EN mode -> "Mineral Construction_Mortar & Concrete_Concrete"
            #   * DE mode -> "Mineralische Baustoffe_Moertel und Beton_Beton"
            # When the user toggles DE/EN, _do_api_search reloads everything
            # and this attach step re-runs with the new self._lang, so the
            # MaterialClass label always reflects the active UI language at
            # the moment of mapping to Revit.
            _label_dict = _DE_TO_EN_LABELS if getattr(self, "_lang", "de") == "en" else None
            ds.RevitMaterialClass = attribute_extractor.format_revit_material_class(
                ds.Classification, _label_dict
            )
    def _do_api_search(self, query):
        """Load datasets for the active source/language, using cache when possible."""
        ds_info = constants.DATASTOCKS.get(self._current_source)
        if not ds_info:
            return
        try:
            results = None

            if not query:
                # Try local cache first
                cache_path = self._client.get_cache_path(self._current_source, self._lang)
                if self._client.is_cache_fresh(cache_path):
                    results = self._client.load_cache(cache_path)
                    self._set_status("Loaded from cache...")

                if results is None:
                    self._set_status(u"Fetching from ÖKOBAUDAT (this may take a few seconds)...")
                    extra = ds_info.get("extra_params", "")
                    results, _ = self._client.search_processes(
                        ds_info["uuid"], query, extra_params=extra, lang=self._lang
                    )
                    cache_path = self._client.get_cache_path(self._current_source, self._lang)
                    self._client.save_cache(cache_path, results)
            else:
                self._set_status(u"Searching ÖKOBAUDAT...")
                extra = ds_info.get("extra_params", "")
                results, _ = self._client.search_processes(
                    ds_info["uuid"], query, extra_params=extra, lang=self._lang
                )

            self.datasets = []
            for r in results:
                ds = MaterialDataset(
                    dataset_key=r["uuid"],
                    name=r["name"],
                    reference_unit="",
                    category="",
                    uuid=r["uuid"],
                    classification=r.get("classification", ""),
                    location=r.get("location", ""),
                    valid_until=r.get("valid_until", ""),
                    dataset_type=r.get("dataset_type", ""),
                    owner=r.get("owner", ""),
                    compliance=r.get("compliance", ""),
                    program_operator=r.get("program_operator", ""),
                    epd_no=r.get("epd_no", ""),
                )
                ds.ClassificationDisplay = translate_path(
                    ds.Classification, self._lang
                )
                self.datasets.append(ds)

            self._datasets_original = list(self.datasets)
            self._load_indicator_cache()
            self._attach_indicator_data()
            self._compute_indicator_ranges()
            self._build_corpus_stats()
            self._apply_sort()
            self._populate_filter_combos()
            self._apply_grid_filters()
            self.datagrid_datasets.ItemsSource = self.datasets_sorted
            self._set_status("{} results loaded".format(len(self.datasets)))

        except Exception as ex:
            logger.error("API search error: {}".format(ex))
            logger.error(traceback.format_exc())
            self._set_status("Error")
            forms.alert(
                u"API search failed:\n{}".format(ex),
                title=constants.TOOL_TITLE
            )
    def chat_input_drag_enter(self, sender, e):
        """Highlight the chat input when a valid item is dragged over it."""
        if _DRAG_ITEM["type"]:
            e.Effects = DragDropEffects.Copy
        else:
            e.Effects = getattr(DragDropEffects, "None")
        sender.BorderBrush = SolidColorBrush(Color.FromRgb(0x00, 0x78, 0xD4))
        e.Handled = True
    def chat_input_drag_leave(self, sender, e):
        """Restore chat input border when drag leaves."""
        sender.BorderBrush = SolidColorBrush(Color.FromRgb(0xD1, 0xD5, 0xDB))
    def chat_input_drop(self, sender, e):
        """Pin the dragged dataset or indicator to the chat context."""
        sender.BorderBrush = SolidColorBrush(Color.FromRgb(0xD1, 0xD5, 0xDB))
        item_type = _DRAG_ITEM.get("type")
        item_data = _DRAG_ITEM.get("data")
        _DRAG_ITEM["type"] = None
        _DRAG_ITEM["data"] = None
        if item_type == "dataset" and item_data:
            self._pin_item_to_chat("dataset", item_data.DisplayName, item_data)
        elif item_type == "indicator" and item_data:
            self._pin_item_to_chat("indicator", item_data.Indicator, item_data)
        e.Handled = True
    def _fetch_and_populate_details(self, dataset):
        """Fetch full ILCD data for a dataset and populate the details grids."""
        try:
            self._set_status("Loading details...")
            process_json = self._client.get_process_detail(dataset.uuid)
            enriched = self._client.parse_ilcd_to_dataset(
                process_json, dataset.uuid, lang=self._lang
            )
            # Transfer enriched data back into the existing stub object
            dataset.modules          = enriched.modules
            dataset.reference_unit   = enriched.reference_unit
            dataset.category         = enriched.category
            dataset.name             = enriched.name
            dataset.DisplayName      = enriched.name
            dataset._details_fetched = True

            self._update_details()
            self._set_status("Loaded: {}".format(dataset.name[:60]))
        except Exception as ex:
            logger.error("Detail fetch error: {}".format(ex))
            logger.error(traceback.format_exc())
            self._set_status("Error loading details")
            forms.alert(
                u"Could not fetch dataset details:\n{}".format(ex),
                title=constants.TOOL_TITLE
            )
    def _update_details(self):
        """Rebuild both LCA detail DataGrids for the currently selected dataset."""
        if not self.selected_dataset:
            self.datagrid_env.ItemsSource = []
            self.datagrid_resource.ItemsSource = []
            self._update_tech_description_text(None)
            return
        try:
            modules = constants._sort_modules(list(self.selected_dataset.modules.keys()))
            self._apply_details_columns(modules)

            is_a1  = (getattr(self, "_compliance", "A2") == "A1")
            use_de = (getattr(self, "_lang", "de") == "de")
            env_rows = []
            res_rows = []

            for prop_name, en_name, de_name, unit, group in constants.INDICATOR_META:
                if is_a1 and prop_name not in constants.A1_INDICATORS:
                    continue
                display_name = de_name if use_de else en_name
                row = IndicatorRow(prop_name, display_name, unit)
                for module in modules:
                    val = self.selected_dataset.modules[module].get(prop_name)
                    if val is not None:
                        row.set_module_value(
                            constants._module_to_col_key(module),
                            "{:.4g}".format(val)
                        )
                if group == "env":
                    env_rows.append(row)
                else:
                    res_rows.append(row)

            self.datagrid_env.ItemsSource = env_rows
            self.datagrid_resource.ItemsSource = res_rows
            self._update_tech_description_text(self.selected_dataset)
        except Exception as ex:
            logger.error("Details error: {}".format(ex))
            logger.error(traceback.format_exc())
            self.datagrid_env.ItemsSource = []
            self.datagrid_resource.ItemsSource = []
            self._update_tech_description_text(None)
    def _update_tech_description_text(self, ds):
        """Show the ILCD technology description for *ds* in the details pane."""
        try:
            use_de = (getattr(self, "_lang", "de") == "de")
            if ds is None:
                self.lbl_tech_description.Text = (
                    u"Bitte w\u00e4hlen Sie einen Datensatz, um die Technologiebeschreibung anzuzeigen."
                    if use_de else
                    u"Select a dataset to view its technology description."
                )
                self.lbl_tech_description.Foreground = SolidColorBrush(
                    Color.FromRgb(0x6B, 0x72, 0x80))
                return
            desc = getattr(ds, "TechnologyDescription", "") or u""
            desc = desc.strip()
            if desc:
                self.lbl_tech_description.Text = desc
                self.lbl_tech_description.Foreground = SolidColorBrush(
                    Color.FromRgb(0x1F, 0x29, 0x37))
            else:
                self.lbl_tech_description.Text = (
                    u"Keine Technologiebeschreibung f\u00fcr diesen Datensatz verf\u00fcgbar."
                    if use_de else
                    u"No technology description available for this dataset."
                )
                self.lbl_tech_description.Foreground = SolidColorBrush(
                    Color.FromRgb(0x6B, 0x72, 0x80))
        except Exception:
            pass
    def _update_language_labels(self):
        try:
            if getattr(self, "_lang", "de") == "de":
                self.chk_env.Content              = u"Umweltwirkungsindikatoren"
                self.chk_resource.Content         = u"Ressourceneinsatz / Lebenszyklusindikatoren"
                self.chk_tech_description.Content = u"Technologiebeschreibung (inkl. Hintergrundsystem)"
            else:
                self.chk_env.Content              = u"Environmental Impact Indicators"
                self.chk_resource.Content         = u"Resource Use / Life Cycle Indicators"
                self.chk_tech_description.Content = u"Technology Description (incl. Background System)"
            self._update_tech_description_text(getattr(self, "selected_dataset", None))
            # Refresh Materials Classification chips at all 4 levels so
            # they match the newly-selected language. L1 chips are
            # hardcoded in XAML - Content is updated directly. L2/L3/L4
            # chips are rendered dynamically - re-rendering preserves
            # selection state via Tag.
            self._refresh_classification_chip_labels()
        except Exception:
            pass

    def _refresh_classification_chip_labels(self):
        """Re-render Materials Classification chips in the active language.

        Called from _update_language_labels after the user toggles DE/EN.
        L1 chips are hardcoded in XAML so we update their .Content directly
        via _chip_label(). L2/L3/L4 chips are dynamic - calling the rebuild
        methods re-creates them with the new language while preserving
        existing selections via Tag-based snapshot inside each rebuild."""
        try:
            for chip_attr, l1_de, _l1_en in getattr(self, '_classification_roots', []):
                chip = getattr(self, chip_attr, None)
                if chip is not None:
                    chip.Content = self._chip_label(l1_de)
        except Exception:
            pass
        try:
            self._rebuild_classification_tree()
        except Exception:
            pass
    def _update_search_mode_button(self):
        # Phase 1: 3-way cycle. `≈` (Unicode U+2248 "almost equal to")
        # is a compact, universally-rendered glyph that reads as
        # "approximate / similar" - appropriate for cosine semantic
        # search alongside `AZ` (fuzzy) and `.*` (regex).
        labels = {
            'fuzzy':    'AZ',
            'regex':    '.*',
            'semantic': u'≈',
        }
        tips = {
            'fuzzy':    'AZ = fuzzy search with typo tolerance (click to cycle)',
            'regex':    '.* = regex search (click to cycle)',
            'semantic': u'≈ = semantic search (bge-m3 cosine, click to cycle)',
        }
        try:
            mode = self.search_mode_materials
            content = labels.get(mode, 'AZ')
            tip = tips.get(mode, '')
            # Make the silent semantic->keyword fallback visible: if the user
            # is in semantic mode but the embedding backing is unavailable
            # (e.g. Ollama was cold at launch), flag it on the mode button so
            # the bare `≈` glyph never misrepresents what is actually ranking.
            if mode == 'semantic' and not getattr(self, '_semantic_available', False):
                content = u'≈⚠'
                tip = (u"≈ semantic is selected, but bge-m3 / Ollama is "
                       u"unavailable — results are using the KEYWORD (BM25F) "
                       u"fallback. Start Ollama (`ollama serve`, model bge-m3); "
                       u"it is re-checked on every search. Click to cycle.")
            self.btn_searchmode_materials.Content = content
            self.btn_searchmode_materials.ToolTip = tip
        except Exception:
            pass
        # Hybrid checkbox is meaningful only in semantic mode (it fuses
        # BM25F with the cosine ranker). Hide it for regex / fuzzy.
        self._update_hybrid_checkbox_visibility()
        self._update_reranker_checkbox_visibility()
    def _update_hybrid_checkbox_visibility(self):
        try:
            from System.Windows import Visibility
            mode = getattr(self, "search_mode_materials", "fuzzy")
            self.chk_hybrid_rrf.Visibility = (
                Visibility.Visible if mode == 'semantic' else Visibility.Collapsed)
        except Exception:
            pass
    def _update_reranker_checkbox_visibility(self):
        """Hide the Rerank checkbox unless (a) we're in semantic mode and
        (b) the local rerank sidecar reports ready. The checkbox is
        greyed (still visible) when the mode is semantic but the
        sidecar isn't up yet - gives the user feedback that something
        is starting in the background."""
        try:
            from System.Windows import Visibility
            mode = getattr(self, "search_mode_materials", "fuzzy")
            if mode != 'semantic':
                self.chk_reranker.Visibility = Visibility.Collapsed
                return
            self.chk_reranker.Visibility = Visibility.Visible
            self.chk_reranker.IsEnabled  = bool(self._reranker_available)
            if self._reranker_available:
                self.chk_reranker.ToolTip = (
                    u"Cross-encoder reranking: re-score the top-{0} "
                    u"candidates using {1}. Sharper top-10 ordering "
                    u"than cosine alone; adds ~1-3s per query on CPU."
                ).format(constants.RERANKER_TOP_K,
                         (self._reranker_client.model
                          if self._reranker_client is not None
                          else constants.RERANKER_MODEL))
            else:
                self.chk_reranker.ToolTip = (
                    u"Rerank disabled: local cross-encoder sidecar not "
                    u"reachable. Install once with "
                    u"`pip install sentence-transformers torch`, then "
                    u"reopen the Connector.")
        except Exception:
            pass
    def hybrid_rrf_changed(self, sender, e):
        # Re-rank the live grid whenever the user toggles RRF on/off.
        # No state to persist beyond the checkbox itself; the next
        # search cycle reads `chk_hybrid_rrf.IsChecked` directly.
        try:
            self._apply_grid_filters()
        except Exception as ex:
            logger.error("hybrid_rrf_changed failed: {}".format(ex))
    def reranker_changed(self, sender, e):
        # Re-rank the live grid whenever the user toggles Rerank on/off.
        try:
            self._apply_grid_filters()
        except Exception as ex:
            logger.error("reranker_changed failed: {}".format(ex))

    # ──────────────────────────────────────────────────────────
    # BIM context - live extraction from the active view + picker
    # ──────────────────────────────────────────────────────────
    #
    # A "BIM Context" toggle in the Advanced Filters panel enables a
    # "Pick project material…" button. The picker lists the materials used by
    # elements in the ACTIVE VIEW (extracted live - no ExtractContext run
    # needed) and lets the user fold the same BIM fields the Validation tool
    # offers into the search. The enriched "name | class | …" string is written
    # into the search box, so EVERY mode (regex / fuzzy / semantic / hybrid /
    # rerank) ranks the same text - built by the SAME
    # query_context.build_enriched_query the Validation / offline tools use.
    @staticmethod
    def _ctx_param_value(element, builtin_param):
        """String/Double/Integer value of a BuiltInParameter (or "")."""
        try:
            p = element.get_Parameter(builtin_param)
            if p:
                st = p.StorageType
                if st == DB.StorageType.String:
                    return p.AsString() or ""
                elif st == DB.StorageType.Double:
                    return p.AsDouble()
                elif st == DB.StorageType.Integer:
                    return p.AsInteger()
        except Exception:
            pass
        return ""

    @staticmethod
    def _ctx_xyz_to_float(val):
        try:
            if hasattr(val, 'X') and hasattr(val, 'Y') and hasattr(val, 'Z'):
                return float(val.X)
        except Exception:
            pass
        return val

    @staticmethod
    def _ctx_clean_float(f, places=4):
        if not isinstance(f, float):
            return f
        try:
            s = "{0:.{1}f}".format(f, places).rstrip("0").rstrip(".")
            return float(s) if ("." in s or s.lstrip("-").isdigit()) else float(f)
        except Exception:
            return f

    @staticmethod
    def _ctx_convert(value, unit_type):
        """UnitUtils.ConvertFromInternalUnits, guarded (UnitTypeId is 2021+)."""
        try:
            return DB.UnitUtils.ConvertFromInternalUnits(float(value), unit_type)
        except Exception:
            return value

    def _build_mat_host_map_lite(self, elements):
        """material id -> {'categories': set, 'types': set} over `elements`.
        Trimmed copy of ExtractContext._build_mat_host_map: reads the same five
        material-id sources. Every Revit read is guarded so one odd element
        cannot abort the scan."""
        mat_map = {}
        for elem in elements:
            if not elem:
                continue
            try:
                try:
                    if hasattr(elem, "CurtainGrid") and elem.CurtainGrid is not None:
                        continue
                except Exception:
                    pass
                try:
                    cat = elem.Category
                    cat_name = cat.Name if cat else ""
                except Exception:
                    cat_name = ""
                if not cat_name:
                    continue
                type_name = ""
                try:
                    et = doc.GetElement(elem.GetTypeId())
                    if et:
                        try:
                            type_name = et.Name or ""
                        except Exception:
                            type_name = ""
                        if not type_name:
                            try:
                                p = et.get_Parameter(
                                    DB.BuiltInParameter.SYMBOL_NAME_PARAM)
                                if p:
                                    type_name = p.AsString() or ""
                            except Exception:
                                pass
                except Exception:
                    pass
                valid_ids = set()
                try:
                    if hasattr(elem, "GetCompoundStructure"):
                        cs = elem.GetCompoundStructure()
                        if cs:
                            for li in range(cs.LayerCount):
                                mid = cs.GetMaterialId(li)
                                if mid and mid.IntegerValue > 0:
                                    valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass
                try:
                    et2 = doc.GetElement(elem.GetTypeId())
                    if et2 and hasattr(et2, "GetCompoundStructure"):
                        cs2 = et2.GetCompoundStructure()
                        if cs2:
                            for li in range(cs2.LayerCount):
                                mid = cs2.GetMaterialId(li)
                                if mid and mid.IntegerValue > 0:
                                    valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass
                try:
                    sp = elem.get_Parameter(
                        DB.BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
                    if sp and sp.AsElementId().IntegerValue > 0:
                        valid_ids.add(int(sp.AsElementId().IntegerValue))
                except Exception:
                    pass
                try:
                    for mid in elem.GetMaterialIds(False):
                        if mid and mid.IntegerValue > 0:
                            valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass
                try:
                    for mid in elem.GetMaterialIds(True):
                        if mid and mid.IntegerValue > 0:
                            valid_ids.add(int(mid.IntegerValue))
                except Exception:
                    pass
                try:
                    for target in [elem, doc.GetElement(elem.GetTypeId())]:
                        if not target:
                            continue
                        for param in target.Parameters:
                            try:
                                if param.StorageType == DB.StorageType.ElementId:
                                    pid = param.AsElementId()
                                    if pid and pid.IntegerValue > 0:
                                        cand = doc.GetElement(pid)
                                        if cand and isinstance(cand, DB.Material):
                                            valid_ids.add(int(pid.IntegerValue))
                            except Exception:
                                pass
                except Exception:
                    pass
                for mid_int in valid_ids:
                    if mid_int not in mat_map:
                        mat_map[mid_int] = {"categories": set(), "types": set()}
                    mat_map[mid_int]["categories"].add(cat_name)
                    if type_name:
                        mat_map[mid_int]["types"].add(type_name)
            except Exception:
                continue
        return mat_map

    def _extract_one_material(self, mat, usage):
        """Per-material dict in the query_context / ExtractContext shape, with
        only the ~9 fields query_context reads. Asset blocks are omitted when
        the material lacks a structural / thermal asset (query_context then
        drops those fields, so the query reduces to name | class | description)."""
        data = {}
        try:
            data["name"] = mat.Name
        except Exception:
            data["name"] = u""
        try:
            data["class"] = mat.MaterialClass or ""
        except Exception:
            data["class"] = ""
        data["description"] = self._ctx_param_value(
            mat, DB.BuiltInParameter.ALL_MODEL_DESCRIPTION) or ""
        data["host_elements"] = {
            "categories": sorted(usage.get("categories", [])),
            "types":      sorted(usage.get("types", [])),
        }
        # Physical (structural) asset - structural_class, density, concrete grade.
        phys = {}
        try:
            sid = mat.StructuralAssetId
            if sid != DB.ElementId.InvalidElementId:
                pse = doc.GetElement(sid)
                asset = pse.GetStructuralAsset() if pse else None
                if asset:
                    cls = ""
                    try:
                        cls = str(asset.StructuralAssetClass)
                        phys["structural_class"] = cls
                    except Exception:
                        pass
                    try:
                        dp = pse.get_Parameter(
                            DB.BuiltInParameter.PHY_MATERIAL_PARAM_STRUCTURAL_DENSITY)
                        if dp:
                            phys["density_kg_m3"] = self._ctx_clean_float(
                                self._ctx_convert(
                                    dp.AsDouble(),
                                    DB.UnitTypeId.KilogramsPerCubicMeter))
                        else:
                            phys["density_kg_m3"] = self._ctx_clean_float(
                                float(self._ctx_xyz_to_float(asset.Density)))
                    except Exception:
                        pass
                    if cls == "Concrete":
                        try:
                            phys["concrete_compression_mpa"] = self._ctx_clean_float(
                                self._ctx_convert(asset.ConcreteCompression,
                                                  DB.UnitTypeId.Megapascals))
                        except Exception:
                            pass
        except Exception:
            pass
        if phys:
            data["physical_asset"] = phys
        # Thermal asset - thermal conductivity.
        therm = {}
        try:
            tid = mat.ThermalAssetId
            if tid != DB.ElementId.InvalidElementId:
                pse = doc.GetElement(tid)
                asset = pse.GetThermalAsset() if pse else None
                if asset:
                    try:
                        tc = asset.ThermalConductivity
                        if tc is not None:
                            therm["thermal_conductivity_w_mk"] = self._ctx_clean_float(
                                self._ctx_convert(
                                    self._ctx_xyz_to_float(tc),
                                    DB.UnitTypeId.WattsPerMeterKelvin))
                    except Exception:
                        pass
        except Exception:
            pass
        if therm:
            data["thermal_asset"] = therm
        return data

    def _collect_active_view_materials(self):
        """[(display_name, context_dict), …] for materials used in the active
        view, deduped by id and sorted by name. [] on empty view / failure."""
        out = []
        try:
            view = doc.ActiveView
            if view is None:
                return out
            elements = (FilteredElementCollector(doc, view.Id)
                        .WhereElementIsNotElementType().ToElements())
            host_map = self._build_mat_host_map_lite(elements)
            if not host_map:
                return out
            all_mats = (FilteredElementCollector(doc)
                        .OfClass(DB.Material).ToElements())
            seen = set()
            for mat in all_mats:
                try:
                    mid = int(mat.Id.IntegerValue)
                except Exception:
                    continue
                if mid not in host_map or mid in seen:
                    continue
                seen.add(mid)
                d = self._extract_one_material(mat, host_map.get(mid, {}))
                nm = d.get("name") or u"(unnamed)"
                out.append((nm, d))
            out.sort(key=lambda t: (t[0] or u"").lower())
        except Exception as ex:
            logger.error("collect active-view materials failed: {}".format(ex))
        return out

    def bim_context_toggled(self, sender, e):
        """Show/hide the field chips, enable the picker, and rebuild the live
        query if a material is already picked. On toggle-off, clear the box ONLY
        if it still holds the query WE wrote (never stomp hand-typed text)."""
        from System.Windows import Visibility
        try:
            on = bool(self.tgl_bim_context.IsChecked)
            self.btn_pick_context_material.IsEnabled = on
            if on:
                self._build_context_field_chips()
                self.panel_context_fields.Visibility = Visibility.Visible
                if self._ctx_material is not None:
                    self._rebuild_context_query()
            else:
                self.panel_context_fields.Visibility = Visibility.Collapsed
                cur = self.textbox_search.Text or u""
                if (self._ctx_last_query is not None
                        and cur == self._ctx_last_query):
                    self.textbox_search.Text = u""   # one debounced re-query
                    self._ctx_last_query = None
        except Exception as ex:
            logger.error("bim_context_toggled failed: {}".format(ex))

    def _build_context_field_chips(self):
        """Build one ToggleButton chip per BIM field into panel_context_fields,
        once. All chips share the uniform ChipToggle look (grey = inactive,
        accent-blue = active); the noise fields (density / λ / host types) keep
        an explanatory tooltip but no special colour. Every chip starts
        INACTIVE (grey) - a chip turns blue only when the user clicks it or when
        it is part of the field set chosen in the picker window. Each chip
        rebuilds the live query on toggle."""
        if self._ctx_field_chips:
            return
        from System.Windows.Controls.Primitives import ToggleButton
        try:
            chip_style = self.FindResource("ChipToggle")
        except Exception:
            chip_style = None
        for key in _CTX_FIELD_ORDER:
            btn = ToggleButton()
            btn.Content = _CTX_FIELD_LABEL.get(key, key)
            if chip_style is not None:
                btn.Style = chip_style
            else:
                from System.Windows import Thickness
                btn.FontSize = 10
                btn.Padding = Thickness(6, 2, 6, 2)
                btn.Margin = Thickness(0, 0, 4, 2)
            btn.Tag = key
            btn.IsChecked = False   # all chips start grey/inactive
            if key in _CTX_NOISE_FIELDS:
                btn.ToolTip = (u"Adds noise to dense retrieval in the offline "
                               u"study — better expressed as a structured filter.")
            btn.Click += self.context_field_chip_changed
            self._ctx_field_chips[key] = btn
            self.panel_context_fields.Children.Add(btn)

    def _sync_chips_from_fields(self, fields):
        """Set the bar chips' checked state to match `fields` (a list that may
        include the implicit 'name'). Programmatic IsChecked changes the chip's
        grey/blue look via the ChipToggle triggers but does NOT fire Click, so
        no per-chip rebuild storm - the caller rebuilds the query once."""
        fset = set(fields or [])
        for key in self._ctx_field_chips:
            try:
                self._ctx_field_chips[key].IsChecked = (key in fset)
            except Exception:
                pass

    def context_field_chip_changed(self, sender, e):
        """A field chip was toggled: rebuild the query live if a material is
        picked, otherwise just remember the chip state for the next pick."""
        try:
            if (self._ctx_material is not None
                    and bool(self.tgl_bim_context.IsChecked)):
                self._rebuild_context_query()
        except Exception as ex:
            logger.error("context_field_chip_changed failed: {}".format(ex))

    def _rebuild_context_query(self):
        """Assemble the enriched query from the picked material + ticked chips
        (the SAME builder the Validation tool uses) and write it into the search
        box. Setting .Text raises TextChanged, whose 250 ms debounce re-runs
        _apply_grid_filters in the active mode - exactly one re-query."""
        if self._ctx_material is None:
            return
        fields = ["name"] + [k for k in _CTX_FIELD_ORDER
                             if k in self._ctx_field_chips
                             and bool(self._ctx_field_chips[k].IsChecked)]
        try:
            query = query_context.build_enriched_query(
                self._ctx_material, fields, lang=self._lang)
        except Exception as ex:
            logger.error("build_enriched_query failed: {}".format(ex))
            return
        if not query:
            return
        # Set _ctx_last_query BEFORE .Text so the toggle-off compare matches.
        self._ctx_last_query = query
        try:
            self.textbox_search.Text = query
        except Exception as ex:
            logger.error("fill search box failed: {}".format(ex))

    def pick_context_material_click(self, sender, e):
        """Open the picker (material + field check-boxes); on OK remember the
        material, label the button with its name, sync the bar chips to the
        ticked fields, and rebuild the live query (one debounced re-query in
        the active mode)."""
        try:
            mats = self._collect_active_view_materials()
            if not mats:
                forms.alert(
                    u"No materials found in the active view. Open a view that "
                    u"shows model elements (with materials), then try again.",
                    title=u"BIM Context")
                return
            picked = self._show_material_picker(mats)
            if picked is None:
                return
            material_dict, fields = picked
            self._ctx_material = material_dict
            self._set_context_button_label(material_dict.get("name") or u"(unnamed)")
            self._sync_chips_from_fields(fields)
            self._rebuild_context_query()
        except Exception as ex:
            logger.error("pick_context_material_click failed: {}".format(ex))
            forms.alert(u"Could not build the BIM-context query: {0}".format(ex),
                        title=u"BIM Context")

    def _set_context_button_label(self, name):
        """Show the picked material's name on the picker button (truncated),
        with the full name in the tooltip."""
        try:
            nm = name or u"(unnamed)"
            short = nm if len(nm) <= 28 else (nm[:27] + u"…")
            self.btn_pick_context_material.Content = short
            self.btn_pick_context_material.ToolTip = nm
        except Exception:
            pass

    def _show_material_picker(self, materials):
        """Modal: pick ONE active-view material AND tick which BIM fields to
        fold into the query. Returns (material_dict, fields_incl_name) or None.
        The field check-boxes initialise from the current bar chip state (so the
        picker mirrors the bar); on OK the caller re-syncs the bar to this tick
        set. Inner funcs are defined before wiring events / ShowDialog."""
        from System.Windows import (Window, Thickness, SizeToContent,
            WindowStartupLocation, FontWeights, HorizontalAlignment, TextWrapping)
        from System.Windows.Controls import (StackPanel, TextBlock, CheckBox,
            Button, Orientation, ListBox)
        from System.Windows.Media import Brushes

        panel = StackPanel(); panel.Margin = Thickness(18)
        title = TextBlock()
        title.Text = u"BIM Context — pick a project material"
        title.FontSize = 14; title.FontWeight = FontWeights.Bold
        title.Margin = Thickness(0, 0, 0, 2)
        panel.Children.Add(title)
        sub = TextBlock()
        sub.Text = (u"Pick one material from the active view and tick the BIM "
                    u"fields to fold into the search. The chosen fields are "
                    u"joined into the search box and ranked in the current mode.")
        sub.FontSize = 10.5; sub.Foreground = Brushes.Gray
        sub.TextWrapping = TextWrapping.Wrap; sub.MaxWidth = 460
        sub.Margin = Thickness(0, 0, 0, 10)
        panel.Children.Add(sub)

        mhdr = TextBlock()
        mhdr.Text = u"Material  ({0} in active view)".format(len(materials))
        mhdr.FontWeight = FontWeights.SemiBold; mhdr.Margin = Thickness(0, 0, 0, 4)
        panel.Children.Add(mhdr)
        listbox = ListBox(); listbox.Height = 190; listbox.MaxWidth = 460
        for nm, _d in materials:
            listbox.Items.Add(nm)
        listbox.SelectedIndex = 0
        panel.Children.Add(listbox)

        fhdr = TextBlock(); fhdr.Text = u"Fields"
        fhdr.FontWeight = FontWeights.SemiBold; fhdr.Margin = Thickness(0, 10, 0, 4)
        panel.Children.Add(fhdr)
        # The picker mirrors the bar once the user has touched it (picked a
        # material or clicked any chip); on a pristine all-grey bar it offers
        # the shipped defaults as a sensible starting point.
        bar_touched = (self._ctx_material is not None) or any(
            bool(self._ctx_field_chips[k].IsChecked)
            for k in self._ctx_field_chips)
        field_boxes = {}
        for key in _CTX_FIELD_ORDER:
            cb = CheckBox()
            lbl = _CTX_FIELD_LABEL.get(key, key)
            is_noise = key in _CTX_NOISE_FIELDS
            cb.Content = (lbl + u"  ⚠") if is_noise else lbl
            cb.FontSize = 12; cb.Margin = Thickness(0, 2, 0, 2)
            if bar_touched and key in self._ctx_field_chips:
                cb.IsChecked = bool(self._ctx_field_chips[key].IsChecked)
            else:
                cb.IsChecked = key in _CTX_DEFAULT_FIELDS
            if is_noise:
                cb.Foreground = Brushes.Chocolate
                cb.ToolTip = (u"Flagged — adds noise to dense retrieval in the "
                              u"offline study; better as a structured filter.")
            field_boxes[key] = cb
            panel.Children.Add(cb)

        phdr = TextBlock(); phdr.Text = u"Query preview"
        phdr.FontWeight = FontWeights.SemiBold; phdr.Margin = Thickness(0, 10, 0, 4)
        panel.Children.Add(phdr)
        preview = TextBlock(); preview.TextWrapping = TextWrapping.Wrap
        preview.MaxWidth = 460; preview.Foreground = Brushes.Gray
        preview.Margin = Thickness(0, 0, 0, 4)
        panel.Children.Add(preview)

        def _picked_fields():
            return ["name"] + [k for k in _CTX_FIELD_ORDER
                               if bool(field_boxes[k].IsChecked)]

        def _selected_dict():
            idx = listbox.SelectedIndex
            if idx is None or idx < 0 or idx >= len(materials):
                return None
            return materials[idx][1]

        def _refresh_preview(*a):
            d = _selected_dict()
            if d is None:
                preview.Text = u""
                return
            try:
                preview.Text = query_context.build_enriched_query(
                    d, _picked_fields(), lang=self._lang)
            except Exception:
                preview.Text = u""

        holder = {"ok": False}
        win = Window()

        def _ok(s, ev):
            holder["ok"] = True
            win.DialogResult = True
            win.Close()

        def _cancel(s, ev):
            win.DialogResult = False
            win.Close()

        listbox.SelectionChanged += _refresh_preview
        for _cb in field_boxes.values():
            _cb.Checked += _refresh_preview
            _cb.Unchecked += _refresh_preview

        btnrow = StackPanel(); btnrow.Orientation = Orientation.Horizontal
        btnrow.HorizontalAlignment = HorizontalAlignment.Right
        btnrow.Margin = Thickness(0, 14, 0, 0)
        b_ok = Button(); b_ok.Content = u"Use in search"; b_ok.MinWidth = 110
        b_ok.Margin = Thickness(0, 0, 8, 0); b_ok.Click += _ok
        b_cancel = Button(); b_cancel.Content = u"Cancel"; b_cancel.MinWidth = 70
        b_cancel.Click += _cancel
        btnrow.Children.Add(b_ok); btnrow.Children.Add(b_cancel)
        panel.Children.Add(btnrow)

        _refresh_preview()
        win.Title = u"BIM Context"
        win.Content = panel
        win.SizeToContent = SizeToContent.WidthAndHeight
        win.WindowStartupLocation = WindowStartupLocation.CenterOwner
        try:
            win.Owner = self
        except Exception:
            pass
        win.ShowDialog()
        if not holder["ok"]:
            return None
        d = _selected_dict()
        if d is None:
            return None
        return (d, _picked_fields())

    def toggle_search_mode_materials(self, sender, e):
        # Phase 1: cycle fuzzy → semantic → regex → fuzzy. When the
        # semantic backing is unavailable, the toggle still includes
        # the `semantic` state (so the cycle is identical to other
        # installs) but `_apply_grid_filters` will surface a status
        # message and fall back to BM25F ranking on the fuzzy filter.
        cycle = {'fuzzy': 'semantic', 'semantic': 'regex', 'regex': 'fuzzy'}
        self.search_mode_materials = cycle.get(
            self.search_mode_materials, 'fuzzy')
        self._update_search_mode_button()
        self._hide_regex_popup()
        self._apply_grid_filters()
    def textbox_search_changed(self, sender, e):
        if getattr(self, "_suppress_popup", False):
            return
        text   = self.textbox_search.Text or ""
        cursor = self.textbox_search.SelectionStart
        if self.search_mode_materials == 'regex':
            slash_pos = text.rfind("/", 0, cursor + 1)
            if slash_pos != -1 and (cursor - slash_pos) <= 20:
                self._show_regex_popup(text[slash_pos + 1: cursor])
            else:
                self._hide_regex_popup()
        else:
            self._hide_regex_popup()

        # Debounce: restart the timer on every keystroke
        self._search_timer.Stop()
        self._search_timer.Start()
    def _on_search_debounce(self, sender, e):
        """Fires after 250ms of no typing - runs the actual filter."""
        self._search_timer.Stop()
        self._apply_grid_filters()
    def _show_regex_popup(self, filter_text=""):
        q = (filter_text or "").lower().strip()
        visible = [i for i in self._popup_items if not q or q in i.Label.lower()]
        if not visible:
            self._hide_regex_popup()
            return
        self.listbox_regex_templates.ItemsSource = visible
        self.listbox_regex_templates.SelectedIndex = 0
        self.popup_regex_templates.IsOpen = True
    def _hide_regex_popup(self):
        self.popup_regex_templates.IsOpen = False
    def _insert_regex_template(self, item):
        self._hide_regex_popup()
        try:
            text      = self.textbox_search.Text or ""
            cursor    = self.textbox_search.SelectionStart
            slash_pos = text.rfind("/", 0, cursor + 1)
            if slash_pos == -1:
                slash_pos = cursor
            before   = text[:slash_pos]
            after    = text[cursor:]
            new_text = before + item.Template + after
            self._suppress_popup = True
            self.textbox_search.Text = new_text
            self._suppress_popup = False
            ph_offset    = item.Template.index(item.Placeholder)
            select_start = slash_pos + ph_offset
            self.textbox_search.Select(select_start, len(item.Placeholder))
            self.textbox_search.Focus()
            self._apply_grid_filters()
        except Exception as ex:
            logger.error("Template insert error: {}".format(ex))
    def textbox_search_keydown(self, sender, e):
        if not self.popup_regex_templates.IsOpen:
            return
        lb = self.listbox_regex_templates
        if e.Key == Key.Escape:
            self._hide_regex_popup()
            e.Handled = True
        elif e.Key == Key.Down:
            lb.SelectedIndex = min(lb.SelectedIndex + 1, lb.Items.Count - 1)
            lb.ScrollIntoView(lb.SelectedItem)
            e.Handled = True
        elif e.Key == Key.Up:
            lb.SelectedIndex = max(lb.SelectedIndex - 1, 0)
            lb.ScrollIntoView(lb.SelectedItem)
            e.Handled = True
        elif e.Key == Key.Return:
            if lb.SelectedItem:
                self._insert_regex_template(lb.SelectedItem)
            e.Handled = True
    def listbox_regex_templates_keydown(self, sender, e):
        if e.Key == Key.Return:
            if self.listbox_regex_templates.SelectedItem:
                self._insert_regex_template(self.listbox_regex_templates.SelectedItem)
            e.Handled = True
        elif e.Key == Key.Escape:
            self._hide_regex_popup()
            self.textbox_search.Focus()
            e.Handled = True
    def listbox_regex_templates_click(self, sender, e):
        item = self.listbox_regex_templates.SelectedItem
        if item:
            self._insert_regex_template(item)
    def _populate_filter_combos(self):
        """Fill every registered ComboBox filter with unique values from the current results."""
        try:
            for tag, ctrl in self._filter_controls.items():
                if hasattr(ctrl, "SelectedIndex"):
                    self._populate_single_combo(tag, ctrl)
        except Exception as ex:
            logger.error("Populate filter combos error: {}".format(ex))
    def _populate_single_combo(self, tag, ctrl):
        """Populate a single ComboBox with exact values from the corresponding dataset column."""
        if not self.datasets:
            return
        try:
            values = set()
            for ds in self.datasets:
                if   tag == "location"         and ds.Location:         values.add(ds.Location)
                elif tag == "type"             and ds.DatasetType:      values.add(ds.DatasetType)
                elif tag == "compliance"       and ds.Compliance:       values.add(ds.Compliance)
                elif tag == "program_operator" and ds.ProgramOperator:  values.add(ds.ProgramOperator)
                elif tag == "classification"   and hasattr(ds, "Classification") and ds.Classification:
                    values.add(ds.Classification)
                elif tag == "owner"            and hasattr(ds, "Owner") and ds.Owner:
                    values.add(ds.Owner)
                elif tag == "valid_until"      and hasattr(ds, "ValidUntil") and ds.ValidUntil:
                    values.add(ds.ValidUntil)
            if not values:
                return
            ctrl.Items.Clear()
            ctrl.Items.Add("All")
            if tag == "classification":
                # Show the classification path in the active UI language but
                # keep the canonical German string as the filter key (Tag)
                # and keep the German sort order, so the option list lines
                # up with DE for easy comparison.
                lang = getattr(self, "_lang", "de")
                for v in sorted(values):
                    item = ComboBoxItem()
                    item.Content = translate_path(v, lang)
                    item.Tag = v
                    ctrl.Items.Add(item)
            else:
                for v in sorted(values):
                    ctrl.Items.Add(v)
            ctrl.SelectedIndex = 0
        except Exception as ex:
            logger.error("Error populating combo {}: {}".format(tag, ex))
    def _clear_filter_inputs(self):
        """Reset all filter TextBoxes and ComboBoxes."""
        try:
            for tag, ctrl in self._filter_controls.items():
                if hasattr(ctrl, 'Text'):
                    ctrl.Text = ""
                elif hasattr(ctrl, 'SelectedIndex'):
                    if ctrl.Items.Count == 0:
                        ctrl.Items.Add("All")
                    ctrl.SelectedIndex = 0
            self._grid_filters = {}
        except Exception:
            pass
    def _apply_grid_filters(self):
        """Apply global search + all column filters; update the DataGrid ItemsSource."""
        try:
            mode = getattr(self, "search_mode_materials", "regex")
            global_query = (self.textbox_search.Text or "").strip()

            if self._advanced_active and self._advanced_filtered is not None:
                filtered = list(self._advanced_filtered)
            else:
                filtered = list(self.datasets_sorted)

            name_f  = (self._grid_filters.get("name")   or "").lower()
            epd_f   = (self._grid_filters.get("epd_no") or "").lower()
            class_f = self._grid_filters.get("classification")   or ""
            owner_f = self._grid_filters.get("owner")            or ""
            loc_f   = self._grid_filters.get("location")         or ""
            type_f  = self._grid_filters.get("type")             or ""
            comp_f  = self._grid_filters.get("compliance")       or ""
            prog_f  = self._grid_filters.get("program_operator") or ""
            valid_f = self._grid_filters.get("valid_until")      or ""
            # Min Score cutoff (advanced panel). Confidence ∈ [0,1]; the
            # slider is 0-100. 0 = no filter. Only bites when a query is
            # active (otherwise MatchScore is None and rows are kept).
            min_thr = self._get_min_score() / 100.0

            # Phase 0 v7: unify haystack with offline benchmark and Validation
            # pushbutton. Both filtering and scoring use the same 2-field
            # haystack (name + classification, classification translated to
            # the current language), produced by `build_searchable`. Metadata
            # columns (Location, EpdNo, Owner, …) are still searchable via
            # the per-column filters; the global search box is reserved for
            # material-identifying text.
            compiled_re  = None
            fuzzy_nl     = None
            fuzzy_tokens = None

            # Phase 1: detect whether to run the semantic ranker on this
            # pass. `semantic_active` requires (a) user selected mode,
            # (b) the .bin sidecar parsed at startup, (c) Ollama
            # healthcheck passed, AND (d) the per-query embed call
            # succeeds. Failure at (d) falls back to BM25F + fuzzy binary
            # filter and surfaces a status message.
            #
            # Per-search re-probe: `_semantic_available` is latched once at
            # startup, but cold bge-m3 can take ~60 s to load - so a Connector
            # launched before Ollama was warm would otherwise stay stuck on the
            # keyword fallback for the whole session. When we're in semantic
            # mode with a loaded index but the flag is off, cheaply re-check
            # Ollama (one /api/tags GET) so semantic recovers without a Revit
            # restart.
            if (mode == 'semantic' and not self._semantic_available
                    and self._embedding_index is not None
                    and self._ollama_client is not None):
                try:
                    if self._ollama_client.is_available():
                        self._semantic_available = True
                        logger.info("Ollama now reachable; semantic mode enabled.")
                        self._update_search_mode_button()
                except Exception as ex:
                    logger.error("Semantic re-probe failed: {}".format(ex))

            semantic_active = (mode == 'semantic' and self._semantic_available
                               and bool(global_query))
            query_vector = None
            # BIM-context enrichment now lives in the search box itself: the
            # enriched "name | class | …" string IS the typed query, so every
            # mode searches the same text. These locals stay inert for the
            # downstream tooltip / embed consumers (ctx_note never renders).
            dense_query = global_query
            ctx_note    = None
            if semantic_active:
                try:
                    query_vector = self._ollama_client.embed(dense_query)
                except Exception as ex:
                    logger.error("Semantic embed exception: {}".format(ex))
                    query_vector = None
                if query_vector is None:
                    semantic_active = False
                    self._set_status(
                        u"Semantic embedding failed — using keyword (BM25F) "
                        u"fallback for this search. Check Ollama / bge-m3.")
                    logger.warning(
                        "Semantic embed failed; falling back to BM25F. {}".format(
                            self._ollama_client.last_error if self._ollama_client else u""))
            elif (mode == 'semantic' and not self._semantic_available
                    and bool(global_query)):
                # Semantic selected but backing unavailable (Ollama still down
                # after the re-probe). Never present the keyword result as if
                # it were semantic - say so in the status bar.
                self._set_status(
                    u"Semantic unavailable — keyword (BM25F) fallback. Start "
                    u"Ollama (model bge-m3) to enable semantic ranking.")

            if global_query and mode == 'regex':
                try:
                    compiled_re = re.compile(global_query, re.IGNORECASE)
                except re.error:
                    compiled_re = None
            elif global_query and (mode == 'fuzzy' or
                                   (mode == 'semantic' and not semantic_active)):
                # Fuzzy filter is the default binary filter for any
                # non-regex mode: explicit `fuzzy` plus the semantic
                # fallback path (semantic requested but unavailable).
                fuzzy_nl = global_query.lower()
                fuzzy_tokens = fuzzy_nl.split()

            # Pre-compute typo-expanded query once per call (re-used across
            # all candidates). Cheap (~10-50ms) when the corpus has a vocab
            # of a few thousand tokens, dominated by Levenshtein on the
            # ±D length band.
            nl_query = (global_query or "").lower()
            expanded_query = None
            if global_query and self._corpus_stats is not None:
                try:
                    expanded_query = expand_query_with_typos(
                        nl_query, self._corpus_stats)
                except Exception as ex:
                    logger.error("Typo expansion failed: {}".format(ex))
                    expanded_query = None

            # Phase 1+ - Hybrid (Reciprocal Rank Fusion). Active only when
            # (a) semantic mode is on, (b) the user opted in via the
            # Hybrid checkbox, (c) BM25F corpus stats exist (need a
            # lexical ranking to fuse with), AND (d) a query is present.
            # Pre-computes a fused score dict so the per-row loop is a
            # plain lookup; ~10-20ms for ~2400 datasets.
            hybrid_rrf_requested = False
            try:
                hybrid_rrf_requested = bool(self.chk_hybrid_rrf.IsChecked)
            except Exception:
                pass
            hybrid_rrf_active = False
            rrf_scores      = None
            rrf_bm25f_ranks = None
            rrf_sem_ranks   = None
            rrf_max         = None
            if (semantic_active and hybrid_rrf_requested
                    and self._corpus_stats is not None and nl_query):
                try:
                    (rrf_scores, rrf_bm25f_ranks, rrf_sem_ranks, rrf_max
                     ) = self._compute_rrf_scores(
                        filtered, nl_query, query_vector, expanded_query)
                    hybrid_rrf_active = True
                except Exception as ex:
                    logger.error("Hybrid RRF fusion failed: {}".format(ex))
                    hybrid_rrf_active = False

            # Phase 1+ - Cross-encoder reranker post-pass. Active when
            # (a) semantic mode is on, (b) the user opted in via the
            # Rerank checkbox, (c) the local sidecar reports ready, AND
            # (d) a query is present. Applied AFTER the per-row scoring
            # loop, on the top-K rows of the first-pass ordering.
            reranker_requested = False
            try:
                reranker_requested = bool(self.chk_reranker.IsChecked)
            except Exception:
                pass
            reranker_active = (semantic_active and reranker_requested
                               and self._reranker_available
                               and self._reranker_client is not None
                               and bool(nl_query))

            result = []
            for ds in filtered:
                hay = None
                if global_query:
                    hay = build_searchable(ds, lang=self._lang)
                    if semantic_active:
                        # Semantic mode is a pure ranker - no binary
                        # filter. Every candidate is scored by cosine;
                        # the Min Score slider does the cutoff. Matches
                        # the dense-retrieval pattern in the reference
                        # daveebbelaar/ai-cookbook repo.
                        pass
                    elif mode == 'regex':
                        flat = ((hay.get("name") or "") + u" " +
                                (hay.get("classification") or "")).strip()
                        if compiled_re:
                            if not compiled_re.search(flat):
                                continue
                        else:
                            if global_query.lower() not in flat.lower():
                                continue
                    else:
                        # Fuzzy filter - used by explicit `fuzzy` mode
                        # AND the `semantic` fallback path.
                        if not _fuzzy_match_inner(fuzzy_nl, fuzzy_tokens, hay):
                            continue

                if name_f  and name_f  not in (ds.DisplayName or "").lower():            continue
                if epd_f   and epd_f   not in (getattr(ds, "EpdNo", "") or "").lower():  continue
                if class_f and class_f != "All" and (getattr(ds, "Classification", "") or "") != class_f: continue
                if owner_f and owner_f != "All" and (getattr(ds, "Owner", "") or "")    != owner_f:       continue
                if loc_f   and loc_f   != "All" and (ds.Location or "")                 != loc_f:         continue
                if type_f  and type_f  != "All" and (ds.DatasetType or "")              != type_f:        continue
                if comp_f  and comp_f  != "All" and (ds.Compliance or "")               != comp_f:        continue
                if prog_f  and prog_f  != "All" and (ds.ProgramOperator or "")          != prog_f:        continue
                if valid_f and valid_f != "All" and (getattr(ds, "ValidUntil", "") or "") != valid_f:     continue

                # Score the survivor. Phase 1: semantic mode swaps the
                # ranker to dense cosine; BM25F remains for regex/fuzzy
                # AND for the semantic fallback path. The confidence
                # calibrator is mode-aware (`_confidence_cals[mode]`)
                # so the displayed Score % and the Min Score slider
                # have identical semantics across modes.
                if global_query and hybrid_rrf_active:
                    try:
                        if hay is None:
                            hay = build_searchable(ds, lang=self._lang)
                        raw_rrf = rrf_scores.get(ds.uuid, 0.0)
                        if raw_rrf <= 0.0:
                            # Missed by BOTH rankers - no signal of any
                            # kind for this dataset. Drop it like the
                            # pure-semantic path drops vector-less rows.
                            continue
                        norm_rrf = (raw_rrf / rrf_max) if rrf_max > 0 else 0.0
                        if norm_rrf > 1.0:
                            norm_rrf = 1.0
                        cc = self._confidence_cals.get("hybrid")
                        if cc is None:
                            cc = ConfidenceCalibrator(mode="hybrid")
                        ds.MatchScore = cc.apply(norm_rrf)
                        ds.MatchScoreRaw = norm_rrf
                        ds.MatchScorePct = u"{0:.0f}%".format(
                            ds.MatchScore * 100.0)
                        ds.MatchScoreTooltip = self._format_hybrid_tooltip(
                            raw_rrf, norm_rrf,
                            rrf_bm25f_ranks.get(ds.uuid),
                            rrf_sem_ranks.get(ds.uuid),
                            cc, hay, dense_query, ctx_note)
                    except Exception as ex:
                        logger.error("Hybrid RRF score error for '{}': {}".format(
                            getattr(ds, "DisplayName", "?"), ex))
                        ds.MatchScore = 0.0
                        ds.MatchScoreRaw = 0.0
                        ds.MatchScorePct = u""
                        ds.MatchScoreTooltip = u""
                elif global_query and semantic_active:
                    try:
                        if hay is None:
                            hay = build_searchable(ds, lang=self._lang)
                        vec = (self._embedding_index.get(ds.uuid)
                               if self._embedding_index is not None else None)
                        if vec is None:
                            # No cached embedding (rare - a handful of
                            # bge-m3 NaN stragglers per source survived
                            # even the lowercase fallback). They have no
                            # meaningful cosine to the query, so dropping
                            # them is more honest than dangling unscored
                            # rows at the bottom of an otherwise relevant
                            # result list. Coverage is now ≥99.9 % so the
                            # user impact is negligible.
                            continue
                        else:
                            raw = cosine_similarity(query_vector, vec)
                            cc = self._confidence_cals.get("semantic")
                            if cc is None:
                                cc = ConfidenceCalibrator(mode="semantic")
                            ds.MatchScore = cc.apply(raw)
                            ds.MatchScoreRaw = raw
                            ds.MatchScorePct = u"{0:.0f}%".format(
                                ds.MatchScore * 100.0)
                            ds.MatchScoreTooltip = self._format_semantic_tooltip(
                                raw, cc, hay, dense_query, ctx_note)
                    except Exception as ex:
                        logger.error("Semantic score error for '{}': {}".format(
                            getattr(ds, "DisplayName", "?"), ex))
                        ds.MatchScore = 0.0
                        ds.MatchScoreRaw = 0.0
                        ds.MatchScorePct = u""
                        ds.MatchScoreTooltip = u""
                elif global_query and self._corpus_stats is not None:
                    try:
                        if hay is None:
                            hay = build_searchable(ds, lang=self._lang)
                        raw = bm25f_score(
                            nl_query, hay, self._corpus_stats,
                            params=self._bm25_params,
                            expanded_query=expanded_query,
                        )
                        # Mode-agnostic confidence. Same `apply(raw)`
                        # interface for BM25F today, cosine in Phase 1,
                        # fusion later - so the Min Score slider has the
                        # same semantics across modes ("0.5 = ambiguous,
                        # 0.8 = confident").
                        cc = self._confidence_cals.get("bm25f")
                        if cc is None:
                            cc = ConfidenceCalibrator(mode="bm25f")
                        ds.MatchScore = cc.apply(raw)
                        ds.MatchScoreRaw = raw
                        ds.MatchScorePct = u"{0:.0f}%".format(ds.MatchScore * 100.0)
                        ds.MatchScoreTooltip = self._format_score_tooltip(
                            nl_query, hay, expanded_query)
                    except Exception as ex:
                        logger.error("BM25F score error for '{}': {}".format(
                            getattr(ds, "DisplayName", "?"), ex))
                        ds.MatchScore = 0.0
                        ds.MatchScoreRaw = 0.0
                        ds.MatchScorePct = u""
                        ds.MatchScoreTooltip = u""
                else:
                    ds.MatchScore        = None
                    ds.MatchScoreRaw     = None
                    ds.MatchScorePct     = u""
                    ds.MatchScoreTooltip = u""

                # Min Score cutoff: drop scored datasets below the
                # threshold; keep unscored rows (MatchScore is None when
                # no query is active) - same rule as the Validation tool.
                if min_thr > 0.0 and ds.MatchScore is not None \
                        and ds.MatchScore < min_thr:
                    continue

                result.append(ds)

            # Rank-by-score only in Sort mode 0 (default). A-Z and Z-A modes
            # preserve the user's explicit sort choice.
            if global_query and self._sort_mode == 0 and (
                    semantic_active or self._corpus_stats is not None):
                result.sort(key=lambda d: (getattr(d, "MatchScore", 0.0) or 0.0),
                            reverse=True)

            # Phase 1+ - Cross-encoder reranker post-pass. Re-scores
            # the top-K rows of the first-pass ordering via the local
            # sidecar; mutates MatchScore + MatchScoreTooltip on the
            # head and returns the head + untouched tail.
            if reranker_active and result:
                try:
                    pairs = [(d, d.MatchScore if d.MatchScore is not None else 0.0)
                             for d in result]
                    pairs = self._apply_reranker_post_pass(
                        pairs, dense_query, constants.RERANKER_TOP_K,
                        ctx_note=ctx_note)
                    result = [d for (d, _) in pairs]
                except Exception as ex:
                    logger.error("Reranker post-pass failed: {}".format(ex))

            self._filtered_datasets = result
            self.datagrid_datasets.ItemsSource = result
            if mode == 'semantic' and not semantic_active and global_query:
                # Loud status message so the user knows the toggle is in
                # `semantic` mode but the ranker silently fell back to
                # BM25F. Names the specific remediation step.
                reason = (
                    u"sidecar missing — run embedding_prefetcher.py"
                    if self._embedding_index is None
                    else u"Ollama unreachable — start `ollama serve`")
                self._set_status(
                    u"{} of {} results — semantic unavailable ({}), "
                    u"using BM25F fallback.".format(
                        len(result), len(self.datasets), reason))
            else:
                self._set_status("{} of {} results".format(
                    len(result), len(self.datasets)))
        except Exception as ex:
            logger.error("Apply grid filters error: {}".format(ex))
    def filter_control_loaded(self, sender, e):
        """Register filter controls by their XAML Tag when they load into the visual tree."""
        try:
            tag = sender.Tag
            if tag:
                self._filter_controls[str(tag)] = sender
                # If datasets are already loaded (fast cache), populate the dropdown now.
                if hasattr(sender, "SelectedIndex") and self.datasets:
                    self._populate_single_combo(str(tag), sender)
        except Exception:
            pass
    def filter_text_changed(self, sender, e):
        """Unified TextBox filter handler (reads Tag to know which column to filter)."""
        try:
            tag = sender.Tag
            if tag:
                self._grid_filters[str(tag)] = (sender.Text or "").strip()
                self._apply_grid_filters()
        except Exception:
            pass
    def filter_combo_changed(self, sender, e):
        """Unified ComboBox filter handler (reads Tag to know which column to filter)."""
        try:
            tag = sender.Tag
            if tag:
                sel = sender.SelectedItem
                val = ""
                if sel:
                    s = str(sel)
                    if hasattr(sel, 'Content'):
                        s = str(sel.Content)
                    # Items carrying a canonical Tag (the classification
                    # combo displays translated text but filters on the
                    # German path) use the Tag as the real filter key.
                    tag_val = getattr(sel, 'Tag', None)
                    if tag_val:
                        s = str(tag_val)
                    if s != "All":
                        val = s
                self._grid_filters[str(tag)] = val
                self._apply_grid_filters()
        except Exception:
            pass
    def toggle_advanced_filters(self, sender, e):
        """Toggle the advanced filter panel visibility."""
        try:
            vis = self.border_advanced_filters.Visibility
            if vis == Visibility.Visible:
                self.border_advanced_filters.Visibility = Visibility.Collapsed
                self.btn_advanced_filters.Content = u"\u25BC Filters"
                self._advanced_panel_visible = False
            else:
                self.border_advanced_filters.Visibility = Visibility.Visible
                self.btn_advanced_filters.Content = u"\u25B2 Filters"
                self._advanced_panel_visible = True
                self._populate_advanced_combos()
        except Exception:
            pass
    def _populate_advanced_combos(self):
        """Fill indicator ComboBoxes if not already populated."""
        try:
            if self.combo_env_indicator.Items.Count > 0:
                return
            # Build tooltip lookup from INDICATOR_META
            meta_lookup = {p: en for p, en, de, unit, grp in constants.INDICATOR_META}
            for prop, label, unit in constants.ENV_FILTER_INDICATORS:
                item = ComboBoxItem()
                item.Content = u"{} ({})".format(label, unit)
                item.ToolTip = meta_lookup.get(prop, label)
                self.combo_env_indicator.Items.Add(item)
            self.combo_env_indicator.SelectedIndex = 0
            for prop, label, unit in constants.RES_FILTER_INDICATORS:
                item = ComboBoxItem()
                item.Content = u"{} ({})".format(label, unit)
                item.ToolTip = meta_lookup.get(prop, label)
                self.combo_res_indicator.Items.Add(item)
            self.combo_res_indicator.SelectedIndex = 0
            # Guard flag to prevent infinite recursion when slider and
            # text box update each other during bidirectional sync.
            self._pct_sync_in_progress = False
            # Cache phase radio button lists: (radio, phase_key)
            self._env_phase_radios = [
                (self.radio_env_sum,  "sum"),
                (self.radio_env_a1a3, "A1A3"),
                (self.radio_env_a1a5, "A1A5"),
                (self.radio_env_b1b7, "B1B7"),
                (self.radio_env_c1c4, "C1C4"),
                (self.radio_env_d,    "D"),
            ]
            self._res_phase_radios = [
                (self.radio_res_sum,  "sum"),
                (self.radio_res_a1a3, "A1A3"),
                (self.radio_res_a1a5, "A1A5"),
                (self.radio_res_b1b7, "B1B7"),
                (self.radio_res_c1c4, "C1C4"),
                (self.radio_res_d,    "D"),
            ]
        except Exception:
            pass
    def _get_active_phase(self, which):
        """Return active phase key ('sum', 'A1A5', 'B1B7', 'C1C4', 'D') for env or res."""
        radios = getattr(self, '_' + which + '_phase_radios', [])
        for radio, key in radios:
            if radio.IsChecked:
                return key
        return "sum"
    def _get_phase_value(self, ds, prop, phase):
        """Return the indicator value for *ds* in the given lifecycle *phase*.

        For 'sum' returns A1-A5 + B1-B7 + C1-C4 + D. Mandatory core per
        EN 15804 is A1-A5 and C1-C4; B1-B7 and D are optional and
        contribute 0 when absent. Returns None if A1-A5 or C1-C4 is
        missing (prevents incomplete EPDs from ranking artificially low).
        """
        if phase == "sum":
            a5 = getattr(ds, prop + "_A1A5", None)
            b  = getattr(ds, prop + "_B1B7", None)
            c  = getattr(ds, prop + "_C1C4", None)
            d  = getattr(ds, prop + "_D",    None)
            if a5 is None or c is None:
                return None
            return a5 + (b or 0.0) + c + (d or 0.0)
        return getattr(ds, prop + "_" + phase, None)
    def _compute_indicator_ranges(self):
        """Scan loaded datasets to compute percentile thresholds per indicator
        for all lifecycle phases: A1-A3, A1-A5, B1-B7, C1-C4, D, and their sum.

        self._indicator_ranges[prop] = {
            'A1A3': {'min', 'max', 'p25', 'p50', 'p75', 'count'},
            'A1A5': {...},
            'B1B7': {...},
            'C1C4': {...},
            'D':    {...},
            'sum':  {...},   # A1-A5 + B1-B7 + C1-C4 + D (requires A1-A5 and C1-C4)
        }
        A1-A3 covers EN 15804+A1 EPDs that only declare production-stage
        impacts; without it those datasets have no tier-filterable data.
        Also refreshes tier labels if filter panel is already open."""
        self._indicator_ranges = {}
        # Datasets just changed (new datastock or refresh) - invalidate the
        # cached Materials Classification L3/L4 tree so it rebuilds lazily
        # against the current datasets on the next chip toggle.
        self._classification_tree_l34 = None
        all_props = ([t[0] for t in constants.ENV_FILTER_INDICATORS] +
                     [t[0] for t in constants.RES_FILTER_INDICATORS])

        def _percentiles(vals):
            vals = sorted(vals)
            n = len(vals)
            return {
                'min':    vals[0],
                'max':    vals[-1],
                'p25':    vals[max(0, n // 4 - 1)],
                'p50':    vals[max(0, n // 2 - 1)],
                'p75':    vals[max(0, 3 * n // 4 - 1)],
                'count':  n,
                # Full sorted list for arbitrary-percentile slider lookups.
                # The slider lets the user pick any X% (1-100); the threshold
                # at X% is _percentile_threshold(prop, phase, X) which indexes
                # into this list via simple-quantile (no interpolation).
                'sorted': vals,
            }

        for prop in all_props:
            buckets = {'A1A3': [], 'A1A5': [], 'B1B7': [], 'C1C4': [], 'D': [], 'sum': []}
            for ds in self.datasets:
                a3 = getattr(ds, prop + "_A1A3", None)
                a5 = getattr(ds, prop + "_A1A5", None)
                b  = getattr(ds, prop + "_B1B7", None)
                c  = getattr(ds, prop + "_C1C4", None)
                d  = getattr(ds, prop + "_D",    None)
                if a3 is not None:
                    buckets['A1A3'].append(a3)
                if a5 is not None:
                    buckets['A1A5'].append(a5)
                if b is not None:
                    buckets['B1B7'].append(b)
                if c is not None:
                    buckets['C1C4'].append(c)
                if d is not None:
                    buckets['D'].append(d)
                # Sum bucket: require mandatory A1-A5 and C1-C4 per EN 15804.
                # B1-B7 and D are optional and contribute 0 when absent.
                if a5 is not None and c is not None:
                    buckets['sum'].append(a5 + (b or 0.0) + c + (d or 0.0))

            phase_rng = {}
            for phase_key, vals in buckets.items():
                if vals:
                    phase_rng[phase_key] = _percentiles(vals)
            if phase_rng:
                self._indicator_ranges[prop] = phase_rng

        # Refresh tier labels if filter panel already open
        try:
            if self.combo_env_indicator.Items.Count > 0:
                self._refresh_tier_label("env")
            if self.combo_res_indicator.Items.Count > 0:
                self._refresh_tier_label("res")
        except Exception:
            pass
    def _percentile_threshold(self, prop, phase, pct):
        """Return the indicator threshold for *prop*+*phase* at percentile *pct*
        (1-100). pct=100 means "no filter" so we return None. Uses the
        same simple-quantile rule as _compute_indicator_ranges (idx =
        floor(pct * n / 100) - 1, clamped to [0, n-1]). Returns None if
        no values are cached for that (prop, phase) pair."""
        try:
            pct = int(round(float(pct)))
        except Exception:
            return None
        if pct >= 100:
            return None
        if pct < 1:
            pct = 1
        rng = (getattr(self, '_indicator_ranges', {}) or {}).get(prop, {}).get(phase, {})
        sorted_vals = rng.get('sorted')
        if not sorted_vals:
            return None
        n = len(sorted_vals)
        idx = (pct * n // 100) - 1
        if idx < 0:
            idx = 0
        if idx >= n:
            idx = n - 1
        return sorted_vals[idx]
    def _refresh_tier_label(self, which):
        """Update the threshold label below the indicator slider, showing the
        actual numeric cut-off corresponding to the selected percentile."""
        try:
            if not hasattr(self, '_indicator_ranges'):
                return
            if which == "env":
                combo = self.combo_env_indicator
                inds  = constants.ENV_FILTER_INDICATORS
                label = self.text_env_tier_label
            else:
                combo = self.combo_res_indicator
                inds  = constants.RES_FILTER_INDICATORS
                label = self.text_res_tier_label

            pct = self._get_pct(which)

            # 100% = no filter \u2192 blank label
            if pct >= 100:
                label.Text = u""
                return

            idx = combo.SelectedIndex
            if idx < 0 or idx >= len(inds):
                label.Text = u""
                return

            prop  = inds[idx][0]
            unit  = inds[idx][2]
            phase = self._get_active_phase(which)

            threshold = self._percentile_threshold(prop, phase, pct)
            if threshold is None:
                label.Text = u"No data for this phase"
                return

            dec = constants.INDICATOR_RANGE_DEFAULTS.get(prop, (0, 1000, 0))[2]
            fmt = "{:.%df}" % dec
            phase_labels = {"sum":  u"Sum(A1\u2013A5 + B1\u2013B7 + C1\u2013C4 + D)",
                            "A1A3": u"A1\u2013A3",
                            "A1A5": u"A1\u2013A5",
                            "B1B7": u"B1\u2013B7",
                            "C1C4": u"C1\u2013C4",
                            "D":    u"D"}
            phase_str = phase_labels.get(phase, phase)
            label.Text = u"{} \u2264 {} {} (best {}%)".format(
                phase_str, fmt.format(threshold), unit, pct)
        except Exception:
            pass
    def combo_env_indicator_changed(self, sender, e):
        """Update env tier label when user picks a different indicator."""
        self._refresh_tier_label("env")
        self._auto_query()
    def combo_res_indicator_changed(self, sender, e):
        """Update res tier label when user picks a different indicator."""
        self._refresh_tier_label("res")
        self._auto_query()
    def env_phase_changed(self, sender, e):
        """Update env tier label when user switches lifecycle phase."""
        self._refresh_tier_label("env")
        self._auto_query()
    def res_phase_changed(self, sender, e):
        """Update res tier label when user switches lifecycle phase."""
        self._refresh_tier_label("res")
        self._auto_query()
    # ── Indicator tier slider + text-box bidirectional sync ────────────
    # The user picks a percentile in [1, 100]: 100 = no filter (show all),
    # 25 = "best 25%" (datasets whose indicator value is ≤ p25). The slider
    # and the text box stay in lock-step. Both fire _auto_query() so the
    # dataset list re-filters live as the slider drags or the user types
    # a new number and presses Enter / tabs out.
    def _get_pct(self, which):
        """Read the current percentile (1-100) for env or res from the slider.
        Falls back to 100 if the slider isn't built yet."""
        try:
            slider = getattr(self, 'slider_' + which + '_pct', None)
            if slider is None:
                return 100
            v = int(round(float(slider.Value)))
            if v < 1:
                v = 1
            if v > 100:
                v = 100
            return v
        except Exception:
            return 100
    def _set_pct(self, which, pct, source):
        """Write *pct* to the slider and text box for *which* ('env'/'res').
        *source* is 'slider' or 'text' to indicate which control already
        holds the new value (so we don't overwrite it). The guard flag
        suppresses the partner control's change event."""
        try:
            pct = int(round(float(pct)))
        except Exception:
            return
        if pct < 1:
            pct = 1
        if pct > 100:
            pct = 100
        slider = getattr(self, 'slider_' + which + '_pct', None)
        textbox = getattr(self, 'text_' + which + '_pct', None)
        self._pct_sync_in_progress = True
        try:
            if source != 'slider' and slider is not None:
                if int(round(float(slider.Value))) != pct:
                    slider.Value = pct
            if source != 'text' and textbox is not None:
                s = u"{}".format(pct)
                if textbox.Text != s:
                    textbox.Text = s
        finally:
            self._pct_sync_in_progress = False
    def env_slider_changed(self, sender, e):
        """Slider drag → update text box, refresh label, run auto-query."""
        if getattr(self, '_pct_sync_in_progress', False):
            return
        pct = self._get_pct("env")
        self._set_pct("env", pct, source='slider')
        self._refresh_tier_label("env")
        self._auto_query()
    def res_slider_changed(self, sender, e):
        """Slider drag → update text box, refresh label, run auto-query."""
        if getattr(self, '_pct_sync_in_progress', False):
            return
        pct = self._get_pct("res")
        self._set_pct("res", pct, source='slider')
        self._refresh_tier_label("res")
        self._auto_query()
    def _commit_pct_textbox(self, which):
        """Validate text-box content, clamp to [1,100], push to slider, query."""
        if getattr(self, '_pct_sync_in_progress', False):
            return
        textbox = getattr(self, 'text_' + which + '_pct', None)
        if textbox is None:
            return
        try:
            raw = (textbox.Text or u"").strip().rstrip(u'%').strip()
            pct = int(round(float(raw)))
        except Exception:
            # Invalid input → restore the current slider value
            pct = self._get_pct(which)
        if pct < 1:
            pct = 1
        if pct > 100:
            pct = 100
        self._set_pct(which, pct, source='text')
        # Make sure text box itself shows the clamped value (e.g. user typed 250 → "100")
        try:
            s = u"{}".format(pct)
            if textbox.Text != s:
                self._pct_sync_in_progress = True
                try:
                    textbox.Text = s
                finally:
                    self._pct_sync_in_progress = False
        except Exception:
            pass
        self._refresh_tier_label(which)
        self._auto_query()
    def env_pct_keydown(self, sender, e):
        """Press Enter inside text box → commit value."""
        try:
            if e.Key == Key.Enter or e.Key == Key.Return:
                self._commit_pct_textbox("env")
                e.Handled = True
        except Exception:
            pass
    def res_pct_keydown(self, sender, e):
        """Press Enter inside text box → commit value."""
        try:
            if e.Key == Key.Enter or e.Key == Key.Return:
                self._commit_pct_textbox("res")
                e.Handled = True
        except Exception:
            pass
    def env_pct_lostfocus(self, sender, e):
        """Tab out / click elsewhere → commit value."""
        self._commit_pct_textbox("env")
    def res_pct_lostfocus(self, sender, e):
        """Tab out / click elsewhere → commit value."""
        self._commit_pct_textbox("res")
    def _get_min_score(self):
        """Read the Min Score threshold (0-100) from the slider.
        Falls back to 50 if the slider isn't built yet."""
        try:
            slider = getattr(self, 'slider_min_score', None)
            if slider is None:
                return 50
            v = int(round(float(slider.Value)))
            if v < 0:
                v = 0
            if v > 100:
                v = 100
            return v
        except Exception:
            return 50
    def _set_min_score(self, pct, source):
        """Write *pct* to the Min Score slider + text box and store it.
        *source* is 'slider' / 'text' (that control already holds the
        value) or 'external' (set both). Clamp [0,100]; 0 = no filter.
        Reuses the shared _pct_sync_in_progress guard so the partner
        control's change event doesn't re-enter."""
        try:
            pct = int(round(float(pct)))
        except Exception:
            return
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        self._min_score_pct = pct
        slider = getattr(self, 'slider_min_score', None)
        textbox = getattr(self, 'text_min_score', None)
        self._pct_sync_in_progress = True
        try:
            if source != 'slider' and slider is not None:
                if int(round(float(slider.Value))) != pct:
                    slider.Value = pct
            if source != 'text' and textbox is not None:
                s = u"{}".format(pct)
                if textbox.Text != s:
                    textbox.Text = s
        finally:
            self._pct_sync_in_progress = False
    def min_score_slider_changed(self, sender, e):
        """Slider drag → sync text box, store threshold, run auto-query."""
        if getattr(self, '_pct_sync_in_progress', False):
            return
        pct = self._get_min_score()
        self._set_min_score(pct, source='slider')
        self._auto_query()
    def _commit_min_score_textbox(self):
        """Validate text-box content, clamp to [0,100], push to slider, query."""
        if getattr(self, '_pct_sync_in_progress', False):
            return
        textbox = getattr(self, 'text_min_score', None)
        if textbox is None:
            return
        try:
            raw = (textbox.Text or u"").strip().rstrip(u'%').strip()
            pct = int(round(float(raw)))
        except Exception:
            pct = self._get_min_score()
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        self._set_min_score(pct, source='text')
        try:
            s = u"{}".format(pct)
            if textbox.Text != s:
                self._pct_sync_in_progress = True
                try:
                    textbox.Text = s
                finally:
                    self._pct_sync_in_progress = False
        except Exception:
            pass
        self._auto_query()
    def min_score_pct_keydown(self, sender, e):
        """Press Enter inside text box → commit value."""
        try:
            if e.Key == Key.Enter or e.Key == Key.Return:
                self._commit_min_score_textbox()
                e.Handled = True
        except Exception:
            pass
    def min_score_pct_lostfocus(self, sender, e):
        """Tab out / click elsewhere → commit value."""
        self._commit_min_score_textbox()
    def filter_auto_query(self, _sender, _e):
        """Generic handler for any filter control that should trigger an immediate query."""
        self._auto_query()
    def _auto_query(self):
        """Run the advanced query immediately if the filter panel is open."""
        if not getattr(self, '_advanced_panel_visible', False):
            return
        self._apply_advanced_query()
    def btn_clear_filters_click(self, sender, e):
        """Reset all advanced filters and refresh."""
        try:
            self._advanced_active = False
            self._advanced_filtered = []
            # Reset all toggle buttons (classification chips, etc.)
            for tgl in self._get_all_toggle_buttons():
                tgl.IsChecked = False
            # Reset both indicator sliders + their text boxes to 100% (no filter)
            for which in ("env", "res"):
                self._set_pct(which, 100, source='external')
            # Reset Min Score to 0 (no filter) - Clear removes all filtering
            self._set_min_score(0, source='external')
            # Reset phase radio buttons to "Sum" (default)
            for radio_list in [getattr(self, '_env_phase_radios', []),
                                getattr(self, '_res_phase_radios', [])]:
                for i, (radio, _) in enumerate(radio_list):
                    radio.IsChecked = (i == 0)   # first = Sum
            # Clear tier threshold labels
            self.text_env_tier_label.Text = u""
            self.text_res_tier_label.Text = u""
            # Reset Lifecycle Modules Required checkboxes
            for _chk_name in ('chk_req_a1a5', 'chk_req_b1b7',
                               'chk_req_c1c4', 'chk_req_d'):
                chk = getattr(self, _chk_name, None)
                if chk is not None:
                    chk.IsChecked = False
            # Clear the dynamically-rendered Materials Classification chip
            # tree (L2/L3/L4 are all in panel_classification_tree). The L1
            # root chips are reset by the toggle-button loop above.
            p = getattr(self, 'panel_classification_tree', None)
            if p is not None:
                p.Children.Clear()
            # Hide active filter summary
            self.text_active_filters.Visibility = Visibility.Collapsed
            self.text_active_filters.Text = u""
            self._apply_grid_filters()
        except Exception:
            pass
    def _get_all_toggle_buttons(self):
        """Return all ToggleButton controls in the advanced filter panel."""
        toggles = []
        for attr_name in dir(self):
            if attr_name.startswith("tgl_"):
                obj = getattr(self, attr_name, None)
                if obj is not None:
                    toggles.append(obj)
        return toggles
    # ── Materials Classification chip handlers ─────────────────────────
    def classification_root_changed(self, sender, args):
        """Called when an L1 root chip is toggled. Rebuilds the entire
        nested classification tree (which interleaves L2/L3/L4 by parent
        path) and then runs an auto-query. The rebuild preserves Tag-
        based selections so deeper chips under OTHER still-selected
        roots survive the toggle."""
        try:
            self._rebuild_classification_tree()
        except Exception:
            pass
        try:
            self._auto_query()
        except Exception:
            pass

    def classification_sub_changed(self, sender, args):
        """Called when an L2 chip is toggled. Rebuilds the nested
        classification tree (so L3 children of the toggled L2 appear
        / disappear under it) and runs an auto-query."""
        try:
            self._rebuild_classification_tree()
        except Exception:
            pass
        try:
            self._auto_query()
        except Exception:
            pass

    def classification_l3_changed(self, sender, args):
        """Called when an L3 chip is toggled. Rebuilds the nested tree
        (so L4 children of the toggled L3 appear / disappear) and runs
        an auto-query."""
        try:
            self._rebuild_classification_tree()
        except Exception:
            pass
        try:
            self._auto_query()
        except Exception:
            pass

    def classification_l4_changed(self, sender, args):
        """Called when an L4 chip is toggled."""
        try:
            self._auto_query()
        except Exception:
            pass

    def _build_classification_tree_l34(self):
        """Scan self.datasets once and build the L3 + L4 tree under each
        (L1_DE, L2_DE) parent context defined in self._classification_subs.

        Uses whitespace-normalized comparison so that paths with double
        spaces (a known OEKOBAUDAT typo, e.g. 'Tageslichtsysteme und
        Rauch- / Wärmeabzugsanlagen  / Oberlichter') are matched correctly.

        Stores the result in self._classification_tree_l34 with shape:
            {
              (L1_DE, L2_DE): {
                'l3': [L3_DE_strings sorted],
                'l4_by_l3': {L3_DE: [L4_DE_strings sorted]},
              },
              ...
            }
        """
        def _norm(s):
            return u' '.join((s or u'').split())

        tree = {}
        # Build the (L1, L2) -> normalized prefix index up front
        l1_l2_pairs = []
        for chip_attr, l1_de, _l1_lbl in getattr(self, '_classification_roots', []):
            for l2_de, _l2_lbl in self._classification_subs.get(l1_de, []):
                prefix_norm = _norm(l1_de + u' / ' + l2_de + u' / ')
                l1_l2_pairs.append((l1_de, l2_de, prefix_norm))
                tree[(l1_de, l2_de)] = {'l3': set(), 'l4_by_l3': {}}

        for ds in (self.datasets or []):
            cls = getattr(ds, 'Classification', None)
            if not cls:
                continue
            cls_n = _norm(cls)
            for l1_de, l2_de, prefix_norm in l1_l2_pairs:
                if cls_n.startswith(prefix_norm):
                    suffix = cls_n[len(prefix_norm):]
                    # _norm strips trailing whitespace via split()+join(),
                    # so prefix_norm has no trailing space - meaning suffix
                    # here starts with one. Strip every part to keep the
                    # dict keys clean (otherwise " Beton" would not match
                    # _DE_TO_EN_LABELS["Beton"], so EN-mode L3 chips would
                    # fall through to the German fallback).
                    parts = [s.strip() for s in suffix.split(u' / ')]
                    parts = [s for s in parts if s]
                    if not parts:
                        break
                    bucket = tree[(l1_de, l2_de)]
                    bucket['l3'].add(parts[0])
                    if len(parts) >= 2:
                        bucket['l4_by_l3'].setdefault(parts[0], set()).add(parts[1])
                    break  # path matched a (L1, L2); don't try other pairs

        # Sort sets to lists for stable rendering
        for k, v in tree.items():
            v['l3'] = sorted(v['l3'])
            v['l4_by_l3'] = {l3: sorted(l4s) for l3, l4s in v['l4_by_l3'].items()}
        self._classification_tree_l34 = tree

    def _chip_label(self, de_str):
        """Return a Materials Classification chip label in the current
        language. In DE mode the German string is used verbatim. In EN
        mode the module-level _DE_TO_EN_LABELS dict is consulted with
        German fallback for any segment not yet translated.

        de_str is the canonical German segment from ds.Classification -
        this is what the filter prefix-match operates on, regardless of
        UI language."""
        if getattr(self, '_lang', 'de') == 'en':
            return _DE_TO_EN_LABELS.get(de_str, de_str)
        return de_str

    def _iter_chips(self, panel):
        """Yield each ToggleButton chip inside a grouped classification
        panel. The panel is a StackPanel containing alternating header
        TextBlocks and WrapPanels of ToggleButtons (built by the
        _rebuild_*_classification_chips methods). Skip TextBlock headers."""
        if panel is None:
            return
        for group in panel.Children:
            children = getattr(group, 'Children', None)
            if children is None:
                continue
            for chip in children:
                yield chip

    def _selected_classification_tags(self, depth):
        """Return list of tag-segment tuples for currently-checked chips
        in panel_classification_tree whose Tag has exactly *depth*
        segments (separated by '||'). Used by the active-filter summary
        and the prefix builder in _apply_advanced_query.

        depth=2 -> L2 selections [(L1_DE, L2_DE), ...]
        depth=3 -> L3 selections [(L1_DE, L2_DE, L3_DE), ...]
        depth=4 -> L4 selections [(L1_DE, L2_DE, L3_DE, L4_DE), ...]
        """
        out = []
        for chip in self._iter_chips(getattr(self, 'panel_classification_tree', None)):
            try:
                if chip.IsChecked and chip.Tag:
                    parts = unicode(chip.Tag).split(u'||')
                    if len(parts) == depth:
                        out.append(tuple(parts))
            except Exception:
                pass
        return out

    def _selected_l2_pairs(self):
        return self._selected_classification_tags(2)

    def _selected_l3_triples(self):
        return self._selected_classification_tags(3)

    def _selected_l4_quads(self):
        return self._selected_classification_tags(4)

    def _make_classification_group_header(self, text, indent_px=0):
        """Create the small italic gray TextBlock used as the parent-path
        header above each chip group inside panel_classification_tree.
        indent_px shifts the header right to mirror the hierarchy depth
        (12 / 24 / 36 px for L2 / L3 / L4)."""
        from System.Windows.Media import SolidColorBrush, Color
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = 10
        tb.Foreground = SolidColorBrush(Color.FromRgb(0x6B, 0x72, 0x80))
        tb.FontStyle = FontStyles.Italic
        tb.Margin = Thickness(indent_px, 4, 0, 2)
        return tb

    def _make_classification_chip_row(self, indent_px=0):
        """Return a fresh WrapPanel used to host one parent's chip group.
        indent_px shifts the whole row right to mirror hierarchy depth."""
        row = WpfWrapPanel()
        row.Margin = Thickness(indent_px, 0, 0, 2)
        return row

    def _make_classification_chip(self, tag, content, prior_tags, click_handler):
        """Create a single ToggleButton chip with the standard styling
        used across all classification levels. Restores its IsChecked
        state from prior_tags so user selections survive panel rebuilds."""
        from System.Windows.Controls.Primitives import ToggleButton
        btn = ToggleButton()
        btn.Content = content
        btn.FontSize = 10
        btn.Padding = Thickness(6, 2, 6, 2)
        btn.Margin = Thickness(0, 0, 4, 2)
        btn.Tag = tag
        btn.IsChecked = (tag in prior_tags)
        btn.Click += click_handler
        return btn

    def _rebuild_classification_tree(self):
        """Rebuild the entire L2 / L3 / L4 chip hierarchy inside the
        single panel_classification_tree, with chips INTERLEAVED by
        parent path so each deeper level sits directly under the parent
        it belongs to. Layout per selected L1 root:

            [L1>:  header at indent 12]
            [WrapPanel of L2 chips for that L1]
            for each selected L2 of that L1:
                [L1 > L2: header at indent 24]
                [WrapPanel of L3 chips]
                for each selected L3 with L4 children:
                    [L1 > L2 > L3: header at indent 36]
                    [WrapPanel of L4 chips]

        This is followed by the next selected L1 root, and so on.
        Selections are preserved across rebuilds via the chip Tag.
        """
        panel = getattr(self, 'panel_classification_tree', None)
        if panel is None:
            return
        if self._classification_tree_l34 is None:
            self._build_classification_tree_l34()

        # Snapshot prior selections (iterates the existing nested layout)
        prior_tags = set()
        for chip in self._iter_chips(panel):
            try:
                if chip.IsChecked and chip.Tag:
                    prior_tags.add(unicode(chip.Tag))
            except Exception:
                pass
        panel.Children.Clear()

        selected_roots = []
        for chip_attr, root_de, _label in getattr(self, '_classification_roots', []):
            tgl = getattr(self, chip_attr, None)
            if tgl and tgl.IsChecked:
                selected_roots.append(root_de)
        if not selected_roots:
            return

        for l1_de in selected_roots:
            l2_list = self._classification_subs.get(l1_de, [])
            if not l2_list:
                continue
            # === L2 group for this L1 ===
            panel.Children.Add(self._make_classification_group_header(
                self._chip_label(l1_de) + u":", indent_px=12))
            l2_row = self._make_classification_chip_row(indent_px=12)
            l2_selected_de = []  # which L2 chips are checked under this L1
            for l2_de, _en in l2_list:
                tag = l1_de + u'||' + l2_de
                btn = self._make_classification_chip(
                    tag, self._chip_label(l2_de),
                    prior_tags, self.classification_sub_changed)
                l2_row.Children.Add(btn)
                if tag in prior_tags:
                    l2_selected_de.append(l2_de)
            panel.Children.Add(l2_row)

            # === L3 groups: one per selected L2 of this L1 ===
            for l2_de in l2_selected_de:
                entry = (self._classification_tree_l34 or {}).get((l1_de, l2_de))
                if not entry or not entry['l3']:
                    continue
                panel.Children.Add(self._make_classification_group_header(
                    u"{} > {}:".format(self._chip_label(l1_de),
                                       self._chip_label(l2_de)),
                    indent_px=24))
                l3_row = self._make_classification_chip_row(indent_px=24)
                l3_selected_de = []
                for l3_de in entry['l3']:
                    tag = l1_de + u'||' + l2_de + u'||' + l3_de
                    btn = self._make_classification_chip(
                        tag, self._chip_label(l3_de),
                        prior_tags, self.classification_l3_changed)
                    l3_row.Children.Add(btn)
                    if tag in prior_tags:
                        l3_selected_de.append(l3_de)
                panel.Children.Add(l3_row)

                # === L4 groups: one per selected L3 of this (L1, L2) ===
                for l3_de in l3_selected_de:
                    l4_list = entry['l4_by_l3'].get(l3_de, [])
                    if not l4_list:
                        continue
                    panel.Children.Add(self._make_classification_group_header(
                        u"{} > {} > {}:".format(self._chip_label(l1_de),
                                                self._chip_label(l2_de),
                                                self._chip_label(l3_de)),
                        indent_px=36))
                    l4_row = self._make_classification_chip_row(indent_px=36)
                    for l4_de in l4_list:
                        tag = l1_de + u'||' + l2_de + u'||' + l3_de + u'||' + l4_de
                        btn = self._make_classification_chip(
                            tag, self._chip_label(l4_de),
                            prior_tags, self.classification_l4_changed)
                        l4_row.Children.Add(btn)
                    panel.Children.Add(l4_row)

    def _apply_advanced_query(self):
        """Apply advanced indicator/attribute/dataset filters as a pre-filter."""
        try:
            # ── Read indicator filter settings ────────────────────────────────
            # Each indicator section now uses a Slider + TextBox in [1, 100]
            # representing "show the best X% of datasets by indicator value".
            # 100 = no filter (slider full right), <100 = active threshold.
            env_idx          = self.combo_env_indicator.SelectedIndex
            env_prop         = constants.ENV_FILTER_INDICATORS[env_idx][0] if env_idx >= 0 else None
            env_label        = constants.ENV_FILTER_INDICATORS[env_idx][1] if env_idx >= 0 else u""
            active_env_phase = self._get_active_phase("env")
            env_pct          = self._get_pct("env")
            env_threshold    = (self._percentile_threshold(env_prop, active_env_phase, env_pct)
                                if env_prop else None)
            env_active       = env_threshold is not None

            res_idx          = self.combo_res_indicator.SelectedIndex
            res_prop         = constants.RES_FILTER_INDICATORS[res_idx][0] if res_idx >= 0 else None
            res_label        = constants.RES_FILTER_INDICATORS[res_idx][1] if res_idx >= 0 else u""
            active_res_phase = self._get_active_phase("res")
            res_pct          = self._get_pct("res")
            res_threshold    = (self._percentile_threshold(res_prop, active_res_phase, res_pct)
                                if res_prop else None)
            res_active       = res_threshold is not None

            # ── Materials Classification (4-level hierarchical chips) ────────
            # Filter on ds.Classification by prefix-matching against the
            # selected L1/L2/L3/L4 chips. The deepest selected level on each
            # branch wins - e.g. if L3="Asphaltbinder" is selected, the L1
            # and L2 of that branch contribute no prefix on their own.
            #
            # OEKOBAUDAT taxonomy: 11 L1 roots (hardcoded in
            # _classification_roots), ~53 L2 subgroups (_classification_subs),
            # 257 L3 leaves and 9 L4 leaves (auto-derived from cached
            # classification paths in _classification_tree_l34).
            selected_roots = []
            for chip_attr, root_de, _label in getattr(self, '_classification_roots', []):
                tgl = getattr(self, chip_attr, None)
                if tgl and tgl.IsChecked:
                    selected_roots.append(root_de)
            selected_l2 = self._selected_l2_pairs()    # (L1, L2)
            selected_l3 = self._selected_l3_triples()  # (L1, L2, L3)
            selected_l4 = self._selected_l4_quads()    # (L1, L2, L3, L4)

            l1_with_l2 = set(t[0] for t in selected_l2)
            l2_with_l3 = set((t[0], t[1]) for t in selected_l3)
            l3_with_l4 = set((t[0], t[1], t[2]) for t in selected_l4)

            accept_prefixes = []
            for r in selected_roots:
                if r not in l1_with_l2:
                    accept_prefixes.append(r + u' /')
            for (l1, l2) in selected_l2:
                if (l1, l2) not in l2_with_l3:
                    accept_prefixes.append(l1 + u' / ' + l2 + u' /')
            for (l1, l2, l3) in selected_l3:
                if (l1, l2, l3) not in l3_with_l4:
                    accept_prefixes.append(l1 + u' / ' + l2 + u' / ' + l3 + u' /')
            for (l1, l2, l3, l4) in selected_l4:
                accept_prefixes.append(l1 + u' / ' + l2 + u' / ' + l3 + u' / ' + l4)
            classification_filter_active = bool(accept_prefixes)
            # Pre-normalize prefixes (collapse multiple spaces) so that
            # OEKOBAUDAT's known double-space typos don't produce false
            # negatives.
            accept_prefixes_norm = [u' '.join(p.split()) for p in accept_prefixes]

            # ── Lifecycle Module Completeness (combined phases) ──────────────
            # A1-A5 = any production/construction module present.
            # B1-B7 = any use-stage module present.
            # C1-C4 = any end-of-life module present.
            # D     = module D present.
            def _req(name):
                chk = getattr(self, name, None)
                return bool(chk and chk.IsChecked)
            req_a1a5 = _req('chk_req_a1a5')
            req_b1b7 = _req('chk_req_b1b7')
            req_c1c4 = _req('chk_req_c1c4')
            req_d    = _req('chk_req_d')
            any_module_req = (req_a1a5 or req_b1b7 or req_c1c4 or req_d)

            # ── Check if any filter is active ─────────────────────────────────
            has_any = (
                env_active or res_active
                or classification_filter_active
                or any_module_req
            )

            if not has_any:
                self._advanced_active = False
                self._advanced_filtered = []
                self.text_active_filters.Visibility = Visibility.Collapsed
                self._apply_grid_filters()
                return

            # ── Apply filters ─────────────────────────────────────────────────
            result = []
            no_data_count = 0
            for ds in self.datasets_sorted:
                # Indicator threshold filter - datasets that don't declare
                # the required modules (val is None) are unconditionally
                # excluded. Users who want to see those datasets can:
                #   * raise the slider to 100% (no filter), or
                #   * switch the phase radio to A1-A3 (which most older
                #     EN 15804+A1 EPDs declare), or
                #   * use the Lifecycle Modules Required checkboxes for
                #     explicit completeness control.
                if env_active:
                    val = self._get_phase_value(ds, env_prop, active_env_phase)
                    if val is None:
                        no_data_count += 1
                        continue
                    if val > env_threshold:
                        continue

                if res_active:
                    val = self._get_phase_value(ds, res_prop, active_res_phase)
                    if val is None:
                        no_data_count += 1
                        continue
                    if val > res_threshold:
                        continue

                # Materials Classification - whitespace-normalized prefix
                # match against the German classification path. Each prefix
                # ends in " /" so "Holz /" doesn't false-match
                # "Holzwerkstoffe / ...". Normalization collapses
                # OEKOBAUDAT's known double-space typo (e.g.
                # "...  / Oberlichter") so it still matches.
                if classification_filter_active:
                    cls_n = u' '.join((ds.Classification or u'').split())
                    matched = False
                    for px in accept_prefixes_norm:
                        if cls_n.startswith(px) or cls_n == px.rstrip(u' /'):
                            matched = True
                            break
                    if not matched:
                        continue

                # Lifecycle Module Completeness (combined phases)
                # Each chip requires the dataset to have *at least one* module
                # in that phase present in ModulesAvailable.
                if any_module_req:
                    mods = set(m.lower() for m in getattr(ds, 'ModulesAvailable', []))
                    if req_a1a5:
                        a_candidates = {"a1", "a2", "a3", "a1-a3", "a4", "a5", "a1-a5"}
                        if not (mods & a_candidates):
                            continue
                    if req_b1b7:
                        b_candidates = {"b1", "b2", "b3", "b4", "b5", "b6", "b7", "b1-b7"}
                        if not (mods & b_candidates):
                            continue
                    if req_c1c4:
                        c_candidates = {"c1", "c2", "c3", "c4", "c1-c4"}
                        if not (mods & c_candidates):
                            continue
                    if req_d and "d" not in mods:
                        continue

                result.append(ds)

            self._advanced_active = True
            self._advanced_filtered = result
            self._apply_grid_filters()

            # ── Status bar ────────────────────────────────────────────────────
            status = u"{} of {} results (filtered)".format(
                len(self._filtered_datasets), len(self.datasets))
            if no_data_count > 0:
                status += u" | {} without indicator data".format(no_data_count)
            self._set_status(status)

            # ── Active filter summary badges ──────────────────────────────────
            parts = []
            _phase_short = {"sum":  "Sum",
                            "A1A3": "A1-A3",
                            "A1A5": "A1-A5",
                            "B1B7": "B1-B7",
                            "C1C4": "C1-C4",
                            "D":    "D"}
            if env_active:
                dec = constants.INDICATOR_RANGE_DEFAULTS.get(env_prop, (0,1,0))[2]
                parts.append(u"{} best {}% [{}] (≤ {})".format(
                    env_label, env_pct,
                    _phase_short.get(active_env_phase, active_env_phase),
                    ("{:.%df}" % dec).format(env_threshold)))
            if res_active:
                dec = constants.INDICATOR_RANGE_DEFAULTS.get(res_prop, (0,1,0))[2]
                parts.append(u"{} best {}% [{}] (≤ {})".format(
                    res_label, res_pct,
                    _phase_short.get(active_res_phase, active_res_phase),
                    ("{:.%df}" % dec).format(res_threshold)))
            if classification_filter_active:
                # Format chip-summary text using language-aware labels
                # (everything routes through _chip_label so the summary
                # follows the DE/EN toggle along with the chips).
                _summary_chunks = []
                # L1-only branches (no L2 narrowing)
                for r in selected_roots:
                    if r not in l1_with_l2:
                        _summary_chunks.append(self._chip_label(r))
                # L2 branches (no L3 narrowing)
                for (l1, l2) in selected_l2:
                    if (l1, l2) not in l2_with_l3:
                        _summary_chunks.append(u"{}>{}".format(
                            self._chip_label(l1), self._chip_label(l2)))
                # L3 branches (no L4 narrowing)
                for (l1, l2, l3) in selected_l3:
                    if (l1, l2, l3) not in l3_with_l4:
                        _summary_chunks.append(u"{}>{}>{}".format(
                            self._chip_label(l1),
                            self._chip_label(l2),
                            self._chip_label(l3)))
                # L4 leaf branches
                for (l1, l2, l3, l4) in selected_l4:
                    _summary_chunks.append(u"{}>{}>{}>{}".format(
                        self._chip_label(l1),
                        self._chip_label(l2),
                        self._chip_label(l3),
                        self._chip_label(l4)))
                if _summary_chunks:
                    _label = (u"Klassifikation: "
                              if getattr(self, '_lang', 'de') == 'de'
                              else u"Classification: ")
                    parts.append(_label + u" | ".join(_summary_chunks))
            req_modules = []
            for _flag, _label in [
                (req_a1a5, "A1-A5"), (req_b1b7, "B1-B7"),
                (req_c1c4, "C1-C4"), (req_d, "D"),
            ]:
                if _flag:
                    req_modules.append(_label)
            if req_modules:
                parts.append(u"Modules: " + u"+".join(req_modules))
            if parts:
                self.text_active_filters.Text = u"Active: " + u" | ".join(parts)
                self.text_active_filters.Visibility = Visibility.Visible
            else:
                self.text_active_filters.Visibility = Visibility.Collapsed

        except Exception as ex:
            logger.error("Advanced query error: {}".format(ex))
    def _set_status(self, text):
        try:
            self.text_status.Text = text
        except Exception:
            pass
    def hyperlink_navigate(self, sender, e):
        try:
            import webbrowser
            webbrowser.open(str(e.Uri))
        except Exception:
            pass
    def button_create_material(self, sender, e):
        if not self.selected_dataset:
            forms.alert("Please select a dataset first.", title=constants.TOOL_TITLE)
            return
        try:
            if self._current_source != "csv" and not self.selected_dataset._details_fetched:
                self._fetch_and_populate_details(self.selected_dataset)

            if not self.selected_dataset.uuid:
                forms.alert("Selected dataset has no UUID.", title=constants.TOOL_TITLE)
                return

            mats = list(FilteredElementCollector(doc).OfClass(DB.Material).ToElements())
            if not mats:
                forms.alert("No Revit materials found.", title=constants.TOOL_TITLE)
                return

            mat_by_name = {m.Name: m for m in mats}
            names = sorted(mat_by_name.keys(), key=lambda s: (s or "").lower())

            picked = forms.SelectFromList.show(
                names,
                title="Select Revit Materials to Map",
                button_name="Map UUID",
                multiselect=True
            )
            if not picked:
                return

            picked_names = picked if isinstance(picked, list) else [picked]
            picked_mats  = [mat_by_name.get(n) for n in picked_names if mat_by_name.get(n)]
            if not picked_mats:
                return

            mapped_names, failed = self._mapper.map_dataset(
                picked_mats, self.selected_dataset
            )

            msg = u"UUID: {}\n\nMapped: {}\n".format(
                self.selected_dataset.uuid, len(mapped_names)
            )
            if failed:
                msg += u"\nFailed: {}\n".format(len(failed))
                for name, reason in failed[:10]:
                    msg += u"- {}: {}\n".format(name, reason)
                if len(failed) > 10:
                    msg += u"... and {} more\n".format(len(failed) - 10)
            forms.alert(msg, title=constants.TOOL_TITLE)

        except Exception as ex:
            logger.error("Map error: {}".format(ex))
            logger.error(traceback.format_exc())
            forms.alert(u"Error:\n{}".format(ex), title=constants.TOOL_TITLE)

    # ════════════════════════════════════════════════════════════════
    # Dataset grid – selection
    def datagrid_datasets_selection_changed(self, sender, args):
        """Load details for the selected dataset."""
        try:
            item = self.datagrid_datasets.SelectedItem
            if item is None:
                return
            self.selected_dataset = item
            self._fetch_and_populate_details(item)
        except Exception as ex:
            logger.error("Selection error: {}".format(ex))
    def indicator_grid_preview_mouse_wheel(self, sender, e):
        """Forward mouse-wheel events from indicator DataGrids to the outer ScrollViewer.

        WPF DataGrid consumes MouseWheel even when VerticalScrollBarVisibility=Disabled,
        so wheel events never reach the parent ScrollViewer unless we forward them here.
        """
        try:
            sv = self.indicator_scroll_viewer
            # e.Delta: +120 per notch upward, -120 per notch downward
            # Scroll ~3 rows (~66 px) per notch to match system feel
            delta = -(e.Delta / 120.0) * 66.0
            sv.ScrollToVerticalOffset(sv.VerticalOffset + delta)
            e.Handled = True
        except Exception:
            pass
    def datagrid_env_selection_changed(self, sender, args):
        """Track the selected environmental indicator row."""
        try:
            self._selected_indicator = self.datagrid_env.SelectedItem
        except Exception:
            pass
    def datagrid_resource_selection_changed(self, sender, args):
        """Track the selected resource-use indicator row."""
        try:
            self._selected_indicator = self.datagrid_resource.SelectedItem
        except Exception:
            pass
    def indicator_grid_mouse_down(self, sender, args):
        """Record the mouse-down point for drag-threshold detection."""
        try:
            self._drag_start_point = args.GetPosition(None)
        except Exception:
            pass
    def indicator_grid_mouse_move(self, sender, args):
        """Initiate a drag-drop if the threshold is exceeded."""
        try:
            if args.LeftButton != MouseButtonState.Pressed:
                return
            if self._drag_start_point is None:
                return
            pos = args.GetPosition(None)
            dx = abs(pos.X - self._drag_start_point.X)
            dy = abs(pos.Y - self._drag_start_point.Y)
            if dx < SystemParameters.MinimumHorizontalDragDistance and \
               dy < SystemParameters.MinimumVerticalDragDistance:
                return
            row = self._selected_indicator
            if row is None:
                return
            _DRAG_ITEM["type"] = "indicator"
            _DRAG_ITEM["data"] = row
            DragDrop.DoDragDrop(sender, DataObject("indicator_row", row),
                                DragDropEffects.Copy)
            self._drag_start_point = None
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    # Dataset grid – drag initiation
    def datagrid_datasets_mouse_down(self, sender, args):
        """Record the mouse-down point for drag-threshold detection."""
        try:
            self._drag_start_point = args.GetPosition(None)
        except Exception:
            pass
    def datagrid_datasets_mouse_move(self, sender, args):
        """Initiate a drag-drop if the threshold is exceeded."""
        try:
            if args.LeftButton != MouseButtonState.Pressed:
                return
            if self._drag_start_point is None:
                return
            pos = args.GetPosition(None)
            dx = abs(pos.X - self._drag_start_point.X)
            dy = abs(pos.Y - self._drag_start_point.Y)
            if dx < SystemParameters.MinimumHorizontalDragDistance and \
               dy < SystemParameters.MinimumVerticalDragDistance:
                return
            ds = self.selected_dataset
            if ds is None:
                return
            _DRAG_ITEM["type"] = "dataset"
            _DRAG_ITEM["data"] = ds
            DragDrop.DoDragDrop(sender, DataObject("dataset", ds),
                                DragDropEffects.Copy)
            self._drag_start_point = None
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    # Section visibility toggles (env / resource indicator grids)
    def chk_env_changed(self, sender, args):
        """Show/hide the environmental impact DataGrid."""
        try:
            self.datagrid_env.Visibility = (
                Visibility.Visible if self.chk_env.IsChecked else Visibility.Collapsed
            )
        except Exception:
            pass
    def chk_resource_changed(self, sender, args):
        """Show/hide the resource use DataGrid."""
        try:
            self.datagrid_resource.Visibility = (
                Visibility.Visible if self.chk_resource.IsChecked else Visibility.Collapsed
            )
        except Exception:
            pass
    def chk_tech_description_changed(self, sender, args):
        """Show/hide the technology description content."""
        try:
            self.border_tech_description_content.Visibility = (
                Visibility.Visible if self.chk_tech_description.IsChecked else Visibility.Collapsed
            )
        except Exception:
            pass


