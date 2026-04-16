import { useEffect, useState } from 'react';

function App() {
  const [livePositions, setLivePositions] = useState({});
  const [systemHealth, setSystemHealth] = useState({});
  const [regime, setRegime] = useState("UNKNOWN");

  useEffect(() => {
    // Force target 8000 for backend WS port
    const wsUrl = `ws://localhost:8000/ws/stream`;
    
    let ws = null;
    let reconnectTimeout = null;
    let isCleanup = false;

    const connect = () => {
      if (isCleanup) return;
      
      console.log(`[WS] Connecting to ${wsUrl}...`);
      ws = new WebSocket(wsUrl);
      
      ws.onopen = () => {
        console.log("[WS] Connected successfully.");
        setSystemHealth({ status: "ONLINE", connected: true });
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          
          if (payload.health) {
             setSystemHealth({ status: payload.health, connected: true });
          }
          
          if (payload.baskets) {
             setLivePositions(payload.baskets);
             // Grab regime from the first available basket if any, or default
             const basketKeys = Object.keys(payload.baskets);
             if (basketKeys.length > 0) {
                 setRegime(payload.baskets[basketKeys[0]].regime || "UNKNOWN");
             }
          }

        } catch (e) {
          console.error("[WS] Parse error", e);
        }
      };

      ws.onclose = (e) => {
        if (!isCleanup) {
          console.warn("[WS] Connection closed unexpectedly. Reconnecting in 3s...", e.reason);
          setSystemHealth({ status: "OFFLINE", connected: false });
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
        ws.onclose = null; 
        ws.close();
      }
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
    };
  }, []);

  const getRegimeColor = (reg) => {
    switch (reg) {
      case 'TRENDING': return 'text-red-500';
      case 'RANGING': return 'text-green-500';
      case 'VOLATILE': return 'text-yellow-500';
      case 'DEAD': return 'text-gray-500';
      default: return 'text-blue-500';
    }
  };

  return (
    <div className="min-h-screen bg-black text-gray-300 p-8 font-mono">
      <div className="max-w-6xl mx-auto space-y-6">
        
        {/* Header Panel */}
        <div className="border border-gray-800 bg-gray-900 p-6 flex justify-between items-center shadow-lg rounded-sm">
          <div>
            <h1 className="text-2xl font-bold tracking-widest text-cyan-400">OMEGA SYSTEM V2 Command Center</h1>
            <p className="text-sm text-gray-500 mt-1">L0 Failsafe Watchdog & Stream Protocol</p>
          </div>
          
          <div className="text-right flex items-center gap-6">
             <div className="flex flex-col items-end">
                <span className="text-xs text-gray-500 tracking-wider">MARKET REGIME</span>
                <span className={`text-xl font-bold ${getRegimeColor(regime)}`}>[{regime}]</span>
             </div>
             
             <div className="flex flex-col items-end">
                <span className="text-xs text-gray-500 tracking-wider">WATCHDOG UPLINK</span>
                <span className={`text-xl font-bold ${systemHealth.connected ? 'text-green-400' : 'text-red-600 animate-pulse'}`}>
                    {systemHealth.connected ? 'ONLINE' : 'OFFLINE'}
                </span>
             </div>
          </div>
        </div>

        {/* Live Positions */}
        <div className="border border-gray-800 bg-gray-900 p-6 shadow-lg rounded-sm">
           <h2 className="text-lg font-bold tracking-widest text-white mb-4 border-b border-gray-800 pb-2">LIVE EXECUTIONS</h2>
           
           {Object.keys(livePositions).length === 0 ? (
               <div className="text-center py-10 text-gray-600">No active baskets executing. Waiting for edge variance...</div>
           ) : (
               <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                   {Object.entries(livePositions).map(([bId, basket]) => (
                       <div key={bId} className="border border-gray-700 bg-gray-800 p-4 rounded-sm flex flex-col">
                           <div className="flex justify-between items-center border-b border-gray-700 pb-2 mb-2">
                               <span className="font-bold text-cyan-500">{bId}</span>
                               <span className={`text-sm px-2 py-1 rounded bg-black font-bold ${basket.status === 'OPEN' ? 'text-green-500' : 'text-yellow-500'}`}>
                                  {basket.status}
                               </span>
                           </div>
                           <div className="flex justify-between text-sm py-1">
                               <span className="text-gray-400">Primary Leg:</span>
                               <span className="text-white">{basket.legs?.PRIMARY || 'Pending'}</span>
                           </div>
                           <div className="flex justify-between text-sm py-1">
                               <span className="text-gray-400">Hedge Leg:</span>
                               <span className="text-white">{basket.legs?.HEDGE || 'Pending'}</span>
                           </div>
                           <div className="flex justify-between text-sm pt-2 mt-2 border-t border-gray-700">
                               <span className="text-gray-400">Contextual Regime:</span>
                               <span className={`font-bold ${getRegimeColor(basket.regime)}`}>{basket.regime}</span>
                           </div>
                       </div>
                   ))}
               </div>
           )}
        </div>
      </div>
    </div>
  )
}

export default App
