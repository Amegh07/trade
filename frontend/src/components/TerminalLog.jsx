import { useEffect, useRef } from 'react'

export default function TerminalLog({ logs }) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [logs])

  const getColor = (line) => {
    if (line.includes("ERROR") || line.includes("CRITICAL")) return "text-bbg-red"
    if (line.includes("WARN")) return "text-yellow-500"
    if (line.includes("ORDER") || line.includes("DEAL") || line.includes("Iceberg")) return "text-bbg-green font-bold"
    if (line.includes("choked") || line.includes("skipped")) return "text-bbg-red"
    return "text-[#8ab4f8]"
  }

  return (
    <div className="flex-grow p-2 overflow-y-auto text-xs font-mono bg-black">
      {logs.length === 0 && <span className="text-gray-600">Awaiting telemetry...</span>}
      {logs.map((log, i) => (
        <div key={i} className={`whitespace-pre-wrap break-all ${getColor(log)} mb-1`}>
          {log}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  )
}
