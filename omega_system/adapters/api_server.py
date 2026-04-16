import asyncio
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from omega_system.engines.state_engine import StateEngine
from omega_system.core.message_bus import MessageBus

logger = logging.getLogger("APIServer")

app = FastAPI()
app.state_engine = None
app.message_bus = None
app.server_logic = None

class APIServer:
    def __init__(self, state_engine: StateEngine, message_bus: MessageBus):
        self.state = state_engine
        self.bus = message_bus
        self.clients = []
        
    async def stream_loop(self):
        logger.info("[APIServer] Bridging React Native WS telemetry stream at 1Hz...")
        while True:
            await asyncio.sleep(1.0)
            if not self.clients:
                continue
                
            snapshot = await self.state.get_snapshot()
            
            # Format raw structs into serialized objects
            formatted_pos = {}
            for k, v in snapshot.items():
                formatted_pos[k] = {
                    "status": v.status.value,
                    "legs": {lk: lv.symbol for lk, lv in v.legs.items()},
                    "regime": getattr(v, 'regime', 'UNKNOWN')
                }
                
            payload = {
                "health": "ONLINE",
                "active_baskets": len(snapshot),
                "baskets": formatted_pos
            }
            
            msg = json.dumps(payload)
            for ws in self.clients.copy():
                try:
                    await ws.send_text(msg)
                except Exception:
                    self.clients.remove(ws)

@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    if app.server_logic is not None:
        app.server_logic.clients.append(websocket)
        logger.info("[APIServer] React Command Center Hook Linked!")
    try:
        while True:
            data = await websocket.receive_text()
            # Command processing vector
    except WebSocketDisconnect:
        if app.server_logic and websocket in app.server_logic.clients:
            app.server_logic.clients.remove(websocket)
        logger.info("[APIServer] React Client Disconnected from Socket Bridge.")
