import { Chessground } from './vendor/chessground.min.js'
import { renderTree } from './tree.js'

const initialState = {
  fen: null, // live position (the tip of the game)
  legal: [],
  history: [], // SAN per ply
  positions: [], // FEN per position: [start, after ply 1, after ply 2, ...]
  ucis: [], // UCI per ply, aligned with positions[1:]
  sans: [], // SAN per ply, aligned with ucis (drives check/mate marking)
  viewPly: 0, // which position is displayed; < positions.length-1 means reviewing
  tipPly: null, // real game tip when a teaching variation extends positions past it
  badge: null, // { ply, square, cls } — move-quality badge on the last user move
  classCounts: {}, // per-class tallies, scored by FIRST attempt per move slot
  accSum: 0, // sum of per-move accuracy percentages
  accCount: 0,
  userColor: 'w', // side the user plays — NOT the board orientation
  prevUserCp: 0, // eval (user POV) before the current move slot
  scoredFen: null, // slot already scored by a bounce or hint at this position
  bookUcis: [], // book moves for the current turn — instant book badges
  overlay: null, // engine's moves as ucis, best first — green arrows on the board
  evalCp: 0, // engine eval of the live position, white's perspective
  flipped: false,
  check: false,
  gameOver: false,
  result: null,
  drill: null,
  gameId: null,
  bounces: 0,
  bounceFen: null,
  bounceTries: 0,
  openingDone: false,
  hintUsed: false, // a hint during the graded opening voids the pass
  undoStack: [],
  mode: 'drill',
  script: null, // opponent line to replay (Repeat button)
  puzzle: null,
  puzzleSet: 'blunders',
  seen: [],
  stats: { right: 0, wrong: 0 },
}

let state = { ...initialState }
let ground = null

function setState(patch) {
  state = { ...state, ...patch }
  render()
}

// evals arrive from white's perspective; flip by the side the user is playing.
// Board orientation must not be used here — F flips the board mid-run.
const userCp = (cp) => (state.userColor === 'b' ? -(cp ?? 0) : (cp ?? 0))

async function api(path, body) {
  const res = await fetch(path, { method: 'POST', body: JSON.stringify(body) })
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

function parseFen(fen) {
  const pieces = {}
  const [placement] = fen.split(' ')
  placement.split('/').forEach((row, rankIdx) => {
    let file = 0
    for (const ch of row) {
      if (/\d/.test(ch)) { file += Number(ch); continue }
      const square = 'abcdefgh'[file] + (8 - rankIdx)
      pieces[square] = ch
      file += 1
    }
  })
  return pieces
}

function isCaptureGuess(fen, uci) {
  const pieces = parseFen(fen)
  const from = uci.slice(0, 2)
  const to = uci.slice(2, 4)
  const mover = pieces[from]
  const enPassant = mover?.toLowerCase() === 'p' && from[0] !== to[0] && !pieces[to]
  return Boolean(pieces[to]) || enPassant
}

// Display-only move application for instant feedback; the server's FEN
// replaces this as soon as it answers.
function applyMoveToFen(fen, uci) {
  const parts = fen.split(' ')
  const pieces = parseFen(fen)
  const from = uci.slice(0, 2)
  const to = uci.slice(2, 4)
  const mover = pieces[from]
  const next = { ...pieces }
  delete next[from]
  if (mover?.toLowerCase() === 'p' && from[0] !== to[0] && !pieces[to]) {
    delete next[to[0] + from[1]] // en passant victim
  }
  if (mover?.toLowerCase() === 'k' && Math.abs(from.charCodeAt(0) - to.charCodeAt(0)) === 2) {
    const rank = from[1]
    if (to[0] === 'g') { next[`f${rank}`] = pieces[`h${rank}`]; delete next[`h${rank}`] }
    if (to[0] === 'c') { next[`d${rank}`] = pieces[`a${rank}`]; delete next[`a${rank}`] }
  }
  let placed = mover
  if (uci.length === 5) placed = mover === mover.toUpperCase() ? uci[4].toUpperCase() : uci[4]
  else if (mover === 'P' && to[1] === '8') placed = 'Q'
  else if (mover === 'p' && to[1] === '1') placed = 'q'
  next[to] = placed

  const rows = []
  for (let r = 8; r >= 1; r -= 1) {
    let row = ''
    let empty = 0
    for (let f = 0; f < 8; f += 1) {
      const piece = next['abcdefgh'[f] + r]
      if (piece) {
        if (empty) { row += empty; empty = 0 }
        row += piece
      } else empty += 1
    }
    if (empty) row += empty
    rows.push(row)
  }
  const turn = parts[1] === 'w' ? 'b' : 'w'
  return [rows.join('/'), turn, parts[2] || '-', '-', '0', parts[5] || '1'].join(' ')
}

// Is the side to move in check? Enough chess logic for instant sound cues;
// the server remains the authority on everything else.
function isCheckFen(fen) {
  const pieces = parseFen(fen)
  const turn = fen.split(' ')[1]
  const kingCh = turn === 'w' ? 'K' : 'k'
  const kingSq = Object.keys(pieces).find((s) => pieces[s] === kingCh)
  if (!kingSq) return false
  const isEnemy = (ch) => (turn === 'w' ? ch === ch.toLowerCase() : ch === ch.toUpperCase())
  const file = kingSq.charCodeAt(0) - 97
  const rank = Number(kingSq[1])
  const at = (df, dr) => {
    const nf = file + df
    const nr = rank + dr
    if (nf < 0 || nf > 7 || nr < 1 || nr > 8) return undefined // off board
    return pieces['abcdefgh'[nf] + nr] || null // null = empty square
  }
  const KNIGHT = [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]]
  for (const [df, dr] of KNIGHT) {
    const p = at(df, dr)
    if (p && isEnemy(p) && p.toLowerCase() === 'n') return true
  }
  const pawnDr = turn === 'w' ? 1 : -1
  for (const df of [-1, 1]) {
    const p = at(df, pawnDr)
    if (p && isEnemy(p) && p.toLowerCase() === 'p') return true
  }
  for (const [df, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]) {
    const p = at(df, dr)
    if (p && isEnemy(p) && p.toLowerCase() === 'k') return true
    const wanted = df === 0 || dr === 0 ? 'rq' : 'bq'
    for (let step = 1; step < 8; step += 1) {
      const q = at(df * step, dr * step)
      if (q === undefined) break
      if (q) {
        if (isEnemy(q) && wanted.includes(q.toLowerCase())) return true
        break
      }
    }
  }
  return false
}

function atTip() {
  return state.viewPly === (state.tipPly ?? state.positions.length - 1)
}

function displayFen() {
  return state.positions[state.viewPly] || state.fen
}

// Arrows are chessground's (lichess's own renderer) — what made them ugly was
// the default palette under the stylesheet's blanket 0.6 dim. Opacity now
// lives on the brush. Amber for anything the user draws, green reserved for
// the engine so its suggestion never reads as your own annotation.
const BRUSHES = {
  green: { key: 'g', color: '#f0a02f', opacity: 0.9, lineWidth: 11 },
  red: { key: 'r', color: '#d0453e', opacity: 0.9, lineWidth: 11 },
  blue: { key: 'b', color: '#4a90d9', opacity: 0.9, lineWidth: 11 },
  yellow: { key: 'y', color: '#e6c33c', opacity: 0.9, lineWidth: 11 },
  best: { key: 'bm', color: '#81b64c', opacity: 0.95, lineWidth: 12 },
  // equally good alternatives — same green, quieter, so the first arrow still
  // reads as the answer and the rest as "these are fine too"
  alt: { key: 'al', color: '#81b64c', opacity: 0.5, lineWidth: 9 },
  mated: { key: 'mt', color: '#ca3431', opacity: 0.95, lineWidth: 12 },
}

function kingSquare(pieces, color) {
  const glyph = color === 'w' ? 'K' : 'k'
  return Object.keys(pieces).find((sq) => pieces[sq] === glyph)
}

const winProb = (cp) => 1 / (1 + Math.exp(-cp / 400))
// lichess-style move accuracy from a win-probability loss (0..1). Note this
// scale is deliberately flat — a whole pawn is only ~8 points of win
// probability — so it grades MOVES, not the opening. Drift does that.
const moveAccuracy = (loss) => Math.max(0, Math.min(100,
  103.1668 * Math.exp(-0.04354 * loss * 100) - 3.1669))
const classifyLoss = (loss) => (loss < 0.02 ? 'excellent'
  : loss < 0.05 ? 'good' : loss < 0.1 ? 'inaccuracy' : loss < 0.2 ? 'mistake' : 'blunder')

// mirrored in tools/trainer_server.py. Runs start from move 1, so the drift
// baseline is fixed: +0.3 for White, -0.3 for Black (Black's job out of the
// opening is to equalise, not to already be equal).
const START_CP = 30
const DRIFT_FAIL_CP = -75
const FLOOR_CP = -100

const pawns = (cp) => (Math.abs(cp) / 100).toFixed(1)
const signedPawns = (cp) => `${cp < 0 ? '−' : '+'}${pawns(cp)}`

// The single source of truth for how a run is described. The modal and the
// panel banner both render exactly this — they must never disagree. Plain
// coach language: no "drift", no pawn arithmetic.
function verdictCopy({ passed, lost, hintUsed, bounces, drift, userColor, flaws }) {
  const them = userColor === 'b' ? 'White' : 'Black'
  if (passed) {
    return {
      title: 'Opening passed',
      reason: drift >= 25 ? 'you came out of the opening better'
        : drift > -25 ? 'you came out of the opening equal'
          : 'you came out of the opening fine',
    }
  }
  const inacc = flaws?.inaccuracy || 0
  return {
    title: 'Room to improve',
    reason: lost ? 'the position got away from you'
      : hintUsed ? 'you needed a hint'
        : bounces > 0 ? `${bounces} correction${bounces > 1 ? 's' : ''} needed`
          : flaws?.blunder ? 'a blunder slipped through'
            : flaws?.mistake ? 'a mistake slipped through'
              : inacc ? (inacc === 1 ? 'an inaccuracy slipped through'
                : `${inacc} inaccuracies slipped through`)
                : drift <= -150 ? `${them} came out clearly better`
                  : drift <= DRIFT_FAIL_CP ? `${them} got the better position`
                    : 'you finished a little worse',
  }
}

// Record the run so the line it played can grow. Silent: the tree is where
// growth shows. Failing to report must not break the verdict.
async function reportResult(passed, before, history, depth) {
  if (!state.drill) return
  try {
    await api('/api/result', {
      drill: state.drill, game: state.gameId, history: history || state.history,
      passed, depth,
    })
  } catch (err) {
    console.error('Could not record the run:', err)
  }
}

// The eval players actually read — where the position stood when the opening
// ended. The starting eval is a constant, so it isn't worth showing.
function renderDrift(el, drift, endCp) {
  const num = document.createElement('div')
  num.className = `drift-num ${drift <= DRIFT_FAIL_CP ? 'loss' : 'gain'}`
  num.textContent = signedPawns(endCp)
  const label = document.createElement('span')
  label.textContent = 'out of the opening'
  num.append(label)
  el.replaceChildren(num)
}

const BADGE_COLORS = {
  book: '#a88b5a',
  best: '#81b64c',
  great: '#5c8bb0',
  excellent: '#96bc4b',
  good: '#95a86e',
  inaccuracy: '#f0c15c',
  mistake: '#e58f2a',
  blunder: '#ca3431',
}

function badgeSvg(cls) {
  const color = BADGE_COLORS[cls]
  if (!color) return null
  const symbols = {
    book: '<g transform="translate(82,18.5)"><path fill="#fff" d="M0,-3.8'
      + ' C-2.4,-5.6 -6.2,-6.1 -8.3,-5.5 L-8.3,4.4 C-6.2,3.8 -2.4,4.2 0,6'
      + ' C2.4,4.2 6.2,3.8 8.3,4.4 L8.3,-5.5 C6.2,-6.1 2.4,-5.6 0,-3.8 Z"/>'
      + '<line x1="0" y1="-3.6" x2="0" y2="5.6" stroke="#a88b5a" stroke-width="1.4"/></g>',
    best: '<g transform="translate(82,18)" fill="#fff"><polygon points="0,-9 2.4,-3.2'
      + ' 8.6,-2.8 3.8,1.2 5.3,7.3 0,4 -5.3,7.3 -3.8,1.2 -8.6,-2.8 -2.4,-3.2"/></g>',
    great: '<text x="82" y="25" font-size="20" font-weight="bold" text-anchor="middle"'
      + ' fill="#fff" font-family="sans-serif">!</text>',
    excellent: '<g transform="translate(75.4,11.4) scale(0.55)" fill="#fff">'
      + '<path d="M4 10 h4 v12 H4 z M10 22 h8.5 c1.5 0 2.6 -1 2.9 -2.4 l1.6 -7'
      + ' c.4 -1.8 -1 -3.6 -2.9 -3.6 h-5 l1 -4.2 c.3 -1.5 -.7 -2.8 -2.2 -2.8'
      + ' -.8 0 -1.5 .4 -1.9 1.1 L10 8.5 Z"/></g>',
    good: '<path d="M76 18 l4.5 4.5 l9 -9" stroke="#fff" stroke-width="3.5" fill="none"'
      + ' stroke-linecap="round"/>',
    inaccuracy: '<text x="82" y="24" font-size="15" font-weight="bold" text-anchor="middle"'
      + ' fill="#fff" font-family="sans-serif">?!</text>',
    mistake: '<text x="82" y="25" font-size="18" font-weight="bold" text-anchor="middle"'
      + ' fill="#fff" font-family="sans-serif">?</text>',
    blunder: '<text x="82" y="24" font-size="14" font-weight="bold" text-anchor="middle"'
      + ' fill="#fff" font-family="sans-serif">??</text>',
  }
  return `<circle cx="82" cy="18" r="15" fill="${color}" stroke="#fff" stroke-width="2"/>`
    + symbols[cls]
}

function highlightSquares() {
  const uci = state.viewPly > 0 ? state.ucis[state.viewPly - 1] : null
  return uci ? [uci.slice(0, 2), uci.slice(2, 4)] : []
}

function soundForSan(san) {
  if (!san) return
  if (san.includes('+') || san.includes('#')) Sound.check()
  else if (san.includes('x')) Sound.capture()
  else Sound.move()
}

function buildDests() {
  const dests = new Map()
  state.legal.forEach((m) => {
    const orig = m.slice(0, 2)
    if (!dests.has(orig)) dests.set(orig, [])
    dests.get(orig).push(m.slice(2, 4))
  })
  return dests
}

const PIECE_VALUES = { p: 1, n: 3, b: 3, r: 5, q: 9 }
const INITIAL_COUNTS = { p: 8, n: 2, b: 2, r: 2, q: 1 }

function renderCaptured(pieces) {
  const counts = { w: { p: 0, n: 0, b: 0, r: 0, q: 0 }, b: { p: 0, n: 0, b: 0, r: 0, q: 0 } }
  let points = 0
  Object.values(pieces).forEach((ch) => {
    const role = ch.toLowerCase()
    if (role === 'k') return
    const color = ch === ch.toUpperCase() ? 'w' : 'b'
    counts[color][role] += 1
    points += (color === 'w' ? 1 : -1) * PIECE_VALUES[role]
  })
  const bottomColor = state.flipped ? 'b' : 'w'
  const build = (rowEl, capturerColor) => {
    const victimColor = capturerColor === 'w' ? 'b' : 'w'
    const items = []
    let first = true
    for (const role of ['p', 'n', 'b', 'r', 'q']) {
      const taken = Math.max(0, INITIAL_COUNTS[role] - counts[victimColor][role])
      for (let i = 0; i < taken; i += 1) {
        const img = document.createElement('img')
        img.src = `pieces/${victimColor}${role.toUpperCase()}.svg`
        img.alt = role
        if (i === 0 && !first) img.className = 'gap'
        items.push(img)
        first = false
      }
    }
    const advantage = capturerColor === 'w' ? points : -points
    if (advantage > 0) {
      const span = document.createElement('span')
      span.className = 'cap-score'
      span.textContent = `+${advantage}`
      items.push(span)
    }
    rowEl.replaceChildren(...items)
  }
  build(document.getElementById('captured-bottom'), bottomColor)
  build(document.getElementById('captured-top'), bottomColor === 'w' ? 'b' : 'w')
}

function render() {
  const fen = displayFen()
  if (!fen) return
  const live = atTip()
  const turn = fen.split(' ')[1]
  const turnColor = turn === 'w' ? 'white' : 'black'
  const lastSan = state.viewPly > 0 ? state.sans[state.viewPly - 1] || '' : ''
  const inCheck = lastSan.includes('+') || lastSan.includes('#') || (live && state.check)
  const isMate = lastSan.includes('#') || (live && state.gameOver && state.check)
  const marked = highlightSquares()
  const dests = live && !state.gameOver ? buildDests() : new Map()

  ground.set({
    fen,
    orientation: state.flipped ? 'black' : 'white',
    turnColor,
    lastMove: marked,
    check: inCheck,
    movable: {
      free: false,
      color: dests.size ? turnColor : undefined,
      dests,
      showDests: true,
    },
  })
  const shapes = []
  if (
    state.badge
    && (state.viewPly === state.badge.ply || state.viewPly === state.badge.ply + 1)
  ) {
    const html = badgeSvg(state.badge.cls)
    if (html) shapes.push({ orig: state.badge.square, customSvg: { html } })
  }
  const arrows = state.overlay || []
  arrows.forEach((uci, i) => shapes.push({
    orig: uci.slice(0, 2),
    dest: uci.slice(2, 4),
    brush: i === 0 ? 'best' : 'alt',
  }))
  if (isMate) {
    const mated = kingSquare(parseFen(fen), turn)
    if (mated) shapes.push({ orig: mated, brush: 'mated' })
  }
  ground.setAutoShapes(shapes)
  renderCaptured(parseFen(fen))

  const movesEl = document.getElementById('moves')
  const rows = []
  for (let i = 0; i < state.history.length; i += 2) {
    const li = document.createElement('li')
    const num = document.createElement('span')
    num.className = 'num'
    num.textContent = `${i / 2 + 1}.`
    li.appendChild(num)
    for (const ply of [i, i + 1]) {
      const span = document.createElement('span')
      if (state.history[ply] !== undefined) {
        span.textContent = state.history[ply]
        span.className = `ply${state.viewPly === ply + 1 ? ' current' : ''}`
        span.addEventListener('click', () => jumpTo(ply + 1))
      }
      li.appendChild(span)
    }
    rows.push(li)
  }
  movesEl.replaceChildren(...rows)
  const current = movesEl.querySelector('.current')
  if (current) current.scrollIntoView({ block: 'nearest' })
  else movesEl.scrollTop = movesEl.scrollHeight

  const turnText = state.gameOver
    ? `Game over: ${state.result}`
    : `${turn === 'w' ? 'White' : 'Black'} to move`
  let status = turnText
  if (!live) status = `Reviewing ply ${state.viewPly}/${state.positions.length - 1} — press → to return`
  else if (state.mode === 'puzzle') status = `${turnText} — solved ${state.stats.right}, missed ${state.stats.wrong}`
  document.getElementById('status').textContent = status

  const share = 50 + 50 * (2 / (1 + Math.exp(-(state.evalCp || 0) / 400)) - 1)
  document.getElementById('evalfill').style.height = `${Math.max(4, Math.min(96, share))}%`
  document.getElementById('evalbar').style.transform = state.flipped ? 'rotate(180deg)' : ''

  const nav = {
    'nav-home': false,
    'nav-practice': state.mode === 'drill',
    'nav-blunders': state.mode === 'puzzle',
  }
  Object.entries(nav).forEach(([id, on]) =>
    document.getElementById(id).classList.toggle('active', on))
}

function showMessage(text, kind = 'note') {
  const el = document.getElementById('message')
  el.textContent = text || ''
  el.className = `message ${kind}`
}

const scoreClass = (pct) => (pct < 45 ? 'bad' : pct > 55 ? 'good' : '')

// The figures that open a run, set as figures. As a sentence they read as
// filler; as type, the two percentages are the whole point — how you do in
// this opening, and how you do once this particular line appears.
function showIntro(stats, fallback) {
  const el = document.getElementById('message')
  el.className = 'message note'
  if (!stats) { el.textContent = fallback || ''; return }
  // Sentences, with the figures picked out — a bare grid of labels and numbers
  // read as a form to be deciphered rather than as something being said.
  const num = (v) => Object.assign(document.createElement('b'),
    { className: 'i-num', textContent: v })
  const pct = (v) => Object.assign(document.createElement('b'),
    { className: `i-pct ${scoreClass(v)}`, textContent: `${v}%` })
  const rows = []
  if (stats.opening) {
    const p = document.createElement('div')
    p.append('You have played this opening ', num(stats.opening.games),
      ' times, scoring ', pct(stats.opening.score), '.')
    rows.push(p)
  }
  if (!rows.length) { el.textContent = fallback || ''; return }
  el.replaceChildren(...rows)
}

function showOpening(op) {
  const el = document.getElementById('opening-name')
  if (!op) { el.replaceChildren(); return }
  el.textContent = op.name
}

function setActionLabels(left, right) {
  document.getElementById('retry').textContent = left
  document.getElementById('next').textContent = right
}

function showContext(ctx) {
  const el = document.getElementById('context')
  el.hidden = !ctx
  if (!ctx) { el.replaceChildren(); return }

  const opponent = document.createElement('div')
  opponent.className = 'ctx-opponent'
  opponent.textContent = `vs ${ctx.opponent}`
  if (ctx.opponent_elo) {
    const elo = document.createElement('span')
    elo.className = 'elo'
    elo.textContent = ctx.opponent_elo
    opponent.appendChild(elo)
  }

  const meta = document.createElement('div')
  meta.className = 'ctx-meta'
  meta.textContent = `${ctx.date} · move ${ctx.move_no} · ${ctx.phase}`

  const task = document.createElement('div')
  task.className = 'ctx-task'
  task.textContent = ctx.task

  el.replaceChildren(opponent, meta, task)
}

function setPuzzleActions(visible) {
  document.getElementById('puzzle-actions').hidden = !visible
}

// Review track for the current puzzle: opponent's last move first, when known.
function puzzleTrack() {
  const p = state.puzzle
  return p.prev_fen
    ? { positions: [p.prev_fen, p.fen], ucis: [p.last_uci], sans: [p.last_san] }
    : { positions: [p.fen], ucis: [], sans: [] }
}

function lineSpans(items, track) {
  const frag = document.createDocumentFragment()
  const offset = track.positions.length - items.length
  items.forEach((item, k) => {
    if (k) frag.append(document.createTextNode(' '))
    const span = document.createElement('span')
    span.className = 'mv'
    span.textContent = item.label ? `${item.label} ${item.san}` : item.san
    span.addEventListener('click', () => {
      soundForSan(item.san)
      document.querySelectorAll('#message .mv.active').forEach((el) => el.classList.remove('active'))
      span.classList.add('active')
      setState({
        positions: track.positions,
        ucis: track.ucis,
        sans: track.sans,
        viewPly: offset + k,
        legal: [],
      })
    })
    frag.append(span)
  })
  return frag
}

function showAttemptFeedback(data) {
  const el = document.getElementById('message')
  el.className = `message ${data.correct ? 'good' : 'bad'}`
  el.replaceChildren()
  const add = (text) => el.append(document.createTextNode(text))
  const base = puzzleTrack()
  const appendReveal = () => {
    if (data.original_san === data.played_san) {
      add('That is what you played in the game.')
      return
    }
    add('In the game you played ')
    const orig = data.original_line || []
    if (orig.length) {
      const origTrack = {
        positions: [...base.positions, orig[0].fen],
        ucis: [...base.ucis, orig[0].uci],
        sans: [...base.sans, orig[0].san],
      }
      el.append(lineSpans(orig, origTrack))
    } else {
      add(data.original_san)
    }
    add('.')
  }

  const cont = data.continuation_line || []
  const contTrack = {
    positions: [...base.positions, data.fen_after, ...cont.map((m) => m.fen)],
    ucis: [...base.ucis, data.played_uci, ...cont.map((m) => m.uci)],
    sans: [...base.sans, data.played_san, ...cont.map((m) => m.san)],
  }
  if (data.correct) {
    add(`Correct — ${data.played_san} works. `)
    appendReveal()
    if (cont.length) {
      add('\n\nLine: ')
      el.append(lineSpans(cont, contTrack))
    }
    return
  }
  add(`${data.played_san} runs into `)
  el.append(lineSpans(cont, contTrack))
  const best = data.best_line || []
  add(`\n\n${data.best_san} was the move: `)
  if (best.length) {
    const bestTrack = {
      positions: [...base.positions, ...best.map((m) => m.fen)],
      ucis: [...base.ucis, ...best.map((m) => m.uci)],
      sans: [...base.sans, ...best.map((m) => m.san)],
    }
    el.append(lineSpans(best, bestTrack))
  }
  add('\n\n')
  appendReveal()
}

function jumpTo(target) {
  if (!state.positions.length || target === state.viewPly) return
  const clamped = Math.max(0, Math.min(state.positions.length - 1, target))
  const forward = clamped > state.viewPly
  soundForSan(forward ? state.sans[clamped - 1] : state.sans[state.viewPly - 1])
  setState({ viewPly: clamped })
}

function stepView(delta) {
  jumpTo(state.viewPly + delta)
}

function onUserMove(orig, dest, captured) {
  const fenBefore = displayFen()
  const pieces = parseFen(fenBefore)
  const mover = pieces[orig]
  let uci = orig + dest
  if (mover?.toLowerCase() === 'p' && (dest[1] === '8' || dest[1] === '1')) uci += 'q'
  if (isCheckFen(applyMoveToFen(fenBefore, uci))) Sound.check()
  else if (captured || isCaptureGuess(fenBefore, uci)) Sound.capture()
  else Sound.move()
  if (state.mode === 'puzzle') attemptPuzzle(uci)
  else submitMove(uci)
}

async function submitMove(uci) {
  const snapshot = {
    fen: state.fen,
    legal: state.legal,
    history: state.history,
    positions: state.positions,
    ucis: state.ucis,
    sans: state.sans,
    bookUcis: state.bookUcis,
  }
  // optimistic: the board already shows the move; align our review track now
  setState({
    positions: [...snapshot.positions, applyMoveToFen(snapshot.fen, uci)],
    ucis: [...snapshot.ucis, uci],
    sans: [...snapshot.sans, ''],
    viewPly: snapshot.positions.length,
    tipPly: null,
    history: [...snapshot.history, '…'],
    legal: [],
    overlay: null,
    check: false,
    badge: (() => {
      const cls = state.bookUcis.includes(uci)
        ? 'book'
        : classTable.fen === snapshot.fen ? classTable.moves[uci] : null
      return cls
        ? { ply: snapshot.positions.length, square: uci.slice(2, 4), cls }
        : state.badge
    })(),
  })

  let data
  try {
    data = await api('/api/move', {
      fen: snapshot.fen,
      history: snapshot.history,
      move: uci,
      drill: state.drill,
      game: state.gameId,
      script: state.script || undefined,
      tries: state.bounceFen === snapshot.fen ? state.bounceTries : 0,
    })
  } catch (err) {
    data = { error: `Server error: ${err.message}` }
  }
  if (data.error || data.rejected) {
    const realBounce = data.rejected && !data.redirect
    if (realBounce) Sound.bad()
    if (data.rejected && state.mode === 'drill') {
      setActionLabels('Repeat', 'Next')
      setPuzzleActions(true)
    }
    const bounceState = {
      gameOver: false,
      bounces: state.bounces + (realBounce ? 1 : 0),
      bounceFen: realBounce ? snapshot.fen : state.bounceFen,
      bounceTries: realBounce
        ? (state.bounceFen === snapshot.fen ? state.bounceTries : 0) + 1
        : state.bounceTries,
    }
    // the first attempt at a slot is what gets scored — even when rejected
    if (
      realBounce && state.mode === 'drill' && !state.openingDone
      && data.move_class && state.scoredFen !== snapshot.fen
    ) {
      bounceState.classCounts = {
        ...state.classCounts,
        [data.move_class]: (state.classCounts[data.move_class] || 0) + 1,
      }
      bounceState.accSum = state.accSum + moveAccuracy(data.loss || 0.1)
      bounceState.accCount = state.accCount + 1
      bounceState.scoredFen = snapshot.fen
    }
    const tipIdx = snapshot.positions.length - 1
    if (data.best_line?.length) {
      // teaching line: append to the game track so it's clickable and
      // walkable with arrows; moves stay locked to the real game tip
      const line = data.best_line
      const track = {
        positions: [...snapshot.positions, ...line.map((m) => m.fen)],
        ucis: [...snapshot.ucis, ...line.map((m) => m.uci)],
        sans: [...snapshot.sans, ...line.map((m) => m.san)],
      }
      const el = document.getElementById('message')
      el.className = 'message bad'
      el.replaceChildren(document.createTextNode(`${data.warning || data.error}\n\nBest here: `))
      el.append(lineSpans(line, track))
      setState({
        ...snapshot,
        ...track,
        viewPly: tipIdx,
        tipPly: tipIdx,
        overlay: [line[0].uci],
        ...bounceState,
      })
    } else {
      showMessage(data.warning || data.error, 'bad')
      setState({
        ...snapshot,
        viewPly: tipIdx,
        tipPly: null,
        overlay: null,
        ...bounceState,
      })
    }
    return
  }

  if (data.opening) showOpening(data.opening)
  const lines = []
  let kind = 'note'
  let openingDone = state.openingDone
  // slots already scored by an earlier bounce or hint don't score again
  const slotScored = state.scoredFen === snapshot.fen
  const curUserCp = userCp(data.eval_cp)
  let slotClass = data.move_class
  let accSum = state.accSum
  let accCount = state.accCount
  if (data.move_class && !slotScored) {
    // the truth of a move is the eval after the reply: if the position
    // dropped clearly more than the classification saw, the drop wins.
    // Only moves the engine itself picked are exempt (they can't lose
    // ground); a noise margin keeps shallow-eval wobble from flipping badges.
    const realized = Math.max(0, winProb(state.prevUserCp) - winProb(curUserCp))
    let slotLoss = data.loss || 0
    if (!data.move_verified && realized > slotLoss + 0.03) {
      slotLoss = realized
      slotClass = classifyLoss(realized)
    }
    accSum += moveAccuracy(slotLoss)
    accCount += 1
  }
  const classCounts = slotClass && !slotScored
    ? { ...state.classCounts, [slotClass]: (state.classCounts[slotClass] || 0) + 1 }
    : state.classCounts
  const userEval = userCp(data.eval_cp)
  const lostNow = state.mode === 'drill' && !openingDone && !data.opening_complete
    && data.eval_cp != null && userEval < FLOOR_CP
  const verdictNow = state.mode === 'drill' && !openingDone
    && (data.opening_complete || lostNow)
  if (verdictNow) {
    openingDone = true
    // what the run is judged on: how far the position moved from where the
    // opening started, not the absolute eval and not per-move accuracy
    const baseline = state.userColor === 'b' ? -START_CP : START_CP
    const drift = userEval - baseline
    // a line isn't learned until it's clean: any move that came out below
    // "good" — including one reclassified once the reply landed — fails the run
    const flaws = {
      inaccuracy: classCounts.inaccuracy || 0,
      mistake: classCounts.mistake || 0,
      blunder: classCounts.blunder || 0,
    }
    const flawed = flaws.inaccuracy + flaws.mistake + flaws.blunder > 0
    const passed = !lostNow && state.bounces === 0 && !state.hintUsed && !flawed
      && userEval >= FLOOR_CP && drift > DRIFT_FAIL_CP
    const accuracy = accCount ? Math.round(accSum / accCount) : 100
    const { title: vTitle, reason } = verdictCopy({
      passed, lost: lostNow, hintUsed: state.hintUsed, bounces: state.bounces,
      drift, userColor: state.userColor, flaws,
    })
    kind = passed ? 'good' : 'bad'
    const banner = document.getElementById('verdict')
    banner.hidden = false
    banner.className = `verdict ${passed ? 'pass' : 'fail'}`
    banner.textContent = `${passed ? '✓ ' : ''}${vTitle} — ${reason}`
    const title = document.getElementById('verdict-title')
    title.className = `verdict-title ${passed ? 'pass' : 'fail'}`
    title.textContent = vTitle
    document.getElementById('verdict-reason').textContent = reason
    renderDrift(document.getElementById('verdict-drift'), drift, userEval)
    const ORDER = ['best', 'great', 'excellent', 'good', 'inaccuracy',
      'mistake', 'blunder', 'hinted', 'book']
    const EXTRA_COLORS = { mistake: '#e58f2a', blunder: '#ca3431', hinted: '#8a8f84' }
    const accEl = document.createElement('span')
    const accB = document.createElement('b')
    accB.textContent = `${accuracy}%`
    accEl.append(accB, ' move accuracy')
    const stats = ORDER
      .filter((c) => classCounts[c])
      .map((c) => {
        const el = document.createElement('span')
        const b = document.createElement('b')
        b.style.color = BADGE_COLORS[c] || EXTRA_COLORS[c] || 'inherit'
        b.textContent = classCounts[c]
        el.append(b, ` ${c}`)
        return el
      })
    document.getElementById('verdict-stats').replaceChildren(accEl, ...stats)
    const fam = data.family_record
    document.getElementById('verdict-family').textContent = fam
      ? `${fam.family}: ${fam.passes}/${fam.completed} runs passed`
      : ''
    reportResult(passed, data.ladder, data.history, data.depth)
    document.getElementById('verdict-modal').hidden = false
  }
  if (verdictNow || (data.game_over && state.mode === 'drill')) {
    setActionLabels('Repeat', 'Next')
    setPuzzleActions(true)
  }
  if (data.prep_note) lines.push(data.prep_note)
  if (data.note) lines.push(data.note)
  // not the opponent's move — it is on the board and in the move list
  if (data.game_over) lines.push(`Result: ${data.result}`)
  showMessage(lines.join('\n'), kind)

  const positions = [...snapshot.positions, data.fen_after_user]
  const ucis = [...snapshot.ucis, uci]
  const sans = [...snapshot.sans, data.user_san]
  if (data.reply_uci) {
    positions.push(data.fen)
    ucis.push(data.reply_uci)
    sans.push(data.reply_san)
    soundForSan(data.reply_san)
  }
  setState({
    fen: data.fen,
    legal: data.legal,
    history: data.history,
    positions,
    ucis,
    sans,
    viewPly: positions.length - 1,
    check: data.check,
    evalCp: data.eval_cp ?? state.evalCp,
    gameOver: data.game_over,
    result: data.result,
    openingDone,
    classCounts,
    accSum,
    accCount,
    prevUserCp: curUserCp,
    scoredFen: null,
    bookUcis: data.book_ucis || [],
    badge: slotClass
      ? { ply: snapshot.positions.length, square: uci.slice(2, 4), cls: slotClass }
      : null,
    bounceFen: null,
    bounceTries: 0,
    tipPly: null,
    undoStack: [...state.undoStack, snapshot],
  })
  if (data.reply_uci && !data.game_over) prefetchClasses(data.fen)
}

async function nextPuzzle(setName) {
  show('game')
  const set = typeof setName === 'string' ? setName : state.puzzleSet
  const data = await api('/api/puzzle/next', { seen: state.seen, set })
  const label = set === 'openings' ? 'Opening mistakes' : 'Blunder replay'
  if (data.done) {
    showMessage(
      data.total
        ? `All ${data.total} positions solved — nothing left in ${label.toLowerCase()}. Re-run the miner after your next games.`
        : 'Nothing mined yet — the generator may still be running. Try again in a minute.',
      'good',
    )
    return
  }
  document.getElementById('drill-name').textContent =
    `${label} · ${data.solved_count}/${data.total} solved`
  hintedPuzzleId = null
  document.getElementById('verdict').hidden = true
  hideVerdictModal()
  showContext(data.context)
  showOpening(null)
  showMessage('')
  setActionLabels('Retry', 'Next puzzle')
  setPuzzleActions(true)
  setState({
    ...initialState,
    mode: 'puzzle',
    puzzleSet: set,
    puzzle: data,
    fen: data.fen,
    legal: data.legal,
    positions: data.prev_fen ? [data.prev_fen, data.fen] : [data.fen],
    ucis: data.last_uci ? [data.last_uci] : [],
    sans: data.last_san ? [data.last_san] : [],
    viewPly: 0,
    userColor: data.orientation === 'black' ? 'b' : 'w',
    flipped: data.orientation === 'black',
    check: data.check,
    evalCp: data.eval_cp,
    seen: [...state.seen, data.id],
    stats: state.stats,
  })
  if (data.prev_fen) setTimeout(() => jumpTo(1), 500)
}

async function attemptPuzzle(uci) {
  const track = puzzleTrack()
  // optimistic: the board already shows the attempt; align the review track
  setState({
    positions: [...track.positions, applyMoveToFen(state.puzzle.fen, uci)],
    ucis: [...track.ucis, uci],
    sans: [...track.sans, ''],
    viewPly: track.positions.length,
    legal: [],
  })

  let data
  try {
    data = await api('/api/puzzle/attempt', {
      id: state.puzzle.id,
      move: uci,
      hinted: hintedPuzzleId === state.puzzle.id,
    })
  } catch (err) {
    data = { error: `Server error: ${err.message}` }
  }
  if (data.error) {
    showMessage(data.error, 'bad')
    setState({
      positions: track.positions,
      ucis: track.ucis,
      sans: track.sans,
      viewPly: track.positions.length - 1,
      legal: state.puzzle.legal,
    })
    return
  }

  setTimeout(data.correct ? Sound.good : Sound.bad, 150)
  showAttemptFeedback(data)
  setPuzzleActions(true)
  setState({
    positions: [...track.positions, data.fen_after],
    ucis: [...track.ucis, data.played_uci],
    sans: [...track.sans, data.played_san],
    viewPly: track.positions.length,
    evalCp: data.eval_cp,
    overlay: data.correct ? null : [data.best_uci],
    stats: data.correct
      ? { ...state.stats, right: state.stats.right + 1 }
      : { ...state.stats, wrong: state.stats.wrong + 1 },
  })
}

let hintedPuzzleId = null
let classTable = { fen: null, moves: {} }

async function prefetchClasses(fen) {
  if (!fen) return
  try {
    const data = await api('/api/classify', { fen })
    classTable = { fen, moves: data.moves || {} }
  } catch { /* badges fall back to arriving with the move response */ }
}

// The engine rarely has one answer. Saying so is the lesson: a position with
// three equal moves is a choice, not a puzzle with a hidden solution. Inside
// the book it isn't a choice at all — that's the move being drilled.
function hintText(sans, book) {
  const label = book ? 'Book here' : 'Hint'
  if (sans.length === 1) return `${label}: ${sans[0]}`
  const list = `${sans.slice(0, -1).join(', ')} or ${sans[sans.length - 1]}`
  return book ? `${label}: ${list}.` : `${label}: ${list} — all equally good here.`
}

async function showHint() {
  if (!atTip() || state.gameOver || !state.fen) return
  const inDrill = state.mode === 'drill'
  const data = await api('/api/hint', {
    fen: displayFen(),
    practice: inDrill,
    mode: state.mode,
    // the drill's own book outranks the engine — without this the hint
    // suggests moves the course guard then bounces
    drill: inDrill ? state.drill : undefined,
    history: inDrill ? state.history : undefined,
  })
  if (data.error || !data.moves?.length) return
  if (state.mode === 'puzzle' && state.puzzle) hintedPuzzleId = state.puzzle.id
  const patch = { overlay: data.moves.map((m) => m.uci) }
  // a hinted slot scores as 'hinted' — the move played after it won't count
  if (state.mode === 'drill' && !state.openingDone && state.scoredFen !== state.fen) {
    patch.classCounts = {
      ...state.classCounts,
      hinted: (state.classCounts.hinted || 0) + 1,
    }
    patch.accSum = state.accSum + moveAccuracy(0.1)
    patch.accCount = state.accCount + 1
    patch.scoredFen = state.fen
    patch.hintUsed = true
  }
  setState(patch)
  showMessage(hintText(data.moves.map((m) => m.san), data.book), 'note')
}

function repeatGame() {
  if (state.mode !== 'drill' || !state.history.length) return
  newGame({ drill: state.drill, script: state.history, repeat_of: state.gameId })
}

function retryPuzzle() {
  if (state.mode === 'drill') { repeatGame(); return }
  if (!state.puzzle) return
  setPuzzleActions(false)
  showMessage('Same position — try again.', 'note')
  const track = puzzleTrack()
  setState({
    positions: track.positions,
    ucis: track.ucis,
    sans: track.sans,
    viewPly: track.positions.length - 1,
    legal: state.puzzle.legal,
    overlay: null,
    evalCp: state.puzzle.eval_cp,
  })
}

function show(view) {
  document.getElementById('home').hidden = view !== 'home'
  document.getElementById('game').hidden = view !== 'game'
  if (view === 'home') {
    document.getElementById('nav-home').classList.add('active')
    document.getElementById('nav-practice').classList.remove('active')
    document.getElementById('nav-blunders').classList.remove('active')
  }
  if (view === 'game') requestAnimationFrame(() => ground.redrawAll())
}

async function showHome() {
  show('home')
  const data = await api('/api/drills', {})
  const profile = data.profile || {}
  document.getElementById('profile-stats').replaceChildren(
    ...['blitz', 'rapid'].filter((k) => profile[k]).map((k) => {
      const p = profile[k]
      const row = document.createElement('div')
      const label = document.createElement('span')
      label.className = 'p-label'
      label.textContent = k
      const rating = document.createElement('span')
      rating.className = 'p-rating'
      rating.textContent = p.rating ?? '—'
      const peak = document.createElement('span')
      peak.className = 'p-rest'
      peak.textContent = `peak ${p.best ?? '—'}`
      row.append(label, rating, peak)
      if ([p.wins, p.losses, p.draws].every((x) => x != null)) {
        const mk = (cls, text) => {
          const s = document.createElement('span')
          s.className = cls
          s.textContent = text
          return s
        }
        row.append(
          mk('p-w', `${p.wins}W`),
          mk('p-l', `${p.losses}L`),
          mk('p-d', `${p.draws}D`),
        )
      }
      return row
    }),
  )
  const sets = data.puzzles || {}
  const blunders = sets.blunders || { total: 0, solved: 0 }
  document.getElementById('replay-desc').textContent = blunders.total
    ? `${blunders.solved} of ${blunders.total} solved`
    : 'nothing mined yet'

  const prepared = data.drills.filter((d) => d.id.includes('/'))
  const bloom = prepared.reduce((acc, d) => {
    const L = d.ladder
    if (!L?.lines) return acc
    return {
      grown: acc.grown + L.grown,
      started: acc.started + L.started,
      lines: acc.lines + L.lines,
    }
  }, { grown: 0, started: 0, lines: 0 })
  document.getElementById('practice-desc').textContent =
    `${bloom.grown} leaves · ${bloom.started} of ${bloom.lines} lines going`

  const rowsById = new Map()
  const branches = renderTree(document.getElementById('tree'), prepared, {
    onEnter: (id) => focusOpening(id, prepared, branches, rowsById),
    onLeave: () => focusOpening(null, prepared, branches, rowsById),
    onPick: (id) => newGame({ drill: id }),
  })

  for (const color of ['white', 'black']) {
    const all = prepared
      .filter((d) => d.user_color === color)
      .sort((a, b) => (b.games || 0) - (a.games || 0))
    const rows = all
      .map((d, i) => {
        const li = document.createElement('li')
        li.style.setProperty('--i', i)
        const name = document.createElement('span')
        name.className = 'o-name'
        name.textContent = d.name.replace(/ — (White|Black)$/, '')

        const meta = document.createElement('span')
        meta.className = 'o-meta'
        if (d.games) {
          const score = document.createElement('span')
          score.className = d.score_pct < 45 ? 'bad' : d.score_pct > 55 ? 'good' : ''
          score.textContent = `${Math.round(d.score_pct)}%`
          meta.append(`${d.games}g `, score)
        }

        const prac = document.createElement('span')
        prac.className = 'o-meta o-prac'
        const L = d.ladder
        if (L && L.lines) {
          const leaves = document.createElement('b')
          leaves.className = L.grown ? 'good' : ''
          leaves.textContent = L.grown
          const bar = document.createElement('span')
          bar.className = 'o-rungs'
          bar.style.setProperty('--filled', `${Math.round(100 * L.started / L.lines)}%`)
          prac.append(leaves, ' leaves', bar)
          prac.title = `${L.grown} leaves grown\n`
            + `${L.started} of ${L.lines} lines going · deepest ${L.deepest} moves`
        } else {
          prac.textContent = '—'
        }

        li.append(name, meta, prac)
        li.classList.add('pick')
        li.addEventListener('click', () => newGame({ drill: d.id }))
        li.addEventListener('pointerenter', () => focusOpening(d.id, prepared, branches, rowsById))
        li.addEventListener('pointerleave', () => focusOpening(null, prepared, branches, rowsById))
        rowsById.set(d.id, li)
        return li
      })
    document.getElementById(`home-${color}`).replaceChildren(...rows)
  }
}

// One opening at a time: the branch lifts, its row lifts, everything else
// recedes, and the caption under the tree says what you are looking at.
function focusOpening(id, drills, branches, rows) {
  const name = document.getElementById('cap-name')
  const detail = document.getElementById('cap-detail')
  document.getElementById('tree').classList.toggle('focused', Boolean(id))
  for (const [key, parts] of branches) {
    for (const g of parts) g.classList.toggle('lit', key === id)
  }
  for (const [key, row] of rows) row.classList.toggle('lit', key === id)
  if (!id) {
    name.textContent = 'Your repertoire'
    detail.textContent = 'Branch length is how deep you have taken an opening; '
      + 'foliage is the lines you have cleared. Hover a branch.'
    return
  }
  const d = drills.find((x) => x.id === id)
  if (!d) return
  const L = d.ladder
  name.textContent = d.name.replace(/ — (White|Black)$/, '')
  detail.textContent = `${L.grown} leaves · ${L.started} of ${L.lines} lines going`
    + (d.games ? ` · faced ${d.games} times` : '')
}

async function newGame(custom) {
  show('game')
  const payload = custom && custom.drill ? custom : { practice: true }
  const data = await api('/api/new', payload)
  document.getElementById('drill-name').textContent = data.drill_name || 'Opening Practice'
  showContext(null)
  showOpening(data.opening)
  showIntro(data.intro_stats, data.message)
  setActionLabels('Repeat', 'Next')
  setPuzzleActions(true)
  const positions = [data.start_fen]
  const ucis = []
  const sans = []
  const history = []
  ;(data.pre_moves || []).forEach((m) => {
    positions.push(m.fen)
    ucis.push(m.uci)
    sans.push(m.san)
    history.push(m.san)
  })
  document.getElementById('verdict').hidden = true
  hideVerdictModal()
  setState({
    ...initialState,
    drill: data.drill_id || null,
    gameId: data.game_id || null,
    script: data.script || (custom && custom.script) || null,
    fen: data.fen,
    legal: data.legal,
    positions,
    ucis,
    sans,
    history,
    viewPly: 0,
    bookUcis: data.book_ucis || [],
    userColor: data.orientation === 'black' ? 'b' : 'w',
    prevUserCp: data.orientation === 'black' ? -START_CP : START_CP,
    flipped: data.orientation === 'black',
    check: data.check,
    evalCp: data.eval_cp ?? 0,
    seen: state.seen,
    stats: state.stats,
  })
  if (positions.length > 1) setTimeout(() => jumpTo(positions.length - 1), 500)
  prefetchClasses(data.fen)
}

function undo() {
  const prev = state.undoStack.at(-1)
  if (!prev || state.mode !== 'drill') return
  showMessage('Took back your last move (and the reply).', 'note')
  setState({
    ...prev,
    viewPly: prev.positions.length - 1,
    tipPly: null,
    badge: null,
    undoStack: state.undoStack.slice(0, -1),
    overlay: null,
    gameOver: false,
  })
}

const KEY_ACTIONS = {
  ArrowLeft: () => stepView(-1),
  ArrowRight: () => stepView(1),
  Escape: () => {
    const modal = document.getElementById('verdict-modal')
    if (!modal.hidden) playOn()
    else showHome()
  },
  h: () => showHome(),
  n: () => newGame(),
  b: () => nextPuzzle('blunders'),
  i: () => showHint(),
  r: () => retryPuzzle(),
  u: () => undo(),
  f: () => setState({ flipped: !state.flipped }),
  m: () => showMessage(Sound.toggle() ? 'Sound off.' : 'Sound on.', 'note'),
}

document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return
  const action = KEY_ACTIONS[e.key.length === 1 ? e.key.toLowerCase() : e.key]
  if (!action) return
  e.preventDefault()
  action()
})

function hideVerdictModal() {
  document.getElementById('verdict-modal').hidden = true
}

async function playOn() {
  hideVerdictModal()
  if (state.mode !== 'drill' || state.gameOver) return
  // the run ended on the user's move — fetch the withheld engine reply
  if (state.history.length % 2 === (state.userColor === 'b' ? 0 : 1)) {
    const data = await api('/api/reply', {
      fen: state.fen, history: state.history, drill: state.drill, game: state.gameId,
    })
    if (data.error || !data.reply_san) return
    soundForSan(data.reply_san)
    if (data.opening) showOpening(data.opening)
    setState({
      fen: data.fen,
      legal: data.legal,
      history: data.history,
      positions: [...state.positions, data.fen],
      ucis: [...state.ucis, data.reply_uci],
      sans: [...state.sans, data.reply_san],
      viewPly: state.positions.length,
      check: data.check,
      evalCp: data.eval_cp ?? state.evalCp,
      bookUcis: data.book_ucis || [],
    })
    prefetchClasses(data.fen)
  }
}

document.getElementById('v-repeat').addEventListener('click', () => { hideVerdictModal(); repeatGame() })
document.getElementById('v-next').addEventListener('click', () => {
  hideVerdictModal()
  if (state.drill && !state.drill.startsWith('practice:')) newGame({ drill: state.drill })
  else newGame()
})
document.getElementById('v-playon').addEventListener('click', () => { playOn() })
document.getElementById('verdict-modal').addEventListener('click', (e) => {
  if (e.target.id === 'verdict-modal') playOn()
})

document.getElementById('retry').addEventListener('click', retryPuzzle)
document.getElementById('next').addEventListener('click', () => {
  if (state.mode !== 'drill') { nextPuzzle(); return }
  // stay focused on a specific opening; random only in mixed practice
  if (state.drill && !state.drill.startsWith('practice:')) newGame({ drill: state.drill })
  else newGame()
})
document.getElementById('home-practice').addEventListener('click', () => newGame())
document.getElementById('home-replay').addEventListener('click', () => nextPuzzle('blunders'))
document.getElementById('brand-link').addEventListener('click', () => showHome())
document.getElementById('nav-home').addEventListener('click', () => showHome())
document.getElementById('hint').addEventListener('click', () => showHint())
document.getElementById('nav-practice').addEventListener('click', () => newGame())
document.getElementById('nav-blunders').addEventListener('click', () => nextPuzzle('blunders'))

ground = Chessground(document.getElementById('board'), {
  coordinates: true,
  animation: { duration: 180 },
  movable: { free: false, showDests: true },
  draggable: { showGhost: true },
  drawable: { enabled: true, brushes: BRUSHES },
  events: { move: onUserMove },
})

showHome()
