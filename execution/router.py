import time
import random
import asyncio
import MetaTrader5 as mt5

from utils.logger import setup_logger

logger = setup_logger("router")

MAX_LOT_HARDCAP = 2.0   # absolute ceiling — prevents margin rejection on tight stops and oversized lots


# ── Symbol Precision Helper ──────────────────────────────────────────────────
def _get_digits(symbol: str) -> int:
    """
    Query the broker for the number of decimal places used for `symbol` prices.

    MT5 strictly rejects orders whose SL/TP/price have more decimal places
    than the symbol supports.  Varying by asset class:
      • Forex major  (EURUSD)  →  5 digits
      • Yen pair    (USDJPY)  →  3 digits
      • Gold        (XAUUSD)  →  2 digits
      • Bitcoin     (BTCUSD)  →  2 digits  (broker-dependent)

    Falls back to 5 (standard Forex) if the symbol cannot be resolved.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.warning(f"_get_digits: symbol_info({symbol}) returned None — defaulting to 5.")
        return 5
    return int(info.digits)


class LiveOrderRouter:
    """
    Canonical execution router. All order dispatch and position queries
    flow through this class. No local state is maintained — every position
    check hits the MT5 terminal directly.
    """
    def __init__(self):
        # Initial jitter delay boundary representing baseline network/agent latency
        self.iceberg_jitter = 0.0

    # ── Position Queries ───────────────────────────────────────────────────
    def get_open_positions(self, symbol: str) -> list:
        """
        Query the MT5 terminal directly for active positions on `symbol`.
        Returns an empty list if none exist or on error.
        """
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            logger.error(
                f"positions_get failed for {symbol} — "
                f"error: {mt5.last_error()}"
            )
            return []
        logger.info(f"Open positions on {symbol}: {len(positions)}")
        return list(positions)

    def get_all_open_positions(self) -> list:
        """
        Query the MT5 terminal directly for all active positions across all symbols.
        Returns an empty list if none exist or on error.
        """
        positions = mt5.positions_get()
        if positions is None:
            logger.error(f"positions_get failed for all symbols — error: {mt5.last_error()}")
            return []
        return list(positions)

    # ── Order Execution with Retry (Trap Maker) ────────────────────────────
    def execute_trap(
        self,
        symbol: str,
        is_buy: bool,
        volume: float,
        atr_value: float,
        trap_price: float,
        tp_target: float,
        strategy_name: str,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        """
        Deploy Pending Orders (Traps) as a Maker instead of taking liquidity.
        """
        # ── 1. Strategy to Order Type Mapping ────────────────────────────────
        if strategy_name == "MeanReversion":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
        elif strategy_name == "MomentumBreakout":
            order_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
        else:
            logger.error(f"Unknown strategy_name: {strategy_name}")
            return None

        # ── 2. Symbol meta (precision + stop level + max volume) ─────────────
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"symbol_info({symbol}) returned None — cannot send order.")
            return None

        digits       = int(info.digits)
        point        = info.point
        stop_level   = int(info.trade_stops_level)
        spread_pts   = int(info.spread)
        volume_max   = float(info.volume_max)

        def _round(price: float) -> float:
            return round(price, digits)

        trap_price = _round(trap_price)

        # ── 3. Fetch live tick price ─────────────────────────────────────────
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"symbol_info_tick({symbol}) returned None — aborting.")
            return None

        current_price = tick.ask if is_buy else tick.bid

        # ── 4. Enforce broker minimum stop level for Trap Placement ──────────
        min_distance = (stop_level + spread_pts) * point

        if abs(trap_price - current_price) < min_distance:
            logger.warning(
                f"[{symbol}] 🚫 TRAP REJECTED | Price too close to market (violates stops_level). Edge lost. Abandoning."
            )
            return None

        # ── 8. Calculate Trailing Margins ────────────────────────────────────
        # Apeiron Layer: 2.5x ATR dynamic volatility Stop Loss expansion
        sl_margin = atr_value * 2.5 

        if is_buy:
            sl_price = max(0.000001, trap_price - sl_margin)
            tp_price = _round(tp_target)
        else:
            sl_price = trap_price + sl_margin
            tp_price = _round(tp_target)

        # Enforce SL/TP distance
        if abs(trap_price - sl_price) < min_distance:
            widened = _round(min_distance + sl_margin)
            sl_price = _round(trap_price - widened) if is_buy else _round(trap_price + widened)
            
        if abs(trap_price - tp_price) < min_distance:
            widened_tp = _round(min_distance)
            tp_price = _round(trap_price + widened_tp) if is_buy else _round(trap_price - widened_tp)

        # ── 6. Cap lot size ──────────────────────────────────────────────────
        capped_volume = min(float(volume), volume_max, MAX_LOT_HARDCAP)
        volume = round(capped_volume, 2)

        # Fix #28: Expiration Timezone Crash. Build expiration relative to the broker server clock
        # extracted from the live tick, rather than the local machine's UTC epoch which could be hours behind.
        server_time = tick.time if tick else int(time.time())

        # ── 7. Build request (Maker Order) ───────────────────────────────────
        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       symbol,
            "volume":       float(volume),
            "type":         order_type,
            "price":        trap_price,
            "sl":           float(sl_price),
            "tp":           float(tp_price),
            "deviation":    20,
            "magic":        100000,
            "comment":      "algo_trap",
            "type_time":    mt5.ORDER_TIME_SPECIFIED,
            "expiration":   int(server_time + (15 * 60)), # 15 minutes TTL
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        # ── 8. Retry loop (For trap placing) ──────────────────────────────────────
        RETRYABLE = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_CONNECTION}

        for attempt in range(1, max_retries + 1):
            logger.info(
                f"[{symbol}] 🪤 TRAP SET | {'Buy' if is_buy else 'Sell'} {order_type} "
                f"placed at {trap_price:.{digits}f}. Expires in 15 mins."
            )

            result = mt5.order_send(request)

            if result is None:
                logger.error(f"order_send returned None — MT5 error: {mt5.last_error()}")
                return None

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Trap DONE ✓ — deal={result.deal} retcode={result.retcode}")
                return result

            if result.retcode in RETRYABLE and attempt < max_retries:
                logger.warning(f"Retryable failure — retcode={result.retcode} ({result.comment}). Retrying in {retry_delay}s…")
                time.sleep(retry_delay)
                continue

            logger.error(f"Trap FAILED — retcode={result.retcode} comment='{result.comment}'")
            return result

        logger.error(f"Trap exhausted all {max_retries} retries for {symbol}.")
        return None

    # ── Convenience Wrappers ───────────────────────────────────────────────
    def execute_buy(self, symbol: str, volume: float, atr_value: float, trap_price: float, tp_target: float, strategy_name: str):
        return self.execute_trap(symbol, True, volume, atr_value, trap_price, tp_target, strategy_name)

    def execute_sell(self, symbol: str, volume: float, atr_value: float, trap_price: float, tp_target: float, strategy_name: str):
        return self.execute_trap(symbol, False, volume, atr_value, trap_price, tp_target, strategy_name)

    def close_position(self, position):
        """
        Closes an open position with a market order.
        """
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            logger.error(f"close_position failed — symbol_info_tick returned None for {position.symbol}")
            return None
            
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": order_type,
            "price": tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask,
            "deviation": 20,
            "magic": 100000,
            "comment": "time_decay_exit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else mt5.last_error()
            logger.error(f"Failed to close position {position.ticket} on {position.symbol} — {err}")
        else:
            logger.info(f"Position {position.ticket} on {position.symbol} successfully closed.")
        return result

    # ── Market Execution (Immediate Fill) ──────────────────────────────────
    def execute_market(
        self,
        symbol: str,
        is_buy: bool,
        volume: float,
        atr_value: float,
        tp_target: float,
        sl_target: float,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        """
        Executes an immediate Market Order (Taker) by sending TRADE_ACTION_DEAL.
        Crucial for StatArb where entering pending limits risks single-leg execution.
        """
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"symbol_info({symbol}) returned None — cannot send order.")
            return None

        digits     = int(info.digits)
        volume_max = float(info.volume_max)
        stop_level = int(info.trade_stops_level)
        spread_pts = int(info.spread)
        point      = info.point
        
        # Cap lot size
        capped_volume = min(float(volume), volume_max, MAX_LOT_HARDCAP)
        volume = round(capped_volume, 2)

        min_distance = (stop_level + spread_pts) * point

        RETRYABLE = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_CONNECTION}

        for attempt in range(1, max_retries + 1):
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error(f"symbol_info_tick({symbol}) returned None — aborting market execution.")
                return None

            mid_price = (tick.ask + tick.bid) / 2.0
            price = tick.ask if is_buy else tick.bid
            
            # Compute dynamic SL based on live execution price boundaries
            sl_price = sl_target
            tp_price = tp_target

            # ── Enforce Broker Minimum Stop Levels ──
            if abs(price - sl_price) < min_distance:
                sl_price = price - (min_distance + (atr_value * 2.5)) if is_buy else price + (min_distance + (atr_value * 2.5))
                
            if abs(price - tp_price) < min_distance:
                tp_price = price + min_distance if is_buy else price - min_distance

            sl_price = round(sl_price, digits)
            tp_price = round(tp_price, digits)

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       float(volume),
                "type":         order_type,
                "price":        float(price),
                "sl":           float(sl_price),
                "tp":           float(tp_price),
                "deviation":    20,
                "magic":        200000,
                "comment":      "algo_market",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_FOK,
            }

            logger.info(f"[{symbol}] ⚡ MARKET {order_type} ORDER | {volume} lots at {price:.{digits}f}")

            result = mt5.order_send(request)

            if result is None:
                logger.error(f"order_send unconditionally failed: {mt5.last_error()}")
                return None

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                # Execution confirmed! Calculate slippage dynamically.
                slippage_points = abs(result.price - mid_price) / point
                logger.info(f"Market deal executed! Deal ticket: {result.deal} | Slippage: {slippage_points:.1f} pts")
                return result, slippage_points

            if result.retcode in RETRYABLE and attempt < max_retries:
                logger.warning(f"Retryable failure — retcode={result.retcode} ({result.comment}). Retrying in {retry_delay}s…")
                time.sleep(retry_delay)
                continue

            logger.error(f"Market FAILED — retcode={result.retcode} comment='{result.comment}'")
            return result, 0.0

        logger.error(f"Market execution exhausted all {max_retries} retries for {symbol}.")
        return None, 0.0

    # ── Iceberg Slicing (Institutional Execution) ──────────────────────────
    async def execute_iceberg(
        self,
        symbol: str,
        is_buy: bool,
        total_volume: float,
        atr_value: float,
        tp_target: float,
    ):
        """
        Slices a huge market order into 3-7 random sub-orders and executes them
        with a jittered delay (400ms - 1800ms) to hide presence and reduce slippage.
        Every slice carries SL/TP — no naked trades allowed.
        """
        num_chunks = random.randint(3, 7)
        chunk_vol  = round(total_volume / num_chunks, 2)
        
        # Adjust remainder volume due to rounding
        chunks = [chunk_vol] * (num_chunks - 1)
        last_chunk = total_volume - sum(chunks)
        chunks.append(round(last_chunk, 2))
        
        info = mt5.symbol_info(symbol)
        if not info:
            logger.error(f"Failed to fetch symbol_info for {symbol} in Iceberg.")
            return

        digits = int(info.digits)
        stop_level = int(info.trade_stops_level)
        spread_pts = int(info.spread)
        point = info.point
        
        min_distance = (stop_level + spread_pts) * point
        
        logger.info(f"[{symbol}] 🧊 Initiating Iceberg Slicing. Total={total_volume} in {len(chunks)} chunks.")
        
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

        for i, chunk in enumerate(chunks):
            # Apeiron Layer: Add TCA mapped dynamic latency buffer (iceberg_jitter)
            await asyncio.sleep(random.uniform(0.5, 1.5) + self.iceberg_jitter)
            
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error(f"Skipping slice {i+1} due to unavailable tick.")
                continue
                
            price    = tick.ask if is_buy else tick.bid
            
            # ── Enforce Broker Minimum Stop Levels ──
            if is_buy:
                sl = max(0.000001, price - (atr_value * 2.5))
            else:
                sl = price + (atr_value * 2.5)
                
            # Align limits sequentially
            if is_buy:
                tp_price = price + min_distance
                if tp_target > tp_price: tp_price = tp_target
                sl_price = price - min_distance
                if sl < sl_price: sl_price = sl
            else:
                tp_price = price - min_distance
                if tp_target < tp_price: tp_price = tp_target
                sl_price = price + min_distance
                if sl > sl_price: sl_price = sl
                
            sl_price = round(sl_price, digits)
            tp_price = round(tp_price, digits)
            
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       float(chunk),
                "type":         order_type,
                "price":        float(price),
                "sl":           float(sl_price),
                "tp":           float(tp_price),
                "deviation":    20,
                "magic":        200000,
                "comment":      f"iceberg_{i+1}/{num_chunks}",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_FOK,
            }
            
            def _execute_slice(req):
                return mt5.order_send(req)
            
            # Fire the slice using run_in_executor to avoid blocking the async flow heavily
            result = await asyncio.get_running_loop().run_in_executor(None, _execute_slice, request)
            
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[{symbol}] 🧊 Slice {i+1}/{num_chunks} ({chunk} lots) DONE ✓")
            else:
                err = result.comment if result else mt5.last_error()
                logger.error(f"[{symbol}] 🧊 Slice {i+1} FAILED — {err}")
                
            # Random Jitter Wait (400ms - 1800ms)
            if i < len(chunks) - 1:
                delay = random.uniform(0.400, 1.800)
                await asyncio.sleep(delay)
