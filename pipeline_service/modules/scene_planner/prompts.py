"""Scene Planner prompt — produces OSD (Scene Brief + parts narratives).

Asks the model for a markdown-formatted Scene Brief that a 3D artist can use
to recreate the object in Three.js, plus a per-part list where each entry
is 2–5 sentences of holistic prose. The Coder consumes the brief + narratives
directly — no fragmented dictionary of shape/size/position/color/material
fields to reconstruct.
"""
from __future__ import annotations


PLANNER_OSD_PROMPT = """\
You are looking at a single 3D object in a photograph. Produce a **Scene
Brief** that a 3D artist can use to rebuild this object in Three.js from
procedural primitives (box, cylinder, sphere, cone, torus, lathe profile,
tube-along-path, extruded shape, and instanced groups).

Use your 3D spatial understanding. Parts continue behind what is visible —
if a chair has one visible front leg and one visible back leg, it almost
certainly has four. If a lamp base supports a neck and a shade, call out
the base, the neck, AND the shade separately. Do not merge several distinct
parts into one.

Return a SINGLE JSON object with this exact shape — fill every field:

{{
  "object_type": "<short lowercase noun, e.g. chair, goblet, bottle, car, mug, lamp>",
  "scene_brief": "<markdown document, 600–2000 tokens, following the sections below>",
  "parts": [
    {{
      "name": "<short stable id, e.g. 'seat', 'backrest', 'front-left leg', 'lampshade'>",
      "narrative": "<2–5 sentences of holistic prose — see per-part guidance below>",
      "count_hint": "<'one', 'four symmetric', 'two mirrored pairs', 'a single row of six'>",
      "motif_role": "<optional: 'support', 'body', 'enclosure', 'decoration'; else null>"
    }}
  ],
  "motif_hint": "<optional — known motif name if the object clearly matches one; else null>",
  "notes": "<optional overall caveats, e.g. 'partially occluded on the left'; else null>"
}}

## Scene Brief structure

The `scene_brief` string IS markdown. Write it with these four sections as
`##` headings (you can expand within each section — prose is welcome):

## Overall silhouette
Two to three paragraphs: what the object is, its dominant shapes and
character, style or era if visible. Describe it holistically — imagine you
are briefing a 3D artist who has not seen the photo.

## Proportions
One or two paragraphs: overall aspect ratio (taller than wide, roughly
cubic, long and shallow), which parts dominate, relative sizes of major
components (e.g. "the seat is about a third of the total height, the legs
account for the bottom half").

## Materials and color palette
One or two paragraphs: PBR cues (metallic / matte / glossy / transparent /
emissive), dominant colors with hex where you are confident, finish
qualities (polished, brushed, weathered, woven, frosted). Mention how the
materials differ across parts.

## Layout and symmetry
One paragraph: how the parts fit together — which are attached to which,
which repeat, visible symmetry axes, whether there is a clear front/back.

## Per-part narratives

For each entry in `parts`, write 2–5 sentences that a 3D artist can
implement directly. Cover, in prose:
  - What primitive family fits. Use phrases that map cleanly to primitives:
      "box with rounded edges", "thin flat panel", "slab", "wedge" → box / extrude
      "tall narrow cylinder", "short wide disc", "hollow tube" → cylinder
      "cone / tapered frustum narrowing from X to Y" → cylinder / cone
      "sphere", "flattened sphere / oblate", "hemisphere" → sphere
      "torus / ring / donut", "thick ring" → torus
      "lathe profile (rotationally symmetric, bulges at mid-height)" → lathe
        (use for bottles, vases, bells, goblets, pears, pawns)
      "curved tube following path A→B→C" → tube along path
      "flat 2D shape extruded along an axis" → extrude
  - Approximate size relative to the whole — "~30% of the object height",
    "two-thirds of the width", or "small / medium / large".
  - Position relative to the whole — "top front", "bottom center touching
    the floor", "wrapping the body at mid-height".
  - Color — a hex code `#rrggbb` when you are confident, otherwise NL like
    "dark walnut brown", "dusty teal".
  - Material phrasing that maps to PBR:
      metal family: "polished metal", "brushed metal", "chrome", "anodized"
      plastic:      "glossy plastic", "matte plastic", "rubber"
      natural:      "wood", "wicker", "ceramic", "leather", "fabric", "velvet"
      transparent:  "clear glass", "frosted glass", "water surface",
                    "translucent plastic"   (IMPORTANT — the Coder uses
                    this to pick MeshPhysicalMaterial with transmission/ior)
      emissive:     "LED / emissive", "light bulb glow"
      generic:      "generic" when unsure
  - Any modifier cue for organic curves — "slight bend along the Y axis",
    "gentle taper toward the top", "twisted", "bulging at mid-height".

Keep `count_hint` as a short structured phrase (the Coder keys off it for
`instanced_group` decisions): "one", "four symmetric", "two mirrored
pairs", "a single row of six".

## Decomposition granularity

Think like a technical illustrator preparing an exploded-view drawing:
  - Simple objects (ball, cup, stool): 3–8 parts.
  - Medium objects (chair, lamp, bottle-with-handle): 6–15 parts.
  - Complex objects (vehicle, appliance, instrument): 12–25 parts.

If your first pass produces fewer parts than this range suggests, scan the
image again for anything you skipped.

## Reminders — commonly-missed part types

Before finalizing, scan specifically for each of these (omit a category if
genuinely absent):

  - Thin / elongated / curved linear elements: cables, tubes, pipes, wires,
    straws, handles, bent rods, rope, strings, antennas. Describe them in
    the narrative as "curved tube following a path" or "straight cylindrical
    rod" — these become TubeGeometry or cylinder downstream.
  - Transparent / translucent surfaces: glass panes, windows, bottle bodies,
    lens covers, water / liquid, acrylic panels. Say "clear glass", "frosted
    glass", "water surface", or "translucent plastic" — map to PBR transmission.
  - Repeated instances suggesting symmetry: chair legs, wheel sets, buttons
    in a row, petals around a center, slats in a bench, fence posts. Group
    them into ONE part with `count_hint` describing the repetition.
  - Decorative trim: rings, bands, piping, seams, rivets, beading, buckles.
    Small but visually important.
  - Attachments: knobs, switches, buttons, dials, labels, logos, screws,
    hinges, latches. These add character even when tiny.
  - Caps and endings: bottle caps, pen caps, lid knobs, foot pads, tips.

## Rules

- Return ONLY a single JSON object — no prose, no markdown fences, no
  $defs wrapping around the object.
- `scene_brief` IS markdown — headings like `## Overall silhouette` and
  paragraphs INSIDE the JSON string are expected and fine.
- Collapse truly identical repeated elements into ONE part with count_hint.
  Four identical chair legs → one part with count_hint "four symmetric".
  Four different legs → four separate parts.
- Do not describe background, lighting, camera, or mood.
- Do not invent hidden parts that are not structurally necessary.
"""
