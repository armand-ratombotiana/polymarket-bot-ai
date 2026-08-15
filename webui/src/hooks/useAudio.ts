// hooks/useAudio.ts — Web Audio API Sound Effects Synthesizer
'use client'

import { useEffect, useState } from 'react'

export function useAudio() {
  const [muted, setMuted] = useState(false)

  useEffect(() => {
    const saved = localStorage.getItem('polymarket_muted')
    if (saved !== null) setMuted(saved === 'true')
  }, [])

  const toggleMute = () => {
    setMuted((prev) => {
      const next = !prev
      localStorage.setItem('polymarket_muted', String(next))
      return next
    })
  }

  const playTone = (frequency: number, type: OscillatorType = 'sine', duration = 0.1, gainVal = 0.05) => {
    if (muted) return
    try {
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()

      osc.type = type
      osc.frequency.setValueAtTime(frequency, ctx.currentTime)
      gain.gain.setValueAtTime(gainVal, ctx.currentTime)
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + duration)

      osc.connect(gain)
      gain.connect(ctx.destination)

      osc.start()
      osc.stop(ctx.currentTime + duration)
    } catch {}
  }

  const playOrderPlaced = () => playTone(880, 'sine', 0.08, 0.04)
  const playTradeFill = () => {
    playTone(587.33, 'triangle', 0.06, 0.04)
    setTimeout(() => playTone(880, 'triangle', 0.12, 0.04), 70)
  }
  const playWhaleAlert = () => {
    playTone(330, 'sawtooth', 0.1, 0.06)
    setTimeout(() => playTone(440, 'sawtooth', 0.15, 0.06), 100)
  }
  const playKillSwitch = () => playTone(220, 'square', 0.3, 0.08)

  return {
    muted,
    toggleMute,
    playOrderPlaced,
    playTradeFill,
    playWhaleAlert,
    playKillSwitch,
  }
}
