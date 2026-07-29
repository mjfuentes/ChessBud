// The repertoire drawn as one tree.
//
// Nothing here is invented: your book already branches every time the opponent
// has a choice, so each fork on screen is a fork in your preparation and each
// leaf is a line you cleared. Nothing here is unearned either — the server sends
// the wood you have grown on and nothing beyond it, so every branch on screen is
// a position you have played onto. An empty repertoire is a shoot, not a bare
// crown, and the tree gets bigger as the leaves accumulate rather than merely
// filling in a silhouette it already had.
//
// Openings that begin the same way share the same wood. All your defences to
// 1.e4 as Black grow off one limb, because they ARE one position until White
// deviates. Geometry therefore comes from a single merged trie of the whole
// repertoire, while the drawing stays per-opening so hovering still lights one
// of them: shared segments are simply drawn twice at identical coordinates.
//
// Three rules borrowed from real trees do the rest:
//   * Leonardo — a limb's cross-section equals the sum of its children's, so
//     width decays as 1/sqrt(n) at every fork and taper is never uniform.
//   * Branches are filled ribbons, not strokes. SVG strokes cannot taper, and
//     a constant-width line is what makes a drawn tree look like wire.
//   * Phototropism — branches bend back toward the light, so no two children
//     leave a fork at mirrored angles.
//
// Jitter is hashed from the move itself, never random: the silhouette is
// stable between visits, so a branch that changed shape really did change.

const VIEW = { w: 640, h: 680 }
const GROUND = VIEW.h - 18
const SVG_NS = 'http://www.w3.org/2000/svg'

const LIMB_DECAY = 0.80
const SPREAD = 44
const UPWARD = 0.20
const ARC_FROM = -158 // one crown, spanning left to right
const ARC_TO = -22

// The tree is the size of what you have grown. A repertoire with four leaves on
// it is a shoot, and it thickens and lengthens as leaves accumulate — so the
// picture changes between visits even when the shape of the wood does not, and
// an empty ladder cannot be mistaken for a tree that dropped its leaves.
//
// Vigour is sqrt so early leaves show most: the first few visibly enlarge the
// tree, and the hundredth barely does. Saturating at LEAVES_FULL keeps the
// mature tree the size the layout was drawn for.
// The floor is set by legibility, not by the data: a nothing-sized shoot reads
// as a rendering artefact rather than as an empty repertoire.
const LEAVES_FULL = 300
const TRUNK_LEN = { min: 46, max: 150 }
const TRUNK_HALF_W = { min: 3.4, max: 15 }
const MIN_SEG = { min: 3.5, max: 9 }
const lerp = (r, t) => r.min + (r.max - r.min) * t

function countLeaves(drills) {
  let n = 0
  const walk = (nodes) => {
    for (const node of nodes) {
      if (node.on) n += 1
      walk(node.kids || [])
    }
  }
  for (const drill of drills) walk(drill.book || [])
  return n
}

// Leaf silhouette (blade only) from green-leaves-svgrepo-com.svg. It is
// symmetric about x=509.6, base at (509.6, 866.3), point at (509.6, 19.1),
// length 847.2 — so it grows straight up, an axis of -90 degrees, and each
// leaf is turned by (twig angle + 90) to lie along its twig. Leaves anchor at
// the BASE so they radiate from where they meet the wood.
const LEAF_PATH = 'M776.5 569.4c0 210.6-119.5 296.9-266.9 296.9s-267-86.3-267'
  + '-296.9 267-550.3 267-550.3 266.9 339.7 266.9 550.3z'
// The rest of the artwork: outline, midrib and the four veins. Drawn with
// presentation attributes rather than a class, because CSS selectors do not
// reach inside a <use> shadow tree — only inherited properties do, which is
// how the blade still takes its colour from .tw-leaf.
const LEAF_OUTLINE = 'M509.6 876.2c-80.3 0-148-25-195.8-72.3-26-25.7-46-58-59.6'
  + '-95.9-14.3-39.9-21.6-86.6-21.6-138.6 0-49.3 14.3-109.4 42.4-178.6 22.3-54.9'
  + ' 53.4-115.6 92.4-180.7C433.8 99.6 501 13.8 501.7 12.9c1.9-2.4 4.8-3.8 7.9'
  + '-3.8s6 1.4 7.9 3.8c0.7 0.9 67.9 86.7 134.2 197.2 39 65 70.1 125.8 92.4 180.7'
  + ' 28.2 69.2 42.4 129.3 42.4 178.6 0 52-7.3 98.7-21.6 138.6-13.6 37.9-33.6'
  + ' 70.2-59.6 95.9-47.7 47.3-115.4 72.3-195.7 72.3z m0-840.7c-19.6 25.8-72.6'
  + ' 97.6-125 185.1-38.5 64.1-69.1 124-91 177.8-27.2 66.7-40.9 124.2-40.9 171 0'
  + ' 96.7 25.3 170.8 75.2 220.3 43.9 43.5 106.8 66.6 181.7 66.6s137.8-23 181.7'
  + '-66.6c49.9-49.5 75.2-123.6 75.2-220.3 0-46.7-13.8-104.3-41-171.1-21.9-53.9'
  + '-52.6-113.8-91.1-177.9C582 133 529.1 61.3 509.6 35.5z'
const LEAF_MIDRIB = 'M509.6 1017.5c-5.5 0-10-4.5-10-10V218.8c0-5.5 4.5-10 10-10s10'
  + ' 4.5 10 10v788.6c0 5.6-4.5 10.1-10 10.1z'
const LEAF_VEINS = 'M509.6 521.2c-2.7 0-5.5-1.1-7.4-3.3l-137.5-153c-3.7-4.1-3.4'
  + '-10.4 0.8-14.1 4.1-3.7 10.4-3.4 14.1 0.8l137.5 153c3.7 4.1 3.4 10.4-0.8 14.1'
  + '-2 1.7-4.3 2.5-6.7 2.5zM509.6 782.4c-2.8 0-5.6-1.2-7.6-3.5L308.7 552c-3.6-4.2'
  + '-3.1-10.5 1.1-14.1 4.2-3.6 10.5-3.1 14.1 1.1l193.2 226.9c3.6 4.2 3.1 10.5-1.1'
  + ' 14.1-1.8 1.6-4.1 2.4-6.4 2.4zM509.6 368.2c-2.6 0-5.1-1-7.1-2.9-3.9-3.9-3.9'
  + '-10.2 0-14.1l93.2-93.2c3.9-3.9 10.2-3.9 14.1 0 3.9 3.9 3.9 10.2 0 14.1l-93.2'
  + ' 93.2c-1.9 1.9-4.5 2.9-7 2.9zM518.6 642.6c-2.7 0-5.5-1.1-7.4-3.3-3.7-4.1-3.3'
  + '-10.4 0.8-14.1l135.8-121.8c4.1-3.7 10.4-3.3 14.1 0.8 3.7 4.1 3.3 10.4-0.8'
  + ' 14.1L525.3 640c-1.9 1.7-4.3 2.6-6.7 2.6z'
// plain hex, not oklch: this is a presentation attribute on content inside a
// <use>, where colour-level-4 support cannot be assumed
const LEAF_INK = '#212d1e'
const LEAF_AXIS = -90
const LEAF_STEM_X = 509.6
const LEAF_STEM_Y = 866.3
const LEAF_UNIT = 847.2

const el = (name, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, name)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  return node
}

const rad = (d) => (d * Math.PI) / 180

function hashNoise(seed) {
  let h = 2166136261
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) % 20011) / 10005.5 - 1
}

function ribbon(ax, ay, bx, by, w0, w1, bow) {
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  const nx = -dy / len
  const ny = dx / len
  const mx = (ax + bx) / 2 + nx * bow
  const my = (ay + by) / 2 + ny * bow
  return `M ${(ax + nx * w0).toFixed(1)} ${(ay + ny * w0).toFixed(1)}`
    + ` Q ${(mx + nx * w1).toFixed(1)} ${(my + ny * w1).toFixed(1)}`
    + ` ${(bx + nx * w1).toFixed(1)} ${(by + ny * w1).toFixed(1)}`
    + ` L ${(bx - nx * w1).toFixed(1)} ${(by - ny * w1).toFixed(1)}`
    + ` Q ${(mx - nx * w1).toFixed(1)} ${(my - ny * w1).toFixed(1)}`
    + ` ${(ax - nx * w0).toFixed(1)} ${(ay - ny * w0).toFixed(1)} Z`
}

// ---- merge every opening into one skeleton -------------------------------
// Nodes are keyed by colour and the opponent moves that reach them, so two
// openings sharing an opening sequence share the wood that carries it.

function mergeForest(drills) {
  const roots = new Map()
  const add = (level, path, node, drill) => {
    // A White drill's first rung has no opponent move yet, so its trie root is
    // a placeholder with no san. Left in, every White opening merges onto that
    // one stem — 333 games of weight — and it draws as a second trunk. Hoist
    // its children instead, so White gets a limb per reply exactly as Black
    // gets one per first move.
    if (!node.san) {
      for (const kid of node.kids || []) add(level, kid.san, kid, drill)
      return null
    }
    // Keyed by colour AND the moves that reach it, never by the move alone.
    // Keyed by move alone, your 1.e4 as White and the 1.e4 you FACE as Black are
    // the same slot: the Italian as White and the Italian as Black merge onto one
    // limb, and since the slot keeps whichever colour reached it first, the other
    // colour's branches then fail their own geometry lookup and silently vanish.
    // The position after 1.e4 is the same position, but playing it and answering
    // it are different practice, and this tree is a picture of practice.
    const key = `${drill.user_color}|${path}`
    let slot = level.get(key)
    if (!slot) {
      slot = { san: node.san, key, kids: new Map(), ids: new Set(), weight: 0 }
      level.set(key, slot)
    }
    slot.ids.add(drill.id)
    slot.weight += drill.games || 1
    for (const kid of node.kids || []) {
      add(slot.kids, `${path} ${kid.san}`.trim(), kid, drill)
    }
    return slot
  }
  for (const drill of drills) {
    for (const node of drill.book || []) add(roots, node.san, node, drill)
  }
  return roots
}

// Walk the merged skeleton once, recording where every node sits. Openings
// then trace their own path through this map.
function layout(level, ctx, geo, bounds, minSeg) {
  const kids = [...level.values()]
  kids.forEach((node, i) => {
    const noise = hashNoise(node.key)
    const len = Math.max(minSeg, ctx.len * LIMB_DECAY)
    const toVertical = (-90 - ctx.angle) * UPWARD
    const spread = SPREAD / Math.max(1, Math.pow(kids.length, 0.55))
    const offset = kids.length === 1
      ? noise * 9
      : (i / (kids.length - 1) - 0.5) * spread * kids.length * 0.8
    const angle = ctx.angle + toVertical + offset + noise * 11
    const a = rad(angle)
    const bx = ctx.x + Math.cos(a) * len
    const by = ctx.y + Math.sin(a) * len
    // Leonardo: the parent's cross-section is shared out among its children by
    // how much of the repertoire flows through each
    // ...and a limb narrows as it runs even where it does not fork, or a chain
    // of single children keeps its full width and ends in a blunt stub
    const total = kids.reduce((s, k) => s + k.weight, 0) || 1
    const w1 = Math.max(0.4, ctx.w * Math.sqrt(node.weight / total) * (ctx.taper || 0.88))
    geo.set(node.key, {
      x0: ctx.x, y0: ctx.y, x1: bx, y1: by, w0: ctx.w, w1, angle, noise, len,
      // wood more than one opening grows on — it belongs to all of them, so
      // hovering any single opening must not claim it
      shared: node.ids.size > 1,
    })
    bounds.minX = Math.min(bounds.minX, bx)
    bounds.maxX = Math.max(bounds.maxX, bx)
    bounds.minY = Math.min(bounds.minY, by)
    layout(node.kids, { x: bx, y: by, angle, len, w: w1, taper: 0.88 },
           geo, bounds, minSeg)
  })
}

function placeLeaf(group, g, cls, scale) {
  const lean = g.noise * 34
  const blade = (17 + g.noise * 2.5) * scale
  group.append(el('use', {
    href: '#cb-leaf',
    class: cls,
    transform: `translate(${g.x1.toFixed(1)} ${g.y1.toFixed(1)})`
      + ` rotate(${(g.angle + lean - LEAF_AXIS).toFixed(0)})`
      + ` scale(${(blade / LEAF_UNIT).toFixed(5)})`
      + ` translate(${-LEAF_STEM_X} ${-LEAF_STEM_Y})`,
  }))
}

// Draw one opening by tracing its own nodes through the shared geometry.
function drawBook(wood, canopy, nodes, path, color, geo) {
  for (const node of nodes) {
    const here = `${path} ${node.san}`.trim()
    const g = geo.get(`${color}|${here}`)
    const kids = node.kids || []
    // the hoisted placeholder has no geometry of its own; keep walking through
    // it rather than dropping the branch it carries
    if (!g) {
      drawBook(wood, canopy, kids, here, color, geo)
      continue
    }
    wood.append(el('path', {
      d: ribbon(g.x0, g.y0, g.x1, g.y1, g.w0, g.w1, g.noise * g.len * 0.13),
      class: g.shared ? 'tw-wood tw-shared' : 'tw-wood',
    }))
    if (node.on) {
      placeLeaf(canopy, g, 'tw-leaf', Math.min(1.25, 0.6 + g.w0 * 0.32))
    } else if (!kids.length) {
      canopy.append(el('use', {
        href: '#cb-leaf',
        class: 'tw-bud',
        transform: `translate(${g.x1.toFixed(1)} ${g.y1.toFixed(1)})`
          + ` rotate(${(g.angle + g.noise * 20 - LEAF_AXIS).toFixed(0)})`
          + ` scale(${(3.4 / LEAF_UNIT).toFixed(5)})`
          + ` translate(${-LEAF_STEM_X} ${-LEAF_STEM_Y})`,
      }))
    }
    drawBook(wood, canopy, kids, here, color, geo)
  }
}

export function renderTree(host, drills, handlers) {
  const bounds = { minX: VIEW.w / 2, maxX: VIEW.w / 2, minY: GROUND }
  const svg = el('svg', {
    class: 'tw-svg',
    role: 'img',
    'aria-label': 'Your repertoire as one tree. Openings that start alike share '
      + 'the same wood; every leaf is a line you have cleared.',
  })
  const defs = el('defs')
  const leaf = el('g', { id: 'cb-leaf' })
  leaf.append(el('path', { d: LEAF_PATH })) // no fill: inherits from .tw-leaf
  for (const d of [LEAF_OUTLINE, LEAF_MIDRIB, LEAF_VEINS]) {
    leaf.append(el('path', { d, fill: LEAF_INK }))
  }
  defs.append(leaf)
  svg.append(defs)

  const playable = drills.filter((d) => d.book?.length)
  const forest = mergeForest(playable)

  // One crown. Limbs are interleaved by colour rather than blocked left and
  // right, so the tree reads as a single canopy instead of two half-trees.
  const byColour = { white: [], black: [] }
  for (const node of forest.values()) {
    byColour[node.key.startsWith('white|') ? 'white' : 'black'].push(node)
  }
  for (const list of Object.values(byColour)) list.sort((a, b) => b.weight - a.weight)
  const limbs = []
  for (let i = 0; i < Math.max(byColour.white.length, byColour.black.length); i += 1) {
    if (byColour.white[i]) limbs.push(byColour.white[i])
    if (byColour.black[i]) limbs.push(byColour.black[i])
  }
  // heaviest limbs low and wide, finer ones higher and steeper
  limbs.sort((a, b) => b.weight - a.weight)
  const order = []
  limbs.forEach((limb, i) => (i % 2 ? order.unshift(limb) : order.push(limb)))

  const vigour = Math.min(1, Math.sqrt(countLeaves(playable) / LEAVES_FULL))
  const trunkLen = lerp(TRUNK_LEN, vigour)
  const trunkBaseW = lerp(TRUNK_HALF_W, vigour)
  const trunkTopW = trunkBaseW * 0.47
  const minSeg = lerp(MIN_SEG, vigour)

  const trunkTop = GROUND - trunkLen
  const geo = new Map()
  const total = order.reduce((s, l) => s + l.weight, 0) || 1
  order.forEach((limb, i) => {
    const t = order.length > 1 ? i / (order.length - 1) : 0.5
    const angle = ARC_FROM + (ARC_TO - ARC_FROM) * t
    // Limbs leave the trunk over its whole upper length, not from one point —
    // measured DOWN from the top, so the highest of them starts exactly where
    // the trunk ends. Measured up from a point part-way, as it was, they floated
    // clear of the trunk: invisible in a dense crown, and a plain broken stem on
    // a sapling with one limb on it.
    const drop = (1 - Math.sin(rad(180 * t))) * trunkLen * 0.23
    // Width is a limb's SHARE of the repertoire, not its raw size: an
    // unbounded sqrt(games) turned the 1.e4 limb — which merges every defence
    // you meet — into a plank wider than the trunk. The first segment also
    // tapers hardest, since that is where the most wood is shed.
    const share = limb.weight / total
    layout(
      new Map([[limb.san, limb]]),
      {
        x: VIEW.w / 2,
        y: trunkTop + drop,
        angle,
        len: trunkLen * (0.46 + Math.min(0.3, share * 1.2)),
        // never as thick as the trunk it leaves, or a heavy limb — 214 of your
        // games as Black answer 1.e4 — reads as a second trunk rather than as
        // a branch
        w: Math.min(trunkTopW * 0.92, 2.4 + Math.min(3.2, share * 22)),
        taper: 0.72,
      },
      geo, bounds, minSeg,
    )
  })

  svg.append(el('path', {
    d: ribbon(VIEW.w / 2, GROUND, VIEW.w / 2, trunkTop,
              trunkBaseW, trunkTopW, trunkBaseW * 0.2),
    class: 'tw-wood tw-trunk',
  }))

  const groups = new Map()
  const canopies = []
  for (const drill of playable) {
    const wood = el('g', { class: 'tw-branch', 'data-id': drill.id })
    const canopy = el('g', { class: 'tw-branch', 'data-id': drill.id })
    drawBook(wood, canopy, drill.book, '', drill.user_color, geo)
    for (const g of [wood, canopy]) {
      g.addEventListener('pointerenter', () => handlers.onEnter(drill.id))
      g.addEventListener('pointerleave', () => handlers.onLeave(drill.id))
      g.addEventListener('click', () => handlers.onPick(drill.id))
    }
    svg.append(wood)
    canopies.push(canopy)
    groups.set(drill.id, [wood, canopy])
  }
  // every leaf in the tree sits above every limb in the tree
  for (const canopy of canopies) svg.append(canopy)

  // The frame never shrinks below the full canvas. Fitting it to the wood would
  // scale a shoot up until it filled the panel, and a shoot that fills the panel
  // is just the old tree again — growth is only visible against a fixed frame.
  // It still expands for a crown that outgrows the canvas.
  const pad = 22
  const x0 = Math.min(bounds.minX - pad, 0)
  const x1 = Math.max(bounds.maxX + pad, VIEW.w)
  const y0 = Math.min(bounds.minY - pad, 0)
  svg.setAttribute('viewBox', `${x0.toFixed(0)} ${y0.toFixed(0)} `
    + `${(x1 - x0).toFixed(0)} ${(GROUND + pad - y0).toFixed(0)}`)
  host.replaceChildren(svg)
  return groups
}
