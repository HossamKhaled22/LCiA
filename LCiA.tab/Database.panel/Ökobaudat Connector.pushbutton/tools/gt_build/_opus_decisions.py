# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Hossamelden Elmalah
# SPDX-License-Identifier: Apache-2.0
"""Opus v0.6 expert decisions for all 145 Snowdon Towers BIM materials.

Format per revit_id:
  SKIP:  {"label":"skip", "skip_reason": "[a|b|c] ..."}
  MATCH: {"label":"match", "lca_reasoning": "...", "confidence": float, "review": bool,
          "picks": [(uuid, grade, rationale), ...],   # ordered, correct first -> top_10 (cap 10) + head of acceptable
          "expand": [ {"name_regex": r"...", "leaf": "<exact DE leaf>", "types":[...]}, ... ] }  # optional, append to acceptable only

UUIDs are copied verbatim from tools/gt_build/_query.py dumps of ds_cache_v2_a2_sphera_en.json.
The assembler pulls exact name/classification from the cache by UUID, so only UUIDs must be right here.
"""

# ---- reusable family rationale snippets ----
SUB = "Same product family / genuine functional substitute."

DECISIONS = {

# ============================== GLASS ==============================
18439: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Soda-lime float glass in curtain panels/windows/storefront. Closest-named canonical is a generic single window-glass pane; flat float glass and insulating glazing units (double/triple) are the assembled substitutes. The four plastic 'Transparent board' (PC/PMMA/PVC) and the opaque glass-ceramic share neighbouring leaves but are NOT glass and are excluded. Tie-breaker: closest-named generic flat glass.",
  "picks":[
    ("dcf38066-e336-46a7-b0a8-b2453dd2872d",3,"Generic single window-glass pane - the most direct EPD for bare 'Glass'/'Soda Lime Glass'."),
    ("ec8ac2c5-9c5f-4ae5-b6f2-c160e878a872",3,"Uncoated flat (float) glass = soda-lime base glass."),
    ("e94dbfcc-56f2-4e55-8e0e-c2e8a2b576ae",2,"Float glass (BV Flachglas) - same flat-glass product."),
    ("4e6d12b9-af77-451e-a69b-d9cb499e650d",2,"Float glass (PRESS GLASS) - same flat-glass product."),
    ("11fdd0b3-1a9a-43f5-a701-40cec9935d09",2,"Coated flat glass - flat soda-lime glass with coating."),
    ("a835bc20-b9fe-4112-bf74-32caa85712c8",2,"Toughened safety glass - processed flat glass."),
    ("f7f14041-760f-4984-b5ae-e5f0033f384e",2,"Laminated safety glass - processed flat glass."),
    ("d7bf4202-d1a8-4f3e-8861-c3d78cd2bf70",2,"Insulated glazing, double pane - assembled glazing unit."),
    ("ba58d8b9-945b-4428-b4e0-aecda8b3df18",2,"Insulated glazing, triple pane - assembled glazing unit."),
    ("1fd9f4eb-e087-49ba-a09d-a5f9f7162c42",2,"Insulating glass unit (double) - assembled glazing."),
    ("c07b71e8-111b-4093-b8d8-a521c03c0c99",2,"Insulating glass unit (triple) - assembled glazing."),
    ("5b8126fe-6d23-4a60-95b1-0fb75b343398",2,"Laminated safety glass (Guardian) - processed flat glass."),
    ("9b7f821b-e6db-450b-b23e-e41f1feab9e0",2,"Toughened safety glass (PRESS GLASS) - processed flat glass."),
    ("e39ded2d-dd75-492a-8a24-be3e2403adf2",1,"Laminated safety glass (PRESS GLASS) - processed flat glass."),
    ("7d80823c-5318-4bb8-9976-533ebe3c1dd9",1,"Multi-pane insulating glass (3-pane) - assembled glazing."),
    ("7ca6b663-d17e-42dd-9cad-8b4df788c352",1,"Multi-pane insulating glass (3-pane) - assembled glazing."),
  ]},

# ============================== CONCRETE ==============================
18939: {"label":"match","confidence":0.9,"review":False,
  "lca_reasoning":"Cast-in-place structural grey concrete; 24.1 MPa cylinder -> C20/25. Rank 1 = generic ready-mix C20/25; every same-strength ready-mix EPD (generic, average and the many plant-specific ECOPact recipes) is a valid substitute (R9), with adjacent strength classes as looser fallbacks. Tie-breaker = strength class C20/25.",
  "picks":[
    ("e3d7a045-ee91-43ac-8fb4-a5216c65ba0b",3,"Generic ready-mix concrete C20/25 - exact strength, cast-in-place."),
    ("d5d98d4b-a9ba-4fb3-b2d2-6766f8ef5a59",3,"Category-average concrete C20/25 - exact strength."),
    ("8f11c179-e367-4833-93f3-10597923c79c",2,"Recycling ready-mix concrete C20/25 - same strength, recycled aggregate."),
  ],
  "expand":[
    {"name_regex":r"C20/25","leaf":"Mineralische Baustoffe / Mörtel und Beton / Beton","types":["specific"]},
    {"name_regex":r"(C16/20|C25/30)","leaf":"Mineralische Baustoffe / Mörtel und Beton / Beton","types":["average","generic"]},
  ]},

18940: {"label":"match","confidence":0.75,"review":False,
  "lca_reasoning":"Precast concrete used in floor build-ups; 34.5 MPa -> C30/37. Rank 1 = generic precast concrete (floor/ceiling) slab; precast wall/stairs and same-strength C30/37 ready-mix are the substitutes (precast EPDs carry the precasting process, ready-mix is the same concrete cast in situ). Tie-breaker = precast form + C30/37.",
  "picks":[
    ("45e6dcac-70b9-4982-bf04-4ff566facba1",3,"Generic precast concrete slab (ceiling/floor) - precast concrete, floor element."),
    ("97b971c7-8d43-4650-bfd6-75fdcf9a2101",2,"Generic precast concrete wall - same precast concrete product."),
    ("20b0761f-99e0-48db-b1e4-0fdb1b7739ef",2,"Generic precast concrete part (stairs) - same precast concrete product."),
    ("b3fb0ba9-2376-49bf-b21a-7f7a5cd97233",2,"Category-average concrete C30/37 - matching strength concrete."),
    ("d6f982e3-beda-49f0-a298-694fcbf3ba38",2,"Generic ready-mix C30/37 - same-strength concrete cast in situ."),
  ],
  "expand":[
    {"name_regex":r"C30/37","leaf":"Mineralische Baustoffe / Mörtel und Beton / Beton","types":["specific"]},
  ]},

1406787: {"label":"match","confidence":0.72,"review":False,
  "lca_reasoning":"Precast smooth light-grey concrete; floor finishes + a few site planters; 34.5 MPa -> C30/37. Rank 1 = generic precast concrete slab; precast wall/stairs and C30/37 ready-mix as substitutes. Tie-breaker = precast form + C30/37.",
  "picks":[
    ("45e6dcac-70b9-4982-bf04-4ff566facba1",3,"Generic precast concrete slab - precast concrete floor element."),
    ("97b971c7-8d43-4650-bfd6-75fdcf9a2101",2,"Generic precast concrete wall - same precast product."),
    ("20b0761f-99e0-48db-b1e4-0fdb1b7739ef",2,"Generic precast concrete stair part - same precast product."),
    ("b3fb0ba9-2376-49bf-b21a-7f7a5cd97233",2,"Category-average concrete C30/37 - matching strength."),
    ("d6f982e3-beda-49f0-a298-694fcbf3ba38",2,"Generic ready-mix C30/37 - same-strength concrete."),
  ],
  "expand":[
    {"name_regex":r"C30/37","leaf":"Mineralische Baustoffe / Mörtel und Beton / Beton","types":["specific"]},
  ]},

2077092: {"label":"match","confidence":0.68,"review":True,
  "lca_reasoning":"Polished concrete floor finish; 34.5 MPa -> C30/37. Name says polished (typically cast-in-place power-floated) while the description says 'Precast concrete' - a name/description conflict, so rank 1 = generic ready-mix C30/37 (in-situ floor) with precast slab and C30/37 plant EPDs as substitutes. Tie-breaker = C30/37.",
  "picks":[
    ("d6f982e3-beda-49f0-a298-694fcbf3ba38",3,"Generic ready-mix C30/37 - in-situ polished concrete floor, matching strength."),
    ("b3fb0ba9-2376-49bf-b21a-7f7a5cd97233",3,"Category-average concrete C30/37 - matching strength."),
    ("45e6dcac-70b9-4982-bf04-4ff566facba1",2,"Generic precast concrete slab - per the 'precast' description."),
    ("97b971c7-8d43-4650-bfd6-75fdcf9a2101",1,"Generic precast concrete wall - same precast concrete."),
  ],
  "expand":[
    {"name_regex":r"C30/37","leaf":"Mineralische Baustoffe / Mörtel und Beton / Beton","types":["specific"]},
  ]},

24202: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Structural lightweight concrete, density 1762 kg/m3, 27.6 MPa, in roof slabs/wall sweeps. OEKOBAUDAT's lightweight-concrete (Leichtbeton) leaf is lightweight-concrete MASONRY blocks, not structural LWC, so no exact match exists. Rank 1 = the lightweight-concrete wall product (closest 'lightweight concrete' substance); lightweight-concrete blocks + matching-strength normal-weight C25/30 ready-mix as substitutes; aerated concrete as a looser low-density option. Tie-breaker = lightweight + ~C25/30. Flagged for review.",
  "picks":[
    ("1f4de7a9-5da3-4924-a2e0-07f04b5a0058",3,"Lightweight-concrete wall (klimaVER) - closest 'lightweight concrete' product."),
    ("a1deced7-6fa9-4266-bc86-da75b3653b06",2,"Lightweight concrete block (natural pumice) - lightweight concrete."),
    ("1c681356-2541-43e7-b1d4-9c4d0a936a08",2,"Expanded-clay hollow lightweight concrete block (1600 kg/m3)."),
    ("a71e60a5-b64b-4715-ae8a-c8c8819149d2",2,"Generic ready-mix C25/30 - matching ~27.6 MPa strength (normal weight)."),
    ("8347f9a7-f4ec-4a36-a266-a0281f5fd16d",2,"Category-average concrete C25/30 - matching strength."),
    ("5c400482-2153-45e8-a4d2-e10a9338909b",1,"Expanded-clay lightweight concrete block (700 kg/m3)."),
    ("e6819670-eb6c-4104-adea-f7f8bb2f49e6",1,"Autoclaved aerated concrete units - low-density mineral alternative."),
  ]},

# ============================== CMU (concrete block) ==============================
18942: {"label":"match","confidence":0.8,"review":False,
  "lca_reasoning":"Concrete masonry units (hollow/solid concrete block), 24 MPa, partition/interior walls. Rank 1 = generic concrete masonry brick (2000 kg/m3 matches). Lightweight-concrete blocks, autoclaved aerated concrete units and sand-lime masonry units are genuine masonry-unit substitutes. Tie-breaker = density / block type. NOTHING from concrete slabs/mortar.",
  "picks":[
    ("2cdcffc6-84e9-4238-b8af-beebabea9a2d",3,"Generic concrete masonry brick (2000 kg/m3) - exact CMU."),
    ("1c681356-2541-43e7-b1d4-9c4d0a936a08",2,"Expanded-clay hollow concrete block - hollow concrete masonry unit."),
    ("5c400482-2153-45e8-a4d2-e10a9338909b",2,"Expanded-clay concrete block (inner wall) - concrete masonry unit."),
    ("23fc083c-960d-4d31-a509-f383e01c3179",2,"Expanded-clay concrete block (outer wall) - concrete masonry unit."),
    ("a1deced7-6fa9-4266-bc86-da75b3653b06",2,"Lightweight concrete block (pumice) - concrete masonry unit."),
    ("2e31cc41-fa94-442a-b19b-48d82c3555d8",1,"Pumice stone block - lightweight concrete masonry unit."),
    ("2ad3b93b-b559-404e-8b63-99c69d472896",1,"Granulated-slag masonry brick - mineral masonry unit."),
    ("1f4de7a9-5da3-4924-a2e0-07f04b5a0058",1,"Lightweight-concrete wall - cast lightweight concrete."),
    ("e6819670-eb6c-4104-adea-f7f8bb2f49e6",1,"Autoclaved aerated concrete units - aerated masonry unit."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit (different binder)."),
    ("c916ceb3-a44d-40b7-8984-861afa589956",1,"Sand-lime brick (generic) - masonry unit."),
  ]},

435571: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"Split-face concrete masonry units (retaining wall); split-face is a surface texture on a standard concrete block. 24 MPa. Rank 1 = generic concrete masonry brick; lightweight-concrete blocks + AAC + sand-lime as masonry-unit substitutes. Tie-breaker = concrete block.",
  "picks":[
    ("2cdcffc6-84e9-4238-b8af-beebabea9a2d",3,"Generic concrete masonry brick - exact CMU (split-face = surface texture)."),
    ("1c681356-2541-43e7-b1d4-9c4d0a936a08",2,"Expanded-clay hollow concrete block - concrete masonry unit."),
    ("5c400482-2153-45e8-a4d2-e10a9338909b",2,"Expanded-clay concrete block - concrete masonry unit."),
    ("a1deced7-6fa9-4266-bc86-da75b3653b06",2,"Lightweight concrete block (pumice) - concrete masonry unit."),
    ("1f4de7a9-5da3-4924-a2e0-07f04b5a0058",1,"Lightweight-concrete wall - cast lightweight concrete."),
    ("e6819670-eb6c-4104-adea-f7f8bb2f49e6",1,"Autoclaved aerated concrete units - aerated masonry unit."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit."),
  ]},

# ============================== BRICK (fired clay) ==============================
18941: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Common fired-clay brick (used here in a brick walk). OEKOBAUDAT 'Ziegel' clay bricks; density 1550-1950, lambda 0.54 consistent with solid/perforated brick. Rank 1 = generic unfilled brick; all clay masonry units (+ sand-lime) are substitutes. NOTHING from concrete slabs/mortar/screed. Tie-breaker = unfilled solid brick.",
  "picks":[
    ("30514538-fcb4-483b-b5d5-c108d2037536",3,"Generic unfilled clay brick - exact canonical for 'Common brick'."),
    ("ccee13d3-1e5b-41e2-a867-0d844ee6c1bf",2,"Brick filled with insulating material - clay masonry unit."),
    ("1401638f-b9d7-4b33-9444-b072e2756f49",2,"Facing brick, clay-based - clay masonry unit."),
    ("127a23f5-d954-41c9-8f8a-ecca18aca691",2,"Facing bricks - clay masonry unit."),
    ("1a365e34-0b95-4c5c-973d-11a038eb7414",2,"Facade clinker - fired clay masonry unit."),
    ("feaaff18-9f56-437b-aef1-3f63928c1138",2,"Clay pavers - clay masonry unit (paving)."),
    ("b822aa09-3cb6-49a3-98f6-a3ebf325a70b",1,"Plan bricks (polystyrene-filled) - clay masonry unit."),
    ("a9141bc2-9a57-40cc-9a5d-f6345af7bf4e",1,"Brick slips - thin clay facing brick."),
    ("2ef1b9f0-3e4f-4bca-9ea0-a26a05bd039a",1,"Urban cladding tile (clay) - fired clay unit."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit (different binder)."),
    ("c916ceb3-a44d-40b7-8984-861afa589956",1,"Sand-lime brick (generic) - masonry unit."),
  ]},

1660456: {"label":"match","confidence":0.8,"review":False,
  "lca_reasoning":"Common fired-clay brick laid as a soldier course in a brick walk/paving. Rank 1 = generic unfilled brick; clay pavers/facing bricks rank high for the paving function; all clay masonry units are substitutes. Tie-breaker = clay brick.",
  "picks":[
    ("30514538-fcb4-483b-b5d5-c108d2037536",3,"Generic unfilled clay brick - canonical common brick."),
    ("feaaff18-9f56-437b-aef1-3f63928c1138",2,"Clay pavers - paving brick (matches the brick-walk use)."),
    ("127a23f5-d954-41c9-8f8a-ecca18aca691",2,"Facing bricks - clay masonry unit (facing)."),
    ("1401638f-b9d7-4b33-9444-b072e2756f49",2,"Facing brick, clay-based - clay masonry unit."),
    ("1a365e34-0b95-4c5c-973d-11a038eb7414",2,"Facade clinker - fired clay unit."),
    ("ccee13d3-1e5b-41e2-a867-0d844ee6c1bf",1,"Brick filled with insulating material - clay masonry unit."),
    ("a9141bc2-9a57-40cc-9a5d-f6345af7bf4e",1,"Brick slips - thin clay facing."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit."),
  ]},

1996715: {"label":"match","confidence":0.82,"review":False,
  "lca_reasoning":"Fired-clay facing/running-bond brick on a wall pilaster (density 1950). Rank 1 = generic unfilled brick; facing bricks/clinker rank high for the wall-facing function; all clay masonry units are substitutes. Tie-breaker = clay facing brick.",
  "picks":[
    ("30514538-fcb4-483b-b5d5-c108d2037536",3,"Generic unfilled clay brick - canonical common brick."),
    ("127a23f5-d954-41c9-8f8a-ecca18aca691",2,"Facing bricks - clay masonry unit (running bond)."),
    ("1401638f-b9d7-4b33-9444-b072e2756f49",2,"Facing brick, clay-based - clay masonry unit."),
    ("1a365e34-0b95-4c5c-973d-11a038eb7414",2,"Facade clinker - fired clay facing unit."),
    ("ccee13d3-1e5b-41e2-a867-0d844ee6c1bf",1,"Brick filled with insulating material - clay masonry unit."),
    ("feaaff18-9f56-437b-aef1-3f63928c1138",1,"Clay pavers - clay masonry unit."),
    ("a9141bc2-9a57-40cc-9a5d-f6345af7bf4e",1,"Brick slips - thin clay facing."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit."),
  ]},

1999690: {"label":"match","confidence":0.82,"review":False,
  "lca_reasoning":"Fired-clay facing/running-bond brick on a wall pilaster (grey, density 1950). Identical treatment to the brown running brick. Rank 1 = generic unfilled brick; facing bricks/clinker high; all clay masonry units as substitutes.",
  "picks":[
    ("30514538-fcb4-483b-b5d5-c108d2037536",3,"Generic unfilled clay brick - canonical common brick."),
    ("127a23f5-d954-41c9-8f8a-ecca18aca691",2,"Facing bricks - clay masonry unit (running bond)."),
    ("1401638f-b9d7-4b33-9444-b072e2756f49",2,"Facing brick, clay-based - clay masonry unit."),
    ("1a365e34-0b95-4c5c-973d-11a038eb7414",2,"Facade clinker - fired clay facing unit."),
    ("ccee13d3-1e5b-41e2-a867-0d844ee6c1bf",1,"Brick filled with insulating material - clay masonry unit."),
    ("feaaff18-9f56-437b-aef1-3f63928c1138",1,"Clay pavers - clay masonry unit."),
    ("a9141bc2-9a57-40cc-9a5d-f6345af7bf4e",1,"Brick slips - thin clay facing."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit."),
  ]},

2282240: {"label":"match","confidence":0.75,"review":False,
  "lca_reasoning":"Up-cycled (reclaimed) clay brick paving border on a roof terrace. No recycled-brick-specific EPD, so rank 1 = generic unfilled clay brick with clay pavers ranked high for the paving function; all clay masonry units as substitutes. Tie-breaker = clay paving brick.",
  "picks":[
    ("30514538-fcb4-483b-b5d5-c108d2037536",3,"Generic unfilled clay brick - canonical for up-cycled common brick."),
    ("feaaff18-9f56-437b-aef1-3f63928c1138",2,"Clay pavers - paving brick (matches the paving-border use)."),
    ("127a23f5-d954-41c9-8f8a-ecca18aca691",2,"Facing bricks - clay masonry unit."),
    ("1401638f-b9d7-4b33-9444-b072e2756f49",2,"Facing brick, clay-based - clay masonry unit."),
    ("1a365e34-0b95-4c5c-973d-11a038eb7414",1,"Facade clinker - fired clay unit."),
    ("a9141bc2-9a57-40cc-9a5d-f6345af7bf4e",1,"Brick slips - thin clay facing."),
    ("cc0d7baa-755a-4a4a-baf3-4fe53d68a041",1,"Sand-lime brick - masonry unit."),
  ]},

# ============================== TERRAZZO ==============================
1557292: {"label":"match","confidence":0.5,"review":True,
  "lca_reasoning":"Cementitious terrazzo floor topping (concrete-like, 24 MPa, d 2407). No cement-terrazzo EPD exists; closest is artificial/cast stone (agglomerate). Rank 1 = artificial stone slab; natural-stone slabs and ceramic/stoneware tiles are genuine hard mineral floor-finish substitutes (R3). Flagged for review (no exact terrazzo).",
  "picks":[
    ("514d0f56-13d8-45a3-9267-01be064d6ce3",3,"Artificial/cast stone slab (resin-bound) - closest agglomerate to terrazzo."),
    ("c241605d-34f1-44db-bf07-78610e849edb",2,"Natural stone slab, rigid, indoor - hard mineral floor slab."),
    ("37a63555-2ac1-45e8-b802-6d947f30b99a",2,"Natural stone slab, flexible, indoor - hard mineral floor finish."),
    ("03ed395a-bc7b-4287-9013-8ba3ac1a2900",2,"Marble slab - natural-stone floor finish."),
    ("e2e9d29e-c775-4eef-af57-de8194db7524",1,"Natural stone slab, rigid, facade - stone slab."),
    ("f82e3ab8-d7bb-4765-a60c-bfe78ede1565",1,"Natural stone slab, rigid, outdoor - stone slab."),
    ("5618689c-57a8-4860-85d3-7403ad5ba201",1,"Glazed stoneware tile - hard mineral floor finish."),
    ("91935678-1a09-43e0-8a84-294522407ac3",1,"Unglazed stoneware tile - hard mineral floor finish."),
    ("98a6e52b-6e12-4542-afa7-371740748685",1,"Ceramic tiles - hard mineral floor finish."),
  ]},

# ============================== STEEL sections ==============================
128891: {"label":"match","confidence":0.88,"review":False,
  "lca_reasoning":"Structural steel W-section (ASTM A992, 50 ksi) column. Rank 1 = generic hot-rolled steel section; structural sections/merchant bars (incl. galvanized) and forged/cast steel are substitutes. Tie-breaker = hot-rolled open section.",
  "picks":[
    ("755a481d-a74b-4ba0-b417-cc26767b2d50",3,"Generic steel section - exact for a hot-rolled structural section."),
    ("20c29cbd-f9a2-4518-a1ce-c681246c29c8",3,"Structural steel: sections and merchant bars - structural sections."),
    ("0f0618af-76f4-4f79-bb83-bd4577b5c17b",2,"Hot-dip galvanized structural steel sections - same product, galvanized."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",2,"Galvanized steel profile (steel sections) - structural section."),
    ("27334524-eb79-4ca4-ba63-def20b685a9c",2,"Rolled profile steel structures - fabricated steel sections."),
    ("f848bd34-ac63-4b06-8ff6-080d310e414f",1,"Steel forging part - forged structural steel."),
    ("b8a652dc-48f0-4bc5-b60c-caec8d576fa4",1,"Grey cast iron part - cast ferrous alternative."),
    ("04d1311a-ae38-4bfb-8e11-8992f1ddb14f",1,"Structural steel heavy plates - plate steel for built-up sections."),
  ]},

1106386: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Structural steel (W-sections, structural columns). Rank 1 = generic steel section; structural sections/merchant bars (incl. galvanized) and forged/cast steel as substitutes. Tie-breaker = hot-rolled section.",
  "picks":[
    ("755a481d-a74b-4ba0-b417-cc26767b2d50",3,"Generic steel section - exact for hot-rolled structural steel."),
    ("20c29cbd-f9a2-4518-a1ce-c681246c29c8",3,"Structural steel: sections and merchant bars."),
    ("0f0618af-76f4-4f79-bb83-bd4577b5c17b",2,"Hot-dip galvanized structural steel sections."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",2,"Galvanized steel profile (steel sections)."),
    ("27334524-eb79-4ca4-ba63-def20b685a9c",2,"Rolled profile steel structures."),
    ("f848bd34-ac63-4b06-8ff6-080d310e414f",1,"Steel forging part - forged structural steel."),
    ("04d1311a-ae38-4bfb-8e11-8992f1ddb14f",1,"Structural steel heavy plates."),
    ("b8a652dc-48f0-4bc5-b60c-caec8d576fa4",1,"Grey cast iron part - cast ferrous alternative."),
  ]},

1500923: {"label":"match","confidence":0.6,"review":True,
  "lca_reasoning":"'Wrought iron' / mild-steel trellis member on a wall. Modern decorative wrought-iron is fabricated from mild-steel bar/section; no wrought-iron EPD exists. Rank 1 = steel forging part (forged ferrous), with steel sections and grey cast iron as substitutes. Flagged for review (no exact wrought iron).",
  "picks":[
    ("f848bd34-ac63-4b06-8ff6-080d310e414f",3,"Steel forging part - closest to forged 'wrought iron'."),
    ("755a481d-a74b-4ba0-b417-cc26767b2d50",2,"Generic steel section - mild-steel bar/section trellis member."),
    ("b8a652dc-48f0-4bc5-b60c-caec8d576fa4",2,"Grey cast iron part - cast ferrous alternative."),
    ("20c29cbd-f9a2-4518-a1ce-c681246c29c8",2,"Structural steel sections and merchant bars."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",1,"Galvanized steel profile (sections)."),
  ]},

613942: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"Painted structural/architectural steel used across stairs, stringers, handrail supports, balcony channels and slab edges. The paint is a thin finish; the bulk is steel. Rank 1 = generic steel section; sections + plate as substitutes. Tie-breaker = hot-rolled section/plate.",
  "picks":[
    ("755a481d-a74b-4ba0-b417-cc26767b2d50",3,"Generic steel section - exact for structural/stair steel."),
    ("20c29cbd-f9a2-4518-a1ce-c681246c29c8",3,"Structural steel: sections and merchant bars."),
    ("04d1311a-ae38-4bfb-8e11-8992f1ddb14f",2,"Structural steel heavy plates - stringer/landing plate."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",2,"Galvanized steel profile (sections)."),
    ("0f0618af-76f4-4f79-bb83-bd4577b5c17b",2,"Hot-dip galvanized structural steel sections."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Steel sheet (0.3-30 mm) - plate/sheet for stair components."),
    ("f848bd34-ac63-4b06-8ff6-080d310e414f",1,"Steel forging part - forged steel."),
    ("b8a652dc-48f0-4bc5-b60c-caec8d576fa4",1,"Grey cast iron part - cast ferrous alternative."),
  ]},

# ============================== STEEL sheet / studs / deck / plate ==============================
18949: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Profiled steel roof deck (steel, mill finish). Rank 1 = profiled steel sheet for roof/wall/deck/ceiling (exact); galvanized/plain steel sheet are substitutes; profiled aluminium sheet a looser cross-metal option. Tie-breaker = profiled deck sheet.",
  "picks":[
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",3,"Profiled steel sheet for roof/wall/deck/ceiling - exact metal deck."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",2,"Hot-dip galvanized steel sheet - galvanized deck sheet."),
    ("fb5aca98-636a-4dcb-b0dd-9ed09117486e",2,"Hot-dip galvanized steel sheet (2-20 mm)."),
    ("800dc37d-c3a2-4b12-b14b-54f7bc775f47",2,"Strip-galvanized steel sheet."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet (0.3-30 mm)."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",1,"Profiled aluminium sheet for roof/wall/ceiling - cross-metal deck option."),
  ]},

18946: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"Light-gauge (cold-formed) galvanized steel studs/framing in partitions and rainscreen walls. Cold-formed studs are roll-formed from galvanized steel sheet. Rank 1 = generic hot-dip galvanized steel sheet; galvanized profile, profiled sheet and plain steel sheet as substitutes. Tie-breaker = thin galvanized sheet.",
  "picks":[
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",3,"Hot-dip galvanized steel sheet - substance of cold-formed studs."),
    ("800dc37d-c3a2-4b12-b14b-54f7bc775f47",3,"Strip-galvanized steel sheet - thin galvanized stud stock."),
    ("fb5aca98-636a-4dcb-b0dd-9ed09117486e",2,"Hot-dip galvanized steel sheet (2-20 mm)."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",2,"Galvanized steel profile - cold-formed section."),
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",2,"Profiled steel sheet - roll-formed steel section/sheet."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet (0.3-30 mm)."),
  ]},

18954: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"Light-gauge (cold-formed) galvanized steel furring channels in walls/columns - same substance as drywall studs. Rank 1 = generic hot-dip galvanized steel sheet; galvanized profile/profiled sheet/plain steel sheet as substitutes.",
  "picks":[
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",3,"Hot-dip galvanized steel sheet - substance of cold-formed furring."),
    ("800dc37d-c3a2-4b12-b14b-54f7bc775f47",3,"Strip-galvanized steel sheet."),
    ("fb5aca98-636a-4dcb-b0dd-9ed09117486e",2,"Hot-dip galvanized steel sheet (2-20 mm)."),
    ("42de525f-7c48-4722-b503-6f423b39b4f4",2,"Galvanized steel profile - cold-formed section."),
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",2,"Profiled steel sheet."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet (0.3-30 mm)."),
  ]},

2045804: {"label":"match","confidence":0.62,"review":True,
  "lca_reasoning":"Weathering ('rusted'/corten) steel 1/2-inch plate forming a planter wall (Walls category). No weathering-steel EPD exists; the substance is heavy steel plate. Rank 1 = structural steel heavy plate; steel sheet/plate variants as substitutes. Flagged for review (no corten EPD).",
  "picks":[
    ("04d1311a-ae38-4bfb-8e11-8992f1ddb14f",3,"Structural steel heavy plate - 12 mm plate, mill finish."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet (0.3-30 mm) - covers plate."),
    ("90e9a897-7c0c-4eb5-8229-3a22de2dba71",2,"Heavy plate (scrap-based electric steel)."),
    ("7802251a-10a9-4089-b1ed-c7d449e3845a",2,"Heavy plates (voestalpine)."),
    ("fb5aca98-636a-4dcb-b0dd-9ed09117486e",1,"Hot-dip galvanized steel sheet (2-20 mm)."),
    ("755a481d-a74b-4ba0-b417-cc26767b2d50",1,"Generic steel section - structural steel alternative."),
  ]},

# steel access door
1337227: {"label":"match","confidence":0.5,"review":True,
  "lca_reasoning":"Painted-steel access door leaf (Doors category). Doors are building fabric; the substance is steel. No interior-door EPD exists, so rank 1 = the steel exterior-door product (closest steel door), with the other steel doors and steel sheet (door-leaf substance) as substitutes. Flagged for review (access door / no interior steel-door EPD).",
  "picks":[
    ("55188b3d-4041-4fb7-b0ac-a558e5e49fc0",3,"Steel exterior door (no light cut-out) - closest steel door product."),
    ("54a4dad1-bc25-4384-acf4-87ed9bab0026",2,"Steel exterior door (with light cut-out) - steel door."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet - the door-leaf substance."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",1,"Hot-dip galvanized steel sheet - door-leaf sheet."),
    ("15d41442-f7d9-46fb-9de4-0c7e68a88f69",1,"Sectional (garage) steel door - steel door product."),
  ]},

# ============================== ALUMINIUM ==============================
18953: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Extruded aluminium (6061) curtain-wall mullions and door/frame members. Rank 1 = generic aluminium section/extrusion; mill-finish profile and aluminium sheet as substitutes; cast aluminium and facade systems looser. Tie-breaker = extruded profile.",
  "picks":[
    ("3feca796-791b-46d3-8160-95ef243ffb9d",3,"Generic aluminium section (extrusion) - exact for 6061 mullions."),
    ("0cb92770-9007-48c6-bc03-466af8894419",3,"Aluminium profile, mill finish - extruded mill-finish profile."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",2,"Aluminium sheet - semi-finished aluminium."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",1,"Profiled aluminium sheet - aluminium cladding/sheet."),
    ("d9bfad25-c581-4fe3-b5e3-171ddb59c808",1,"Aluminium die-cast parts - cast aluminium component."),
    ("d409739a-ae3c-4005-8a26-d55fccc1d9e5",1,"Aluminium facade system (Kalzip) - aluminium facade profile."),
    ("7290c768-0a2c-4503-bd81-a7c27edc4541",1,"Aluminium standing-seam roof/wall cladding (Kalzip)."),
  ]},

453620: {"label":"match","confidence":0.6,"review":True,
  "lca_reasoning":"Aluminium storefront/curtain-wall door (72x84). The substance is extruded aluminium (the frame). Rank 1 = generic aluminium section; aluminium door products and aluminium sheet as substitutes. Flagged for review (low-information door material).",
  "picks":[
    ("3feca796-791b-46d3-8160-95ef243ffb9d",3,"Generic aluminium section - extruded aluminium door/frame substance."),
    ("0cb92770-9007-48c6-bc03-466af8894419",2,"Aluminium profile, mill finish - extruded frame profile."),
    ("d37c88c8-5053-471c-b233-90fdee78044b",1,"Aluminium exterior door (with light cut-out) - aluminium door product."),
    ("27324576-c801-4546-86a8-235406efed22",1,"Aluminium exterior door (without light cut-out) - aluminium door product."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",1,"Aluminium sheet - aluminium semi-finished."),
  ]},

# ============================== METAL PANELS / coping (sheet metal) ==============================
1483426: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Architectural sheet-metal rainscreen cladding panels. The modelled density (~8080) implies stainless/steel but architectural panels are commonly steel or aluminium; the true substance is not determinable from the BIM, so a cross-metal sheet-cladding set is offered (R3). Rank 1 = profiled steel cladding sheet; aluminium/stainless/galvanized/zinc/copper sheet as genuine substitutes. Flagged for review.",
  "picks":[
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",3,"Profiled steel cladding sheet (roof/wall) - sheet-metal rainscreen panel."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",3,"Profiled aluminium cladding sheet - sheet-metal rainscreen panel."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet - cladding panel sheet."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",2,"Aluminium sheet - cladding panel sheet."),
    ("deae7522-47b9-47a8-b9a3-176db7c250ae",2,"Stainless steel sheet - matches modelled ~8000 kg/m3."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",2,"Hot-dip galvanized steel sheet - coated steel panel."),
    ("26353b00-6cd3-426d-903b-9fc5b1670398",1,"Zinc sheet - metal cladding alternative."),
    ("e5a4ebf9-0e5c-4fd8-bb04-1acb5497312f",1,"Copper sheet - metal cladding alternative."),
    ("d409739a-ae3c-4005-8a26-d55fccc1d9e5",1,"Aluminium facade system (Kalzip) - aluminium cladding."),
  ]},

1483428: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Architectural sheet-metal soffit/ceiling panels (exterior soffit). Substance not determinable from the BIM; cross-metal sheet-cladding set offered (R3). Rank 1 = profiled steel cladding sheet; aluminium/stainless/galvanized/zinc sheet as substitutes. Flagged for review.",
  "picks":[
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",3,"Profiled steel cladding sheet - sheet-metal soffit panel."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",3,"Profiled aluminium cladding sheet - sheet-metal soffit panel."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",2,"Aluminium sheet."),
    ("deae7522-47b9-47a8-b9a3-176db7c250ae",2,"Stainless steel sheet - matches modelled density."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",2,"Hot-dip galvanized steel sheet."),
    ("26353b00-6cd3-426d-903b-9fc5b1670398",1,"Zinc sheet - cladding alternative."),
  ]},

1483430: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Architectural sheet-metal rainscreen/column cladding panels (white). Substance not determinable; cross-metal sheet-cladding set (R3). Rank 1 = profiled steel cladding sheet; aluminium/stainless/galvanized/zinc/copper sheet as substitutes. Flagged for review.",
  "picks":[
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",3,"Profiled steel cladding sheet - sheet-metal rainscreen panel."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",3,"Profiled aluminium cladding sheet - sheet-metal rainscreen panel."),
    ("89318772-3a50-4f56-9839-d0a8c702e396",2,"Generic steel sheet."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",2,"Aluminium sheet."),
    ("deae7522-47b9-47a8-b9a3-176db7c250ae",2,"Stainless steel sheet - matches modelled density."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",2,"Hot-dip galvanized steel sheet."),
    ("26353b00-6cd3-426d-903b-9fc5b1670398",1,"Zinc sheet - cladding alternative."),
    ("e5a4ebf9-0e5c-4fd8-bb04-1acb5497312f",1,"Copper sheet - cladding alternative."),
  ]},

1964624: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Sheet-metal parapet coping/cap (wall sweeps). Coping is bent flat sheet metal; substance not determinable from the BIM. Rank 1 = generic steel sheet; aluminium/galvanized/stainless/zinc sheet and profiled metal sheets as substitutes (R3). Flagged for review.",
  "picks":[
    ("89318772-3a50-4f56-9839-d0a8c702e396",3,"Generic steel sheet - bent sheet-metal coping."),
    ("143a4df2-563e-4ef9-b75b-90e1cf9f7ef9",3,"Aluminium sheet - common coping substance."),
    ("717f4a1c-0df0-49e6-8169-528b50b01e56",2,"Hot-dip galvanized steel sheet - coated coping."),
    ("deae7522-47b9-47a8-b9a3-176db7c250ae",2,"Stainless steel sheet - matches modelled density."),
    ("26353b00-6cd3-426d-903b-9fc5b1670398",2,"Zinc sheet - common coping metal."),
    ("e5a4ebf9-0e5c-4fd8-bb04-1acb5497312f",1,"Copper sheet - coping metal alternative."),
    ("a3460377-cb55-459a-bd70-6e4485faa0bb",1,"Profiled steel sheet - sheet-metal cladding/coping."),
    ("8e46a65c-1862-4628-8031-66a71a7e4679",1,"Profiled aluminium sheet - sheet-metal cladding/coping."),
  ]},

# ============================== BRONZE ==============================
1557297: {"label":"match","confidence":0.55,"review":True,
  "lca_reasoning":"Architectural bronze (a copper alloy; US 'architectural bronze' is typically a leaded brass) on a lobby stair, balustrade and supports. No 'bronze' EPD exists. Rank 1 = brass component (closest Cu alloy); red brass and copper sheet as substitutes. Flagged for review (bronze vs brass).",
  "picks":[
    ("fd3a4016-3f8c-4d8e-8bba-73b349781002",3,"Brass component - architectural bronze is essentially a leaded brass (Cu alloy)."),
    ("8a775569-aff7-4ebf-a439-d56aebb50cd8",3,"Red brass part - red brass closely approximates architectural bronze."),
    ("e5a4ebf9-0e5c-4fd8-bb04-1acb5497312f",2,"Copper sheet - base copper-alloy substitute."),
  ]},

# ============================== WOOD panels ==============================
18957: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"Structural plywood sheathing (d 552). Rank 1 = generic plywood board; veneer plywood + OSB + chipboard + 3/5-layer panels are functionally interchangeable wood-based sheathing panels (R3). Tie-breaker = plywood.",
  "picks":[
    ("24b673e5-b737-4fe9-ab51-ca68391ccdd3",3,"Generic plywood board - exact for sheathing plywood."),
    ("1c01243d-fd22-4655-bfaf-484dc024d620",3,"Veneer plywood (German average) - plywood."),
    ("aab0ed28-e5b0-43d6-a932-4cdc4770c518",2,"OSB (German average) - interchangeable structural sheathing panel."),
    ("766ea5bd-3f0f-4d48-a399-9c4a372524d8",2,"EGGER OSB boards - structural sheathing panel."),
    ("0f2f2410-d9e2-4e0f-837a-987cb3186d81",1,"Chipboard - wood-based panel."),
    ("27f20dc1-5529-4194-8a06-1ae5b7ba6a51",1,"3/5-layer solid wood panel - wood-based panel."),
    ("3489c0eb-415f-4dc8-8c2d-b276f6efebc6",1,"Five-layer laminated timber board - wood-based panel."),
  ]},

937249: {"label":"match","confidence":0.88,"review":False,
  "lca_reasoning":"Oriented strand board (OSB) sheathing. Rank 1 = OSB German average; EGGER OSB plus plywood and chipboard as interchangeable wood-based sheathing panels (R3). Tie-breaker = OSB.",
  "picks":[
    ("aab0ed28-e5b0-43d6-a932-4cdc4770c518",3,"OSB (German average) - exact OSB."),
    ("766ea5bd-3f0f-4d48-a399-9c4a372524d8",3,"EGGER OSB boards - OSB."),
    ("24b673e5-b737-4fe9-ab51-ca68391ccdd3",2,"Generic plywood board - interchangeable sheathing panel."),
    ("1c01243d-fd22-4655-bfaf-484dc024d620",2,"Veneer plywood (German average) - sheathing panel."),
    ("0f2f2410-d9e2-4e0f-837a-987cb3186d81",2,"Chipboard - wood-based panel."),
    ("27f20dc1-5529-4194-8a06-1ae5b7ba6a51",1,"3/5-layer solid wood panel - wood-based panel."),
  ]},

# ============================== WOOD flooring ==============================
18959: {"label":"match","confidence":0.8,"review":False,
  "lca_reasoning":"Solid oak (Quercus) flooring on stairs/ramps. Rank 1 = solid-wood parquet; multi-layer/strip parquet and laminate are wood-floor substitutes (R3). Tie-breaker = solid-wood parquet.",
  "picks":[
    ("bbbae8e3-3023-42b1-a58b-3cae9d482e10",3,"Solid-wood parquet (German average) - exact for solid oak flooring."),
    ("19c8f194-c3ab-4797-9b1a-8f1788bf12d8",2,"Multi-layer parquet (generic) - engineered wood flooring."),
    ("bfa87d42-18f1-4d7c-8786-ee60826b5050",2,"Multi-layer parquet (German average) - engineered wood flooring."),
    ("2b389c60-468e-4f3e-abfa-f279262ecd5d",2,"Strip parquet (generic) - wood flooring."),
    ("6b316150-e55c-4c57-ba3f-128768599784",2,"Multi-layer parquet (VdP) - wood flooring."),
    ("d0a11ffa-3f0f-435b-9f93-ff3821869335",1,"JOKA INKU parquet - wood flooring."),
    ("9cd7c3e7-6959-4f28-aa80-ede9b40bf22f",1,"Longlife parquet - wood flooring."),
    ("53b9dc3a-0128-40b9-b363-750bf86e342b",1,"Engineered wood flooring (Parador) - wood flooring."),
    ("14286caa-6bc1-4aa0-93d6-22d0f5b21195",1,"Laminate (DPL) - looser wood-based floor substitute."),
  ]},

2077616: {"label":"match","confidence":0.7,"review":False,
  "lca_reasoning":"Espresso-stained oak flooring used in 'Finish Floor - Laminate' build-ups (engineered/laminate oak). Rank 1 = multi-layer (engineered) parquet; solid parquet and laminate flooring as substitutes. Tie-breaker = engineered multilayer floor.",
  "picks":[
    ("bfa87d42-18f1-4d7c-8786-ee60826b5050",3,"Multi-layer parquet (German average) - engineered oak flooring."),
    ("19c8f194-c3ab-4797-9b1a-8f1788bf12d8",3,"Multi-layer parquet (generic) - engineered wood flooring."),
    ("bbbae8e3-3023-42b1-a58b-3cae9d482e10",2,"Solid-wood parquet (German average) - wood flooring."),
    ("3a888531-f0de-4e0f-88c2-530bebdc7b8e",2,"Laminate flooring - laminate floor (matches 'Laminate' build-up)."),
    ("7439ee69-3cf8-49fc-9acf-e4be4e55543c",2,"DPL laminate flooring - laminate floor."),
    ("14286caa-6bc1-4aa0-93d6-22d0f5b21195",2,"Laminate (DPL) - laminate floor."),
    ("2b389c60-468e-4f3e-abfa-f279262ecd5d",1,"Strip parquet (generic) - wood flooring."),
    ("60e7796e-cd49-4d03-a930-014271b30144",1,"Solid-timber multilayer flooring (Admonter) - engineered wood floor."),
  ]},

# ============================== WOOD timber ==============================
2282259: {"label":"match","confidence":0.6,"review":True,
  "lca_reasoning":"Pressure-treated solid-wood decking boards on a roof terrace. No decking-specific EPD; the substance is planed solid timber. Rank 1 = planed coniferous lumber; kiln-dried coniferous/hardwood lumber and larch/oak timber as solid-wood substitutes. Flagged for review (treated decking, no exact).",
  "picks":[
    ("4dadfb92-ba5d-48de-bb7b-394c28911a90",3,"Planed coniferous lumber - planed solid-wood decking board."),
    ("2bd4c91f-16e5-4e01-b8b5-0c01aac363ce",2,"Kiln-dried coniferous lumber - solid softwood decking."),
    ("e131431f-4d99-4d7a-ae0a-744a06de3524",2,"Kiln-dried hardwood lumber - hardwood decking option."),
    ("9f6cf0e4-7455-4c31-a99f-552926c4040e",2,"Timber larch - common decking species."),
    ("c8f1d599-b130-4ea0-b0fb-9da088e7c061",1,"Coniferous lumber (fresh) - solid softwood."),
    ("17bcb2ce-39fd-400b-baf9-370c63589efd",1,"Timber oak - hardwood decking option."),
    ("1121ded4-ec6e-4e09-b4ce-9410b5325688",1,"Sawn timber (Rubner) - solid-wood board."),
  ]},

24146: {"label":"match","confidence":0.6,"review":True,
  "lca_reasoning":"Wood joist/rafter framing layer (stair structure). The BIM models it as an air-dominated layer (no bulk density), but the name/class identify structural timber joists/rafters. Rank 1 = solid construction timber (KVH); KVH/finger-jointed/coniferous lumber and glulam beams as structural-timber substitutes. Flagged for review (framing fraction unknown - modelled as airspace).",
  "picks":[
    ("6b363ddc-f127-4d44-9f4b-54daada07465",3,"Solid construction timber (generic) - structural joist/rafter timber."),
    ("7aba3603-0689-4da5-8d24-fd92ae398d07",3,"KVH structural timber (German average) - structural framing timber."),
    ("55a6caa5-0c2f-410f-b752-f78e20ed93fa",2,"Finger-jointed solid structural timber."),
    ("c8f1d599-b130-4ea0-b0fb-9da088e7c061",2,"Coniferous construction lumber (fresh)."),
    ("2bd4c91f-16e5-4e01-b8b5-0c01aac363ce",2,"Coniferous construction lumber (kiln dried)."),
    ("aeab0f39-5eab-422f-8c4b-822bb98aa63c",2,"Laminated softwood beam (glulam) - engineered structural timber."),
    ("86138863-3c1c-4345-9ae9-03ba28a5f9a7",1,"Laminated coniferous timber planks - glulam."),
    ("ba8e3b37-f28b-4778-9e4c-336102cf589b",1,"Glued laminated timber - engineered structural timber."),
  ]},

# ============================== WOOD hardwood (birch door) ==============================
385171: {"label":"match","confidence":0.5,"review":True,
  "lca_reasoning":"Solid birch (a hardwood, d 512) used mainly for interior door leaves (many door sizes) plus a little furniture. Door leaves are building fabric. No interior-wood-door or birch-specific EPD exists, so rank 1 = hardwood lumber (the substance); beech/oak timber and birch plywood as substitutes. Flagged for review (species not exact; partial furniture use).",
  "picks":[
    ("e131431f-4d99-4d7a-ae0a-744a06de3524",3,"Kiln-dried hardwood lumber - birch is a hardwood."),
    ("67b6c807-3924-4bc5-b983-6aeff6537e45",2,"Timber beech - hardwood comparable to birch."),
    ("17bcb2ce-39fd-400b-baf9-370c63589efd",2,"Timber oak - hardwood."),
    ("24b673e5-b737-4fe9-ab51-ca68391ccdd3",1,"Generic plywood board - birch plywood is common for door panels."),
    ("1c01243d-fd22-4655-bfaf-484dc024d620",1,"Veneer plywood - plywood door panel."),
    ("2bd4c91f-16e5-4e01-b8b5-0c01aac363ce",1,"Kiln-dried coniferous lumber - solid-wood alternative."),
  ]},

# ============================== INSULATION rigid ==============================
18977: {"label":"match","confidence":0.7,"review":False,
  "lca_reasoning":"Rigid foam insulation board (physical density 50, used in rainscreen walls/roofs). The specific polymer is not stated; rigid thermal insulation boards legitimately span EPS, XPS, PIR/PUR, mineral-wool board, phenolic and foam glass (R3). Rank 1 = generic EPS rigid foam board; the other rigid boards are graded substitutes. Tie-breaker = generic rigid foam.",
  "picks":[
    ("121c71e8-0f4c-4529-b72e-331198d15f0f",3,"Generic EPS rigid foam insulation board - canonical rigid foam board."),
    ("c4ddfdbc-ee2b-4dd5-a754-b317cf06470a",3,"Extruded polystyrene (XPS) board - rigid foam board (matches ~50 kg/m3 better)."),
    ("355dbd58-78dc-45cc-be8a-43d66d117e15",2,"PIR high-density rigid foam board."),
    ("b960f95d-466b-4014-9565-64e51407a364",2,"PUR rigid foam board."),
    ("4da47295-f607-4254-ab06-6925e3d5fc07",2,"Phenolic resin rigid foam board."),
    ("155a3b83-ed73-4462-a4f1-69ce64e24981",2,"Mineral-wool board (high density) - rigid insulation board."),
    ("205b761d-e344-49c3-a3a9-bb1bfd59b916",2,"EPS board (density 15 kg/m3) - rigid foam board."),
    ("bd7ae84e-54c4-4dee-bf0e-8c22cecbcf75",2,"Grey EPS board (15 kg/m3) - rigid foam board."),
    ("7091503e-34e5-44cd-9acb-7c2f16f6097f",1,"Foam-glass board - rigid mineral insulation board."),
  ]},

1924358: {"label":"match","confidence":0.68,"review":False,
  "lca_reasoning":"Tapered rigid foam roof insulation (lambda 0.035). Same material class as the wall rigid insulation; the polymer is unspecified and rigid boards span EPS/XPS/PIR/PUR/mineral-wool/phenolic (R3). Rank 1 = generic EPS rigid foam board with the other rigid boards as substitutes.",
  "picks":[
    ("121c71e8-0f4c-4529-b72e-331198d15f0f",3,"Generic EPS rigid foam board - canonical tapered roof insulation."),
    ("c4ddfdbc-ee2b-4dd5-a754-b317cf06470a",3,"XPS board - rigid foam board."),
    ("355dbd58-78dc-45cc-be8a-43d66d117e15",2,"PIR high-density rigid foam board - common flat-roof insulation."),
    ("b960f95d-466b-4014-9565-64e51407a364",2,"PUR rigid foam board."),
    ("4da47295-f607-4254-ab06-6925e3d5fc07",2,"Phenolic resin rigid foam board."),
    ("155a3b83-ed73-4462-a4f1-69ce64e24981",2,"Mineral-wool board (high density) - rigid roof board."),
    ("7091503e-34e5-44cd-9acb-7c2f16f6097f",1,"Foam-glass board - rigid roof insulation."),
  ]},

# ============================== EIFS / ETICS ==============================
18967: {"label":"match","confidence":0.62,"review":True,
  "lca_reasoning":"EIFS = External Thermal Insulation Composite System (ETICS/WDVS); lambda 0.023, d 23 indicate an EPS core. Rank 1 = ETICS with EPS insulation board (bonded+dowelled); mineral-wool/wood-fibre ETICS variants and the EPS core board as substitutes. Flagged for review (system vs component).",
  "picks":[
    ("dc7f4fb1-f30d-43aa-9eda-edbd8e0d0ecb",3,"ETICS with EPS insulation board - EIFS system with EPS core."),
    ("6d16dafc-0a4b-496a-9f3f-39c066bc0401",2,"External thermal insulation composite system EPS (rail-fixed) - EIFS/EPS."),
    ("f756421b-f810-4e64-b96b-b71f95defe48",2,"ETICS with mineral-wool board - EIFS variant."),
    ("a3b10cdc-938a-4852-a105-65e37b1dc0d8",2,"ETICS with wood-fibre insulation - EIFS variant."),
    ("121c71e8-0f4c-4529-b72e-331198d15f0f",1,"EPS rigid foam board - the EIFS insulation core only."),
    ("c4ddfdbc-ee2b-4dd5-a754-b317cf06470a",1,"XPS board - alternative EIFS insulation core."),
  ]},

# ============================== GYPSUM board ==============================
18986: {"label":"match","confidence":0.82,"review":False,
  "lca_reasoning":"Gypsum wall board (plasterboard) in partitions/ceilings/linings (d 1100). Rank 1 = 'Gypsum wallboard' (closest-named); fire/impregnated/perforated plaster boards and gypsum fibre boards are the substitutes (R3). Tie-breaker = name match.",
  "picks":[
    ("7b69e7b4-68dd-49a3-b2dc-ae608b66eece",3,"'Gypsum wallboard' - exact name match for 'Gypsum Wall Board'."),
    ("0773f90c-78ef-4727-a966-3b005c1a1469",2,"Generic plasterboard (tool) - standard gypsum board."),
    ("46c055f0-84a2-423b-ae82-5264886e51ad",2,"Gypsum plaster board (fire protection, 12.5 mm)."),
    ("deeb0bda-20fa-412a-b945-1a589638db21",2,"Gypsum plaster board (impregnated, 12.5 mm)."),
    ("1b0a3488-9b02-4c98-b421-8c746d350f97",2,"Gypsum fibre board (10 mm)."),
    ("edec09e1-c56e-4655-a094-c9a18e1563ac",1,"Gypsum plaster board (perforated, 12.5 mm)."),
    ("2272105e-734d-491d-a36c-d4ba2d759900",1,"Gypsum fibre board (average)."),
    ("fbde9f28-e9d8-4300-9b21-dea035f5bc15",1,"Fire-resistant plasterboard (tool)."),
    ("398d138e-8bed-4254-b4a7-adeffb07a48d",1,"Impregnated plasterboard (tool)."),
    ("29248c83-68d5-4ff6-ae8d-cf91e54dd000",1,"Hard gypsum board (Hartgipsplatte)."),
  ]},

1295335: {"label":"match","confidence":0.7,"review":False,
  "lca_reasoning":"Shaft-liner board (description = gypsum wall board; typically a fire-rated gypsum core board) in a party wall. Rank 1 = 'Gypsum wallboard' (per description); fire-rated and impregnated plaster boards rank high; gypsum fibre boards as substitutes.",
  "picks":[
    ("7b69e7b4-68dd-49a3-b2dc-ae608b66eece",3,"'Gypsum wallboard' - matches the 'Gypsum Wall Board' description."),
    ("46c055f0-84a2-423b-ae82-5264886e51ad",2,"Gypsum plaster board (fire protection) - shaft liners are fire-rated."),
    ("fbde9f28-e9d8-4300-9b21-dea035f5bc15",2,"Fire-resistant plasterboard."),
    ("0773f90c-78ef-4727-a966-3b005c1a1469",2,"Generic plasterboard (tool)."),
    ("deeb0bda-20fa-412a-b945-1a589638db21",1,"Gypsum plaster board (impregnated)."),
    ("1b0a3488-9b02-4c98-b421-8c746d350f97",1,"Gypsum fibre board (10 mm)."),
    ("29248c83-68d5-4ff6-ae8d-cf91e54dd000",1,"Hard gypsum board."),
  ]},

1483290: {"label":"match","confidence":0.7,"review":False,
  "lca_reasoning":"Exterior gypsum sheathing (description = gypsum wall board) in rainscreen walls - typically an impregnated/water-resistant gypsum board. Rank 1 = impregnated gypsum plaster board; gypsum wallboard/fibre board and fibre-cement panel as exterior-board substitutes.",
  "picks":[
    ("deeb0bda-20fa-412a-b945-1a589638db21",3,"Gypsum plaster board (impregnated) - exterior weather-resistant sheathing."),
    ("398d138e-8bed-4254-b4a7-adeffb07a48d",2,"Impregnated plasterboard (tool)."),
    ("7b69e7b4-68dd-49a3-b2dc-ae608b66eece",2,"Gypsum wallboard."),
    ("46c055f0-84a2-423b-ae82-5264886e51ad",2,"Gypsum plaster board (fire protection)."),
    ("1b0a3488-9b02-4c98-b421-8c746d350f97",1,"Gypsum fibre board (10 mm)."),
    ("0773f90c-78ef-4727-a966-3b005c1a1469",1,"Generic plasterboard (tool)."),
    ("161e1abd-fe38-45f6-8658-8bf4fa88eb7c",1,"Fibre-cement facade panel - alternative exterior sheathing board."),
  ]},

1924356: {"label":"match","confidence":0.65,"review":False,
  "lca_reasoning":"Glass-mat gypsum roof cover board (DensDeck, 5/8\"). Closest is a water-resistant/impregnated gypsum board. Rank 1 = impregnated gypsum plaster board; fire/standard gypsum boards and fibre-cement panel as cover-board substitutes.",
  "picks":[
    ("deeb0bda-20fa-412a-b945-1a589638db21",3,"Gypsum plaster board (impregnated) - closest to water-resistant gypsum roof board."),
    ("398d138e-8bed-4254-b4a7-adeffb07a48d",2,"Impregnated plasterboard (tool)."),
    ("46c055f0-84a2-423b-ae82-5264886e51ad",2,"Gypsum plaster board (fire protection)."),
    ("7b69e7b4-68dd-49a3-b2dc-ae608b66eece",2,"Gypsum wallboard."),
    ("1b0a3488-9b02-4c98-b421-8c746d350f97",1,"Gypsum fibre board (10 mm)."),
    ("161e1abd-fe38-45f6-8658-8bf4fa88eb7c",1,"Fibre-cement facade panel - alternative cover board."),
  ]},

# ============================== CERAMIC tile ==============================
1220087: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"Porcelain floor tiles (roof porcelain float deck, d 2000). Porcelain = stoneware/fine stoneware. Rank 1 = glazed stoneware tile (d 2000 matches); unglazed stoneware and ceramic tiles, plus stone slabs as looser hard-tile substitutes. Tie-breaker = porcelain = stoneware.",
  "picks":[
    ("5618689c-57a8-4860-85d3-7403ad5ba201",3,"Glazed stoneware tile (2000 kg/m3) - porcelain floor tile."),
    ("91935678-1a09-43e0-8a84-294522407ac3",3,"Unglazed stoneware tile (2000 kg/m3) - porcelain floor tile."),
    ("98a6e52b-6e12-4542-afa7-371740748685",2,"Ceramic tiles - ceramic floor tile."),
    ("f82e3ab8-d7bb-4765-a60c-bfe78ede1565",1,"Natural stone slab, rigid, outdoor - hard outdoor floor slab."),
    ("c241605d-34f1-44db-bf07-78610e849edb",1,"Natural stone slab, rigid, indoor - hard floor slab."),
    ("514d0f56-13d8-45a3-9267-01be064d6ce3",1,"Artificial/cast stone slab - hard mineral floor slab."),
  ]},

2114498: {"label":"match","confidence":0.7,"review":True,
  "lca_reasoning":"Wood-look ceramic/porcelain floor tile ('Finish Floor - Ceramic Tile (Wood)'). The BIM class 'Wood' is demonstrably mislabelled - it is a glazed stoneware tile, not wood (R3). Rank 1 = glazed stoneware tile; ceramic and unglazed stoneware tiles as substitutes. Flagged for review (class mislabel).",
  "picks":[
    ("5618689c-57a8-4860-85d3-7403ad5ba201",3,"Glazed stoneware tile - wood-look porcelain floor tile."),
    ("98a6e52b-6e12-4542-afa7-371740748685",3,"Ceramic tiles - ceramic floor tile."),
    ("91935678-1a09-43e0-8a84-294522407ac3",2,"Unglazed stoneware tile - porcelain floor tile."),
    ("c241605d-34f1-44db-bf07-78610e849edb",1,"Natural stone slab, rigid, indoor - hard floor slab."),
  ]},

# ============================== SANITARY ceramic ware ==============================
468384: {"label":"match","confidence":0.7,"review":True,
  "lca_reasoning":"Vitreous-china WC pan (toilet, 19-inch seat-height plumbing fixture). Sanitary ceramic ware is inside this study's fabric+sanitary boundary. Rank 1 = 'Toilets' (sanitary ceramic); generic sanitary ceramic and washbasins as substitutes. Flagged for review (sanitary-ware boundary call).",
  "picks":[
    ("ff8e657a-940c-4b99-a84d-494c8ca8079b",3,"Toilets - sanitary ceramic WC, exact."),
    ("f1974493-0668-4b34-880b-21c3f27345bc",2,"Generic sanitary ceramic - the WC material."),
    ("153f7ab7-3858-4bce-b6f3-ae7a73770931",1,"Washbasins - adjacent sanitary-ceramic fixture."),
  ]},

469172: {"label":"match","confidence":0.7,"review":True,
  "lca_reasoning":"Vitreous-china WC pan (Toilet-Domestic-3D plumbing fixture). Sanitary ceramic ware is within the boundary. Rank 1 = 'Toilets'; generic sanitary ceramic and washbasins as substitutes. Flagged for review (sanitary-ware boundary call).",
  "picks":[
    ("ff8e657a-940c-4b99-a84d-494c8ca8079b",3,"Toilets - sanitary ceramic WC, exact."),
    ("f1974493-0668-4b34-880b-21c3f27345bc",2,"Generic sanitary ceramic - the WC material."),
    ("153f7ab7-3858-4bce-b6f3-ae7a73770931",1,"Washbasins - adjacent sanitary-ceramic fixture."),
  ]},

870414: {"label":"match","confidence":0.65,"review":True,
  "lca_reasoning":"Vitreous-china washbasin/lavatory (22x27 plumbing fixture, porcelain). Sanitary ceramic ware is within the boundary. Rank 1 = 'Washbasins'; generic sanitary ceramic and toilets as substitutes. Flagged for review (sanitary-ware boundary call).",
  "picks":[
    ("153f7ab7-3858-4bce-b6f3-ae7a73770931",3,"Washbasins - sanitary ceramic basin, exact."),
    ("f1974493-0668-4b34-880b-21c3f27345bc",2,"Generic sanitary ceramic - the basin material."),
    ("ff8e657a-940c-4b99-a84d-494c8ca8079b",1,"Toilets - adjacent sanitary-ceramic fixture."),
  ]},

# ============================== ASPHALT ==============================
1104850: {"label":"match","confidence":0.7,"review":True,
  "lca_reasoning":"Asphalt/tarmac pavement (sidewalk surface, d 2300). Rank 1 = asphalt surface layer; asphalt supporting layer, stone-mastic and mastic asphalt as paving substitutes. Flagged for review (external site paving = boundary call).",
  "picks":[
    ("c6f77799-ed43-4ec4-bfc3-0759e41b60ca",3,"Asphalt surface layer - pavement wearing course."),
    ("9795c91c-0918-46fa-8a51-47deb180c271",2,"Asphalt supporting/base layer - pavement course."),
    ("d24a85e3-f395-4124-9874-c01f977a893f",2,"Stone-mastic asphalt (SMA) - asphalt paving."),
    ("b29d88ed-8621-4ece-9e2f-78af05cb3f0f",2,"Mastic asphalt - asphalt paving/screed."),
    ("85e76e87-fc16-4fd5-80ed-a07fa5767f56",1,"Asphalt binder - asphalt mix layer."),
    ("c7381cc7-53b7-427e-9c0b-c9edc4152602",1,"Mastic asphalt screed - asphalt layer."),
  ]},

# ============================== ACRYLIC / plastic glazing ==============================
1483994: {"label":"match","confidence":0.72,"review":True,
  "lca_reasoning":"Clear acrylic (PMMA) sheet glazing for a bandstand canopy (d 1190 = PMMA). Rank 1 = cast PMMA transparent board (exact density); extruded PMMA and PC transparent boards as substitutes; PVC board looser. Flagged for review (canopy = borderline structure).",
  "picks":[
    ("f7646e58-4d38-4bc0-b908-e4207c9f2a89",3,"Cast PMMA transparent board (1190 kg/m3) - exact acrylic sheet."),
    ("7a265769-b70e-4cbb-a9ac-f6af4e55f8a6",3,"Extruded PMMA transparent board (1190 kg/m3) - acrylic sheet."),
    ("b2ba83d3-0427-404c-8c27-f551d0a26bd3",2,"PC transparent board - transparent plastic glazing sheet."),
    ("2e60360a-61f1-4d86-a537-a50a3d4165ba",1,"PVC transparent board - transparent plastic sheet (looser)."),
  ]},

# ============================== MEMBRANES ==============================
24244: {"label":"match","confidence":0.85,"review":False,
  "lca_reasoning":"EPDM single-ply roof membrane (d 930). Rank 1 = generic EPDM roof sheet (exact); other synthetic single-ply membranes (TPO/PVC/PIB, EPDM brands) are strong substitutes; bitumen/ECB/EVA sheets looser. Tie-breaker = EPDM elastomer.",
  "picks":[
    ("498cd894-8ae6-458c-b874-9022cb148409",3,"Generic EPDM roof sheet - exact."),
    ("6a497eaa-2a18-4cc6-a023-64d1779a4f19",2,"Elastomer roof membrane (Sarnafil TG) - single-ply elastomer."),
    ("ada97412-720d-4a00-85ec-99403cc5c947",2,"TPO roof membrane (EverGuard) - synthetic single-ply."),
    ("fdba1500-af16-4bdb-ae7f-28537dabb80e",2,"TPO/FPO roof membrane (BauderTHERMOFIN)."),
    ("ab14e105-1768-4cd7-bcd2-3436c16fad16",2,"PVC roof membrane - synthetic single-ply."),
    ("91412f3f-6077-44d4-9c9d-95c543bcb419",2,"PIB synthetic roofing membrane - single-ply."),
    ("67e2254e-7fcf-4321-a703-9a1c977a01f2",1,"PIB roof membrane (Rhepanol) - single-ply."),
    ("a3cbfec7-6e44-4b0a-b124-afca92a4d934",1,"Bitumen-EPDM membrane (RESITRIX) - elastomer-bitumen sheet."),
    ("27ccffb8-02ec-466d-b925-f6db91d4ef71",1,"Bituminous roofing sheet - looser membrane substitute."),
    ("dcde634c-51b1-4d94-9206-cba9f9fd6822",1,"ECB roofing membrane - polymer-bitumen sheet."),
  ]},

1924355: {"label":"match","confidence":0.78,"review":False,
  "lca_reasoning":"TPO (FPO) single-ply roof membrane. No generic TPO dataset exists, so the four brand TPO EPDs are equally valid (R9) with one chosen as the representative rank 1; PVC/EPDM/PIB single-ply are strong substitutes; bitumen looser. Tie-breaker = TPO/FPO.",
  "picks":[
    ("ada97412-720d-4a00-85ec-99403cc5c947",3,"TPO roof membrane (EverGuard) - representative TPO."),
    ("fdba1500-af16-4bdb-ae7f-28537dabb80e",3,"TPO/FPO roof membrane (BauderTHERMOFIN)."),
    ("c7a23482-c7ee-4b2f-b807-6c34bbea495b",3,"TPO/FPO roof membrane (BauderTHERMOPLAN)."),
    ("4b0f5c51-622e-4af8-81f0-c9f13ea7b6b0",3,"TPO/FPO roof membrane (BauderTHERMOPLEX)."),
    ("ab14e105-1768-4cd7-bcd2-3436c16fad16",2,"PVC roof membrane - synthetic single-ply."),
    ("498cd894-8ae6-458c-b874-9022cb148409",2,"EPDM roof sheet - synthetic single-ply."),
    ("91412f3f-6077-44d4-9c9d-95c543bcb419",2,"PIB synthetic roofing membrane - single-ply."),
    ("6390d54c-edcb-432e-a75f-c5371847cc4d",2,"PVC roof membrane (BauderTHERMOFOL) - single-ply."),
    ("27ccffb8-02ec-466d-b925-f6db91d4ef71",1,"Bituminous roofing sheet - looser membrane substitute."),
  ]},

18982: {"label":"match","confidence":0.82,"review":False,
  "lca_reasoning":"Polyethylene (PE) film vapour retarder in a wall. Rank 1 = generic PE damp-proof/vapour film (exact); PA and PET vapour-control films and PE sealing foils as substitutes. Tie-breaker = PE film.",
  "picks":[
    ("6869f7c1-1b2b-4f30-afc9-823a0104f1d9",3,"PE damp-proof/vapour film - exact PE vapour retarder."),
    ("c4293a12-5023-4e57-bdcd-9b52fbfa6a58",2,"PA vapour-control film - vapour barrier."),
    ("28a26228-4acd-4bba-b62c-54a54c35526f",2,"PET vapour-control film - vapour barrier."),
    ("5f0bc680-a252-46d6-a47a-a4b5a05318bc",2,"Dimpled PE foil - PE sealing membrane."),
    ("ed734c48-a58f-4f92-a9f7-57631b500fdc",1,"PE-HD sealing membrane with PP fleece."),
    ("bf9fcef5-5056-4749-9e84-fb2eeb915e57",1,"Monolayer membrane (Tyvek) - building film."),
    ("b4634111-dc95-4873-8614-9ab38bd11d42",1,"PP underroof membrane - building film."),
  ]},

1924357: {"label":"match","confidence":0.82,"review":False,
  "lca_reasoning":"6-mil polyethylene vapour barrier under a roof build-up. Same substance as the wall vapour retarder. Rank 1 = generic PE damp-proof/vapour film; PA/PET vapour films and PE sealing foils as substitutes.",
  "picks":[
    ("6869f7c1-1b2b-4f30-afc9-823a0104f1d9",3,"PE damp-proof/vapour film - exact PE vapour barrier."),
    ("c4293a12-5023-4e57-bdcd-9b52fbfa6a58",2,"PA vapour-control film."),
    ("28a26228-4acd-4bba-b62c-54a54c35526f",2,"PET vapour-control film."),
    ("5f0bc680-a252-46d6-a47a-a4b5a05318bc",2,"Dimpled PE foil - PE sealing membrane."),
    ("ed734c48-a58f-4f92-a9f7-57631b500fdc",1,"PE-HD sealing membrane with PP fleece."),
    ("b4634111-dc95-4873-8614-9ab38bd11d42",1,"PP underroof membrane - building film."),
  ]},

# ============================== SKIPS ==============================
# (a) not a material
18974: {"label":"skip","skip_reason":"[not a material] Air gap / cavity - empty space modelled as a 'material' by Revit. Nothing is manufactured or consumed, so it carries zero embodied impact and is excluded from BIM->EPD mapping."},

# (b) negligible appearance / paint finish
36879: {"label":"skip","skip_reason":"[negligible appearance finish] Parking/road-stripe marking paint - a thin applied line marking, not a building-fabric material."},
336821: {"label":"skip","skip_reason":"[negligible appearance finish] 'Clad - White' is a colour/appearance on window & curtain-wall frame families (no physical data); the frame bulk is counted via the aluminium/glazing materials."},
336822: {"label":"skip","skip_reason":"[negligible appearance finish] Wood-stain colour appearance on window/door frame families (no physical data); a surface finish, not a fabric material."},
385173: {"label":"skip","skip_reason":"[negligible appearance finish] Grey paint finish on door leaves (no bulk substance / Paint class); thin coating, not a fabric material."},
453617: {"label":"skip","skip_reason":"[negligible appearance finish] Grey paint colour on a door whose aluminium substance is mapped separately ('Metal - Aluminium'); paint finish excluded."},
453621: {"label":"skip","skip_reason":"[negligible appearance finish] 'Brindle' colour finish on the same door whose aluminium substance is mapped separately; appearance only."},
465406: {"label":"skip","skip_reason":"[negligible appearance finish] Ivory-gloss paint on a light-fixture body; surface colour, not a fabric material."},
790065: {"label":"skip","skip_reason":"[negligible appearance finish] White site-marking paint (parking symbols/arrows); thin surface marking."},
2073722: {"label":"skip","skip_reason":"[negligible appearance finish] White-gloss paint on doors/wall sweeps; thin decorative coating."},
2076458: {"label":"skip","skip_reason":"[negligible appearance finish] Grey textured paint on walls/wall-sweeps; decorative coating, not a fabric material."},
930496: {"label":"skip","skip_reason":"[negligible appearance finish] Orange colour/appearance on window, mullion & door families (no physical data); the aluminium bulk is counted via the 'Aluminum' material."},
930559: {"label":"skip","skip_reason":"[negligible appearance finish] Black colour/appearance on window, mullion & door families (no physical data); aluminium bulk counted via 'Aluminum'."},

# (c) outside building-fabric boundary - placeholders
18435: {"label":"skip","skip_reason":"[outside building-fabric boundary] Revit default/placeholder material (no substance, no physical data)."},
18438: {"label":"skip","skip_reason":"[outside building-fabric boundary] Revit default/placeholder wall material (no substance, no physical data)."},
78544: {"label":"skip","skip_reason":"[outside building-fabric boundary] 'Poche' is a Revit graphic fill convention (Cameras / 3D views), not a physical building material."},
135528: {"label":"skip","skip_reason":"[outside building-fabric boundary] Light-source render placeholder for lighting fixtures (MEP), not a fabric material."},

# (c) lamp / fixture glass
465404: {"label":"skip","skip_reason":"[outside building-fabric boundary] Luminous lamp/diffuser glass inside a lighting fixture (MEP), not building glazing."},
466340: {"label":"skip","skip_reason":"[outside building-fabric boundary] Luminous lamp glass in lighting fixtures (MEP), not building glazing."},
1419906: {"label":"skip","skip_reason":"[outside building-fabric boundary] Frosted lamp/diffuser glass in lighting fixtures (MEP), not building glazing."},

# (c) furniture / casework / textile
35335: {"label":"skip","skip_reason":"[outside building-fabric boundary] Decorative plastic laminate on furniture/casework/equipment surfaces (loose furnishing finish)."},
449947: {"label":"skip","skip_reason":"[outside building-fabric boundary] Casework / cabinetry (loose furniture), outside building fabric."},
449951: {"label":"skip","skip_reason":"[outside building-fabric boundary] Cabinet handle hardware (casework/furniture)."},
450621: {"label":"skip","skip_reason":"[outside building-fabric boundary] Casework countertop (furniture/joinery), outside building fabric."},
805649: {"label":"skip","skip_reason":"[outside building-fabric boundary] Casework cabinet (loose furniture)."},
2109186: {"label":"skip","skip_reason":"[outside building-fabric boundary] Furniture laminate finish (loose furnishing)."},
1242014: {"label":"skip","skip_reason":"[outside building-fabric boundary] Furniture plastic (banquette seating), loose furnishing."},
1288602: {"label":"skip","skip_reason":"[outside building-fabric boundary] Bed-linen textile (furniture / soft furnishing)."},
737421: {"label":"skip","skip_reason":"[outside building-fabric boundary] Furniture upholstery textile (chair), loose furnishing."},
2108591: {"label":"skip","skip_reason":"[outside building-fabric boundary] Furniture upholstery leather (chair), loose furnishing."},
2108592: {"label":"skip","skip_reason":"[outside building-fabric boundary] Chrome finish on furniture (chair frame), loose furnishing."},
435538: {"label":"skip","skip_reason":"[outside building-fabric boundary] Pine used only in loose furniture (tables), outside building fabric."},
593863: {"label":"skip","skip_reason":"[outside building-fabric boundary] Birch used predominantly in loose furniture (beds/tables) and window-render families; not a fabric build-up material."},
2130336: {"label":"skip","skip_reason":"[outside building-fabric boundary] Teak site bench / furniture (loose furnishing)."},
1106374: {"label":"skip","skip_reason":"[outside building-fabric boundary] Generic wood for a trash container (specialty equipment / furnishing)."},

# (c) plumbing fittings / fixtures (non-ceramic)
35341: {"label":"skip","skip_reason":"[outside building-fabric boundary] Chrome-plated steel on faucets/fixtures/furniture/lighting (plumbing fittings & equipment)."},
450622: {"label":"skip","skip_reason":"[outside building-fabric boundary] Polished steel on plumbing fixtures/lighting/specialty equipment (fittings)."},
870415: {"label":"skip","skip_reason":"[outside building-fabric boundary] Polished steel plumbing fixture/fitting."},
1106380: {"label":"skip","skip_reason":"[outside building-fabric boundary] PBT plastic faucet/tap part (plumbing fitting)."},
1106381: {"label":"skip","skip_reason":"[outside building-fabric boundary] PBT plastic faucet/tap part (plumbing fitting)."},
1106372: {"label":"skip","skip_reason":"[outside building-fabric boundary] Polished stainless on food-service equipment / plumbing fixtures (equipment/fittings)."},
1106373: {"label":"skip","skip_reason":"[outside building-fabric boundary] Brushed stainless on food-service equipment & plumbing fixtures (equipment/fittings), not fabric."},

# (c) appliances / food-service / specialty equipment
470591: {"label":"skip","skip_reason":"[outside building-fabric boundary] Stainless-steel kitchen appliance (loose equipment)."},
470604: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance gasket (loose equipment)."},
470611: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance steel part (loose equipment)."},
730340: {"label":"skip","skip_reason":"[outside building-fabric boundary] Formed-plastic specialty-equipment part."},
730868: {"label":"skip","skip_reason":"[outside building-fabric boundary] Stainless dispensers (washroom specialty equipment / accessories)."},
731925: {"label":"skip","skip_reason":"[outside building-fabric boundary] Washroom mirror (bathroom accessory / specialty equipment)."},
764704: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance polycarbonate part (loose equipment)."},
764706: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance control part (loose equipment)."},
764707: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance door-trim part (loose equipment)."},
764709: {"label":"skip","skip_reason":"[outside building-fabric boundary] Appliance panel part (loose equipment)."},
778536: {"label":"skip","skip_reason":"[outside building-fabric boundary] Painted-steel lighting-fixture / specialty-equipment body (MEP/equipment)."},
784137: {"label":"skip","skip_reason":"[outside building-fabric boundary] Painted metal on food-service / specialty equipment (loose equipment)."},
1106366: {"label":"skip","skip_reason":"[outside building-fabric boundary] Matte-black finish on food-service equipment (loose equipment)."},
1106368: {"label":"skip","skip_reason":"[outside building-fabric boundary] Powder-coated metal food-service equipment part (loose equipment)."},
1106369: {"label":"skip","skip_reason":"[outside building-fabric boundary] Plastic beverage-dispenser part (food-service equipment)."},
1106370: {"label":"skip","skip_reason":"[outside building-fabric boundary] Satin-bronze aluminium finish on food-service equipment / signage (loose equipment)."},
1106371: {"label":"skip","skip_reason":"[outside building-fabric boundary] Rubber part on food-service equipment (loose equipment)."},
1106375: {"label":"skip","skip_reason":"[outside building-fabric boundary] Plastic soap-dispenser / trash-can (washroom specialty equipment)."},
1106378: {"label":"skip","skip_reason":"[outside building-fabric boundary] Plastic part on food-service equipment (loose equipment)."},
1106382: {"label":"skip","skip_reason":"[outside building-fabric boundary] Plastic cash-drawer part (specialty equipment)."},
1106385: {"label":"skip","skip_reason":"[outside building-fabric boundary] Plastic part on food-service / specialty equipment (loose equipment)."},
1660388: {"label":"skip","skip_reason":"[outside building-fabric boundary] Sheet-metal specialty-equipment part (loose equipment)."},
2250032: {"label":"skip","skip_reason":"[outside building-fabric boundary] Aluminium towel-dispenser part (washroom specialty equipment / accessory)."},
1420526: {"label":"skip","skip_reason":"[outside building-fabric boundary] Painted steel on lighting fixtures & a trash-chute (MEP / specialty equipment)."},
1420527: {"label":"skip","skip_reason":"[outside building-fabric boundary] Painted steel on lighting fixtures (MEP / equipment)."},
466339: {"label":"skip","skip_reason":"[outside building-fabric boundary] Painted-steel site element (Site category furnishing/post), outside building fabric."},

# (c) elevator / vertical-circulation equipment
472710: {"label":"skip","skip_reason":"[outside building-fabric boundary] Elevator car steel (vertical-circulation / MEP equipment)."},
720110: {"label":"skip","skip_reason":"[outside building-fabric boundary] Elevator annunciator escutcheon (MEP / equipment)."},
720111: {"label":"skip","skip_reason":"[outside building-fabric boundary] Elevator annunciator buttons (MEP / equipment)."},
720114: {"label":"skip","skip_reason":"[outside building-fabric boundary] Elevator call escutcheon (MEP / equipment)."},
720115: {"label":"skip","skip_reason":"[outside building-fabric boundary] Elevator call buttons (MEP / equipment)."},
473949: {"label":"skip","skip_reason":"[outside building-fabric boundary] Brushed-aluminium grab bar (washroom accessory / specialty equipment)."},

# (c) photovoltaics
974574: {"label":"skip","skip_reason":"[outside building-fabric boundary] Photovoltaic solar-cell panel (building-services / PV equipment)."},
978846: {"label":"skip","skip_reason":"[outside building-fabric boundary] Photovoltaic solar infill panel (building-services / PV equipment)."},

# (c) site / landscape
1231151: {"label":"skip","skip_reason":"[outside building-fabric boundary] Tree grate (site furnishing / landscape)."},
1233741: {"label":"skip","skip_reason":"[outside building-fabric boundary] Lawn / grass (landscape / planting)."},
1237324: {"label":"skip","skip_reason":"[outside building-fabric boundary] Tree canopy (landscape / planting)."},
1237325: {"label":"skip","skip_reason":"[outside building-fabric boundary] Tree trunk (landscape / planting)."},
1343483: {"label":"skip","skip_reason":"[outside building-fabric boundary] Living / green wall planting (landscape)."},
1383170: {"label":"skip","skip_reason":"[outside building-fabric boundary] Soil / growing medium (landscape / site)."},
1383173: {"label":"skip","skip_reason":"[outside building-fabric boundary] Decorative pea-stone / planter element at Site (Potted Tree); the name says pea-stone gravel while the description ('Precast concrete') appears copied in error - either way a site/landscape element."},
1397158: {"label":"skip","skip_reason":"[outside building-fabric boundary] Planting bed (landscape)."},
1402087: {"label":"skip","skip_reason":"[outside building-fabric boundary] Sculpture / planting bed (landscape / site)."},
2282191: {"label":"skip","skip_reason":"[outside building-fabric boundary] Artificial turf roof/landscape finish (landscape)."},
1464536: {"label":"skip","skip_reason":"[outside building-fabric boundary] Anodized-aluminium planter box (Hardscape site furnishing)."},
1464537: {"label":"skip","skip_reason":"[outside building-fabric boundary] Anodized-aluminium planter box (Hardscape site furnishing)."},
2456791: {"label":"skip","skip_reason":"[outside building-fabric boundary] Patio umbrella (loose site furniture)."},
2456793: {"label":"skip","skip_reason":"[outside building-fabric boundary] Umbrella support (loose site furniture)."},

}
