// Recorded samples from the lichess (lila) open-source standard sound set,
// decoded through Web Audio for low latency. Falls back to synthesized tones
// if a file is missing or still loading.
const Sound = (() => {
  const FILES = {
    move: 'sounds/Move.mp3',
    capture: 'sounds/Capture.mp3',
    check: 'sounds/GenericNotify.mp3',
    good: 'sounds/Confirmation.mp3',
    bad: 'sounds/Error.mp3',
  }
  const buffers = {}
  const lastAttempt = {}
  let muted = false
  // Create the context up front (suspended until the first user gesture) so
  // samples decode eagerly — otherwise early moves fall back to synth beeps.
  const ctx = new (window.AudioContext || window.webkitAudioContext)()

  function loadOne(name) {
    if (buffers[name]) return
    const now = Date.now()
    if (lastAttempt[name] && now - lastAttempt[name] < 3000) return
    lastAttempt[name] = now
    fetch(FILES[name])
      .then((res) => (res.ok ? res.arrayBuffer() : Promise.reject(new Error(res.status))))
      .then((ab) => ctx.decodeAudioData(ab))
      .then((buf) => { buffers[name] = buf })
      .catch(() => { /* retried on a later play */ })
  }
  Object.keys(FILES).forEach(loadOne)

  function ensure() {
    if (ctx.state === 'suspended') ctx.resume()
    return ctx
  }

  function playBuffer(name, vol) {
    const c = ensure()
    if (!buffers[name]) {
      loadOne(name) // heal: a failed load (e.g. server was restarting) retries
      return false
    }
    const src = c.createBufferSource()
    src.buffer = buffers[name]
    const gain = c.createGain()
    gain.gain.value = vol
    src.connect(gain)
    gain.connect(c.destination)
    src.start()
    return true
  }

  function tone({ freq, dur, vol = 0.2, type = 'sine', drop = 0.65, delay = 0 }) {
    const c = ensure()
    const t = c.currentTime + delay
    const osc = c.createOscillator()
    const gain = c.createGain()
    osc.type = type
    osc.frequency.setValueAtTime(freq, t)
    osc.frequency.exponentialRampToValueAtTime(freq * drop, t + dur)
    gain.gain.setValueAtTime(vol, t)
    gain.gain.exponentialRampToValueAtTime(0.001, t + dur)
    osc.connect(gain)
    gain.connect(c.destination)
    osc.start(t)
    osc.stop(t + dur)
  }

  const SYNTH = {
    move: () => tone({ freq: 210, dur: 0.08, vol: 0.22 }),
    capture: () => tone({ freq: 150, dur: 0.11, vol: 0.28 }),
    check: () => {
      tone({ freq: 330, dur: 0.07, vol: 0.2 })
      tone({ freq: 392, dur: 0.09, vol: 0.18, delay: 0.07 })
    },
    good: () => {
      tone({ freq: 523, dur: 0.1, vol: 0.14, drop: 1 })
      tone({ freq: 784, dur: 0.16, vol: 0.12, drop: 1, delay: 0.09 })
    },
    bad: () => tone({ freq: 196, dur: 0.2, vol: 0.15, type: 'square', drop: 0.5 }),
  }

  const VOLUME = { move: 0.9, capture: 0.9, check: 0.7, good: 0.8, bad: 0.7 }

  const play = (name) => () => {
    if (muted) return
    try {
      if (!playBuffer(name, VOLUME[name])) SYNTH[name]()
    } catch { /* audio is never worth breaking the app over */ }
  }

  return {
    move: play('move'),
    capture: play('capture'),
    check: play('check'),
    good: play('good'),
    bad: play('bad'),
    toggle: () => {
      muted = !muted
      return muted
    },
  }
})()
