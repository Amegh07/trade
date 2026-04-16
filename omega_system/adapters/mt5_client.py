import asyncio
import MetaTrader5 as mt5
import pandas as pd
import time
from omega_system.utils.logger import setup_logger

logger = setup_logger("MT5Client")

class MT5Client:
    def __init__(self):
        pass

    async def initialize(self, login=None, password=None, server=None):
        loop = asyncio.get_event_loop()
        def _init():
            if login and password and server:
                return mt5.initialize(login=int(login), password=password, server=server)
            return mt5.initialize()
        return await loop.run_in_executor(None, _init)

    async def symbol_info_tick(self, symbol):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, mt5.symbol_info_tick, symbol)

    async def positions_get(self, symbol=None, ticket=None):
        loop = asyncio.get_event_loop()
        def _pos():
            if ticket: return mt5.positions_get(ticket=ticket)
            if symbol: return mt5.positions_get(symbol=symbol)
            return mt5.positions_get()
        return await loop.run_in_executor(None, _pos)

    async def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, mt5.copy_rates_from_pos, symbol, timeframe, start_pos, count)

    async def order_send(self, request):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, mt5.order_send, request)

    # Core Execution Engine Resolvers (Non-Blocking)
    async def market_order(self, symbol, action, volume):
        tick = await self.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"[MT5Client] market_order failed to retrieve tick for {symbol}")
            return None
            
        ord_type = mt5.ORDER_TYPE_BUY if action in ["BUY", "ENTER"] else mt5.ORDER_TYPE_SELL
        price = tick.ask if ord_type == mt5.ORDER_TYPE_BUY else tick.bid
        
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": ord_type,
            "price": price,
            "deviation": 20,
            "magic": 100000,
            "comment": "omega_v2",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = await self.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return res.order
        if res:
            logger.error(f"[MT5Client] Order rejected by broker: {res.comment} [{res.retcode}]")
        return None

    async def close_position_by_ticket(self, symbol, ticket):
        positions = await self.positions_get(ticket=ticket)
        if not positions: return False
        
        pos = positions[0]
        ord_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = await self.symbol_info_tick(symbol)
        if not tick: return False
        
        price = tick.bid if ord_type == mt5.ORDER_TYPE_SELL else tick.ask
        
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": ord_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 100000,
            "comment": "closing",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = await self.order_send(req)
        return (res is not None and res.retcode == mt5.TRADE_RETCODE_DONE)

    async def close_position_by_symbol(self, symbol):
        success = True
        positions = await self.positions_get(symbol=symbol)
        if not positions: return True
        for p in positions:
            res = await self.close_position_by_ticket(symbol, p.ticket)
            if not res: success = False
        return success
        
    async def terminal_info(self):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, mt5.terminal_info)
        
    async def get_order_book(self, symbol: str) -> dict:
        """Leak-proof Async DOM fetcher for Smart Routing liquidity tests."""
        loop = asyncio.get_event_loop()
        
        subscribed = await loop.run_in_executor(None, mt5.market_book_add, symbol)
        if not subscribed:
            return {"bid_vol": 0.0, "ask_vol": 0.0}
            
        try:
            await asyncio.sleep(0.05) # Allow MT5 engine to pool DOM frames
            book = await loop.run_in_executor(None, mt5.market_book_get, symbol)
            if not book:
                return {"bid_vol": 0.0, "ask_vol": 0.0}
                
            bid_vol = sum(i.volume for i in book if i.type == mt5.BOOK_TYPE_BUY)
            ask_vol = sum(i.volume for i in book if i.type == mt5.BOOK_TYPE_SELL)
            return {"bid_vol": bid_vol, "ask_vol": ask_vol}
            
        finally:
            # Memory leak patch: Guarantee release overriding any network failure cases
            await loop.run_in_executor(None, mt5.market_book_release, symbol)
        
    def shutdown(self):
        mt5.shutdown()
