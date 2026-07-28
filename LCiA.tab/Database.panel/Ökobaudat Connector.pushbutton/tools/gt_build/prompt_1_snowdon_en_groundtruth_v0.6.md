# Prompt 1 — Snowdon Towers (English) ÖKOBAUDAT ground-truth build (v0.6)

ROLE
You are a senior Life-Cycle Assessment (LCA) consultant and construction-materials expert.
You are building a benchmark REFERENCE SET that maps the BIM materials of an Autodesk Revit
sample project ("Snowdon Towers") to Environmental Product Declarations (EPDs) in the German
ÖKOBAUDAT database (EN 15804+A2, Sphera background; dataset names are English, classification
paths are German). Your labels will be cross-checked against two other LLMs and adjudicated by
a human LCA expert, so each pick must be defensible, reproducible, and coherent — a careful
peer reviewer must agree with every rank-1 choice and with every candidate you list.

WORK ENTIRELY FROM THESE LOCAL FILES (do not guess; do not browse the web):

1. BIM materials to map (145):
   LCiA.tab\Context.panel\ExtractContext.pushbutton\data\materials_context.json
   Fields per material: revit_id, name, class, description, host_elements{categories,types},
   physical_asset{density_kg_m3, concrete_compression_mpa, lightweight, structural_class},
   thermal_asset{density_kg_m3, thermal_conductivity_w_mk}.

2. ÖKOBAUDAT dataset cache (2,383 datasets — the ONLY source of UUIDs):
   LCiA.tab\Database.panel\Ökobaudat Connector.pushbutton\LCiA_Extension_Cache\ds_cache_v2_a2_sphera_en.json
   Each dataset in "results": uuid, name (English), classification (German "/"-separated path),
   dataset_type (generic / representative / average / specific), program_operator, owner.
   You may use ONLY UUIDs that exist here. NEVER invent or recall a UUID from memory.

3. Helper that flattens the cache into a searchable index (run first, CPython 3):
   python tools/gt_build/_dump_indexes.py
   -> tools/gt_build/cache_index.tsv : one line per dataset, uuid <TAB> EN name <TAB> DE path,
      sorted by classification so every product family is a contiguous block.

4. German→English classification translator (CPython 3):
   classification_labels.translate_path(path, "en")   (module in the pushbutton folder)
   Use it to read each family in English and confirm coherence.

5. EXACT output schema to copy:
   LCiA.tab\Database.panel\Ökobaudat Connector.pushbutton\sample_project_a2_sphera_en_v0.3.json

INDEPENDENCE: the reference set must stay independent of the systems it will measure. Build
candidate lists by enumerating ÖKOBAUDAT classification families directly from the cache index
(taxonomy-driven). Do NOT use the plugin's own BM25F / semantic / hybrid search to pick candidates.

════════════════════════════════════════════════════════════════════════
HARD RULES

R1. MAP or SKIP each of the 145 materials against an EXPLICIT system boundary (record the
    boundary in scope_notes). The boundary is the single most important decision — apply it
    consistently.
      IN (MAP)  = building FABRIC: structure, envelope, partitions, floor/roof/wall build-ups,
                  glazing, insulation, waterproofing / membranes, masonry, screed, tile —
                  PLUS sanitary ceramic ware (toilets, washbasins: real products with ÖKOBAUDAT
                  EPDs, commonly inside building LCA).
      OUT (SKIP) = building services / MEP, loose equipment & appliances, furniture & casework,
                  site / landscape, and pure appearance finishes.
    Tag every SKIP with ONE reason category (begin skip_reason with it, e.g. "[not a material] …"):
      (a) "not a material" — air / cavities. Zero embodied impact; nothing is manufactured.
          Revit models the void as a "material", but it is empty space. (Air is the ONLY true
          non-material.)
      (b) "negligible appearance finish" — pure colour / paint / plating / anodize / polish
          finishes. Coatings DO have EPDs, but are excluded as a thin surface treatment, or as
          a colour assigned to a host whose bulk material is already counted elsewhere.
      (c) "outside building-fabric boundary" — plumbing fittings & faucets, lighting fixtures,
          appliances, elevators, furniture, textiles / leather, living / landscape / site
          elements, photovoltaics and other MEP / loose equipment (these are real products, but
          building-services / equipment, not fabric); and Revit placeholder materials.
    Do NOT call a real product a "non-product": only category (a) is non-existence; (b) and (c)
    are scope/materiality decisions. Sanitary ceramic ware is MAPPED (needs_human_review=true),
    not skipped, unless your declared boundary explicitly excludes building services.

R2. CANDIDATE COUNT IS VARIABLE — list ALL genuine substitutes, never a fixed number.
    acceptable_uuids = every EPD an LCA reviewer would accept as a valid answer for this
    material, in ranked order, correct_uuid first. Do NOT pad to a target count and do NOT
    truncate valid substitutes. A material may legitimately have 1–2, ~5–12, or (for near-
    identical products) 20+ acceptable EPDs. top_10 = the detailed objects for the top ranked
    candidates, UP TO 10 (fewer if fewer genuine substitutes exist; NEVER padded with non-
    substitutes). It is correct and expected that counts differ between materials.

R3. FUNCTIONAL EQUIVALENCE, not "same classification family". Candidates must be the same
    product TYPE and the same building FUNCTION/FORM as the material. They MAY span different
    ÖKOBAUDAT classification leaves WHEN, and only when, an LCA reviewer would treat them as
    interchangeable for this material — e.g. rigid thermal insulation legitimately spans EPS,
    XPS, mineral wool and PIR (four different leaves). They must NEVER cross FUNCTION or FORM:
    a wall masonry UNIT is not a floor SLAB; structural is not finishing; brick is not concrete
    slab; metal ≠ plastic ≠ glass ≠ wood ≠ mineral (unless the BIM class is demonstrably
    mislabelled — then say which and why). Use the classification leaf to FIND candidates; use
    functional substitutability to ACCEPT them.

R4. rank 1 = correct_uuid = the single closest-named / closest-function EPD. Assign every
    candidate a relevance grade:
        3 = exact / canonical (the dataset a careful human calls "the same thing");
        2 = strong substitute (same function & product type, accepted without hesitation);
        1 = acceptable but looser (same function / material class, accepted as a fallback).
    Only grade-1-or-higher items may appear; rank by grade then closeness.

R5. Prefer GENERIC / representative / category-average datasets (use dataset_type) and, where a
    real choice exists, ENGLISH-named datasets, for rank 1 — for generalisability and language
    coherence. ÖKOBAUDAT is German, so some names are unavoidably German (e.g. ready-mix
    "Beton C20/25"); that is acceptable ONLY when no English-named equivalent exists, and every
    candidate in the list must still be the same product type. Never mix unrelated products.

R6. Tie-breakers, in order: concrete strength class (convert cylinder MPa → C-class: ~24 MPa →
    C20/25, ~30 → C25/30, ~35 → C30/37), bulk density (kg/m³), thermal conductivity, and host-
    element function (wall / floor / roof / column / door / window / stair). State which
    tie-breaker decided rank 1 in lca_reasoning.

R7. Use ONLY real cache UUIDs; quote dataset name and classification EXACTLY as in the cache;
    verify each UUID exists. The invariant correct_uuid == top_10[0].uuid == acceptable_uuids[0]
    MUST hold for every mapped material.

R8. Set confidence in [0,1]; set needs_human_review=true ONLY when the call is genuinely
    borderline (ambiguous appearance-named metal, a substance with no exact family, a
    name/description conflict, or a scope-boundary judgement such as sanitary ware). Do NOT
    over-flag obvious canonical matches.

R9. Near-identical families: if a family holds many essentially-identical datasets (e.g. the
    same ready-mix concrete from different plants), list them ALL in acceptable_uuids (each is
    a valid answer), choose one representative as rank 1, and note this in the rationale.

════════════════════════════════════════════════════════════════════════
METHOD (per material, from scratch — no anchoring to any previous version)

  1. Read what it physically IS (name, class, description, host elements, density, strength,
     conductivity). Decide MAP or SKIP against the R1 boundary; if SKIP, choose category (a)/(b)/(c).
  2. If MAP: name the real product; find its ÖKOBAUDAT classification leaf in cache_index.tsv
     (translate the German path to English to confirm). Pull the whole family, plus any sibling
     leaves needed for genuine functional substitutes (R3).
  3. Grade and rank every genuine substitute (R4–R6); rank 1 = correct. Keep ALL grade-≥1
     items in acceptable_uuids (R2); detail the top ≤10 in top_10.
  4. Write the entry (schema below): a 1–3 sentence lca_reasoning naming the substance, the
     chosen family/function, and the deciding tie-breaker; a one-line rationale per top_10 item.
  5. If SKIP: write skip_reason (prefixed with its category); correct_uuid=null,
     acceptable_uuids=[], top_10=[].

════════════════════════════════════════════════════════════════════════
OUTPUT

Write ONE JSON file named (do NOT overwrite any existing v0.x file; do NOT edit _settings.json —
a human will merge the three models' outputs and wire up the final file):
   LCiA.tab\Database.panel\Ökobaudat Connector.pushbutton\sample_project_a2_sphera_en_v0.6__<YOUR-MODEL-ID>.json
(replace <YOUR-MODEL-ID> with your own model name+version, e.g. ...v0.6__opus-4.8.json).

Top-level: { version:"0.6-snowdon-expert", annotator_model:"<your model id>", annotation_date,
source_bim, source_revit_model:"Snowdon Towers Sample Architectural.rvt", datastock:"a2_sphera",
language:"en", dataset_count:2383, matched_count, skipped_count, needs_human_review_count,
system_boundary:"<one sentence stating IN/OUT>", methodology, ranking_basis, metric_target,
scope_notes, entries:[ ... ] }

MATCH entry:
{ revit_id, name, description, class, host_categories:[...], host_types:[...],
  physical_asset:{...}, thermal_asset:{...}, label:"match",
  expected_classification_path:"<German path of correct_uuid>",
  lca_reasoning:"...", confidence:0.0-1.0, needs_human_review:false,
  correct_uuid:"<uuid>",
  acceptable_uuids:[ correct first, then ALL other genuine substitutes in ranked order ],
  top_10:[ {rank, uuid, name (exact), classification (exact), relevance:3|2|1, rationale}, ... up to 10 ] }

SKIP entry: same metadata fields, plus label:"skip",
skip_reason:"[<category a|b|c>] <precise reason>",
expected_classification_path:null, correct_uuid:null, acceptable_uuids:[], top_10:[].

════════════════════════════════════════════════════════════════════════
WORKED EXAMPLES (match this quality and coherence)

GOOD MAP — single-family — "Brick, Common" (fired-clay masonry unit, wall/paving use):
  correct = "Brick (unfilled)" [grade 3]. All substitutes are bricks / masonry units from
  "… / Steine und Elemente / Ziegel" (+ "Kalksandstein"): Facing brick clay-based [2], Facing
  bricks [2], Facade clinker [2], Brick (filled w/ insulating material) [1], Plan bricks [1],
  Brick Slips [1], Pavers [1], Sand-lime brick [1]. NOTHING from concrete slabs, mortars or
  screeds. (Here ~9 genuine substitutes exist — list exactly those, not a padded 10.)

GOOD MAP — legitimate CROSS-family — "Rigid insulation" (rigid foam board):
  correct = the closest rigid foam board [3]; substitutes span FOUR leaves because they are
  functionally interchangeable thermal-insulation boards: XPS [3/2], EPS rigid foam [2],
  mineral-wool board [2], PIR/PUR board [2]. Different classification leaves, same function —
  allowed by R3.

GOOD SKIP — "Air" — the ONE clean non-material. Revit represents an empty wall cavity / air gap
  as a "material", but it is just empty space: nothing is manufactured or consumed and it carries
  zero embodied impact.
  skip_reason: "[not a material] Air gap / cavity — empty space modelled as a material by Revit.
  No product is consumed, so it carries zero embodied impact and is excluded from BIM→EPD mapping."
  correct_uuid=null, acceptable_uuids=[], top_10=[].
  NOTE the other two SKIP categories are SCOPE decisions, NOT "non-products" (those items are real
  products that may have EPDs): (b) paint / colour / plating appearance finishes — excluded as
  negligible surface finish; (c) plumbing fittings, lighting fixtures, appliances, furniture,
  site/landscape, PV — excluded as building-services / loose equipment, outside the fabric boundary.
  Sanitary ceramic ware (toilets, washbasins) is the borderline case and is MAPPED (to "Sanitary
  ceramic" / "Toilets" / "Washbasins"), needs_human_review=true — NOT skipped.

════════════════════════════════════════════════════════════════════════
SELF-CHECK before finishing (report results):
  • Every mapped material: rank 1 grade 3; candidates ranked by grade then closeness; ALL
    genuine substitutes listed (none padded, none truncated); top_10 ≤ 10 items.
  • Spot-read 10 random lists: confirm zero cross-FUNCTION/FORM items (no wall-block↔slab,
    no metal↔plastic, etc.); cross-leaf items appear only where genuinely interchangeable.
  • correct_uuid == top_10[0].uuid == acceptable_uuids[0] everywhere; every UUID exists in cache.
  • Every SKIP has a category tag (a/b/c) and a precise reason; no SKIP carries candidates.
    Confirm sanitary ceramic ware is MAPPED, not skipped. matched_count + skipped_count == 145.
  • List every needs_human_review material with one line on why.

Take as long as you need; accuracy and coherence beat speed. State explicitly any material where
no coherent set of genuine substitutes exists rather than forcing or padding one.
