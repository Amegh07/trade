import { useEffect, useState } from 'react'
import EquityChart from './components/EquityChart'
import BrainView from './components/BrainView'
import RiskPanel from './components/RiskPanel'
import LiveTrades from './components/LiveTrades'
import ControlPad from './components/ControlPad'
import TerminalLog from './components/TerminalLog'

function App() {
  const [signals, setSignals] = useState([])
  const [logs, setLogs] = useState([])
  
  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/stream`
    
    let ws = null;
    let reconnectTimeout = null;
    let isCleanup = false;

    const connect = () => {
      if (isCleanup) return;
      
      console.log(`[WS] Connecting to ${wsUrl}...`);
      ws = new WebSocket(wsUrl);
      
      ws.onopen = () => {
        console.log("[WS] Connected successfully.");
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === 'BRAIN_TICK') {
            setSignals(payload.data);
          } else if (payload.type === 'LOG') {
            setLogs(prev => [...prev.slice(-300), payload.message]);
          }
        } catch (e) {
          console.error("[WS] Parse error", e);
        }
      };

      ws.onclose = (e) => {
        if (!isCleanup) {
          console.warn("[WS] Connection closed unexpectedly. Reconnecting in 3s...", e.reason);
          reconnectTimeout = setTimeout(connect, 3000);
        } else {
          console.log("[WS] Connection closed for cleanup.");
        }
      };

      ws.onerror = (err) => {
        console.error("[WS] Connection error:", err);
      };
    };

    connect();

    return () => {
      isCleanup = true;
      if (ws) {
        // Prevent the onclose handler from firing reconnection
        ws.onclose = null; 
        ws.close();
      }
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
    };
  }, []);

  return (
    <div className="min-h-screen bg-bbg-bg text-gray-300 grid grid-cols-12 grid-rows-6 gap-2 p-2 h-screen">
      
      {/* Top Left: Chart (Col 1-8, Row 1-4) */}
      <div className="col-span-9 row-span-4 border border-bbg-border bg-bbg-panel overflow-hidden flex flex-col">
        <div className="border-b border-bbg-border p-2 text-bbg-cyan font-bold bg-black tracking-widest text-sm">
          [ PORTFOLIO EQUITY ]
        </div>
        <div className="flex-grow relative bg-[#0a0a0a]">
          <EquityChart />
        </div>
      </div>

      {/* Top Right: Risk & Controls (Col 9-12, Row 1-4) */}
      <div className="col-span-3 row-span-4 flex flex-col gap-2">
        <RiskPanel />
        <ControlPad />
        <LiveTrades />
      </div>

      {/* Bottom Left: Terminal (Col 1-8, Row 5-6) */}
      <div className="col-span-8 row-span-2 border border-bbg-border bg-bbg-panel flex flex-col overflow-hidden">
        <div className="border-b border-bbg-border p-2 text-bbg-cyan font-bold bg-black tracking-widest text-sm">
          [ SYSTEM TERMINAL ]
        </div>
        <TerminalLog logs={logs} />
      </div>

      {/* Bottom Right: Brain View (Col 9-12, Row 5-6) */}
      <div className="col-span-4 row-span-2 border border-bbg-border bg-bbg-panel flex flex-col overflow-hidden">
        <div className="border-b border-bbg-border p-2 text-bbg-cyan font-bold bg-black flex justify-between tracking-widest text-sm">
          <span>[ BRAIN MATRIX ]</span>
          <span className="text-bbg-green animate-pulse">● LIVE</span>
        </div>
        <BrainView signals={signals} />
      </div>

    </div>
  )
}

export default App
