import os
import json
import asyncio
import logging
from typing import Dict
from omega_system.core.types import BasketState, LegState, OrderStatus

logger = logging.getLogger("StateRepo")

class StateRepository:
    def __init__(self, backup_path="logs/state_backup.json"):
        self.backup_path = backup_path
        os.makedirs(os.path.dirname(self.backup_path), exist_ok=True)
        self.temp_path = self.backup_path + ".tmp"
        
    async def save_snapshot(self, state_dict: Dict[str, BasketState]):
        """Atomically serializes the active state pool to disk."""
        loop = asyncio.get_event_loop()
        
        def _write():
            raw_dict = {}
            for k, v in state_dict.items():
                legs_dict = {}
                for l_k, l_v in v.legs.items():
                    legs_dict[l_k] = {
                        "symbol": l_v.symbol,
                        "role": l_v.role,
                        "target_lot": l_v.target_lot,
                        "filled_lot": l_v.filled_lot,
                        "ticket": l_v.ticket,
                        "status": l_v.status.value
                    }
                
                raw_dict[k] = {
                    "basket_id": v.basket_id,
                    "status": v.status.value,
                    "legs": legs_dict,
                    "entry_time": v.entry_time,
                    "z_score_entry": v.z_score_entry,
                    "error_msg": v.error_msg,
                    "regime": v.regime
                }
            
            with open(self.temp_path, "w") as f:
                json.dump(raw_dict, f)
            os.replace(self.temp_path, self.backup_path) # Atomic overwrite
            
        await loop.run_in_executor(None, _write)

    async def load_snapshot(self) -> Dict[str, BasketState]:
        """Loads and reconstitutes the exact runtime classes from memory payload."""
        loop = asyncio.get_event_loop()
        
        def _read():
            if not os.path.exists(self.backup_path):
                return {}
            try:
                with open(self.backup_path, "r") as f:
                    raw_dict = json.load(f)
                
                restored = {}
                for k, v in raw_dict.items():
                    legs = {}
                    for l_k, l_v in v['legs'].items():
                        legs[l_k] = LegState(
                            symbol=l_v['symbol'],
                            role=l_v['role'],
                            target_lot=l_v['target_lot'],
                            filled_lot=l_v['filled_lot'],
                            ticket=l_v['ticket'],
                            status=OrderStatus(l_v['status'])
                        )
                        
                    restored[k] = BasketState(
                        basket_id=v['basket_id'],
                        status=OrderStatus(v['status']),
                        legs=legs,
                        entry_time=v['entry_time'],
                        z_score_entry=v['z_score_entry'],
                        error_msg=v['error_msg'],
                        regime=v.get('regime', 'UNKNOWN')
                    )
                return restored
            except Exception as e:
                logger.error(f"[StateRepo] Failed to reconstitute memory snapshot: {e}")
                return {}
                
        return await loop.run_in_executor(None, _read)
