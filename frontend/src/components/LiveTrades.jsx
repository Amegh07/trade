import { useEffect, useState } from 'react'

export default function LiveTrades() {
  const [trades, setTrades] = useState([])

  useEffect(() => {
    const update = async () => {
      try {
        const res = await fetch('/api/positions')
        setTrades(await res.json())
      } catch (e) {}
    }
    update()
    const int = setInterval(update, 3000)
    return () => clearInterval(int)
  }, [])

  return (
    <div className="border border-bbg-border bg-bbg-panel flex-grow flex flex-col overflow-hidden">
      <div className="text-bbg-cyan font-bold tracking-widest text-sm p-3 border-b border-bbg-border bg-black">[ ACTIVE POSITIONS ]</div>
      <div className="overflow-y-auto flex-grow p-2">
        {trades.map((t, i) => (
          <div key={i} className="flex justify-between items-center bg-[#0a0a0a] border border-[#1a1a1a] p-2 mb-2 text-xs">
            <div>
              <div className="font-bold text-sm tracking-widest">{t.ticker}</div>
              <div className="text-gray-600">#{t.ticket}</div>
            </div>
            <div className="text-right">
              <div className={`font-bold ${t.side === 'BUY' ? 'text-bbg-green' : 'text-bbg-red'}`}>
                {t.side} {t.lot}
              </div>
              <div className="text-gray-400">@ {t.entry_price}</div>
            </div>
          </div>
        ))}
        {trades.length === 0 && <div className="text-gray-500 font-mono tracking-widest text-xs text-center mt-4 pt-4">NO ACTIVE SIGNALS</div>}
      </div>
    </div>
  )
}
