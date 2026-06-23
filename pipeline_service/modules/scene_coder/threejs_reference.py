from __future__ import annotations


THREEJS_PRIMITIVE_REFERENCE = """\
Three.js primitive reference (authoritative for SIR). Y is up; the compiler
auto-scales the whole scene into [-0.5, 0.5]^3, so emit correct proportions
and let normalization handle absolute sizes.

## Parametric primitives — SIR `params` list maps directly to Three.js ctor args

### box — BoxGeometry(width, height, depth)
Axis-aligned box centered at local origin. Defaults to 1×1×1.
Example params: [0.6, 0.05, 0.6] for a thin square table top.

### cylinder — CylinderGeometry(radiusTop, radiusBottom, height, radialSegments=32)
Central axis is Y; caps are at ±height/2. Key trick:
  - radiusTop == radiusBottom → straight cylinder
  - radiusTop == 0 AND radiusBottom > 0 → cone with apex pointing +Y
  - radiusBottom == 0 AND radiusTop > 0 → cone with apex pointing -Y
  - radiusTop != radiusBottom (both > 0) → frustum (truncated cone, tapered
    — perfect for bottle necks, rocket nozzles, bell profiles)
Use radialSegments=6 for a hexagonal prism, 32+ for smooth cylinders.

### sphere — SphereGeometry(radius, widthSegments=32, heightSegments=16)
Pole axis is Y. heightSegments is usually half of widthSegments.

### cone — ConeGeometry(radius, height, radialSegments=32)
Apex always points +Y, base radius at -height/2. Equivalent to
CylinderGeometry(0, radius, height, segments) — prefer `cylinder` when you
need to flip direction or taper asymmetrically.

### torus — TorusGeometry(radius, tube, radialSegments=16, tubularSegments=32)
Donut in the XY plane. `radius` = center-of-tube radius; `tube` = tube
cross-section radius. For tire-shaped objects: tube ≈ 0.3 * radius.

### plane — PlaneGeometry(width, height)
Flat rectangle in the XY plane facing +Z. Use `material.side = DoubleSide`
semantics implicitly: the compiler always emits DoubleSide via standard
material. Thin panels, signs, paper.

### circle — CircleGeometry(radius, segments=32)
Flat filled disc in XY plane. Like a plane but circular.

### ring — RingGeometry(innerRadius, outerRadius, thetaSegments=32)
Flat annulus in XY plane. innerRadius < outerRadius strictly.
For halos, washer rings, planetary rings.

XY-plane orientation rule — applies to torus, plane, circle, and ring:
These primitives all lie in the XY plane by default, perpendicular to Z.
The cylinder (and the scene's natural upright axis) runs along Y, with
horizontal cross-sections in XZ. Whenever one of these flat shapes should
lie horizontal — a tabletop, floor tile, base disc, collar, or hoop around
a cylinder — apply rotation.x = Math.PI/2 to tip it into the XZ plane.
Omitting this rotation is the most common source of incorrectly oriented
flat parts.

### torusknot — TorusKnotGeometry(radius, tube, tubularSegments=64, radialSegments=16, p=2, q=3)
Decorative pretzel-like knot. Rarely the right choice — prefer torus unless
the object is explicitly a knot.

### tetrahedron / octahedron / dodecahedron / icosahedron
Ctor: (radius, detail=0). `detail=0` = exact platonic; higher subdivides for
a more sphere-like look. Use for dice, crystals, low-poly rocks.

## Profile / path primitives — SIR uses dedicated fields, NOT `params`

### lathe — LatheGeometry(profile, segments=12)
SIR shape: `{"primitive": "lathe", "segments": 16, "profile": [[r,y], ...]}`
Rotates a 2D profile around Y axis. Rules:
  - Each point is `[radius, y]`. Order bottom→top (increasing y).
  - All radii must be ≥ 0. A radius of 0 closes that end of the lathe.
  - First point at r=0 closes the bottom; last point at r=0 closes the top.
Perfect for: vases, bottles, bells, goblets, pears, pawns, chess pieces,
flower pots, lamp posts — anything rotationally symmetric with a non-trivial
silhouette.

Smooth profiles: for organic, flowing silhouettes (vases, gourds, lamp bases)
route control points through a CatmullRomCurve3 confined to the XY plane (z=0),
then sample it with getSpacedPoints(48) and map each Vector3 to Vector2(p.x, p.y).
Use type 'centripetal' to prevent loops near tight turns; getSpacedPoints distributes
samples by arc length so curved sections are never under-sampled. For profiles with
sharp transitions (flat base → curved wall → straight neck), chain a CurvePath of
LineCurve3 and CubicBezierCurve3 segments instead.

Example -- simple vase via SplineCurve (bottom->top, 7 control points):
  const profile = new THREE.SplineCurve([
    new THREE.Vector2(0.00, 0.00),  // closed bottom center
    new THREE.Vector2(0.30, 0.00),  // base rim
    new THREE.Vector2(0.35, 0.20),  // belly start
    new THREE.Vector2(0.25, 0.60),  // waist
    new THREE.Vector2(0.30, 0.85),  // shoulder flare
    new THREE.Vector2(0.28, 1.00),  // neck
    new THREE.Vector2(0.00, 1.00),  // closed top
  ]).getSpacedPoints(48);
  new THREE.LatheGeometry(profile, 32);

### tube — TubeGeometry(CatmullRomCurve3(path), tubularSegments, radius, radialSegments, closed)
SIR shape: `{"primitive": "tube", "path": [[x,y,z], ...], "radius": 0.05,
"tubular_segments": 20, "radial_segments": 8, "closed": false}`
A circular tube swept along a 3D Catmull-Rom spline through the control
points. Use for: handles (arch above an object), pipes, cables, bent rods,
rope curves. radius=0.05 is a thin cable, 0.15 is a thick handle.
Path is local to the mesh's own transform.

### extrude — ExtrudeGeometry(Shape, options)
SIR shape: `{"primitive": "extrude", "shape": [[x,y], ...], "depth": 0.1,
"steps": 1, "bevel_enabled": false}`
Sweeps a 2D polygon (closed — first point reused as last) along local +Z by
`depth`. Use for: coins, badges, flat vehicle bodies (car silhouette
extruded sideways), letters, keys, custom flat panels. CCW winding for the
2D shape. bevel_enabled=true softens the edges for thicker objects.

Blade / edged-weapon cross-section: for sword blades, knife blades, axe
heads — use a 2D silhouette shape with small depth (0.02–0.05) and a
bevel that is large relative to that depth: bevelThickness ≈ depth * 0.8,
bevelSize ≈ depth * 0.4, bevelSegments=4. The bevel creates a lenticular
or wedge cross-section; without it the blade reads as a flat cardboard
cookie-cutter with no sense of sharpness.

## Modifiers — applied in order on top of the geometry, baked into vertices

Only add modifiers when NO parametric primitive can express the needed
shape. `modifiers: []` is a totally valid, preferred default.
  - bend(axis, angle): bend along axis by angle radians (e.g. arc a cylinder
    into a horseshoe). axis ∈ {x,y,z}. angle ∈ (-2π, 2π).
  - twist(axis, angle): rotate cross-sections along axis progressively.
  - taper(axis, factor): linearly narrow along axis. factor ∈ (-1, 1);
    positive = narrows toward +axis end.
  - bulge(axis, position, factor, spread): local fattening/pinching at a
    normalized position (0=bottom,1=top) on the axis.
  - spherify(factor): blend vertex positions toward a sphere. factor=1 is
    "push every vertex onto a sphere".

## Material — SIR Material → Three.js MeshStandardMaterial / MeshPhysicalMaterial

Material fields:
  color:             "#rrggbb" string
  kind:              "standard" (default) | "physical" | "basic"
  metalness:         0.0 (dielectric: wood, plastic, fabric) → 1.0 (pure metal)
  roughness:         0.0 (mirror) → 1.0 (completely diffuse / matte)
  transmission:      0 (opaque) → 1 (fully transparent, PHYSICAL ONLY)
  ior:               index of refraction, PHYSICAL ONLY. 1.5 glass, 1.33 water,
                     1.45 acrylic, 2.4 diamond
  opacity:           0 (invisible) → 1 (solid). Use for thin translucent panels.
  emissive:          "#rrggbb" — light the surface emits regardless of scene lighting.
                     Matches `color` for a uniformly glowing object.
  emissiveIntensity: multiplier on emissive color. 1.0 = subtle glow visible in
                     shadow; 2–5 = vivid self-lit appearance. kind=basic ignores
                     all scene lighting entirely — use for indicator lights or
                     flat graphic elements that must not be shaded.

Material selection cheat-sheet (drive from the reference image):
  polished metal      kind=standard metalness=0.95 roughness=0.20
  brushed metal       kind=standard metalness=0.80 roughness=0.45
  plastic (glossy)    kind=standard metalness=0.00 roughness=0.35
  plastic (matte)     kind=standard metalness=0.00 roughness=0.70
  wood                kind=standard metalness=0.00 roughness=0.90
  fabric / velvet     kind=standard metalness=0.00 roughness=0.95
  ceramic             kind=standard metalness=0.00 roughness=0.40
  rubber              kind=standard metalness=0.00 roughness=0.85
  glass (clear)       kind=physical  metalness=0.00 roughness=0.05 transmission=0.95 ior=1.5
  glass (frosted)     kind=physical  metalness=0.00 roughness=0.50 transmission=0.80 ior=1.5
  water surface       kind=physical  metalness=0.00 roughness=0.02 transmission=0.90 ior=1.33
  LED / neon glow     kind=standard  metalness=0.00 roughness=0.50 emissive=<color> emissiveIntensity=3.0
  screen / display    kind=standard  metalness=0.00 roughness=0.20 emissive=<color> emissiveIntensity=1.5
  indicator light     kind=basic     color=<color>
  unknown / generic   kind=standard metalness=0.00 roughness=0.80

When in doubt, prefer standard — physical is more expensive and only
visible when transmission or strong specular matters.

## Instanced groups — one geometry, N transforms

Use when the same mesh appears 2+ times in the same role:
  - 4 identical table legs (one leg mesh, 4 instance_transforms)
  - 8 petals around a flower center (radial symmetry via cos/sin)
  - 12 segments of a watch dial
  - Pairs of wheels / eyes / wings / arms (bilateral symmetry, 2 instances)
  - Regular arrays: keyboard keys, fence posts, solar-panel cells

SIR structure: an InstancedGroupNode holds `count` + `instance_transforms`
(parent-local placements of each copy) and ONE child mesh node that defines
the geometry/material. The mesh's own transform is the base; each instance
transform is composed on top of it.

Tip for radial layouts: place N instances at angle θ_i = 2π·i/N. Their
position becomes [r·cos(θ_i), 0, r·sin(θ_i)] in parent-local XZ plane; Y
stays constant for an XZ ring. Rotation.y = -θ_i to make each instance face
outward.

## Coordinate conventions reminder

  - Y is up. Horizontal plane is XZ.
  - Transforms cascade: a child at local [0.1,0,0] inside a group at
    world [0.5,0,0] lands at world [0.6,0,0].
  - `rotation` values are Euler xyz in radians. 90° = Math.PI/2.
  - Use groups to batch translate/rotate related parts (engine = block +
    exhaust + intake → all inside `engine_group` rotated 5° around Y, not
    three separate rotated meshes).
"""