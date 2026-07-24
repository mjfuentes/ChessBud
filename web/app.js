import { Chessground } from './vendor/chessground.min.js'

const initialState = {
  fen: null, // live position (the tip of the game)
  legal: [],
  history: [], // SAN per ply
  positions: [], // FEN per position: [start, after ply 1, after ply 2, ...]
  ucis: [], // UCI per ply, aligned with positions[1:]
  sans: [], // SAN per ply, aligned with ucis (drives check/mate marking)
  viewPly: 0, // which position is displayed; < positions.length-1 means reviewing
  overlay: null, // engine's move [from, to], shown as a green arrow (puzzle feedback)
  evalCp: 0, // engine eval of the live position, white's perspective
  flipped: false,
  check: false,
  gameOver: false,
  result: null,
  drill: null,
  gameId: null,
  bounces: 0,
  openingDone: false,
  undoStack: [],
  mode: 'drill',
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

function atTip() {
  return state.viewPly >= state.positions.length - 1
}

function displayFen() {
  return state.positions[state.viewPly] || state.fen
}

function kingSquare(pieces, color) {
  const glyph = color === 'w' ? 'K' : 'k'
  return Object.keys(pieces).find((sq) => pieces[sq] === glyph)
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
  if (state.overlay) shapes.push({ orig: state.overlay[0], dest: state.overlay[1], brush: 'green' })
  if (isMate) {
    const mated = kingSquare(parseFen(fen), turn)
    if (mated) shapes.push({ orig: mated, brush: 'red' })
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
  if (captured || isCaptureGuess(fenBefore, uci)) Sound.capture()
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
  }
  // optimistic: the board already shows the move; align our review track now
  setState({
    positions: [...snapshot.positions, applyMoveToFen(snapshot.fen, uci)],
    ucis: [...snapshot.ucis, uci],
    sans: [...snapshot.sans, ''],
    viewPly: snapshot.positions.length,
    history: [...snapshot.history, '…'],
    legal: [],
    overlay: null,
    check: false,
  })

  let data
  try {
    data = await api('/api/move', {
      fen: snapshot.fen, history: snapshot.history, move: uci, drill: state.drill, game: state.gameId,
    })
  } catch (err) {
    data = { error: `Server error: ${err.message}` }
  }
  if (data.error || data.rejected) {
    if (data.rejected) Sound.bad()
    showMessage(data.warning || data.error, 'bad')
    setState({
      ...snapshot,
      viewPly: snapshot.positions.length - 1,
      overlay: null,
      gameOver: false,
      bounces: state.bounces + (data.rejected ? 1 : 0),
    })
    return
  }

  if (/[+#]/.test(data.user_san)) Sound.check()
  const lines = []
  let kind = 'note'
  let openingDone = state.openingDone
  if (state.mode === 'drill' && !openingDone && data.history.length >= 20) {
    openingDone = true
    const userEval = state.flipped ? -(data.eval_cp ?? 0) : (data.eval_cp ?? 0)
    const passed = state.bounces === 0 && userEval >= -100
    if (passed) {
      kind = 'good'
      lines.push('Opening passed — 10 moves, sound position. Playing on.')
      setTimeout(Sound.good, 250)
    } else {
      kind = 'bad'
      const why = state.bounces > 0
        ? `${state.bounces} bounce${state.bounces > 1 ? 's' : ''} on the way`
        : 'the position is already worse'
      lines.push(`Opening done, but not a clean pass — ${why}. Press N to run another.`)
    }
  }
  if (data.prep_note) lines.push(data.prep_note)
  if (data.note) lines.push(data.note)
  if (data.reply_san) lines.push(`Opponent (${data.source}): ${data.reply_san}`)
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
    undoStack: [...state.undoStack, snapshot],
  })
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
  showContext(data.context)
  showMessage('')
  setPuzzleActions(false)
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
    data = await api('/api/puzzle/attempt', { id: state.puzzle.id, move: uci })
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

  if (/[+#]/.test(data.played_san)) Sound.check()
  setTimeout(data.correct ? Sound.good : Sound.bad, 150)
  showAttemptFeedback(data)
  setPuzzleActions(true)
  setState({
    positions: [...track.positions, data.fen_after],
    ucis: [...track.ucis, data.played_uci],
    sans: [...track.sans, data.played_san],
    viewPly: track.positions.length,
    evalCp: data.eval_cp,
    overlay: data.correct
      ? null
      : [data.best_uci.slice(0, 2), data.best_uci.slice(2, 4)],
    stats: data.correct
      ? { ...state.stats, right: state.stats.right + 1 }
      : { ...state.stats, wrong: state.stats.wrong + 1 },
  })
}

function retryPuzzle() {
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
  if (view === 'game') requestAnimationFrame(() => ground.redrawAll())
}

async function showHome() {
  show('home')
  const data = await api('/api/drills', {})
  document.getElementById('home-sub').textContent = `${data.user} · blitz coaching`
  const sets = data.puzzles || {}
  const blunders = sets.blunders || { total: 0, solved: 0 }
  document.getElementById('replay-desc').textContent = blunders.total
    ? `${blunders.solved} of ${blunders.total} blunders from your games solved — first try counts`
    : 'Nothing mined yet — generation may still be running'
  document.getElementById('replay-bar').style.width = blunders.total
    ? `${(100 * blunders.solved) / blunders.total}%`
    : '0'

  const prepared = data.drills.filter((d) => d.id.includes('/'))
  const mistakes = sets.openings || { total: 0, solved: 0 }
  document.getElementById('practice-desc').textContent =
    `${prepared.length} openings prepared — the opponent picks lines and steers into the `
    + `${mistakes.total - mistakes.solved} past opening mistakes you haven't fixed yet `
    + `(${mistakes.solved}/${mistakes.total} fixed)`
  document.getElementById('practice-bar').style.width = mistakes.total
    ? `${(100 * mistakes.solved) / mistakes.total}%`
    : '0'
  for (const color of ['white', 'black']) {
    const rows = prepared
      .filter((d) => d.user_color === color)
      .sort((a, b) => (b.games || 0) - (a.games || 0))
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
          meta.append(`${d.games} games · `, score)
        }
        li.append(name, meta)
        return li
      })
    document.getElementById(`home-${color}`).replaceChildren(...rows)
  }
}

async function newGame() {
  show('game')
  const data = await api('/api/new', { practice: true })
  document.getElementById('drill-name').textContent = data.drill_name || 'Opening Practice'
  showContext(null)
  showMessage(data.message, 'note')
  setPuzzleActions(false)
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
  setState({
    ...initialState,
    drill: data.drill_id || null,
    gameId: data.game_id || null,
    fen: data.fen,
    legal: data.legal,
    positions,
    ucis,
    sans,
    history,
    viewPly: 0,
    flipped: data.orientation === 'black',
    check: data.check,
    evalCp: data.eval_cp ?? 0,
    seen: state.seen,
    stats: state.stats,
  })
  if (positions.length > 1) setTimeout(() => jumpTo(positions.length - 1), 500)
}

function undo() {
  const prev = state.undoStack.at(-1)
  if (!prev || state.mode !== 'drill') return
  showMessage('Took back your last move (and the reply).', 'note')
  setState({
    ...prev,
    viewPly: prev.positions.length - 1,
    undoStack: state.undoStack.slice(0, -1),
    overlay: null,
    gameOver: false,
  })
}

const KEY_ACTIONS = {
  ArrowLeft: () => stepView(-1),
  ArrowRight: () => stepView(1),
  Escape: () => showHome(),
  h: () => showHome(),
  n: () => newGame(),
  b: () => nextPuzzle('blunders'),
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

document.getElementById('retry').addEventListener('click', retryPuzzle)
document.getElementById('next').addEventListener('click', () => nextPuzzle())
document.getElementById('home-practice').addEventListener('click', () => newGame())
document.getElementById('home-replay').addEventListener('click', () => nextPuzzle('blunders'))
document.getElementById('brand-link').addEventListener('click', () => showHome())
document.getElementById('nav-home').addEventListener('click', () => showHome())
document.getElementById('nav-practice').addEventListener('click', () => newGame())
document.getElementById('nav-blunders').addEventListener('click', () => nextPuzzle('blunders'))

ground = Chessground(document.getElementById('board'), {
  coordinates: true,
  animation: { duration: 180 },
  movable: { free: false, showDests: true },
  draggable: { showGhost: true },
  drawable: { enabled: true },
  events: { move: onUserMove },
})

showHome()
