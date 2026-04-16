export default function BrainView({ signals }) {
  return (
    <div className="flex-grow overflow-y-auto w-full bg-[#050505]">
      <table className="w-full text-left text-xs border-collapse">
        <thead className="sticky top-0 bg-[#0a0a0a] text-bbg-cyan uppercase border-b border-bbg-border">
          <tr>
            <th className="p-2 font-normal">SYM</th>
            <th className="p-2 font-normal">REG</th>
            <th className="p-2 text-right font-normal">BETA</th>
            <th className="p-2 text-right font-normal">Z-SCR</th>
            <th className="p-2 text-right font-normal">CNF</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s, i) => (
            <tr key={i} className="border-b border-[#1a1a1a] hover:bg-[#111]">
              <td className="p-2 font-bold tracking-widest">{s.symbol}</td>
              <td className={`p-2 ${s.regime === 'COINTEGRATED' ? 'text-bbg-green' : 'text-bbg-red'}`}>{s.regime}</td>
              <td className="p-2 text-right text-gray-400">
                {s.beta?.toFixed(2)}
              </td>
              <td className="p-2 text-right">{s.z_score?.toFixed(2)}</td>
              <td className="p-2 text-right text-gray-500">{(s.confidence * 100).toFixed(0)}%</td>
            </tr>
          ))}
          {signals.length === 0 && (
            <tr><td colSpan="5" className="p-4 text-center font-mono tracking-widest text-gray-500">WAITING FOR MARKET DATA...</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
