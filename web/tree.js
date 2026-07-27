// The repertoire drawn as what it actually is: a tree.
//
// Your drills branch on the opponent's replies, and a "line" is that reply
// sequence — so nothing here is decorative. One trunk, two limbs (White leans
// left, Black right, matching the lists either side). Each opening is a branch
// whose LENGTH is its ladder depth and whose THICKNESS is how often you really
// face it. Distance along a branch is the depth axis, so foliage sits at the
// rung it was earned on and the bare buds at the tip are what is left to clear.
//
// Layout is deterministic — seeded entirely by the data — so your tree keeps
// its silhouette between visits and you notice when a branch has grown.

const VIEW = { w: 620, h: 660 }
const TRUNK_X = VIEW.w / 2
const TRUNK_BASE = VIEW.h - 28
const TRUNK_TOP = VIEW.h - 250
const SVG_NS = 'http://www.w3.org/2000/svg'

// a rung of depth is this many pixels of branch
const PX_PER_DEPTH = 13
const BRANCH_BASE = 26
const MAX_BUDS_DRAWN = 9 // beyond this the tip reads as a cluster, not a count

const el = (name, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, name)
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  return node
}

const rad = (deg) => (deg * Math.PI) / 180

// Branches emerge at intervals along the limb and lift toward the vertical as
// they climb, the way a real tree opens: heavy low limbs, fine high ones.
function branchGeometry(index, count, isWhite) {
  const t = count > 1 ? index / (count - 1) : 0.35
  const dir = isWhite ? -1 : 1
  const originX = TRUNK_X + dir * (10 + t * 26)
  const originY = TRUNK_TOP - t * 250
  // low branches reach out, high ones reach up; the alternation stops the fan
  // from looking like a hand of cards
  const spread = 62 - t * 34 + (index % 2 ? -7 : 7)
  return { originX, originY, angle: dir * spread - 90 + (isWhite ? 0 : 0) }
}

function budPositions(tipX, tipY, angle, n) {
  const drawn = Math.min(n, MAX_BUDS_DRAWN)
  const out = []
  for (let i = 0; i < drawn; i += 1) {
    const offset = drawn === 1 ? 0 : (i / (drawn - 1) - 0.5) * 54
    const a = rad(angle + offset)
    const reach = 7 + (i % 2 ? 4 : 0)
    out.push([tipX + Math.cos(a) * reach, tipY + Math.sin(a) * reach])
  }
  return out
}

function drawBranch(group, drill, geo, handlers) {
  const L = drill.ladder
  const length = BRANCH_BASE + L.depth * PX_PER_DEPTH
  const a = rad(geo.angle)
  const tipX = geo.originX + Math.cos(a) * length
  const tipY = geo.originY + Math.sin(a) * length
  // a gentle bow so branches read as grown rather than drafted
  const bowX = (geo.originX + tipX) / 2 + Math.cos(a + rad(90)) * length * 0.09
  const bowY = (geo.originY + tipY) / 2 + Math.sin(a + rad(90)) * length * 0.09
  const width = 1 + Math.sqrt(drill.games || 1) * 0.42

  const limb = el('path', {
    d: `M ${geo.originX} ${geo.originY} Q ${bowX} ${bowY} ${tipX} ${tipY}`,
    class: 'tw-limb',
    'stroke-width': width.toFixed(2),
  })
  group.append(limb)

  // foliage from rungs already cleared, placed at the depth it was earned
  for (const rung of L.rungs || []) {
    if (!rung.cleared) continue
    const at = (BRANCH_BASE + rung.depth * PX_PER_DEPTH) / length
    const px = geo.originX + Math.cos(a) * length * at
    const py = geo.originY + Math.sin(a) * length * at
    const isTip = rung.depth === L.depth
    for (const [x, y] of budPositions(px, py, geo.angle, rung.cleared)) {
      group.append(el('circle', {
        cx: x.toFixed(1), cy: y.toFixed(1), r: isTip ? 3.1 : 2.4,
        class: isTip ? 'tw-leaf tw-leaf-tip' : 'tw-leaf',
      }))
    }
  }
  // what is still to clear at the growing tip
  const left = Math.max(0, L.total - L.cleared)
  for (const [x, y] of budPositions(tipX, tipY, geo.angle, left)) {
    group.append(el('circle', {
      cx: x.toFixed(1), cy: y.toFixed(1), r: 2.6, class: 'tw-bud',
    }))
  }
  if (L.at_ceiling) {
    group.append(el('circle', {
      cx: tipX.toFixed(1), cy: tipY.toFixed(1), r: 1.6, class: 'tw-cap',
    }))
  }
  // a fat invisible target: buds are far too small to aim at
  const hit = el('path', {
    d: `M ${geo.originX} ${geo.originY} Q ${bowX} ${bowY} ${tipX} ${tipY}`,
    class: 'tw-hit',
    'stroke-width': Math.max(18, width + 14),
  })
  hit.addEventListener('pointerenter', () => handlers.onEnter(drill.id))
  hit.addEventListener('pointerleave', () => handlers.onLeave(drill.id))
  hit.addEventListener('click', () => handlers.onPick(drill.id))
  group.append(hit)
  return { tipX, tipY }
}

export function renderTree(host, drills, handlers) {
  const svg = el('svg', {
    viewBox: `0 0 ${VIEW.w} ${VIEW.h}`,
    class: 'tw-svg',
    role: 'img',
    'aria-label': 'Your repertoire as a tree: branch length is depth, '
      + 'foliage is lines you have cleared.',
  })

  svg.append(el('path', {
    d: `M ${TRUNK_X - 9} ${TRUNK_BASE} Q ${TRUNK_X - 3} ${(TRUNK_BASE + TRUNK_TOP) / 2}`
      + ` ${TRUNK_X - 2.5} ${TRUNK_TOP - 60}`,
    class: 'tw-trunk',
  }))
  svg.append(el('path', {
    d: `M ${TRUNK_X + 9} ${TRUNK_BASE} Q ${TRUNK_X + 3} ${(TRUNK_BASE + TRUNK_TOP) / 2}`
      + ` ${TRUNK_X + 2.5} ${TRUNK_TOP - 60}`,
    class: 'tw-trunk',
  }))
  svg.append(el('path', {
    d: `M ${TRUNK_X - 2.5} ${TRUNK_TOP - 60} C ${TRUNK_X - 26} ${TRUNK_TOP - 150}`
      + ` ${TRUNK_X - 30} ${TRUNK_TOP - 190} ${TRUNK_X - 34} ${TRUNK_TOP - 250}`,
    class: 'tw-limb tw-limb-main',
  }))
  svg.append(el('path', {
    d: `M ${TRUNK_X + 2.5} ${TRUNK_TOP - 60} C ${TRUNK_X + 26} ${TRUNK_TOP - 150}`
      + ` ${TRUNK_X + 30} ${TRUNK_TOP - 190} ${TRUNK_X + 34} ${TRUNK_TOP - 250}`,
    class: 'tw-limb tw-limb-main',
  }))

  const groups = new Map()
  for (const isWhite of [true, false]) {
    const side = drills
      .filter((d) => (d.user_color === 'white') === isWhite && d.ladder?.total)
      .sort((a, b) => (b.games || 0) - (a.games || 0))
    side.forEach((drill, i) => {
      const group = el('g', { class: 'tw-branch', 'data-id': drill.id })
      drawBranch(group, drill, branchGeometry(i, side.length, isWhite), handlers)
      svg.append(group)
      groups.set(drill.id, group)
    })
  }
  host.replaceChildren(svg)
  return groups
}
