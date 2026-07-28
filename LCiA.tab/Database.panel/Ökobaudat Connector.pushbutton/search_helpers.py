# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""
Text search utility functions (regex and fuzzy) used by the dataset filter system.
"""
#pylint: disable=invalid-name
import math
import random as _random
import re as _re
import json as _json
import struct as _struct
from collections import defaultdict

# Backend selection for `OllamaEmbeddingClient` - `System.Net.WebClient`
# when running under IronPython 2.7 inside Revit, `urllib.request` when
# running under CPython 3 in offline tools (`embedding_prefetcher.py`,
# `phase0_benchmark.py`).
try:
    from System.Net import WebClient as _WebClient
    from System.Net import WebRequest as _WebRequest
    from System.Net import HttpWebRequest as _HttpWebRequest
    from System.IO import StreamReader as _StreamReader
    from System.IO import MemoryStream as _MemoryStream
    from System.Text import Encoding as _Encoding
    from System import Array as _Array, Byte as _Byte
    _HAVE_DOTNET = True
except (ImportError, NameError):
    _HAVE_DOTNET = False
    try:
        import urllib.request as _urllib_req
        import urllib.error   as _urllib_error
    except ImportError:
        import urllib2 as _urllib_req
        _urllib_error = _urllib_req  # HTTPError is `urllib2.HTTPError` on Py2

# Module-version sentinel. Bump on every Unicode/HTTP transport hotfix so
# the live UI can prove the new code actually loaded (IronPython 2.7 caches
# imported modules in-process - editing the .py on disk does NOT reload it
# until Revit is restarted). Surfaced in `LocalRerankerClient.last_error`
# on transient errors and in the OllamaEmbeddingClient status as well.
SEARCH_HELPERS_VERSION = "1.7d"  # Phase 1.7d: hand-rolled JSON encoder, no _json.dumps


# Word-boundary tokenizer used by the fuzzy matcher / scorer.
# Splits on any non-word character (so punctuation like "-", "/", ",", ";"
# is dropped rather than retained as a 1-char token). Without this, a query
# like "Door - Panel" would yield tokens ["door", "-", "panel"] and the "-"
# token would always exact-match against haystacks (which usually contain a
# hyphen), inflating token_avg artificially.
_WORD_RE = _re.compile(r"\w+", _re.UNICODE)


def _tokenize(text):
    """Return the alphanumeric tokens in `text` (lowercased upstream)."""
    return _WORD_RE.findall(text) if text else []


def _regex_match(pattern, text):
    """Return True if pattern matches text as a regex (case-insensitive).
    Falls back to substring match if the pattern is not valid regex."""
    try:
        return _re.search(pattern, text, _re.IGNORECASE) is not None
    except _re.error:
        return (pattern or "").lower() in (text or "").lower()


# ── Levenshtein helpers for typo-tolerant fuzzy matching ──────

def _levenshtein(s1, s2, max_dist):
    """Bounded Levenshtein distance using single-row DP.
    Returns actual distance if <= max_dist, otherwise max_dist + 1."""
    len1, len2 = len(s1), len(s2)
    if abs(len1 - len2) > max_dist:
        return max_dist + 1
    # Orient shorter string as columns for smaller array
    if len1 > len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1
    prev = list(range(len1 + 1))
    for j in range(1, len2 + 1):
        curr = [j] + [0] * len1
        row_min = j
        for i in range(1, len1 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[i] = min(curr[i - 1] + 1, prev[i] + 1, prev[i - 1] + cost)
            if curr[i] < row_min:
                row_min = curr[i]
        if row_min > max_dist:
            return max_dist + 1
        prev = curr
    return prev[len1]


def _max_distance(word_len):
    """Edit distance threshold based on query word length."""
    if word_len <= 2:
        return 0
    if word_len <= 4:
        return 1
    if word_len <= 8:
        return 2
    return 3


def _fuzzy_match(needle, haystack):
    """Typo-tolerant fuzzy match with three tiers:
    1. Exact substring (current behavior)
    2. Multi-word AND substring
    3. Token-level edit distance
    """
    if not needle:
        return True
    nl = (needle or "").lower()
    hl = (haystack or "").lower()
    return _fuzzy_match_inner(nl, _tokenize(nl), hl)


def _qt_matches_any(qt, qlen, max_d, hay_words):
    """True if query token `qt` matches at least one haystack word via
    whole-word edit distance or partial-in-compound edit distance.
    Pure helper, no side effects. Assumes max_d > 0; callers handle the
    max_d == 0 (no-typo-budget) case with a substring check upstream."""
    for hw in hay_words:
        # Whole-word edit-distance match
        if abs(qlen - len(hw)) <= max_d:
            if _levenshtein(qt, hw, max_d) <= max_d:
                return True
        # Partial-in-compound match: slide a qlen-wide window over longer
        # haystack words (handles compound words like "Baustahl" matching
        # typo "staahl").
        if len(hw) > qlen:
            for pos in range(len(hw) - qlen + 1):
                sub = hw[pos:pos + qlen]
                if _levenshtein(qt, sub, max_d) <= max_d:
                    return True
    return False


def _haystack_string(searchable):
    """Flatten a v6 dict searchable to a single string for the binary
    filter. Accepts either a dict (preferred) or the legacy flat string.
    Filtering doesn't need field separation - only ranking does."""
    if isinstance(searchable, dict):
        name  = searchable.get("name", "") or ""
        clas  = searchable.get("classification", "") or ""
        return (name + " " + clas) if (name and clas) else (name or clas)
    return searchable or ""


def _fuzzy_match_inner(nl, tokens, hl):
    """Inner fuzzy match using pre-lowered needle and tokens (avoids
    re-computing these when called in a tight loop over many datasets).

    `hl` may be either a flat string (legacy) or a v6 dict; if a dict,
    it's flattened internally via `_haystack_string` for the filter.

    Phase 0 v4 (2026-05): majority-match filter.

    Previous behaviour was strict-AND: every query token had to find at
    least a substring-or-edit-distance match in the haystack. That killed
    real candidates whenever a query mixed a strong discriminator word
    with a weak generic descriptor - e.g. "Brick, Common" required BOTH
    "brick" AND "common" to match, so the obvious "Brick (unfilled)"
    dataset (which has "brick" but no word remotely close to "common")
    was dropped before scoring.

    New behaviour: count how many tokens match, pass if at least
    ceil(n_tokens / 2) match. So 1-token queries are unchanged, 2-token
    queries pass on 1 hit, 4-token queries need 2 hits, etc. The score
    function (_fuzzy_score) takes care of ranking the loose-filter
    survivors by overall match quality - a candidate matching 2 of 2
    tokens still outscores one matching 1 of 2, because token_avg
    averages 0.0 for unmatched tokens with 1.0 for exact-word matches.
    """
    # Accept either a flat string (legacy) or a v6 dict searchable.
    if isinstance(hl, dict):
        hl = _haystack_string(hl)
    # Tier 1: full query string as substring (unchanged - short-circuit
    # for the strongest possible match).
    if nl in hl:
        return True
    if not tokens:
        return True
    hay_words = _tokenize(hl)
    if not hay_words:
        return False

    # Tier 2: count token matches, pass if at least half (rounded up) hit.
    matched = 0
    for qt in tokens:
        qlen  = len(qt)
        max_d = _max_distance(qlen)
        # Quick substring path - true for any qt that appears verbatim
        # anywhere in the haystack text (covers both the max_d == 0 short
        # token case and the typical word-found-in-text case).
        if qt in hl:
            matched += 1
            continue
        if max_d == 0:
            # Short tokens (length ≤ 2) get no typo budget. The only way
            # they can match is via the substring check above, which
            # already failed → don't count this token.
            continue
        # Edit-distance / partial-in-compound check
        if _qt_matches_any(qt, qlen, max_d, hay_words):
            matched += 1

    threshold = max(1, (len(tokens) + 1) // 2)   # ceil(n / 2), min 1
    return matched >= threshold


def _match(query, text, mode):
    """Dispatch to regex or fuzzy match based on mode ('regex' or 'fuzzy')."""
    if not query:
        return True
    return _regex_match(query, text) if mode == 'regex' else _fuzzy_match(query, text)


# ── Trigram similarity (for German compound words) ────────────

def _trigram_set(s):
    """Return the set of character trigrams for string *s*."""
    if len(s) < 3:
        return set([s]) if s else set()
    return set(s[i : i + 3] for i in range(len(s) - 2))


def _trigram_similarity(s1, s2):
    """Trigram (3-gram) Jaccard-style similarity in [0.0, 1.0].

    Works like PostgreSQL's pg_trgm - excellent for catching rearranged or
    partially-overlapping compound words (e.g. "Fertigteilbetonboden" vs
    "Betonfertigteil Decke").
    """
    t1 = _trigram_set(s1)
    t2 = _trigram_set(s2)
    if not t1 or not t2:
        return 0.0
    inter = len(t1 & t2)
    return inter / float(max(len(t1), len(t2)))


# ── Numeric fuzzy scoring for hybrid ranking ──────────────────

# Soft "target field length" in chars. Fields ≤ FIELD_NORM_K get
# length_factor=1.0; longer fields fall off as sqrt(K/len). 25 chars
# matches typical short product names in OEKOBAUDAT (e.g. "Cement
# screed", "Window glass single"). Tune higher to be more lenient
# on long names, lower to penalise them harder.
FIELD_NORM_K = 25.0

# Weight applied to a classification-only match. A perfect classification
# match alone can never produce a final score above this value, because
# `final = max(name_score, CLASS_WEIGHT * class_score)`. Set to 0.5 so
# that classification matches act as corroborating evidence but never
# alone push a candidate to the top of the ranking - the *name* is
# what identifies the EPD.
CLASS_WEIGHT = 0.5


def _best_token_match(t, field_lower, field_tokens):
    """Score one query token against one field. Returns 0..1.

    1.00 = exact whole-word match
    0.75 = plain substring anywhere in the field
    0.65 × (1 - d/(D+1)) = edit-distance whole-word match (D = max_distance)
    0.60 = substring inside a compound word
    0.40 = typo-tolerant partial inside a compound word
    0.00 = no match

    Extracted from the legacy `_fuzzy_score` per-token loop so it can be
    reused by both `_field_score` (ranking) and the Validation tooltip
    breakdown (which mirrors the same math).
    """
    tlen  = len(t)
    max_d = _max_distance(tlen)
    best  = 0.0

    if t in field_lower:
        best = 0.75

    for hw in field_tokens:
        if t == hw:
            return 1.0
        if max_d > 0 and abs(tlen - len(hw)) <= max_d:
            d = _levenshtein(t, hw, max_d)
            if d <= max_d:
                best = max(best, 0.65 * (1.0 - d / (max_d + 1.0)))
        if len(hw) > tlen:
            for pos in range(len(hw) - tlen + 1):
                sub = hw[pos:pos + tlen]
                if t == sub:
                    best = max(best, 0.60)
                    break
                if max_d > 0:
                    d = _levenshtein(t, sub, max_d)
                    if d <= max_d:
                        best = max(best, 0.40)
                        break
        if best >= 1.0:
            break
    return best


def _length_factor(field_len):
    """Soft length normaliser. Fields ≤ FIELD_NORM_K get factor 1.0;
    longer fields fall off as sqrt(K/len). At 2K → 0.71, at 4K → 0.50.
    Returns 0.0 for empty fields."""
    if field_len <= 0:
        return 0.0
    if field_len <= FIELD_NORM_K:
        return 1.0
    return (FIELD_NORM_K / field_len) ** 0.5


def _field_score(needle_lower, field_lower):
    """Return a 0..1 score for matching `needle_lower` against ONE field
    (the dataset's name OR its classification path, separately).

    Computes the three v5 signals (token_avg, substring_bonus,
    trigram_sim) scoped to this single field, takes their max, then
    multiplies by a length factor so that a perfect match in a short
    focused field outscores the same match buried in a long field.
    """
    if not field_lower or not needle_lower:
        return 0.0
    if needle_lower == field_lower:
        return 1.0

    needle_tokens = _tokenize(needle_lower)
    field_tokens  = _tokenize(field_lower)
    if not needle_tokens:
        return 1.0
    if not field_tokens:
        return 0.0

    # Signal 1: token_avg
    token_scores = [_best_token_match(t, field_lower, field_tokens)
                    for t in needle_tokens]
    token_avg = sum(token_scores) / len(token_scores) if token_scores else 0.0

    # Signal 2: substring bonus
    if needle_lower in field_lower:
        substring_bonus = 0.85 + 0.10 * min(
            len(needle_lower) / max(len(field_lower), 1), 1.0)
    else:
        substring_bonus = 0.0

    # Signal 3: trigram similarity
    trigram_sim = _trigram_similarity(needle_lower, field_lower)

    raw = max(token_avg, substring_bonus, trigram_sim)
    return raw * _length_factor(len(field_lower))


def _split_searchable(searchable):
    """Accept either the new dict form (`{"name": ..., "classification": ...}`)
    or the legacy flat-string form (treated as name with no classification).
    Returns `(name_lower, class_lower)`."""
    if isinstance(searchable, dict):
        return (searchable.get("name", "") or "",
                searchable.get("classification", "") or "")
    return ((searchable or "").lower(), "")


def _fuzzy_score(needle, searchable):
    """Return a float 0.0–1.0 representing fuzzy match quality.

    Phase 0 v6 (2026-05): field-weighted + length-normalised. Splits
    the haystack into NAME and CLASSIFICATION, scores each separately
    with the v5 signal pipeline (token_avg, substring_bonus,
    trigram_sim → max → × length_factor), then combines via

        final = max(name_score, CLASS_WEIGHT × class_score)

    where CLASS_WEIGHT = 0.5. So a perfect classification-only match
    tops out at 0.50 - classification is corroborating evidence, not
    a primary signal. A name match always dominates.

    The fix solves the v5 saturation problem: previously, any haystack
    that contained the query as a whole-word token scored 1.00 (perfect),
    so single-word queries like "Glass" tied ALL nine candidates at
    100% with no useful ranking. Now a 5-char query in a 5-char name
    scores 1.00, but the same query in a 38-char name scores
    1.00 × sqrt(25/38) ≈ 0.81 - and a classification-only match
    caps at 0.50.

    `searchable` accepts:
      * dict (preferred): {"name": <lowered>, "classification": <lowered>}
        - produced by `build_searchable()`.
      * str (legacy): treated as name-only with no classification.
    """
    if not needle:
        return 1.0
    nl = (needle or "").lower()
    if not nl:
        return 1.0

    name_lower, class_lower = _split_searchable(searchable)
    if not name_lower and not class_lower:
        return 0.0

    name_score  = _field_score(nl, name_lower)
    class_score = _field_score(nl, class_lower)

    return max(name_score, CLASS_WEIGHT * class_score)


# Regex template definitions for the "/" slash-command popup.
# Each tuple: (label, description, template, placeholder)
# placeholder is the exact substring inside template that gets selected after insertion.
REGEX_TEMPLATES = [
    ("Starts with",    "Match values at the beginning",       "^value",        "value"),
    ("Ends with",      "Match values at the end",             "value$",        "value"),
    ("Exact match",    "Match the whole field exactly",       "^value$",       "value"),
    ("Any of (OR)",    "Match any of several values",         "value1|value2", "value1|value2"),
    ("Not containing", "Exclude values that contain a term",  "^(?!.*value)",  "value"),
    ("Contains word",  "Match a whole word boundary",         "\\bvalue\\b",   "value"),
    ("Contains all (AND)", "Match results containing ALL terms", "(?=.*word1)(?=.*word2)", "word1"),
]


# ──────────────────────────────────────────────────────────────────
# Benchmark helpers (shared between tools/phase0_benchmark.py and the
# in-window Phase 0 benchmark UI). Single source of truth for what
# "the regex search" and "the fuzzy search" do, so the live UI numbers
# always match the offline CLI numbers.
# ──────────────────────────────────────────────────────────────────

def build_searchable(ds, lang="de"):
    """Build the searchable haystack for one dataset.

    Accepts EITHER a dict (offline tools using ds_cache_v2_*.json) OR a
    MaterialDataset instance (live Revit UI).

    Phase 0 v3 (2026-05):
      * Trimmed from 10 fields to 2 (name + classification). The dropped
        fields (uuid, location, valid_until, dataset_type, owner, compliance,
        program_operator, epd_no) carry no match signal for material-name
        queries - they only inflate haystack length, which compresses
        scoring and lets random metadata noise leak into matches. The
        tooltip in the Validation pushbutton now honestly says
        "name + classification".
      * Added the `lang` parameter. When lang=="en", the classification
        path is translated segment-by-segment via classification_labels
        before being joined into the haystack - so an English query like
        "windows" matches a dataset whose German classification is
        "Komponenten von Fenstern und Vorhangfassaden / …" via its English
        equivalent "Windows & Façades / …". Search and display stay
        consistent with the language toggle.

    Note on benchmark trade-off (recorded here to avoid an accidental
    re-revert): trimming reduced Fuzzy Recall@10 from 33% to ~28% on the
    UK Sample Project benchmark (Glass moved from rank 9 to rank 11). This
    drop was accepted as the price of a transparent, noise-free
    search. The "lost" Glass hit is in any case a synonym gap ("Glass" →
    "Insulated glazing") that deterministic search cannot honestly fix.

    Live Connector grid (window._apply_grid_filters) does NOT use this
    function and is unaffected. SmartMatch has its own copy.
    """
    if isinstance(ds, dict):
        name           = ds.get("name", "") or ""
        classification = ds.get("classification", "") or ""
    else:
        name           = getattr(ds, "name", "") or ""
        classification = getattr(ds, "Classification", "") or ""
    if lang == "en" and classification:
        # Lazy import to avoid a circular dependency if anyone ever
        # decides classification_labels should import from search_helpers.
        from classification_labels import translate_path
        classification = translate_path(classification, "en")
    # Phase 0 v6: return a structured pair so `_fuzzy_score` can score
    # name and classification with different weights. `_fuzzy_match_inner`
    # joins them back into one string for the binary filter (filtering
    # doesn't need field separation; only ranking does).
    return {
        "name":           (name or "").lower(),
        "classification": (classification or "").lower(),
    }


def _get_uuid(ds):
    """Read a dataset's UUID whether it's a dict or a MaterialDataset."""
    if isinstance(ds, dict):
        return ds.get("uuid", "")
    return getattr(ds, "uuid", "")


def search_regex(query, datasets, searchables=None):
    """Run the regex match (case-insensitive) against each dataset and
    return survivors sorted by fuzzy_score (descending).

    Mirrors the live behavior of window._apply_grid_filters in `regex` mode:
    a binary filter, then ranking the survivors by the same fuzzy_score the
    live search uses so the K-th match is well-defined.

    Args:
        query: User-typed search string (treated as regex pattern).
        datasets: Iterable of dataset dicts or MaterialDataset instances.
        searchables: Optional precomputed list of searchable strings,
            one per dataset, in matching order. Pass this when calling in
            a loop over many queries to skip rebuilding the searchable
            string for every query (typical Phase 0 benchmark speed-up).

    Returns:
        List of (ds, score) tuples, sorted by score descending.
    """
    nl = (query or "").lower()
    matched = []
    if searchables is None:
        for ds in datasets:
            hay = build_searchable(ds)                # v6: returns dict
            flat = _haystack_string(hay)               # flat string for regex test
            if _regex_match(query, flat):
                matched.append((ds, _fuzzy_score(nl, hay)))
    else:
        for ds, hay in zip(datasets, searchables):
            flat = _haystack_string(hay)
            if _regex_match(query, flat):
                matched.append((ds, _fuzzy_score(nl, hay)))
    matched.sort(key=lambda t: t[1], reverse=True)
    return matched


def search_fuzzy(query, datasets, searchables=None):
    """Run the Levenshtein-based fuzzy match against each dataset and
    return survivors sorted by fuzzy_score (descending).

    Mirrors the live behavior of window._apply_grid_filters in `fuzzy` mode.

    Args:
        query: User-typed search string.
        datasets: Iterable of dataset dicts or MaterialDataset instances.
        searchables: Optional precomputed lowercased searchable strings.

    Returns:
        List of (ds, score) tuples, sorted by score descending.
    """
    nl     = (query or "").lower()
    tokens = _tokenize(nl)
    matched = []
    if searchables is None:
        for ds in datasets:
            hay = build_searchable(ds)            # v6: dict
            if _fuzzy_match_inner(nl, tokens, hay):
                matched.append((ds, _fuzzy_score(nl, hay)))
    else:
        for ds, hay in zip(datasets, searchables):
            # Searchables already lower-cased by build_searchable.
            # _fuzzy_match_inner handles dict-or-string.
            if _fuzzy_match_inner(nl, tokens, hay):
                matched.append((ds, _fuzzy_score(nl, hay)))
    matched.sort(key=lambda t: t[1], reverse=True)
    return matched


def topk_hit(ranked, accept_uuids, k):
    """True if any UUID in accept_uuids appears in the top-k of ranked."""
    acc = set(accept_uuids or [])
    for ds, _sc in ranked[:k]:
        if _get_uuid(ds) in acc:
            return True
    return False


def rank_of_first_hit(ranked, accept_uuids):
    """1-based rank of the first acceptable hit; len(ranked)+1 if none."""
    acc = set(accept_uuids or [])
    for i, (ds, _sc) in enumerate(ranked):
        if _get_uuid(ds) in acc:
            return i + 1
    return len(ranked) + 1


# ═══════════════════════════════════════════════════════════════════
# Phase 0 v7 (2026-05) - BM25F retrieval (the principled replacement
# for `_fuzzy_score`'s ad-hoc max() composite).
#
# Replaces the empirical constants 0.75 / 0.65 / 0.60 / 0.40 / 0.85 /
# 0.10 / CLASS_WEIGHT=0.5 / FIELD_NORM_K=25 with a citable formula:
#
#   * BM25     Robertson & Spärck-Jones 1976; Robertson et al. 2009
#              ("The Probabilistic Relevance Framework: BM25 and Beyond")
#   * BM25F    Zaragoza, Craswell & Taylor 2004
#              ("Microsoft Cambridge at TREC-13: Web and Hard Tracks")
#   * Typo-tolerant query expansion via bounded Levenshtein edit distance
#              Manning, Raghavan & Schütze 2008 §9 (IIR textbook)
#   * Isotonic calibration of raw scores
#              Niculescu-Mizil & Caruana 2005
#
# Hyperparameters (only four named, all tunable via tools/tune_bm25.py):
#   k1        TF saturation                      default 1.5
#   b_name    name-field length-norm aggression  default 0.75
#   b_class   class-field length-norm aggression default 0.75
#   w_name    field weight (discriminator)       default 1.0
#   w_class   field weight (corroborating)       default 0.5
#   alpha_3g  trigram BM25 blend weight          default 0.0
#
# All other constants in this section are derivable from these and the
# corpus statistics (CorpusStats), not hand-picked.
# ═══════════════════════════════════════════════════════════════════

# Default BM25 hyperparameters. k1 in the [1.2, 2.0] Anserini/Lucene
# convention; b in [0, 1]. Refine via tools/tune_bm25.py on a held-out
# split of the ground truth, then write to bm25_params_v1.json.
BM25_K1        = 1.5
BM25_B_NAME    = 0.75
BM25_B_CLASS   = 0.75
BM25_W_NAME    = 1.0
BM25_W_CLASS   = 0.5
BM25_ALPHA_3G  = 0.0

# IDF for out-of-vocabulary query tokens (e.g. typos with no nearby
# vocab match). Conservative: low enough that an OOV term can't dominate
# the score, high enough that a single-OOV query against a perfectly
# matching document still produces a non-zero ranking signal.
_BM25_OOV_IDF  = 1.5


def _trigrams(s):
    """Multiset (list) of character 3-grams. Order-preserving. Empty for
    strings shorter than 3 chars."""
    if not s or len(s) < 3:
        return []
    return [s[i:i + 3] for i in range(len(s) - 2)]


class CorpusStats(object):
    """Pre-computed inverted-index statistics for BM25F ranking.

    Built once per dataset-list reload via `CorpusStats.build(searchables)`.
    O(N · avg_tokens) one-time work, then read-only.

    Holds:
      N            -- number of documents
      avgdl_name   -- average name-field token count
      avgdl_class  -- average classification-field token count
      df[t]        -- document frequency (# docs where t appears in any field)
      idf[t]       -- log((N - df + 0.5) / (df + 0.5) + 1)
      vocab        -- set of all observed tokens (for typo expansion)
      vocab_by_len -- {token_length: [tokens]} bucket index for fast typo
                      expansion (avoid scanning the whole vocab each query)
      avgdl_3g_*   -- as above but over character 3-grams (Layer 3)
      df_3g[g]     -- as above for trigrams
      idf_3g[g]    -- as above for trigrams
    """

    def __init__(self):
        self.N           = 0
        self.avgdl_name  = 1.0
        self.avgdl_class = 1.0
        self.df          = {}
        self.idf         = {}
        self.vocab       = set()
        self.vocab_by_len = {}
        self.avgdl_3g_name  = 1.0
        self.avgdl_3g_class = 1.0
        self.df_3g          = {}
        self.idf_3g         = {}

    @staticmethod
    def _doc_postings(name_lower, class_lower):
        return (_tokenize(name_lower),
                _tokenize(class_lower),
                _trigrams(name_lower),
                _trigrams(class_lower))

    @classmethod
    def build(cls, searchables):
        """Build stats from an iterable of `searchable` dicts produced by
        `build_searchable`. Plain strings are accepted (treated as
        name-only) for backwards compatibility."""
        stats = cls()
        N = 0
        sum_name_len = 0
        sum_class_len = 0
        sum_3g_name = 0
        sum_3g_class = 0
        df_acc    = defaultdict(int)
        df_3g_acc = defaultdict(int)
        for hay in searchables:
            if isinstance(hay, dict):
                name_lower  = hay.get("name", "") or ""
                class_lower = hay.get("classification", "") or ""
            else:
                name_lower, class_lower = (hay or "").lower(), ""
            n_toks, c_toks, n_3g, c_3g = cls._doc_postings(
                name_lower, class_lower)
            sum_name_len  += len(n_toks)
            sum_class_len += len(c_toks)
            sum_3g_name   += len(n_3g)
            sum_3g_class  += len(c_3g)
            unique_toks = set(n_toks) | set(c_toks)
            for t in unique_toks:
                df_acc[t] += 1
            unique_3g = set(n_3g) | set(c_3g)
            for g in unique_3g:
                df_3g_acc[g] += 1
            N += 1
        stats.N           = N
        stats.avgdl_name  = (sum_name_len / float(N)) if N else 1.0
        stats.avgdl_class = (sum_class_len / float(N)) if N else 1.0
        stats.avgdl_3g_name  = (sum_3g_name / float(N)) if N else 1.0
        stats.avgdl_3g_class = (sum_3g_class / float(N)) if N else 1.0
        stats.df    = dict(df_acc)
        stats.df_3g = dict(df_3g_acc)
        N_f = float(N if N > 0 else 1)
        stats.idf    = dict((t, math.log((N_f - df + 0.5) / (df + 0.5) + 1.0))
                            for t, df in stats.df.items())
        stats.idf_3g = dict((g, math.log((N_f - df + 0.5) / (df + 0.5) + 1.0))
                            for g, df in stats.df_3g.items())
        stats.vocab = set(stats.df.keys())
        buckets = defaultdict(list)
        for t in stats.vocab:
            buckets[len(t)].append(t)
        stats.vocab_by_len = dict(buckets)
        return stats

    def to_dict(self):
        """JSON-serialisable snapshot. vocab_by_len + idf tables are
        recomputed on load (cheap; df dominates storage)."""
        return {
            "_schema":        "corpus_stats_v1",
            "N":              self.N,
            "avgdl_name":     self.avgdl_name,
            "avgdl_class":    self.avgdl_class,
            "avgdl_3g_name":  self.avgdl_3g_name,
            "avgdl_3g_class": self.avgdl_3g_class,
            "df":             self.df,
            "df_3g":          self.df_3g,
        }

    @classmethod
    def from_dict(cls, d):
        stats = cls()
        stats.N           = d.get("N", 0)
        stats.avgdl_name  = d.get("avgdl_name", 1.0)
        stats.avgdl_class = d.get("avgdl_class", 1.0)
        stats.avgdl_3g_name  = d.get("avgdl_3g_name", 1.0)
        stats.avgdl_3g_class = d.get("avgdl_3g_class", 1.0)
        stats.df    = d.get("df", {})
        stats.df_3g = d.get("df_3g", {})
        N_f = float(stats.N if stats.N > 0 else 1)
        stats.idf    = dict((t, math.log((N_f - df + 0.5) / (df + 0.5) + 1.0))
                            for t, df in stats.df.items())
        stats.idf_3g = dict((g, math.log((N_f - df + 0.5) / (df + 0.5) + 1.0))
                            for g, df in stats.df_3g.items())
        stats.vocab = set(stats.df.keys())
        buckets = defaultdict(list)
        for t in stats.vocab:
            buckets[len(t)].append(t)
        stats.vocab_by_len = dict(buckets)
        return stats


def _bm25f_params(overrides):
    """Resolve params dict against the module defaults."""
    p = {
        "k1":       BM25_K1,
        "b_name":   BM25_B_NAME,
        "b_class":  BM25_B_CLASS,
        "w_name":   BM25_W_NAME,
        "w_class":  BM25_W_CLASS,
        "alpha_3g": BM25_ALPHA_3G,
    }
    if overrides:
        for k, v in overrides.items():
            if k in p:
                p[k] = v
    return p


def _field_tf(field_lower):
    """Token tf dict for one field; returns (tf_dict, total_tokens)."""
    counts = defaultdict(int)
    for t in _tokenize(field_lower):
        counts[t] += 1
    return dict(counts), sum(counts.values())


def _field_tf_3g(field_lower):
    """3-gram tf dict for one field; returns (tf_dict, total_3grams)."""
    counts = defaultdict(int)
    for g in _trigrams(field_lower):
        counts[g] += 1
    return dict(counts), sum(counts.values())


def _bm25f_term_contrib(w_name, tf_name, dl_name,
                        w_class, tf_class, dl_class,
                        idf, k1, b_name, b_class,
                        avgdl_name, avgdl_class):
    """One BM25F term contribution.

    Per-field length-normalised tf is computed first, weighted, then
    summed into a virtual TF; the k1 saturation is applied once over
    the summed TF (BM25F, Zaragoza et al. 2004 - *not* per-field BM25
    summed afterwards, which would double-saturate)."""
    if idf <= 0.0:
        return 0.0
    if avgdl_name > 0:
        norm_n = tf_name / ((1.0 - b_name) + b_name * dl_name / avgdl_name)
    else:
        norm_n = 0.0
    if avgdl_class > 0:
        norm_c = tf_class / ((1.0 - b_class) + b_class * dl_class / avgdl_class)
    else:
        norm_c = 0.0
    combined = w_name * norm_n + w_class * norm_c
    if combined <= 0.0:
        return 0.0
    return idf * combined / (k1 + combined)


def expand_query_with_typos(query_lower, stats, max_d_fn=None):
    """For each token in *query_lower*, return the original plus every
    vocabulary token within bounded Levenshtein edit distance,
    weighted 1 / (1 + d). Duplicates are deduped by keeping the max
    weight. The original query tokens are always included with weight
    1.0 even when out-of-vocabulary.

    Args:
        query_lower: lower-case query string.
        stats:       CorpusStats (uses vocab_by_len for fast bucketing).
        max_d_fn:    token-length -> max edit distance.
                     Defaults to the module `_max_distance` ladder.

    Returns:
        List of (token, weight) tuples.
    """
    expanded, _origins = _expand_with_origin(query_lower, stats, max_d_fn)
    return expanded


def _expand_with_origin(query_lower, stats, max_d_fn=None):
    """Same as `expand_query_with_typos` but also returns
    {expanded_token: (origin_query_token, distance)} for `explain_score`."""
    if max_d_fn is None:
        max_d_fn = _max_distance
    tokens = _tokenize(query_lower)
    if not tokens:
        return [], {}
    out = {}    # token -> (weight, origin_token, distance)
    for qt in tokens:
        prev = out.get(qt)
        if prev is None or prev[0] < 1.0:
            out[qt] = (1.0, qt, 0)
        D = max_d_fn(len(qt))
        if D <= 0 or not stats.vocab_by_len:
            continue
        # Only inspect vocab tokens whose length is within ±D of qt; the
        # Levenshtein distance lower bound is |len(a) - len(b)|, so
        # anything outside this band cannot match.
        for L in range(len(qt) - D, len(qt) + D + 1):
            for v in stats.vocab_by_len.get(L, ()):
                if v == qt:
                    continue
                d = _levenshtein(qt, v, D)
                if d <= D:
                    w = 1.0 / (1.0 + d)
                    prev = out.get(v)
                    if prev is None or prev[0] < w:
                        out[v] = (w, qt, d)
    expanded = [(t, info[0]) for t, info in out.items()]
    origins  = dict((t, (info[1], info[2])) for t, info in out.items())
    return expanded, origins


def bm25f_score(query_lower, searchable, stats, params=None,
                expanded_query=None):
    """Compute raw BM25F score for one query against one document.

    Args:
        query_lower:    lower-case query string.
        searchable:     dict {"name":..., "classification":...} (lower-case).
        stats:          CorpusStats built from the same corpus.
        params:         dict overriding BM25_* defaults (optional).
        expanded_query: precomputed [(token, weight)] from
                        `expand_query_with_typos`. When None, uses the
                        plain tokenisation (no typo tolerance - typically
                        the live UI computes this once per query and
                        reuses it across all datasets).

    Returns:
        Raw BM25F score (float, unbounded). Use IsotonicCalibrator.apply
        for a calibrated probability in [0, 1].
    """
    if not query_lower:
        return 0.0
    if not isinstance(searchable, dict):
        searchable = {"name": (searchable or "").lower(),
                      "classification": ""}
    p = _bm25f_params(params)
    name_lower  = searchable.get("name", "") or ""
    class_lower = searchable.get("classification", "") or ""
    tf_n, dl_n  = _field_tf(name_lower)
    tf_c, dl_c  = _field_tf(class_lower)
    if expanded_query is None:
        expanded_query = [(t, 1.0) for t in _tokenize(query_lower)]
    if not expanded_query:
        return 0.0
    total = 0.0
    for (t, w) in expanded_query:
        idf = stats.idf.get(t)
        if idf is None or idf <= 0.0:
            idf = _BM25_OOV_IDF
        contrib = _bm25f_term_contrib(
            p["w_name"],  tf_n.get(t, 0), dl_n,
            p["w_class"], tf_c.get(t, 0), dl_c,
            idf, p["k1"], p["b_name"], p["b_class"],
            stats.avgdl_name, stats.avgdl_class,
        )
        total += w * contrib
    if p["alpha_3g"] > 0.0:
        total += p["alpha_3g"] * _bm25f_3gram_score(
            query_lower, name_lower, class_lower, stats, p)
    return total


def _bm25f_3gram_score(query_lower, name_lower, class_lower, stats, p):
    """Character-3-gram BM25 over the same two fields. Same BM25F formula,
    different vocabulary. Useful for German Komposita where word-level
    tokenisation cannot reach inside the compound (e.g. 'Stahlbeton' vs
    query 'Beton')."""
    q_3g = _trigrams(query_lower)
    if not q_3g:
        return 0.0
    tf_n, dl_n = _field_tf_3g(name_lower)
    tf_c, dl_c = _field_tf_3g(class_lower)
    total = 0.0
    counted = set()
    for g in q_3g:
        if g in counted:
            continue
        counted.add(g)
        idf = stats.idf_3g.get(g)
        if idf is None or idf <= 0.0:
            idf = _BM25_OOV_IDF
        contrib = _bm25f_term_contrib(
            p["w_name"],  tf_n.get(g, 0), dl_n,
            p["w_class"], tf_c.get(g, 0), dl_c,
            idf, p["k1"], p["b_name"], p["b_class"],
            stats.avgdl_3g_name, stats.avgdl_3g_class,
        )
        total += contrib
    return total


class IsotonicCalibrator(object):
    """Piecewise-linear monotone mapping raw BM25F score -> P(correct).

    Built offline by `tools/calibrate_score.py` from the ground-truth set
    (every query × dataset pair is a (raw_score, is_correct) point;
    isotonic regression on these produces a step function - see
    Niculescu-Mizil & Caruana 2005). We store the step function as a
    sorted list of (score, probability) tuples and linearly interpolate
    between adjacent knots at apply time.

    When unavailable, callers should fall back to `_soft_bound(raw, k1)`
    so the displayed score still lies in [0, 1] without misrepresenting
    itself as calibrated. The fallback is also produced by
    `explain_score` and labelled in the tooltip as "raw (no calibrator)
    - soft-bounded".
    """

    def __init__(self, knots):
        self.knots = sorted(list(knots or []), key=lambda kv: kv[0])

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("knots", []) if d else [])

    def to_dict(self):
        return {"_schema": "isotonic_v1", "knots": list(self.knots)}

    def apply(self, raw):
        ks = self.knots
        if not ks:
            return 0.0
        if raw <= ks[0][0]:
            return ks[0][1]
        if raw >= ks[-1][0]:
            return ks[-1][1]
        lo, hi = 0, len(ks) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ks[mid][0] <= raw:
                lo = mid
            else:
                hi = mid
        x0, y0 = ks[lo]
        x1, y1 = ks[hi]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)


def soft_bound(raw, k1=None):
    """Smooth, monotone bound of a raw BM25F score into [0, 1].
    `1 - exp(-raw / k1)` - doubling the raw score halves the residual gap
    to 1.0. Kept for backwards compatibility; new code should use
    `ConfidenceCalibrator(mode='bm25f').apply(raw)` instead - same shape,
    mode-agnostic interface."""
    if k1 is None:
        k1 = BM25_K1
    if raw <= 0.0:
        return 0.0
    return 1.0 - math.exp(-raw / max(k1, 0.001))


# ═══════════════════════════════════════════════════════════════════
# Phase 0 → 1 bridge - mode-agnostic confidence layer
#
# Each search mode produces raw scores in a different scale:
#   * BM25F     (regex + fuzzy modes today)        raw in ~[0, 5+]
#   * Cosine    (Phase 1 semantic via bge-m3)      raw in ~[0.2, 0.8]
#   * Fusion    (Phase 1+ hybrid)                  raw depends on combiner
#
# `ConfidenceCalibrator` translates each mode's raw to a [0, 1] confidence
# that means the same thing across modes ("0.7 = likely match" regardless of
# whether the search was BM25F or cosine). The live UI and the Min Score
# filter operate on confidence, not raw.
#
# Primary path  - isotonic calibrator from `tools/fit_confidence.py`
#                 (balanced negative subsampling, so probabilities are not
#                 squashed by the population base rate).
# Fallback path - per-mode sigmoid with hand-picked (midpoint, spread).
#
# Each (midpoint, spread) is *one* named parameter pair per mode, with a
# published shape (logistic), and is replaceable by the isotonic fit the
# moment ground truth is available.
# ═══════════════════════════════════════════════════════════════════

# Default sigmoid parameters per mode. Derived from inspection of the
# rank-1 raw-score distribution in the current Phase 0 benchmark
# (bm25f) and from published bge-m3 cosine ranges for relevant pairs
# (semantic). Tuned so confidence 0.5 marks the "ambiguous" boundary.
CONFIDENCE_DEFAULTS = {
    "bm25f":    (1.0, 1.0),    # raw 1.0 → 0.50; raw 3.0 → 0.88; raw 5.0 → 0.98
    "semantic": (0.5, 0.08),   # cos 0.50 → 0.50; cos 0.60 → 0.78; cos 0.70 → 0.92
    # Phase 1+ - Hybrid (RRF) mode. The raw signal is normalised RRF in
    # [0, 1] (rank-1 in both rankers → 1.0; absent from both → 0.0).
    # The sigmoid below maps:
    #   norm 0.20 → 9 %    (rank ~50 in one, absent in other)
    #   norm 0.40 → 50 %   (e.g. rank-3 in both, or rank-1 + rank-15)
    #   norm 0.60 → 91 %   (rank-1 + rank-3 territory)
    #   norm 0.80 → 99 %
    # so the live UI's Score % matches user intuition: both rankers
    # agreeing on the same dataset reads as a high-confidence hit.
    "hybrid":   (0.4, 0.10),
    # Phase 1+ - Cross-encoder reranker (BAAI/bge-reranker-v2-m3).
    # Raw signal is the model's logit. Empirically (verified on a
    # cross-lingual construction-material smoke test) bge-reranker-v2-m3
    # outputs a tighter range than typical MSMARCO cross-encoders:
    # ~0 for unrelated pairs, ~+0.4 to +0.7 for clearly-relevant pairs,
    # ~+1 to +2 for paraphrase-level matches. Sigmoid (0.3, 0.3) maps:
    #   logit  0.0 → 27 %    (irrelevant / no signal)
    #   logit +0.3 → 50 %    (borderline)
    #   logit +0.5 → 66 %    (relevant)
    #   logit +0.7 → 79 %    (clearly relevant)
    #   logit +1.0 → 91 %    (paraphrase / synonym)
    "reranker": (0.3, 0.3),
}


# Global epistemic cap on every displayed confidence, applied in
# `ConfidenceCalibrator.apply` regardless of whether the underlying
# path is sigmoid or isotonic.
#
# Rationale: no retrieval calibrator should ever claim 100% certainty.
# Even a perfectly fit isotonic curve with a 100%-positive top bin
# reflects the limit of the labelled sample, not ground epistemic truth.
# A hard cap at 0.97 leaves visible headroom for "100% minus residual
# calibration error" while preventing the display from saturating.
#
# This is the *only* hand-set knob in the calibration system. It is a
# global constant, not a per-mode hierarchy - the cross-mode
# differentiation must come from the empirically-fit isotonic curves,
# not from per-mode ceilings (which would amount to engineering the
# thesis hierarchy into the calibrator rather than discovering it from
# the data).
MAX_CONFIDENCE = 0.97


def _sigmoid(x):
    """Numerically-stable logistic sigmoid 1 / (1 + exp(-x))."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _isotonic_is_degenerate(knots, max_plateau_frac=0.40,
                            mid_lo=0.10, mid_hi=0.90):
    """True when an isotonic curve has collapsed into a low-resolution fit
    that cannot discriminate across its operating range.

    Signature of degeneracy: a wide flat plateau at a *non-extreme*
    probability. Plateaus at the saturation extremes (p approx 0 for clear
    non-matches, p approx 1 for near-certain matches) are legitimate and
    are NOT flagged; only a wide plateau at a mid-range probability is,
    because it means many distinct raw scores all map to the same
    confidence -- the curve has failed to learn a usable mapping
    (typically from too little or too compressed calibration data).

    Returns True if the widest contiguous run of equal probability whose
    value lies in [mid_lo, mid_hi] spans more than `max_plateau_frac` of
    the knot x-range. `ConfidenceCalibrator.from_dict` uses this to fall
    back to the parametric sigmoid default -- a single uniform policy
    applied to every mode (Niculescu-Mizil & Caruana 2005: parametric
    calibration is preferred when data is too sparse for a reliable
    non-parametric isotonic fit).

    Empirical separation on the shipped a2_sphera_en calibrators:
      bm25f / hybrid / semantic -- widest plateau at p~0 or p~1 -> keep
      reranker                  -- plateau p=0.333 over 54% of range -> reject
    """
    ks = sorted(list(knots or []), key=lambda kv: kv[0])
    if len(ks) < 2:
        return False
    xs = [kv[0] for kv in ks]
    x_range = xs[-1] - xs[0]
    if x_range <= 0:
        return False
    worst = 0.0
    run_p = ks[0][1]
    run_start = xs[0]
    run_end = xs[0]
    for j in range(1, len(ks)):
        if abs(ks[j][1] - run_p) < 1e-9:
            run_end = xs[j]
        else:
            if mid_lo <= run_p <= mid_hi:
                frac = (run_end - run_start) / float(x_range)
                if frac > worst:
                    worst = frac
            run_p = ks[j][1]
            run_start = xs[j]
            run_end = xs[j]
    if mid_lo <= run_p <= mid_hi:
        frac = (run_end - run_start) / float(x_range)
        if frac > worst:
            worst = frac
    return worst > max_plateau_frac


class ConfidenceCalibrator(object):
    """Per-mode mapping raw_score → confidence ∈ [0, 1].

    The displayed `Score %` and the `Min Score` filter both operate on
    `apply(raw)`. Same interface for every mode (`bm25f`, `semantic`,
    `hybrid`), so the live UI doesn't change when a new search mode
    lands; only its CalibrationCalibrator instance does.

    Construction modes:
      * `ConfidenceCalibrator()` - `bm25f` sigmoid default.
      * `ConfidenceCalibrator(mode='semantic')` - `semantic` sigmoid default.
      * `ConfidenceCalibrator(mode='X', sigmoid=(m, s))` - custom sigmoid.
      * `ConfidenceCalibrator.from_dict(mode, json_dict)` - load isotonic
        fit produced by `tools/fit_confidence.py`. Isotonic, when present,
        overrides the sigmoid fallback. The JSON may also carry a custom
        sigmoid for use if the isotonic is later invalidated.
    """

    def __init__(self, mode="bm25f", sigmoid=None, isotonic=None):
        self.mode = mode
        if sigmoid is None:
            sigmoid = CONFIDENCE_DEFAULTS.get(mode, (1.0, 1.0))
        self.midpoint, self.spread = sigmoid
        if self.spread <= 0.0:
            self.spread = 1.0
        self.isotonic = isotonic
        # True when from_dict() received an isotonic but rejected it as
        # degenerate and fell back to this sigmoid (surfaced in source()).
        self._isotonic_rejected = False

    @classmethod
    def from_dict(cls, mode, d):
        """Build from a JSON dict (callers handle their own file I/O).
        When `d` is None or has no isotonic knots, returns the sigmoid
        default for `mode`. Recognises a `sigmoid: [midpoint, spread]`
        key for callers that want to ship a non-default sigmoid alongside
        the isotonic curve."""
        if not d:
            return cls(mode=mode)
        sig = d.get("sigmoid") if isinstance(d, dict) else None
        iso = None
        iso_rejected = False
        if isinstance(d, dict) and d.get("knots"):
            if _isotonic_is_degenerate(d.get("knots")):
                # Low-resolution isotonic (a wide mid-range plateau from
                # sparse / compressed calibration data) cannot discriminate
                # and would display a flat, uninformative confidence. Fall
                # back to the parametric sigmoid default. Uniform policy
                # across all modes; in practice only the reranker (the
                # sparsest calibration set) trips it today.
                iso_rejected = True
            else:
                iso = IsotonicCalibrator.from_dict(d)
        if sig:
            cal = cls(mode=mode, sigmoid=tuple(sig), isotonic=iso)
        else:
            cal = cls(mode=mode, isotonic=iso)
        cal._isotonic_rejected = iso_rejected
        return cal

    def apply(self, raw):
        """Return confidence in [0, MAX_CONFIDENCE]. Isotonic when present,
        else sigmoid. The global `MAX_CONFIDENCE` cap (0.97) is enforced on
        both paths so no calibrator ever displays 100% - see the comment
        above the constant for the rationale."""
        if self.isotonic is not None and self.isotonic.knots:
            result = self.isotonic.apply(raw)
        else:
            result = _sigmoid((raw - self.midpoint) / self.spread)
        if result > MAX_CONFIDENCE:
            return MAX_CONFIDENCE
        return result

    def source(self):
        """Short label describing which path produced the score (for tooltips)."""
        if self.isotonic is not None and self.isotonic.knots:
            return "isotonic (mode={}, {} knots, cap={})".format(
                self.mode, len(self.isotonic.knots), MAX_CONFIDENCE)
        label = "sigmoid (mode={}, midpoint={}, spread={}, cap={})".format(
            self.mode, self.midpoint, self.spread, MAX_CONFIDENCE)
        if getattr(self, "_isotonic_rejected", False):
            label += " [isotonic rejected: low-resolution fit]"
        return label


def explain_score(query_lower, searchable, stats, params=None,
                  calibrator=None, expanded_query=None, confidence=None):
    """Return a dict describing how the BM25F score for one (query, doc)
    pair was computed. Single source of truth for the live-grid Score%
    tooltip *and* the Validation pushbutton tooltip - both must call this,
    never re-implement the formula.

    Returns:
        dict with keys:
          final          calibrated probability (or soft-bounded fallback)
          raw            raw BM25F score
          per_term       list of {term, weight, idf, tf_name, tf_class,
                                  contrib, via_typo_of?, distance?}
          trigram_contrib float
          params         the params dict actually used
          calibration    "isotonic from N knots" or "raw (soft-bounded)"
    """
    if not query_lower:
        return {"final": 1.0, "raw": 0.0, "per_term": [],
                "trigram_contrib": 0.0,
                "params": _bm25f_params(params),
                "calibration": "empty query"}
    if not isinstance(searchable, dict):
        searchable = {"name": (searchable or "").lower(),
                      "classification": ""}
    p = _bm25f_params(params)
    name_lower  = searchable.get("name", "") or ""
    class_lower = searchable.get("classification", "") or ""
    tf_n, dl_n  = _field_tf(name_lower)
    tf_c, dl_c  = _field_tf(class_lower)
    if expanded_query is None:
        expanded_query, origins = _expand_with_origin(query_lower, stats)
    else:
        origins = {}
    per_term = []
    raw_total = 0.0
    for (t, w) in expanded_query:
        idf = stats.idf.get(t)
        if idf is None or idf <= 0.0:
            idf = _BM25_OOV_IDF
        contrib = _bm25f_term_contrib(
            p["w_name"],  tf_n.get(t, 0), dl_n,
            p["w_class"], tf_c.get(t, 0), dl_c,
            idf, p["k1"], p["b_name"], p["b_class"],
            stats.avgdl_name, stats.avgdl_class,
        )
        weighted = w * contrib
        raw_total += weighted
        info = {
            "term":     t,
            "weight":   round(w, 3),
            "idf":      round(idf, 3),
            "tf_name":  tf_n.get(t, 0),
            "tf_class": tf_c.get(t, 0),
            "contrib":  round(weighted, 4),
        }
        origin = origins.get(t)
        if origin and origin[0] != t:
            info["via_typo_of"] = origin[0]
            info["distance"]    = origin[1]
        per_term.append(info)
    trig = 0.0
    if p["alpha_3g"] > 0.0:
        trig = p["alpha_3g"] * _bm25f_3gram_score(
            query_lower, name_lower, class_lower, stats, p)
        raw_total += trig
    # `final` is the mode-agnostic confidence produced by `ConfidenceCalibrator`
    # - the same number that drives the live UI Score% column and the Min Score
    # filter. This makes Score% comparable across modes (BM25F today, cosine in
    # Phase 1). The optional unbalanced isotonic calibrator (`calibrator`) is
    # kept separate as a Phase 3 thresholding artifact and surfaced only in the
    # tooltip with its population-frequency caveat.
    confidence_calibrator = (confidence
                             if confidence is not None
                             else ConfidenceCalibrator(mode="bm25f"))
    final = confidence_calibrator.apply(raw_total)
    if calibrator is not None and calibrator.knots:
        final_calibrated = calibrator.apply(raw_total)
        cal_note = ("{}; legacy P(correct|filter pass) calibrator "
                    "available ({} knots, thresholding-only)").format(
                        confidence_calibrator.source(), len(calibrator.knots))
    else:
        final_calibrated = None
        cal_note = confidence_calibrator.source()
    return {
        "final":            final,
        "final_calibrated": final_calibrated,
        "raw":              raw_total,
        "per_term":         per_term,
        "trigram_contrib":  trig,
        "params":           p,
        "calibration":      cal_note,
    }


def search_bm25f(query, datasets, stats, params=None, searchables=None,
                 calibrator=None):
    """Run BM25F ranking. Binary candidate set is the same as
    `search_fuzzy` (so changing only the ranker, not the filter, lets
    the benchmark isolate the *ranking* contribution of BM25F).

    Args:
        query, datasets, searchables: as `search_fuzzy`.
        stats:      CorpusStats built from `searchables` (mandatory).
        params:     override dict for BM25 hyperparameters.
        calibrator: optional IsotonicCalibrator; when present, returned
                    scores are calibrated probabilities, else raw BM25F.

    Returns:
        List of (ds, score) sorted by score descending.
    """
    nl = (query or "").lower()
    tokens = _tokenize(nl)
    if searchables is None:
        searchables = [build_searchable(ds) for ds in datasets]
    if not nl:
        return [(ds, 0.0) for ds in datasets]
    expanded = expand_query_with_typos(nl, stats)
    out = []
    for ds, hay in zip(datasets, searchables):
        if not _fuzzy_match_inner(nl, tokens, hay):
            continue
        raw = bm25f_score(nl, hay, stats, params=params,
                          expanded_query=expanded)
        if calibrator is not None and calibrator.knots:
            out.append((ds, calibrator.apply(raw)))
        else:
            out.append((ds, raw))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


# ═══════════════════════════════════════════════════════════════════
# Phase 1 - dense retrieval primitives
# ═══════════════════════════════════════════════════════════════════
# Architecture:
#   * `EmbeddingIndex`        in-memory {uuid: vector} loaded from the
#                             .bin sidecar produced by
#                             `embedding_prefetcher.py`. Pure Python -
#                             no native deps, runs in IronPython 2.7.
#   * `OllamaEmbeddingClient` HTTP client for `/api/embed` against a
#                             local Ollama server (default
#                             `http://localhost:11434`). Picks
#                             `System.Net.WebClient` under IronPython
#                             and `urllib.request` under CPython 3.
#   * `search_semantic`       search-mode entry point; mirrors the
#                             `search_fuzzy` / `search_bm25f`
#                             signature for drop-in dispatching by
#                             name in the live UI, the Validation
#                             pushbutton, and `phase0_benchmark.py`.
#
# Sidecar format (consumed by IronPython, written by both IronPython
# and the CPython prefetcher):
#     4 bytes  magic                              = b"EMB1"
#     4 bytes  meta_len   (little-endian uint32)
#     N bytes  meta_json  (UTF-8)
#     count * dim * 4 bytes  float32 row-major (L2-normalised)
# ═══════════════════════════════════════════════════════════════════

EMBEDDING_BIN_MAGIC = b"EMB1"


def cosine_similarity(a, b):
    """Dot product of two equal-length sequences of floats. For
    L2-normalised vectors this equals cosine similarity. Pure Python."""
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


def _l2_normalize(v):
    """Return a new list with `v` scaled to unit L2 norm. If `v` is
    already zero, returns it unchanged."""
    s = 0.0
    for x in v:
        s += x * x
    if s <= 0.0:
        return list(v)
    norm = math.sqrt(s)
    return [x / norm for x in v]


class EmbeddingIndex(object):
    """In-memory {uuid: vector} index loaded from a .bin sidecar.

    Read-only at runtime - produced by `embedding_prefetcher.py` from
    the canonical LanceDB table. The sidecar exists because IronPython
    2.7 has no LanceDB client; it is the runtime projection of the
    canonical store, read directly by the Revit plugin."""

    MAGIC = EMBEDDING_BIN_MAGIC

    def __init__(self, vectors, model, dim, source, lang, haystack_fields):
        self.vectors         = vectors
        self.model           = model
        self.dim             = dim
        self.source          = source
        self.lang            = lang
        self.haystack_fields = haystack_fields or ["name", "classification"]

    def __len__(self):
        return len(self.vectors)

    def __contains__(self, uuid):
        return uuid in self.vectors

    def get(self, uuid):
        return self.vectors.get(uuid)

    @classmethod
    def load_bin(cls, path):
        """Read a .bin sidecar. Raises ValueError on a bad header or
        truncated body."""
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != cls.MAGIC:
                raise ValueError(
                    "Not an {} file (got {!r}): {}".format(
                        cls.MAGIC.decode("ascii"), magic, path))
            meta_len_bytes = f.read(4)
            if len(meta_len_bytes) != 4:
                raise ValueError("Truncated EMB header: {}".format(path))
            meta_len = _struct.unpack("<I", meta_len_bytes)[0]
            meta_raw = f.read(meta_len)
            if len(meta_raw) != meta_len:
                raise ValueError("Truncated EMB metadata: {}".format(path))
            meta = _json.loads(meta_raw.decode("utf-8"))
            count = int(meta["count"])
            dim   = int(meta["dim"])
            uuid_order = meta["uuid_order"]
            if len(uuid_order) != count:
                raise ValueError(
                    "EMB metadata count mismatch ({} != {}): {}".format(
                        count, len(uuid_order), path))
            # Read+parse one vector at a time - avoids materialising a
            # multi-megabyte struct format string.
            fmt = "<" + "f" * dim
            bytes_per_vec = dim * 4
            vectors = {}
            for uuid in uuid_order:
                chunk = f.read(bytes_per_vec)
                if len(chunk) != bytes_per_vec:
                    raise ValueError(
                        "Truncated EMB vector at uuid={}: {}".format(uuid, path))
                vectors[uuid] = list(_struct.unpack(fmt, chunk))
        return cls(
            vectors=vectors,
            model=meta.get("model"),
            dim=dim,
            source=meta.get("source"),
            lang=meta.get("lang"),
            haystack_fields=meta.get(
                "haystack_fields", ["name", "classification"]),
        )

    def dump_bin(self, path, uuid_order=None,
                 generated_at=None, extra_meta=None):
        """Serialise to the .bin sidecar format. Used by the offline
        prefetcher. `uuid_order` pins the on-disk order (defaults to
        sorted UUIDs for deterministic output)."""
        if uuid_order is None:
            uuid_order = sorted(self.vectors.keys())
        count = len(uuid_order)
        meta = {
            "version":         1,
            "model":           self.model,
            "dim":             self.dim,
            "lang":            self.lang,
            "source":          self.source,
            "count":           count,
            "dtype":           "float32",
            "haystack_fields": self.haystack_fields,
            "uuid_order":      list(uuid_order),
        }
        if generated_at is not None:
            meta["generated_at"] = generated_at
        if extra_meta:
            for k, v in extra_meta.items():
                meta.setdefault(k, v)
        meta_bytes = _json.dumps(meta, ensure_ascii=False).encode("utf-8")
        fmt = "<" + "f" * self.dim
        with open(path, "wb") as f:
            f.write(self.MAGIC)
            f.write(_struct.pack("<I", len(meta_bytes)))
            f.write(meta_bytes)
            for uuid in uuid_order:
                vec = self.vectors.get(uuid)
                if vec is None or len(vec) != self.dim:
                    raise ValueError(
                        "Missing or wrong-dim vector for uuid={}: got {}".format(
                            uuid, "None" if vec is None else len(vec)))
                f.write(_struct.pack(fmt, *vec))


# ─────────────────────────────────────────────────────────────────
# Phase 1.7b - Robust ASCII-JSON request encoder (IronPython 2.7 fix)
# ─────────────────────────────────────────────────────────────────
# IronPython 2.7's `json.dumps` + System.Net.WebClient combination
# breaks on non-ASCII content in request bodies via two distinct paths:
#
#   1. UnicodeEncodeError('ascii', u'<chr>', ...) - Python-level.
#      `json.dumps` defaults to `ensure_ascii=True` but IP 2.7's
#      str/unicode boundary leaks: certain inputs produce a `str`
#      containing raw UTF-8 bytes, and a follow-up `.encode("utf-8")`
#      triggers an implicit `.decode("ascii")` first, which fails.
#
#   2. UnicodeDecodeError('unknown', u'<chr>', ...) - .NET-level.
#      WebClient's request-serialisation path interprets the byte[]
#      body via the system's default code page when its `Encoding`
#      property is at its default (ASCII) and the body contains
#      bytes > 0x7F.
#
# Both paths fire on common ÖKOBAUDAT haystacks (ä, ö, ü, ®, ™,
# en-dash, micro-sign, ç). The fix:
#   * Force `ensure_ascii=True` and normalise inputs to `unicode`.
#   * `\uXXXX`-escape all non-ASCII in the JSON body so the wire
#     bytes are pure ASCII (the server-side Python 3 `json.loads`
#     decodes them back to the original characters - semantic
#     content preserved end-to-end).
#   * Defensive `wc.Encoding = Encoding.UTF8` in the WebClient
#     branch of `_http_post` to short-circuit the .NET code-page
#     interpretation path.

if _HAVE_DOTNET:
    _BUILTIN_UNICODE = unicode   # IronPython 2.7 native
else:
    try:
        _BUILTIN_UNICODE = unicode   # CPython 2
    except NameError:
        _BUILTIN_UNICODE = str       # CPython 3


def _to_unicode(s):
    """Coerce arbitrary input into unicode for JSON encoding.
    Bytes are decoded as UTF-8 with replacement (lossy if the input
    is actually not UTF-8, but the alternative is an exception)."""
    if s is None:
        return _BUILTIN_UNICODE("")
    if isinstance(s, bytes):
        return s.decode("utf-8", "replace")
    if isinstance(s, _BUILTIN_UNICODE):
        return s
    return _BUILTIN_UNICODE(s)


def _safe_unicode(s):
    """Coerce ``s`` to unicode without ever raising.

    Order of attempts: already-unicode passthrough → UTF-8 decode →
    Latin-1 decode (round-trip-safe, never raises) → unicode() coercion.
    Used by the hand-rolled JSON encoder so that *any* string in the
    payload - whether it came in as a Py2 ``str`` with UTF-8 bytes
    (cached JSON), a Py2 ``str`` with Latin-1 bytes (CP-1252 round-
    trip from some Revit API), or already-unicode - ends up as a
    unicode object that we can iterate by code point.
    """
    if s is None:
        return _BUILTIN_UNICODE("")
    if isinstance(s, _BUILTIN_UNICODE):
        return s
    if hasattr(s, "decode"):
        try:
            return s.decode("utf-8")
        except (UnicodeDecodeError, UnicodeError, Exception):
            pass
        try:
            # Latin-1 maps every byte 0–255 to its same-numbered Unicode
            # code point - it can never raise on bytes input.
            return s.decode("latin-1")
        except Exception:
            try:
                return s.decode("utf-8", "replace")
            except Exception:
                pass
    try:
        return _BUILTIN_UNICODE(s)
    except Exception:
        return _BUILTIN_UNICODE("")


def _json_escape_unicode(u):
    """Hand-rolled JSON string escape - pure unicode → pure unicode.

    Output uses only printable ASCII (0x20–0x7E) plus the escape
    sequences ``\\n``, ``\\t``, ``\\b``, ``\\f``, ``\\r``, ``\\"``,
    ``\\\\``. Non-printable and non-ASCII code points are emitted as
    ``\\uXXXX`` (with surrogate pairs for code points beyond U+FFFF).
    Encoding the result as ASCII can never fail.
    """
    if u is None:
        u = _BUILTIN_UNICODE("")
    elif not isinstance(u, _BUILTIN_UNICODE):
        u = _safe_unicode(u)
    parts = [u'"']
    for ch in u:
        cp = ord(ch)
        if cp == 0x22:        # "
            parts.append(u'\\"')
        elif cp == 0x5C:      # \
            parts.append(u'\\\\')
        elif cp == 0x08:
            parts.append(u'\\b')
        elif cp == 0x09:
            parts.append(u'\\t')
        elif cp == 0x0A:
            parts.append(u'\\n')
        elif cp == 0x0C:
            parts.append(u'\\f')
        elif cp == 0x0D:
            parts.append(u'\\r')
        elif 0x20 <= cp <= 0x7E:
            parts.append(ch)
        elif cp <= 0xFFFF:
            parts.append(u'\\u{0:04x}'.format(cp))
        else:
            # Non-BMP → surrogate pair (JSON RFC 8259 §7).
            cp2 = cp - 0x10000
            hi = 0xD800 + (cp2 >> 10)
            lo = 0xDC00 + (cp2 & 0x3FF)
            parts.append(u'\\u{0:04x}\\u{1:04x}'.format(hi, lo))
    parts.append(u'"')
    return u''.join(parts)


def _ascii_json_bytes(payload):
    """Serialise ``payload`` to JSON as pure-ASCII bytes.

    Phase 1.7d - bypasses ``_json.dumps`` entirely. IronPython 2.7's
    json module, when handed a dict containing a Py2 ``str`` value with
    non-ASCII UTF-8 bytes, internally calls ``str.decode`` using the
    system code page (not ASCII, not UTF-8) and re-raises
    ``DecoderFallbackException`` as ``UnicodeDecodeError('unknown', ...)``
    before our ``ensure_ascii=True`` flag can take effect.

    The hand-rolled encoder here:
      1. Coerces every string value (key and value) to unicode via
         ``_safe_unicode`` (UTF-8 → Latin-1 → replacement-mode UTF-8).
      2. Escapes each unicode character through ``_json_escape_unicode``
         so the output is pure ASCII unicode.
      3. ``.encode("ascii")`` to bytes - never fails because every
         character in the encoder output is already in [0x20, 0x7E].

    Supports the limited payload shapes the reranker / Ollama clients
    actually use: a dict whose values are strings, lists of strings,
    numbers, or booleans. No nested dicts, no arbitrary objects.
    """
    if not isinstance(payload, dict):
        raise TypeError("Expected dict payload, got " + type(payload).__name__)

    def _encode_value(v):
        if v is None:
            return u"null"
        if isinstance(v, bool):
            return u"true" if v else u"false"
        if isinstance(v, (int, float)):
            return _BUILTIN_UNICODE(repr(v))
        if isinstance(v, (list, tuple)):
            inner = u",".join(_encode_value(x) for x in v)
            return u"[" + inner + u"]"
        # Fallback: treat as string.
        return _json_escape_unicode(v)

    items = []
    for k, v in payload.items():
        items.append(_json_escape_unicode(k) + u":" + _encode_value(v))
    body = u"{" + u",".join(items) + u"}"
    # Pure ASCII unicode → ASCII bytes. The `replace` error handler is
    # defensive only: every char in `body` is guaranteed in [0x20, 0x7E].
    return body.encode("ascii", "replace")


if _HAVE_DOTNET:
    def _bytes_to_net_array(body_bytes):
        """Build a .NET ``Byte[]`` from a Python bytes-like object using
        per-index ``ord()`` (no ``bytearray()``/``tuple()`` intermediary).

        Why: ``_Array[_Byte](tuple(bytearray(body_bytes)))`` round-trips
        through IronPython's ``bytearray`` which, in some locales, treats
        a Py2 ``str`` as a system-code-page-encoded string and re-raises
        ``DecoderFallbackException`` as ``UnicodeDecodeError('unknown')``.
        The per-index ``ord()`` path bypasses that.
        """
        n = len(body_bytes)
        arr = _Array.CreateInstance(_Byte, n)
        for i in range(n):
            c = body_bytes[i]
            bv = c if isinstance(c, int) else ord(c)
            if bv > 0xFF:
                bv = 0x3F   # '?' - should never happen with ASCII input
            arr[i] = _Byte(bv)
        return arr

    def _http_post_via_request(url, body_bytes, timeout_ms,
                                content_type="application/json"):
        """Bypass ``System.Net.WebClient`` and use ``HttpWebRequest``
        directly. Eliminates the WebClient ``Encoding`` quirk that fires
        ``DecoderFallbackException → UnicodeDecodeError('unknown')`` on
        bytes > 0x7F even when the body is a pure byte array."""
        net_bytes = _bytes_to_net_array(body_bytes)
        n = len(net_bytes)
        req = _WebRequest.Create(url)
        req.Method = "POST"
        req.ContentType = content_type
        req.ContentLength = n
        try:
            req.Timeout = int(timeout_ms)
        except Exception:
            pass
        rs = req.GetRequestStream()
        try:
            rs.Write(net_bytes, 0, n)
        finally:
            rs.Close()
        resp = req.GetResponse()
        try:
            stream = resp.GetResponseStream()
            try:
                reader = _StreamReader(stream, _Encoding.UTF8)
                try:
                    return reader.ReadToEnd()
                finally:
                    reader.Close()
            finally:
                stream.Close()
        finally:
            resp.Close()

    def _http_get_via_request(url, timeout_ms):
        """GET counterpart to ``_http_post_via_request``. Same rationale."""
        req = _WebRequest.Create(url)
        req.Method = "GET"
        try:
            req.Timeout = int(timeout_ms)
        except Exception:
            pass
        resp = req.GetResponse()
        try:
            stream = resp.GetResponseStream()
            try:
                reader = _StreamReader(stream, _Encoding.UTF8)
                try:
                    return reader.ReadToEnd()
                finally:
                    reader.Close()
            finally:
                stream.Close()
        finally:
            resp.Close()


class OllamaEmbeddingClient(object):
    """Local Ollama HTTP client for embedding generation.

    Backend chosen at import time:
      * `System.Net.WebClient` when running inside Revit (IronPython
        2.7) - `urllib.request` is not reliable there.
      * `urllib.request` under CPython 3 (offline tools).

    Returns embeddings as `list[float]`. No numpy needed."""

    def __init__(self, base_url="http://localhost:11434",
                 model="bge-m3", timeout_ms=10000, normalize=True,
                 lowercase=True):
        self.base_url    = (base_url or "").rstrip("/")
        self.model       = model
        self.timeout_ms  = int(timeout_ms)
        self.normalize   = bool(normalize)
        # bge-m3 at F16-GPU produces NaN for a small fraction of inputs
        # (~2 % of ÖKOBAUDAT names) - the model's response then fails
        # Ollama's JSON serializer with `unsupported value: NaN` → HTTP
        # 500. Lowercasing the input breaks the problematic token
        # sequence and recovers 100 % coverage. Cosine drop vs original
        # case is ~0.05-0.10 - small enough that applying it uniformly
        # to both haystacks AND queries is the right call (consistency
        # preserves perfect cosine fidelity between query and dataset).
        self.lowercase   = bool(lowercase)
        self._last_error = None

    def _preprocess(self, text):
        if text is None:
            return text
        return text.lower() if self.lowercase else text

    @property
    def last_error(self):
        return self._last_error

    def is_available(self):
        """Cheap healthcheck against `/api/tags`. Returns True iff
        Ollama responds; updates `last_error` otherwise."""
        try:
            self._http_get(self.base_url + "/api/tags")
            self._last_error = None
            return True
        except Exception as ex:
            self._last_error = "healthcheck: " + repr(ex)
            return False

    def embed(self, text):
        """POST `/api/embed` for a single string and return `list[float]`.
        Returns None on failure. Live UI hot-path - must be cheap, so
        this stays separate from `embed_batch` (no list-wrapping cost)."""
        if not text:
            return None
        text = self._preprocess(text)
        try:
            # Phase 1.7b: ASCII-only JSON body so non-ASCII queries
            # (German "Stahlbeton", pasted Revit material names with
            # ®/™/umlauts) survive the IronPython 2.7 → WebClient path.
            body = _ascii_json_bytes({"model": self.model,
                                       "input": _to_unicode(text)})
            raw = self._http_post(self.base_url + "/api/embed", body)
            if not raw:
                self._last_error = "embed: empty response"
                return None
            data = _json.loads(raw)
            vec = None
            if isinstance(data, dict):
                # `/api/embed` (newer)        -> {"embeddings": [[...]]}
                # `/api/embeddings` (older)   -> {"embedding": [...]}
                if "embeddings" in data and data["embeddings"]:
                    vec = data["embeddings"][0]
                elif "embedding" in data:
                    vec = data["embedding"]
            if vec is None:
                self._last_error = "embed: no embedding in response: " + repr(data)[:200]
                return None
            vec = [float(x) for x in vec]
            if self.normalize:
                vec = _l2_normalize(vec)
            self._last_error = None
            return vec
        except Exception as ex:
            self._last_error = "embed: " + repr(ex)
            return None

    def embed_batch(self, texts):
        """POST `/api/embed` with `"input": [...]` and return a list of
        `list[float]` aligned to `texts`. Used by the offline prefetcher
        - one batched request fans out into a single GPU forward pass,
        giving roughly batch_size× the throughput of `embed()`.

        Returns None on any failure (whole batch fails atomically; the
        caller can retry per-input if needed)."""
        if not texts:
            return []
        texts = [self._preprocess(t) for t in texts]
        try:
            # Phase 1.7b: ASCII-only JSON for the batch path too.
            # The prefetcher runs in CPython 3 where this is already
            # safe, but the helper is symmetric across runtimes and
            # the cost is identical (~5 us per text for the escape pass).
            body = _ascii_json_bytes({"model": self.model,
                                       "input": [_to_unicode(t) for t in texts]})
            raw = self._http_post(self.base_url + "/api/embed", body)
            if not raw:
                self._last_error = "embed_batch: empty response"
                return None
            data = _json.loads(raw)
            arr = None
            if isinstance(data, dict):
                if "embeddings" in data and data["embeddings"]:
                    arr = data["embeddings"]
                elif "embedding" in data:
                    # Legacy single-shot endpoint - degrade gracefully.
                    arr = [data["embedding"]]
            if arr is None:
                self._last_error = "embed_batch: no embeddings in response: " + repr(data)[:200]
                return None
            out = []
            for vec in arr:
                v = [float(x) for x in vec]
                if self.normalize:
                    v = _l2_normalize(v)
                out.append(v)
            if len(out) != len(texts):
                self._last_error = "embed_batch: returned {} vectors for {} inputs".format(
                    len(out), len(texts))
                return None
            self._last_error = None
            return out
        except Exception as ex:
            self._last_error = "embed_batch: " + repr(ex)
            return None

    # ── Backend-specific HTTP plumbing ─────────────────────────────

    if _HAVE_DOTNET:
        def _http_get(self, url):
            # Phase 1.7c: HttpWebRequest. Even GET runs through
            # `_http_get_via_request` so headers / response decode
            # never touch WebClient's `Encoding` quirk.
            return _http_get_via_request(url, self.timeout_ms)

        def _http_post(self, url, body_bytes):
            # Phase 1.7c: HttpWebRequest + per-byte build, no WebClient.
            # See `_http_post_via_request` for the rationale.
            return _http_post_via_request(url, body_bytes, self.timeout_ms)
    else:
        def _http_get(self, url):
            req = _urllib_req.Request(
                url, headers={"Accept": "application/json"})
            timeout_s = max(1.0, self.timeout_ms / 1000.0)
            resp = _urllib_req.urlopen(req, timeout=timeout_s)
            raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw

        def _http_post(self, url, body_bytes):
            req = _urllib_req.Request(
                url, data=body_bytes,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
            timeout_s = max(1.0, self.timeout_ms / 1000.0)
            try:
                resp = _urllib_req.urlopen(req, timeout=timeout_s)
            except _urllib_error.HTTPError as ex:
                # Bubble the server's error body up to the caller so
                # `embed_batch`'s `last_error` carries Ollama's actual
                # message (e.g. "context length exceeded"), not just the
                # bare status code. Without this the diagnostic loop is
                # blind: every transient 500 looks identical.
                try:
                    body = ex.read()
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", "replace")
                except Exception:
                    body = ""
                raise IOError("HTTP {0} {1}: {2}".format(
                    ex.code, ex.reason, (body or "")[:500]))
            raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw


# ═══════════════════════════════════════════════════════════════════
# Phase 1+ - Cross-encoder reranker HTTP client
# ═══════════════════════════════════════════════════════════════════
# The cross-encoder runs in a small CPython 3 sidecar (`rerank_service.py`)
# rather than Ollama, because Ollama has no `/api/rerank` endpoint and
# its `/api/embed` only accepts single-string inputs (no pair scoring).
# The sidecar wraps `sentence_transformers.CrossEncoder('BAAI/bge-reranker-v2-m3')`
# and exposes:
#   GET  /health  → {"model":..., "ready": bool, "device": "cpu"|"cuda"}
#   POST /rerank  → {"query": str, "documents": [str,...]}
#                   returns {"scores": [float,...]} (raw logits)
#
# Same dual-backend HTTP pattern as `OllamaEmbeddingClient`:
# .NET `WebClient` under IronPython 2.7, `urllib.request` under CPython 3.
# ═══════════════════════════════════════════════════════════════════

class LocalRerankerClient(object):
    """IronPython-2.7-compatible HTTP client for the local rerank sidecar.

    Falls back to `urllib.request` under CPython 3 so the same class is
    re-usable from the Validation pushbutton and the offline benchmark.

    Usage:
        client = LocalRerankerClient(base_url="http://127.0.0.1:11500")
        if client.is_available():
            scores = client.rerank("steel beam", ["rebar B500", "ceramic tile"])
            # scores: list[float] aligned to the input documents
    """
    def __init__(self, base_url="http://127.0.0.1:11500", timeout_ms=15000):
        self.base_url    = (base_url or "").rstrip("/")
        self.timeout_ms  = int(timeout_ms)
        self._last_error = None
        self._cached_model = None

    @property
    def last_error(self):
        return self._last_error

    @property
    def model(self):
        return self._cached_model

    def is_available(self):
        """Cheap healthcheck against `/health`. Returns True iff the
        service answers AND has finished loading the model. Updates
        `last_error` and caches the model name when reachable."""
        try:
            raw = self._http_get(self.base_url + "/health")
            data = _json.loads(raw) if raw else {}
            if not data.get("ready"):
                self._last_error = "rerank healthcheck: not ready ({0})".format(
                    data.get("error") or "loading")
                self._cached_model = data.get("model")
                return False
            self._last_error = None
            self._cached_model = data.get("model")
            return True
        except Exception as ex:
            self._last_error = "rerank healthcheck: " + repr(ex)
            return False

    def rerank(self, query, documents):
        """POST `/rerank` with a (query, documents) pair list and return
        a list of float logits parallel to `documents`.

        On any error returns `None` and stashes the reason in `last_error`
        so the caller can decide between falling back to the base ranker
        OR surfacing the failure to the status bar."""
        if not query or not documents:
            return []
        try:
            # Phase 1.7b: ASCII-only JSON body. Non-ASCII chars in
            # the query / documents (German umlauts, ®, ™, en-dashes,
            # µ, ç) become \uXXXX escapes that the sidecar decodes
            # back server-side - semantic content reaches the cross-
            # encoder intact, wire bytes never trip IronPython 2.7's
            # str/unicode boundary or .NET's CP-1252 fallback.
            body = _ascii_json_bytes({
                "query":     _to_unicode(query),
                "documents": [_to_unicode(d) for d in documents],
            })
            raw = self._http_post(self.base_url + "/rerank", body)
            data = _json.loads(raw) if raw else {}
            if "error" in data:
                self._last_error = "rerank: " + str(data["error"])
                return None
            scores = data.get("scores")
            if not isinstance(scores, list) or len(scores) != len(documents):
                self._last_error = "rerank: returned {0} scores for {1} inputs".format(
                    len(scores) if isinstance(scores, list) else "?",
                    len(documents))
                return None
            # Cast to plain floats (JSON parser may return int for ±0).
            out = [float(x) for x in scores]
            self._last_error = None
            return out
        except Exception as ex:
            self._last_error = "rerank: " + repr(ex)
            return None

    # ── Backend-specific HTTP plumbing (mirrors OllamaEmbeddingClient) ──

    if _HAVE_DOTNET:
        def _http_get(self, url):
            # Phase 1.7c: bypass WebClient via HttpWebRequest. The
            # previous WebClient.DownloadString path triggered
            # `DecoderFallbackException → UnicodeDecodeError('unknown', ...)`
            # in IronPython 2.7 the moment a response header or body
            # carried bytes > 0x7F under a non-UTF-8 system code page.
            return _http_get_via_request(url, self.timeout_ms)

        def _http_post(self, url, body_bytes):
            # Phase 1.7c: HttpWebRequest + per-byte byte[] build, no
            # WebClient. The reranker is where the WebClient encoding
            # quirk bites hardest in practice - dataset haystacks
            # always carry German / typographic chars, so essentially
            # every request runs into the failure mode under the
            # old transport.
            return _http_post_via_request(url, body_bytes, self.timeout_ms)
    else:
        def _http_get(self, url):
            req = _urllib_req.Request(
                url, headers={"Accept": "application/json"})
            timeout_s = max(1.0, self.timeout_ms / 1000.0)
            resp = _urllib_req.urlopen(req, timeout=timeout_s)
            raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw

        def _http_post(self, url, body_bytes):
            req = _urllib_req.Request(
                url, data=body_bytes,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
            timeout_s = max(1.0, self.timeout_ms / 1000.0)
            try:
                resp = _urllib_req.urlopen(req, timeout=timeout_s)
            except _urllib_error.HTTPError as ex:
                try:
                    body = ex.read()
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", "replace")
                except Exception:
                    body = ""
                raise IOError("HTTP {0} {1}: {2}".format(
                    ex.code, ex.reason, (body or "")[:500]))
            raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw


def build_semantic_haystack(searchable):
    """Build the per-dataset embedding input - single source of truth
    shared by the offline prefetcher (embedding each dataset) and the
    live UI's tooltip / reranker-document text.

    The haystack is **bilingual** so a query in either language can
    match it directly (instead of relying on bge-m3's lossy
    cross-language jump). Keys read from `searchable`:

      name              - primary-language name (required)
      name_alt          - the *other* language's name (optional). The
                          prefetcher supplies it by cross-referencing the
                          sibling ds_cache, so EN and DE names are both
                          embedded; runtime callers omit it.
      classification    - raw (German) ÖKOBAUDAT classification path
      classification_en - optional precomputed English translation; when
                          absent it is derived via translate_path so the
                          classification is present in both languages.

    Returns a unicode string of the non-empty, de-duplicated parts joined
    by " | " (e.g. ``en_name | de_name | de_class | en_class``). Pure
    text; safe under IronPython 2.7 and CPython 3. Note: the live query is
    embedded raw (not via this function), so query- and dataset-space
    still align through the shared bge-m3 model."""
    if not isinstance(searchable, dict):
        return u""
    parts = []
    seen  = set()

    def _add(value):
        v = (value or u"").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            parts.append(v)

    _add(searchable.get("name"))
    _add(searchable.get("name_alt"))

    cls    = (searchable.get("classification") or u"").strip()
    cls_en = (searchable.get("classification_en") or u"").strip()
    if cls and not cls_en:
        # Lazy import (mirrors build_searchable) - avoids any import cycle
        # and keeps this module importable without classification_labels.
        try:
            from classification_labels import translate_path
            cls_en = (translate_path(cls, "en") or u"").strip()
        except Exception:
            cls_en = u""
    _add(cls)
    _add(cls_en)

    return u" | ".join(parts)


def search_semantic(query, datasets, searchables=None,
                    embedding_index=None, ollama_client=None,
                    lang="de", query_vector=None):
    """Phase 1 dense-retrieval search mode - drop-in replacement for
    `search_fuzzy` / `search_bm25f` in the offline benchmark and the
    Validation pushbutton.

    No binary filter. Every dataset with a cached vector is scored by
    cosine similarity against the query embedding, then sorted
    descending. Datasets without a cached vector are silently dropped.

    Args:
        query:           Raw user query string. Embedded via
                         `ollama_client.embed(query)` if
                         `query_vector` is not pre-supplied.
        datasets:        Iterable of MaterialDataset-like objects or
                         dicts; each must expose `uuid` (attribute or
                         key).
        searchables:     Unused (semantic mode has no field weights);
                         kept for signature parity with the other
                         search functions.
        embedding_index: An `EmbeddingIndex` instance - mandatory.
        ollama_client:   An `OllamaEmbeddingClient`. Required when
                         `query_vector` is not pre-supplied.
        lang:            Currently unused; reserved for API parity.
        query_vector:    Optional pre-computed query embedding - lets
                         benchmark sweeps cache one embedding across
                         a whole datastock pass.

    Returns:
        List of (ds, raw_cosine) sorted by raw_cosine descending.
        Empty list when the query is empty, the index/client missing,
        or the embed call fails."""
    if not query or embedding_index is None:
        return []
    if query_vector is None:
        if ollama_client is None:
            return []
        query_vector = ollama_client.embed(query)
        if query_vector is None:
            return []
    out = []
    for ds in datasets:
        uuid = getattr(ds, "uuid", None)
        if uuid is None and isinstance(ds, dict):
            uuid = ds.get("uuid")
        if uuid is None:
            continue
        vec = embedding_index.get(uuid)
        if vec is None:
            continue
        out.append((ds, cosine_similarity(query_vector, vec)))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


# ═══════════════════════════════════════════════════════════════════
# Phase 1+ - Reciprocal Rank Fusion (RRF)
# ═══════════════════════════════════════════════════════════════════
# Cormack, Clarke & Buttcher (SIGIR 2009): combine ranks not scores.
# BM25F is unbounded, cosine is in [-1, 1] - naive averaging breaks
# under different score distributions. RRF sidesteps this by using
# only the rank of each item in each ranker.
#
#     RRF(item) = sum_r  1 / (k + rank_r(item))
#
# An item missing from a ranker contributes 0 for that ranker.
# k = 60 is the original paper's default and remains canonical.
# ═══════════════════════════════════════════════════════════════════

def reciprocal_rank_fusion(rankings, k=60, weights=None):
    """Fuse multiple ranked lists into a single score per item using RRF.

    Args:
        rankings:  list of iterables; each inner iterable is the ranked
                   items (best first) from one ranker. Items must be
                   hashable (we use UUID strings in this codebase).
        k:         the RRF constant (default 60, Cormack et al. 2009).
        weights:   optional per-ranker weights, same order as `rankings`.
                   When None every ranker is weight 1.0 - byte-for-byte the
                   classic unweighted RRF. The weighted form is
                   ``Sigma_r w_r / (k + rank_r(item))`` and matches the
                   offline `tools/tune_fusion.py`, so the tuned
                   `w_bm25` / `w_sem` from `hybrid_params_*.json` reproduce
                   live. Shorter `weights` are padded with 1.0.

    Returns:
        dict {item: rrf_score}. Higher = more relevant. Items appearing
        in more rankings, or at better ranks, score higher.

    Notes:
        * `rrf_score(item)` is bounded above by `Sigma_r w_r / (k + 1)`,
          attained when the item is rank-1 in every input ranking.
          Use `rrf_max_score(num_rankers, k, weights)` to get that ceiling
          for display normalisation.
        * Stable: identical inputs produce identical outputs.
    """
    if not rankings:
        return {}
    if weights is None:
        weights = [1.0] * len(rankings)
    elif len(weights) < len(rankings):
        weights = list(weights) + [1.0] * (len(rankings) - len(weights))
    scores = {}
    for ranking, w in zip(rankings, weights):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
    return scores


def rrf_max_score(num_rankings, k=60, weights=None):
    """Theoretical maximum RRF score: an item ranked #1 in every input
    ranking. Used to normalise the raw RRF sum into [0, 1] for display.
    With per-ranker `weights`, the ceiling is ``Sigma_r w_r / (k + 1)``;
    with `weights=None` it is the unweighted ``num_rankings / (k + 1)``."""
    if weights is not None:
        total_w = float(sum(weights))
    else:
        total_w = float(num_rankings)
    if total_w <= 0:
        return 1.0
    return total_w / (k + 1)


def bm25f_ranking(query_lower, items, stats, params=None, expanded_query=None):
    """Rank a whole candidate set by BM25F - the "symmetric pool" for
    Reciprocal Rank Fusion (no fuzzy pre-filter; every item is scored).

    Args:
        query_lower:    lower-case query string.
        items:          iterable of (item_id, searchable) pairs, where
                        `searchable` is the {"name","classification"} dict
                        from `build_searchable`. `item_id` is whatever key
                        the caller fuses on (UUID strings here).
        stats:          CorpusStats over the same corpus.
        params:         BM25F param overrides (optional).
        expanded_query: precomputed typo expansion (optional), forwarded to
                        `bm25f_score` verbatim. None => no typo tolerance.

    Returns:
        List of item_ids, best-first. Stable: ties preserve input order.

    Single source of truth for the live `window.py:_compute_rrf_scores`
    symmetric pool and the offline `tools/tune_fusion.py`, so the tuned
    `hybrid_params_*` reproduce live.
    """
    if not query_lower:
        return []
    scored = []
    for item_id, hay in items:
        raw = bm25f_score(query_lower, hay, stats, params=params,
                          expanded_query=expanded_query)
        scored.append((item_id, raw))
    scored.sort(key=lambda t: t[1], reverse=True)
    return [item_id for item_id, _ in scored]


# ═══════════════════════════════════════════════════════════════════
# Evaluation metrics (Phase 0 benchmark + Validation pushbutton)
# ═══════════════════════════════════════════════════════════════════
# Statistical and ranking-quality helpers used by:
#   * tools/phase0_benchmark.py    - offline R@K + MRR reporting
#   * Validation.pushbutton/script.py - live 5-mode benchmark,
#     exact-correct R@K, MRR/nDCG with bootstrap CIs, pairwise
#     McNemar significance
#
# All pure Python (math + random); no numpy/scipy required, so
# IronPython 2.7 inside Revit can import them at runtime.
# ═══════════════════════════════════════════════════════════════════

# Wilson 95% binomial CI z-score (two-sided).
WILSON_Z = 1.96

# Bootstrap resampling replicates. 1000 is the smallest round number
# where the 2.5th/97.5th percentile estimates stabilise to ~1% on
# re-resampling.
BOOTSTRAP_B = 1000


def wilson_ci(successes, n, z=WILSON_Z):
    """95% Wilson score CI for a binomial proportion.

    Returns `(point, lo, hi)` with the bounds clamped to [0, 1].
    Reference: Wilson 1927, "Probable Inference, the Law of Succession,
    and Statistical Inference." Far less degenerate than the normal
    approximation at the small-N or near-0/1 ends we live in.
    """
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = successes / float(n)
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half   = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_ci(values, agg=None, B=BOOTSTRAP_B, alpha=0.05, seed=42):
    """Resampled CI of an aggregator (default: mean) over `values`.

    Returns `(point_estimate, lo, hi)` from `B` bootstrap resamples
    using `seed` for reproducibility. The point estimate is the
    aggregator evaluated on the original `values` (not on the
    resamples), so it doesn't drift across runs.
    Reference: Efron & Tibshirani 1993, "An Introduction to the
    Bootstrap." The percentile method is the simplest; for tightly
    bounded statistics like MRR / nDCG on n < 50 it is adequate.
    """
    if agg is None:
        agg = lambda xs: sum(xs) / float(len(xs)) if xs else 0.0
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0)
    rng = _random.Random(seed)
    samples = []
    for _ in range(B):
        s = [values[rng.randint(0, n - 1)] for _ in range(n)]
        samples.append(agg(s))
    samples.sort()
    lo = samples[int(alpha / 2.0 * B)]
    hi = samples[min(B - 1, int((1 - alpha / 2.0) * B))]
    return (agg(values), lo, hi)


def mrr(ranks):
    """Mean reciprocal rank over a list of per-query first-hit ranks.

    `ranks` is a list of 1-indexed integers (1 = top hit). Use
    `None` (or any falsy value) for queries that missed entirely -
    they contribute 0 to the mean. Returns 0.0 for an empty list.
    """
    n = len(ranks)
    if n == 0:
        return 0.0
    s = 0.0
    for r in ranks:
        if r:
            s += 1.0 / float(r)
    return s / n


def ndcg_at_k(uuids_in_order, correct_uuid, acceptable_uuids, k=10,
              rel_correct=2, rel_acceptable=1):
    """Graded normalised DCG@k (Burges et al. 2005).

        DCG@k  = Σ_{i=1..k} (2^rel_i − 1) / log2(i + 1)
        IDCG@k = DCG of the ideal ranking (all positives at the top,
                 sorted by relevance desc)
        nDCG   = DCG / IDCG  (or 0 when IDCG = 0)

    Relevance grades default to {correct: 2, acceptable: 1, other: 0},
    giving gain weights 3 / 1 / 0 - a strong incentive to surface the
    canonical EPD first. Pass `rel_correct=1` for binary relevance.

    `uuids_in_order`: ranker's output, best first. Items beyond `k`
        are ignored. Missing items (< k results) do not pad - the DCG
        sum simply stops short, lowering the score below the ideal.
    `correct_uuid`: the canonical "correct" UUID (string), or "" /
        None when no canonical answer exists.
    `acceptable_uuids`: iterable of acceptable UUIDs; may or may not
        include `correct_uuid` (handled either way).
    """
    accept = set(acceptable_uuids or [])
    if correct_uuid:
        accept.add(correct_uuid)

    def _rel(u):
        if u and correct_uuid and u == correct_uuid:
            return rel_correct
        if u in accept:
            return rel_acceptable
        return 0

    # DCG over the actual ranking, capped at k
    dcg = 0.0
    for i, u in enumerate(uuids_in_order[:k], start=1):
        rel = _rel(u)
        if rel:
            dcg += (2.0 ** rel - 1.0) / math.log(i + 1, 2)

    # IDCG: sort all positive grades descending, take top k
    grades = []
    if correct_uuid:
        grades.append(rel_correct)
    for u in accept:
        if u != correct_uuid:
            grades.append(rel_acceptable)
    grades.sort(reverse=True)
    idcg = 0.0
    for i, rel in enumerate(grades[:k], start=1):
        idcg += (2.0 ** rel - 1.0) / math.log(i + 1, 2)

    if idcg <= 0.0:
        return 0.0
    return dcg / idcg


def mcnemar_pvalue(b, c):
    """Two-sided exact McNemar binomial test on discordant-pair counts.

    For paired binary outcomes (e.g. mode A vs mode B on the same
    query, did each hit at rank K?), the discordant pairs are:
        b = # queries where A hit and B missed
        c = # queries where A missed and B hit
    Under H0 (no difference between A and B), the smaller of `b`/`c`
    is binomially distributed with n = b + c, p = 0.5. The two-sided
    p-value is 2 × P(X ≤ min(b, c) | n, 0.5), clamped to ≤ 1.

    Returns 1.0 when there are no discordant pairs (perfect agreement
    - nothing to test). Reference: McNemar 1947, "Note on the Sampling
    Error of the Difference between Correlated Proportions or
    Percentages." Exact (not chi-squared) so it stays valid for the
    n = 17 ground truth.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = b if b < c else c
    # cumulative = Σ_{i=0..k} C(n, i)
    coef = 1.0
    cumulative = 1.0  # i = 0 term, C(n, 0) = 1
    for i in range(1, k + 1):
        coef = coef * (n - i + 1) / float(i)
        cumulative += coef
    p = 2.0 * cumulative * (0.5 ** n)
    if p > 1.0:
        return 1.0
    return p
