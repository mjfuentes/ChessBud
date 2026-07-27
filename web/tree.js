// The repertoire drawn as one tree.
//
// Nothing here is invented: your book already branches every time the opponent
// has a choice, so each fork on screen is a fork in your preparation and each
// leaf is a line you cleared.
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

const TRUNK_LEN = 150
const LIMB_DECAY = 0.80
const MIN_SEG = 9
const SPREAD = 44
const UPWARD = 0.20
const ARC_FROM = -158 // one crown, spanning left to right
const ARC_TO = -22

// Leaf silhouette (blade only) from green-leaves-svgrepo-com.svg. It is
// symmetric about x=509.6, base at (509.6, 866.3), point at (509.6, 19.1),
// length 847.2 — so it grows straight up, an axis of -90 degrees, and each
// leaf is turned by (twig angle + 90) to lie along its twig. Leaves anchor at
// the BASE so they radiate from where they meet the wood.
const LEAF_PATH = 'M776.5 569.4c0 210.6-119.5 296.9-266.9 296.9s-267-86.3-267'
  + '-296.9 267-550.3 267-550.3 266.9 339.7 266.9 550.3z'
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
    const key = `${drill.user_color}|${path}`
    let slot = level.get(node.san)
    if (!slot) {
      slot = { san: node.san, key, kids: new Map(), ids: new Set(), weight: 0 }
      level.set(node.san, slot)
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
function layout(level, ctx, geo, bounds) {
  const kids = [...level.values()]
  kids.forEach((node, i) => {
    const noise = hashNoise(node.key)
    const len = Math.max(MIN_SEG, ctx.len * LIMB_DECAY)
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
    layout(node.kids, { x: bx, y: by, angle, len, w: w1, taper: 0.88 }, geo, bounds)
  })
}

function placeLeaf(group, g, cls, scale) {
  const lean = g.noise * 34
  const blade = (11 + g.noise * 2.5) * scale
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
    if (!g) continue
    wood.append(el('path', {
      d: ribbon(g.x0, g.y0, g.x1, g.y1, g.w0, g.w1, g.noise * g.len * 0.13),
      class: g.shared ? 'tw-wood tw-shared' : 'tw-wood',
    }))
    const kids = node.kids || []
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
  defs.append(el('path', { id: 'cb-leaf', d: LEAF_PATH }))
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

  const trunkTop = GROUND - TRUNK_LEN
  const geo = new Map()
  const total = order.reduce((s, l) => s + l.weight, 0) || 1
  order.forEach((limb, i) => {
    const t = order.length > 1 ? i / (order.length - 1) : 0.5
    const angle = ARC_FROM + (ARC_TO - ARC_FROM) * t
    // limbs leave the trunk over its whole upper length, not from one point
    const rise = Math.sin(rad(180 * t)) * 34
    // Width is a limb's SHARE of the repertoire, not its raw size: an
    // unbounded sqrt(games) turned the 1.e4 limb — which merges every defence
    // you meet — into a plank wider than the trunk. The first segment also
    // tapers hardest, since that is where the most wood is shed.
    const share = limb.weight / total
    layout(
      new Map([[limb.san, limb]]),
      {
        x: VIEW.w / 2,
        y: trunkTop + 18 - rise,
        angle,
        len: TRUNK_LEN * (0.46 + Math.min(0.3, share * 1.2)),
        w: 2.4 + Math.min(6.5, share * 22),
        taper: 0.72,
      },
      geo, bounds,
    )
  })

  svg.append(el('path', {
    d: ribbon(VIEW.w / 2, GROUND, VIEW.w / 2, trunkTop, 15, 7, 3),
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

  const pad = 22
  svg.setAttribute('viewBox', `${(bounds.minX - pad).toFixed(0)} `
    + `${(bounds.minY - pad).toFixed(0)} `
    + `${(bounds.maxX - bounds.minX + pad * 2).toFixed(0)} `
    + `${(GROUND - bounds.minY + pad * 2).toFixed(0)}`)
  host.replaceChildren(svg)
  return groups
}
