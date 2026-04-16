import { useEffect, useState } from 'react'

export default function RiskPanel() {
  const [data, setData] = useState(null)

  useEffect(() => {
    const update = async () => {
      try {
        const res = await fetch('/api/risk')
        const json = await res.json()
        setData(json)
      } catch (e) {
        console.error("Risk fetch error", e)
      }
    }
    update()
    const int = setInterval(update, 5000)
    return () => clearInterval(int)
  }, [])

  if (!data) {
    return (
      <div className="border border-bbg-border bg-bbg-panel p-3 h-full flex flex-col justify-center items-center text-gray-600 font-mono text-xs tracking-widest">
        WAITING FOR RISK DATA...
      </div>
    )
  }

  return (
    <div className="border border-bbg-border bg-bbg-panel p-3">
      <div className="text-bbg-cyan font-bold tracking-widest text-sm mb-3">[ RISK METRICS ]</div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="text-xs text-gray-500 mb-1">TOTAL EXPOSURE</div>
          <div className="text-xl font-bold">{data.total_exposure.toFixed(2)} L</div>
        </div>
        <div>
          <div className="text-xs text-gray-500 mb-1">CURRENT DRAWDOWN</div>
          <div className={`text-xl font-bold ${data.drawdown_pct > 1.5 ? 'text-bbg-red' : 'text-gray-300'}`}>
            {data.drawdown_pct.toFixed(2)}%
          </div>
        </div>
      </div>
      <div className="mt-4">
        <div className="text-xs text-gray-500 mb-1 flex justify-between">
          <span>COVARIANCE OVERLAY PENALTY</span>
          <span className="text-bbg-red font-bold">{(data.correlation_penalty * 100).toFixed(0)}%</span>
        </div>
        <div className="w-full bg-[#1a1a1a] h-2">
          <div className="bg-bbg-red h-full transition-all duration-500" style={{width: `${data.correlation_penalty * 100}%`}}></div>
        </div>
      </div>
    </div>
  )
}
