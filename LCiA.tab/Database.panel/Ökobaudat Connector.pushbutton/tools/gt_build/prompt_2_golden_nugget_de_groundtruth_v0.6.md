# Prompt 2 — BIM_Projekt_Golden_Nugget (German) ÖKOBAUDAT ground-truth build (v0.6)

# ROLE
You are a senior LCA / building-materials expert curating a RETRIEVAL GROUND TRUTH
for a German BIM model (Autodesk "BIM_Projekt_Golden_Nugget"). For each Revit
material you decide MAP or SKIP, and for every MAP you pick the single closest
ÖKOBAUDAT EPD plus a graded set of genuine substitutes. The materials AND the EPDs
are both in GERMAN — you must be fluent in German construction terminology
(Komposita) and match German to German. Work like a human annotator, not a search
engine: reason from what each material physically IS, not from keyword overlap.

# INPUTS (read these, do not guess)
1. BIM materials (88 entries, the work-list):
   LCiA.tab/Context.panel/ExtractContext.pushbutton/data/materials_context.json
   — a JSON list; each entry has: revit_id, name, description, class,
     host_elements{categories[], types[]}, physical_asset{density_kg_m3,
     concrete_compression_mpa, lightweight, structural_class},
     thermal_asset{thermal_conductivity_w_mk, density_kg_m3}.
2. ÖKOBAUDAT candidate corpus (the ONLY source of UUIDs — German A2 Sphera):
   LCiA_Extension_Cache/ds_cache_v2_a2_sphera_de.json
   — {"results":[{uuid, name, classification, dataset_type, owner,
     compliance, location, valid_until, epd_no}, ...]} ; 2,341 datasets.
   Names are GERMAN; classification paths are GERMAN (e.g.
   "Mineralische Baustoffe / Steine und Elemente / Ziegel").
   dataset_type ∈ {generic, average, representative, specific, template}.
3. To enumerate candidate pools, EITHER point tools/gt_build/_dump_indexes.py at the
   DE cache (change its one CACHE path to ds_cache_v2_a2_sphera_de.json) to emit a
   uuid/name/classification TSV sorted by classification, OR Grep the DE cache
   directly by German classification family + product terms. Both are fine.
4. Schema reference: an existing v0.3/v0.6 ground-truth JSON
   (sample_project_a2_sphera_en_v0.6__opus-4.8.json) — copy its field layout exactly.

# INDEPENDENCE
Build candidate pools by ÖKOBAUDAT CLASSIFICATION FAMILY (taxonomy-driven), NOT by
running the plugin's BM25F / semantic search. This GT is the control the plugin's
retrieval is later measured against; it must not be derived from that retrieval.

# GERMAN TERMINOLOGY (so Komposita are not misread)
Ortbeton = cast-in-place concrete · Beton C25/30 / C30/37 = ready-mix strength class
· Betonfertigteil = precast element · Mauerwerk / Mauerziegel = masonry / clay brick
· Hochlochziegel / Vollziegel = perforated / solid brick · Vormauerziegel, Klinker,
Fassadenklinker = facing brick / clinker · Pflasterziegel = clay PAVER (horizontal,
≠ wall brick) · Dachziegel = ROOF tile (≠ wall brick) · Kalksandstein (KS) =
sand-lime brick · Porenbeton = AAC · Leichtbeton = lightweight concrete ·
Dämmung/Dämmstoff = insulation · EPS-Hartschaum / XPS / PUR-PIR / Mineralwolle /
Holzfaser = rigid-foam / extruded-PS / polyurethane / mineral-wool / wood-fibre
insulation · WDVS = ETICS/EIFS · Konstruktionsvollholz (KVH) = structural solid
timber · Brettschichtholz (BSH) = glulam · Brettsperrholz (BSP/CLT) = cross-laminated
timber · Sperrholz = plywood · Spanplatte = chipboard · OSB-Platte = OSB ·
Gipskarton-/Gipswandbauplatte = gypsum board · Gipsfaserplatte = gypsum fibreboard ·
Dampfsperre / Dampfbremse = vapour barrier / retarder · (Zement)Estrich = (cement)
screed · Heizestrich = heated screed · Trittschalldämmung = impact-sound insulation ·
Putz / Mörtel = plaster / mortar · Baustahl / Stahlprofil / Stabstahl = structural
steel / section / bar · S235 = structural-steel grade · Betonstahl / Bewehrungsstahl
/ "BSt 500" = reinforcing steel (rebar) · Edelstahl = stainless · Aluminiumblech /
-profil = aluminium sheet / profile · Bitumenbahn / Dachbahn = bitumen sheet /
roofing membrane · Fliesen / Feinsteinzeug = tiles / porcelain stoneware · Naturstein
= natural stone · Luft / Luftschicht = air / air cavity.

# SYSTEM BOUNDARY (R1)
IN = building fabric: structure, envelope, partitions, floor/roof/wall build-ups,
glazing, insulation, waterproofing/membranes, masonry, screed, plaster, tile, doors
that close the envelope/partitions — PLUS sanitary ceramic ware (WCs, washbasins).
OUT = MEP/building services, loose equipment/appliances, furniture/casework, site &
landscape furnishings, photovoltaics, pure appearance/paint finishes, and Revit/
software placeholders.
Every SKIP carries one of THREE category tags (prefix the skip_reason with it):
  [not a material]          — no embodied substance: Luftschicht (air cavity);
                              Revit/Autodesk render placeholders ("Autodesk - Logo /
                              Fuge / Gehäuse"); graphic-fill conventions.
  [negligible appearance finish] — a real product, excluded as materiality/scope:
                              paint, lacquer, stain, road-marking ("Farbe -
                              Straßenmarkierung", "Lack - weiß"), thin colour coats.
  [outside building-fabric boundary] — a real product, outside scope: MEP, fixtures,
                              appliances, furniture, textiles, site/landscape, PV,
                              and CONTEXT MASSING of neighbouring buildings
                              ("Umgebung - Gebäude *").
Sanitary ceramic ware is MAPPED (not skipped) with needs_human_review=true.
A finish-named metal/wood on a fabric host maps to its BULK substance (e.g.
"Metall - Stahl schwarz" on a fabric element → steel section); the SAME finish on a
fixture/appliance/furniture/site host is SKIPPED [outside…]. When a real bulk
substance exists within the fabric, prefer MAP over SKIP.

# RANKING & ACCEPTANCE RULES
R2  acceptable_uuids is VARIABLE-LENGTH: list every genuine substitute, no fixed
    count, no padding, no truncation. correct_uuid is element 0.
R3  FUNCTIONAL EQUIVALENCE governs acceptance — candidates may span different
    classification leaves ONLY when an LCA reviewer would treat them as
    interchangeable for this use (e.g. EPS↔XPS↔Mineralwolle for a generic rigid
    insulation board). NEVER across function or form (a wall brick ≠ a floor slab ≠
    a roof tile ≠ a paver). Cross-family inclusions must be graded ≤2.
R4  GRADED relevance per candidate: 3 = exact match (same product/function/form),
    2 = strong substitute, 1 = looser-but-defensible. rank-1 is always relevance 3.
R5  Prefer GENERIC / REPRESENTATIVE / AVERAGE (dataset_type) over single-vendor
    SPECIFIC datasets for rank 1. Where several equally-generic datasets exist,
    prefer the one whose German name most directly matches the material. (Note:
    a brand-sounding name can still be dataset_type "average" — judge by the field,
    not the name.)
R6  Use ONLY real uuid strings copied verbatim from the DE cache. No invented UUIDs.
R7  INVARIANT: correct_uuid == top_10[0].uuid == acceptable_uuids[0].
R8  top_10 = the first up-to-10 of acceptable_uuids, each with rank, uuid, name and
    classification copied VERBATIM from the DE cache (byte-for-byte), relevance, and a
    one-line German-or-English rationale.
R9  List near-identical families IN FULL in acceptable_uuids — e.g. every plant-
    specific "Beton C30/37 …" of the matching strength class, every "Mineralwolle
    (…-Dämmung)" variant. Completeness over brevity for true equivalents.
Tie-breakers when ranking: concrete strength class (MPa → C-class: 24→C20/25,
30→C25/30, 35→C30/37), bulk density, thermal conductivity, host-element function,
rigidity ("hart" → rigid foam, "weich" → flexible mat).

# METHOD (per material, independently)
1. Read what it physically is (name + description + class + host categories/types +
   density + MPa + λ). Use the host to disambiguate finishes and scope.
2. Decide MAP or SKIP against the boundary (R1). Skipping is a valid, honest outcome.
3. For MAP: enumerate the candidate pool from the ÖKOBAUDAT classification family;
   pick correct_uuid (R5); add all genuine substitutes graded 3/2/1 (R2–R4, R9);
   set expected_classification_path = the DE-cache classification of correct_uuid;
   write a real lca_reasoning; honest confidence ∈ [0,1]; needs_human_review=true
   only when genuinely borderline (finish-on-fabric, sanitary ware, ambiguous class).
4. For SKIP: null the path/correct_uuid, empty acceptable_uuids/top_10, write a
   precise [category]-tagged skip_reason.

# OUTPUT
Write ONE new file (do NOT overwrite the Snowdon EN file, do NOT edit _settings.json):
  Database.panel/Ökobaudat Connector.pushbutton/golden_nugget_a2_sphera_de_v0.6__<MODEL-ID>.json
where <MODEL-ID> ∈ {opus-4.8, gpt-5.5, gemini-3.1}. Also write a copy into the
Validation.pushbutton folder. Top-level header:
  version "0.6-golden-nugget-expert", annotator_model, annotation_date,
  source_bim (the context path), datastock "a2_sphera", language "de",
  dataset_count, matched_count, skipped_count, needs_human_review_count,
  system_boundary (one paragraph), methodology (one paragraph), ranking_basis (list),
  metric_target, scope_notes, entries[].
Each MAP entry: revit_id, name, description, class, host_categories[], host_types[]
  (flatten host_elements.categories / .types), physical_asset, thermal_asset,
  label "match", expected_classification_path, lca_reasoning, confidence,
  needs_human_review, correct_uuid, acceptable_uuids[] (correct first),
  top_10[]{rank, uuid, name, classification, relevance(3|2|1), rationale}.
Each SKIP entry: same identity/host fields, label "skip", expected_classification_path
  null, correct_uuid null, acceptable_uuids [], top_10 [], skip_reason "[category] …".

# WORKED EXAMPLES (real DE-cache names/UUIDs — verify each before committing)
1) MAP, single family — "Mauerwerk - ohne Dämmeigenschaften" (class Beton/Masonry):
   a plain clay-brick wall. correct = "Mauerziegel (ungefüllt)"
   30514538-… (rel 3). acceptable += "Vormauerziegel" 1401638f,
   "Vormauerziegel und Klinker" 127a23f5, "Fassadenklinker" 1a365e34 (rel 2,
   facing-brick variants), "Kalksandstein" cc0d7baa (rel 2, masonry unit, same
   walling function). EXCLUDE "Pflasterziegel und Pflasterklinker" feaaff18 (a clay
   PAVER — horizontal, different form; at most rel 1) and "Dachziegel" 592ffe6e (ROOF
   tile — different function). Contrast: "Mauerwerk - mit Dämmeigenschaften" (host
   carries thermal mass/λ) → correct = "Mauerziegel (mit Dämmstoff gefüllt)" ccee13d3
   (the insulation-filled brick), with "(mit Polystyrol gefüllt)" b822aa09 as rel 2.

2) MAP, cross-family by FUNCTION — "Dämmung - hart" (rigid insulation board):
   correct = "EPS-Hartschaumdämmung" 121c71e8 (rel 3). acceptable +=
   "Extrudierter Polystyrol Dämmstoff (XPS)" c4ddfdbc (rel 2),
   "Holzfaserdämmplatte (Nassverfahren)" c91aa879 (rel 1),
   "Mineralwolle-Dämmstoff im hohen Rohdichtebereich" 155a3b83 (rel 1) — different
   chemistries, all RIGID boards, interchangeable for a generic rigid-insulation
   layer (R3). Contrast: "Dämmung - weich" (flexible mat — λ/density low, "weich") →
   correct = "Mineralwolle (Innenausbau-Dämmung)" a5b22bbe or "(Fassaden-Dämmung)"
   50d421e2, since a soft mat is not a rigid foam — tie-breaker is rigidity, and R9
   says list the full "Mineralwolle (…-Dämmung)" variant set in acceptable.

3) SKIP — "Luftschicht": null everything, skip_reason =
   "[not a material] Air cavity (Luftschicht) — empty space Revit models as a
   material; zero embodied substance." Model-specific siblings to skip the same way:
   "Umgebung - Gebäude *" (the 7 coloured neighbouring-context massing blocks) →
   "[outside building-fabric boundary] context massing of surrounding buildings";
   "Autodesk - Logo / Fuge / Gehäuse" → "[not a material] software render
   placeholder"; "Farbe - Straßenmarkierung" / "Lack - weiß" →
   "[negligible appearance finish] thin paint/lacquer colour coat, no bulk substance."

# SELF-CHECK before you finish
- matched_count + skipped_count == 88 (every material decided exactly once).
- Every uuid (correct, acceptable, top_10) exists VERBATIM in ds_cache_v2_a2_sphera_de.json.
- R7 invariant holds for every MAP. rank-1 relevance == 3 everywhere.
- top_10 names/classifications are byte-for-byte the DE-cache strings.
- Every SKIP skip_reason starts with one of the three [category] tags.
- No fixed-length acceptable sets; no padding; no cross-function/form items graded 3.
- needs_human_review set honestly (expect it on finish-on-fabric, sanitary ware,
  bronze/brass, terrazzo-like, and any ambiguous "Kunststoff/Metall" colour entries).
