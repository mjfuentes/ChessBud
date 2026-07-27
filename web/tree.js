// The repertoire drawn as an actual tree.
//
// Nothing here is invented: your book already branches every time the opponent
// has a choice, so each fork on screen is a fork in your preparation and each
// leaf is a line you cleared. The drawing follows three rules borrowed from
// real trees, which is what makes it read as botanical rather than as a chart:
//
//   * Leonardo's rule — a limb's cross-section equals the sum of its children's,
//     so width decays as 1/sqrt(n) at every fork and the taper is never uniform.
//   * Branches are filled ribbons, not strokes. SVG strokes cannot taper, and a
//     constant-width line is the single thing that makes a tree look like wire.
//   * Phototropism — every branch bends a little toward the light, so no two
//     children leave a fork at mirrored angles.
//
// Jitter is hashed from the move itself, never random: your tree keeps its
// silhouette between visits, and a branch that changed shape really did change.

const VIEW = { w: 640, h: 680 }
const GROUND = VIEW.h - 18
const SVG_NS = 'http://www.w3.org/2000/svg'

const TRUNK_LEN = 132
const LIMB_DECAY = 0.80 // each generation is this fraction of its parent
const MIN_SEG = 9
const SPREAD = 46 // degrees a fork opens, before jitter
const UPWARD = 0.22 // how strongly branches turn back toward vertical

const el = (name, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, name)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  return node
}

const rad = (d) => (d * Math.PI) / 180

// deterministic per-node noise in [-1, 1]
function hashNoise(seed) {
  let h = 2166136261
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) % 20011) / 10005.5 - 1
}

// A tapered ribbon from a to b: two quadratic edges meeting at the tip, so the
// limb narrows continuously and the curve carries a real bow.
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

// Leaves sit around the twig end on a golden-angle spiral — the arrangement
// real foliage uses, and the reason a cluster never looks like a row of dots.
function leafCluster(group, x, y, angle, n, cls, scale) {
  const count = Math.min(n, 7)
  for (let i = 0; i < count; i += 1) {
    const spin = i * 137.5
    const reach = (2.4 + i * 1.5) * scale
    const a = rad(angle + Math.sin(rad(spin)) * 42)
    group.append(el('ellipse', {
      cx: (x + Math.cos(a) * reach).toFixed(1),
      cy: (y + Math.sin(a) * reach).toFixed(1),
      rx: (3.1 * scale).toFixed(1),
      ry: (1.9 * scale).toFixed(1),
      transform: `rotate(${(angle + Math.sin(rad(spin)) * 42 + 90).toFixed(0)} `
        + `${(x + Math.cos(a) * reach).toFixed(1)} ${(y + Math.sin(a) * reach).toFixed(1)})`,
      class: cls,
    }))
  }
}

function grow(group, node, ctx, bounds) {
  const noise = hashNoise(node.san + node.d)
  const len = Math.max(MIN_SEG, ctx.len * LIMB_DECAY)
  // pull back toward vertical as we climb, then scatter by the move's own hash
  const toVertical = (-90 - ctx.angle) * UPWARD
  const angle = ctx.angle + toVertical + noise * 13
  const a = rad(angle)
  const bx = ctx.x + Math.cos(a) * len
  const by = ctx.y + Math.sin(a) * len
  bounds.minX = Math.min(bounds.minX, bx)
  bounds.maxX = Math.max(bounds.maxX, bx)
  bounds.minY = Math.min(bounds.minY, by)
  bounds.maxY = Math.max(bounds.maxY, by)
  const kids = node.kids || []
  // Leonardo: cross-section is conserved across a fork
  const childW = ctx.w / Math.sqrt(Math.max(1, kids.length) + 0.6)

  group.append(el('path', {
    d: ribbon(ctx.x, ctx.y, bx, by, ctx.w, Math.max(0.45, childW), noise * len * 0.13),
    class: 'tw-wood',
  }))

  if (node.on) {
    leafCluster(group, bx, by, angle, 3, 'tw-leaf', Math.min(1.15, 0.55 + ctx.w * 0.3))
  } else if (!kids.length) {
    group.append(el('circle', {
      cx: bx.toFixed(1), cy: by.toFixed(1), r: 2.1, class: 'tw-bud',
    }))
  }

  const spread = SPREAD / Math.max(1, Math.pow(kids.length, 0.55))
  kids.forEach((kid, i) => {
    const offset = kids.length === 1
      ? noise * 9
      : (i / (kids.length - 1) - 0.5) * spread * kids.length * 0.8
    grow(group, kid, {
      x: bx, y: by, angle: angle + offset, len, w: childW,
    }, bounds)
  })
}

export function renderTree(host, drills, handlers) {
  const bounds = { minX: VIEW.w / 2, maxX: VIEW.w / 2, minY: GROUND, maxY: GROUND }
  const svg = el('svg', {
    class: 'tw-svg',
    role: 'img',
    'aria-label': 'Your repertoire as a tree. Every fork is a choice your '
      + 'opponents make; every leaf is a line you have cleared.',
  })

  const sides = [
    { white: true, dir: -1, list: [] },
    { white: false, dir: 1, list: [] },
  ]
  for (const side of sides) {
    side.list = drills
      .filter((d) => (d.user_color === 'white') === side.white && d.book?.length)
      .sort((a, b) => (b.games || 0) - (a.games || 0))
  }

  const trunkTop = GROUND - TRUNK_LEN
  svg.append(el('path', {
    d: ribbon(VIEW.w / 2, GROUND, VIEW.w / 2 - 3, trunkTop, 13, 8, 4),
    class: 'tw-wood tw-trunk',
  }))

  const groups = new Map()
  for (const side of sides) {
    // limbs leave the trunk at intervals, heaviest lowest, like a real crown
    side.list.forEach((drill, i) => {
      const t = side.list.length > 1 ? i / (side.list.length - 1) : 0.3
      const group = el('g', { class: 'tw-branch', 'data-id': drill.id })
      const baseW = 1.1 + Math.sqrt(drill.games || 1) * 0.5
      const originY = trunkTop + 26 - t * 34
      const originX = VIEW.w / 2 - 3 + side.dir * 4
      const angle = -90 + side.dir * (74 - t * 40)
      const root = { san: drill.id, d: 0, on: false, kids: drill.book }
      grow(group, root, {
        x: originX, y: originY, angle,
        len: TRUNK_LEN * (0.52 + Math.min(0.35, (drill.games || 1) / 260)),
        w: baseW,
      }, bounds)
      group.addEventListener('pointerenter', () => handlers.onEnter(drill.id))
      group.addEventListener('pointerleave', () => handlers.onLeave(drill.id))
      group.addEventListener('click', () => handlers.onPick(drill.id))
      svg.append(group)
      groups.set(drill.id, group)
    })
  }
  // fit the frame to what actually grew, so no twig is ever clipped and the
  // crown fills the space it is given whatever shape the repertoire takes
  const pad = 22
  const x = bounds.minX - pad
  const y = bounds.minY - pad
  svg.setAttribute('viewBox', `${x.toFixed(0)} ${y.toFixed(0)} `
    + `${(bounds.maxX - bounds.minX + pad * 2).toFixed(0)} `
    + `${(GROUND - bounds.minY + pad * 2).toFixed(0)}`)
  host.replaceChildren(svg)
  return groups
}
