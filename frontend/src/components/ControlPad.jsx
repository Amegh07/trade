import { useState } from 'react'

export default function ControlPad() {
  const [override, setOverride] = useState(false)
  const [ghost, setGhost] = useState(false)
  const [risk, setRisk] = useState(1.0)
  const [killed, setKilled] = useState(false)

  const syncState = async (m_over, m_ghost, m_risk) => {
    try {
      await fetch('/api/control/override', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manual_override: m_over, ghost_mode: m_ghost, max_risk_pct: m_risk })
      })
    } catch (e) {
      console.error(e)
    }
  }

  const handleKill = async () => {
    try {
      await fetch('/api/control/kill', { method: 'POST' })
      setKilled(true)
    } catch (e) {}
  }

  return (
    <div className="border border-bbg-border bg-bbg-panel p-3">
      <div className="text-bbg-cyan font-bold tracking-widest text-sm mb-4">[ TACTICAL CONTROLS ]</div>
      
      <div className="space-y-5">
        {/* Kill Switch */}
        <button 
          onClick={handleKill}
          disabled={killed}
          className={`w-full py-3 text-center font-bold tracking-widest text-black uppercase transition-colors ${killed ? 'bg-[#330000] text-red-900 border border-red-900' : 'bg-bbg-red hover:bg-red-600 shadow-[0_0_15px_rgba(255,0,0,0.4)]'}`}
        >
          {killed ? 'SYSTEM KILLED' : 'EMERGENCY STOP'}
        </button>

        {/* Toggles */}
        <div className="flex justify-between items-center bg-[#0a0a0a] p-2 border border-[#1a1a1a]">
          <span className="text-xs text-gray-400 font-bold">COMMANDER OVERRIDE</span>
          <label className="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" className="sr-only peer" checked={override} onChange={(e) => {
              setOverride(e.target.checked); syncState(e.target.checked, ghost, risk);
            }} />
            <div className="w-9 h-5 bg-[#1a1a1a] peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-gray-400 after:border-gray-500 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-bbg-green peer-checked:after:bg-black"></div>
          </label>
        </div>

        <div className="flex justify-between items-center bg-[#0a0a0a] p-2 border border-[#1a1a1a]">
          <span className="text-xs text-gray-400 font-bold">GHOST MODE</span>
          <label className="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" className="sr-only peer" checked={ghost} onChange={(e) => {
              setGhost(e.target.checked); syncState(override, e.target.checked, risk);
            }} />
            <div className="w-9 h-5 bg-[#1a1a1a] peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-gray-400 after:border-gray-500 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-purple-600 peer-checked:after:bg-black"></div>
          </label>
        </div>

        {/* Slider */}
        <div className="pt-2">
          <div className="text-xs text-gray-400 mb-2 flex justify-between font-bold">
            <span>KELLY SOFT LIMIT (%)</span>
            <span className="text-bbg-cyan">{risk.toFixed(1)}%</span>
          </div>
          <input 
            type="range" min="0.1" max="5.0" step="0.1" 
            value={risk} 
            onChange={(e) => setRisk(parseFloat(e.target.value))}
            onMouseUp={() => syncState(override, ghost, risk)}
            className="w-full h-1 bg-[#1a1a1a] rounded-lg appearance-none cursor-pointer" 
          />
        </div>
      </div>
    </div>
  )
}
