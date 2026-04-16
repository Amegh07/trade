import asyncio
import MetaTrader5 as mt5
import pandas as pd
import time
from omega_system.utils.logger import setup_logger

# Use professional logger exclusively configured within the utils path
logger = setup_logger("mt5_client")

def initialize_mt5(login=None, password=None, server=None):
    """
    Initializes the MetaTrader5 terminal. If credentials are provided, attempts login.
    """
    logger.info("Initializing MetaTrader5 connection...")
    
    # MT5 strictly requires integer login IDs natively
    if login is not None:
        try:
            login = int(login)
        except (ValueError, TypeError):
            logger.warning(f"Invalid login ID '{login}'. Resetting to generic launch.")
            login = None

    if login and password and server:
        logger.info(f"Attempting to launch and log in to '{server}' account '{login}'...")
        if not mt5.initialize(login=login, password=password, server=server):
            logger.error(f"mt5.initialize(credentials) failed, error code: {mt5.last_error()}")
            return False
    else:
        logger.info("Launching generic MT5 terminal (no remote credentials applied)...")
        if not mt5.initialize():
            logger.error(f"mt5.initialize() failed generically, error code: {mt5.last_error()}")
            return False
            
    logger.info("MetaTrader5 initialized successfully.")
    return True

def fetch_rates(symbol, count, timeframe=mt5.TIMEFRAME_M5):
    """
    Fetches historical M5 data (or specified timeframe) for a given symbol.
    Returns a localized pandas DataFrame with 'time' as the index.
    """
    logger.info(f"Fetching {count} rates for {symbol} at timeframe {timeframe}...")
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    
    if rates is None:
        logger.error(f"Failed to fetch rates, error code: {mt5.last_error()}")
        # Return an empty DataFrame with the expected index name
        df = pd.DataFrame()
        df.index.name = 'time'
        return df
        
    # Convert to DataFrame
    df = pd.DataFrame(rates)
    
    # Convert 'time' to datetime objects and set as index
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # "localized pandas DataFrame" - often means timezone awareness
    # MT5 times are server-time, but let's localize if needed or leave as is.
    # To be extremely safe, we assign the time column as the index.
    df.set_index('time', inplace=True)
    
    logger.info(f"Successfully fetched {len(df)} records for {symbol}.")
    return df

def shutdown_mt5():
    """
    Shuts down the MetaTrader5 connection.
    """
    logger.info("Shutting down MetaTrader5...")
    mt5.shutdown()
    logger.info("MetaTrader5 shutdown complete.")

def get_open_positions(symbol=None):
    """
    Directly queries the MT5 terminal to find active trades.
    Bypassing local configurations ensures robust state logic.
    """
    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()
        
    if positions is None:
        logger.error(f"Failed to fetch positions. Error code: {mt5.last_error()}")
        return []
        
    # Return as list allowing immediate iterative access
    return list(positions)

def get_active_symbols():
    """
    Returns a list of all currently visible (Market Watch) symbols from the MT5 terminal
    that the engine is equipped to trade. Supports Forex, Metals, Indices, and Synthetics.
    """
    symbols = mt5.symbols_get()
    if symbols is None:
        logger.error(f"Failed to fetch symbols. Error code: {mt5.last_error()}")
        return []

    # Fix #24: Expand beyond FOREX-only. The engine is tick_size-aware (Flaws 11/12)
    # and can correctly handle Metals, Indices, and Synthetics.
    # Include all visible symbols whose calc mode is one of the well-supported types.
    SUPPORTED_CALC_MODES = {
        mt5.SYMBOL_CALC_MODE_FOREX,          # Standard Forex pairs
        mt5.SYMBOL_CALC_MODE_CFD,            # Indices, Metals (e.g., XAUUSD, US500)
        mt5.SYMBOL_CALC_MODE_CFDINDEX,       # Index CFDs (some brokers)
        mt5.SYMBOL_CALC_MODE_CFDLEVERAGE,    # Leveraged CFDs
    }

    active_symbols = [
        s.name for s in symbols
        if s.visible and s.trade_calc_mode in SUPPORTED_CALC_MODES
    ]

    logger.info(f"Dynamically discovered {len(active_symbols)} tradeable symbols (Forex + CFDs).")
    return active_symbols

def execute_order(symbol, order_type, volume, sl=None, tp=None):
    """
    Constructs the MT5 request dictionary and executes an order,
    incorporating a retry mechanism for requotes and price changes.
    """
    # 0 implies a market execution deal
    action = mt5.TRADE_ACTION_DEAL
    
    # Capture the latest live price context to format the order efficiently
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logger.error(f"execute_order: symbol_info_tick({symbol}) returned None — aborting.")
        return None

    if order_type == mt5.ORDER_TYPE_BUY:
        price = tick.ask
    elif order_type == mt5.ORDER_TYPE_SELL:
        price = tick.bid
    else:
        logger.error(f"Unsupported order_type natively passed: {order_type}")
        return None
        
    # Instantiate universally standardized MT5 routing dictionary
    request = {
        "action": action,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 100000,
        "comment": "algorithmic_trade",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    if sl is not None:
        request["sl"] = float(sl)
    if tp is not None:
        request["tp"] = float(tp)
        
    max_retries = 3
    for attempt in range(max_retries):
        logger.info(f"Attempting order execution ({attempt+1}/{max_retries}) - Symbol: {symbol}, Type: {order_type}, Vol: {volume}")
        
        # Fire signal payload over the terminal bridge
        result = mt5.order_send(request)
        
        if result is None:
            logger.error(f"order_send failed unconditionally. Error: {mt5.last_error()}")
            return None
            
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Order executed successfully! Deal ticket: {result.deal}, Retcode: {result.retcode}")
            return result
            
        # Inspect for requotes / volatile price failures and initialize retry
        if result.retcode in [mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED]:
            logger.warning(f"Order failure. Retcode: {result.retcode}. Price volatility/requote triggered. Retrying after 1s...")
            
            # Re-poll live ticker pricing since market momentum skewed the previous target
            if order_type == mt5.ORDER_TYPE_BUY:
                request["price"] = mt5.symbol_info_tick(symbol).ask
            elif order_type == mt5.ORDER_TYPE_SELL:
                request["price"] = mt5.symbol_info_tick(symbol).bid
                
            time.sleep(1)
            continue
            
        # Any disparate failure logs statically and fractures out
        logger.error(f"Permanent order failure. Retcode: {result.retcode}, Comment: {result.comment}")
        return result
        
    logger.error(f"Failed to execute order across {max_retries} loop spans due to rolling volatility constraint.")
    return None

async def get_order_book_micro_price(symbol: str) -> float:
    subscribed = mt5.market_book_add(symbol)
    if not subscribed:
        logger.warning(f"Failed to subscribe to market book for {symbol}")
        return 0.0
        
    try:
        # Yield event loop while MT5 populates the book (non-blocking)
        await asyncio.sleep(0.05)
        book = mt5.market_book_get(symbol)
        if not book:
            return 0.0
            
        best_bid = 0.0
        best_ask = float('inf')
        vol_bid = 0.0
        vol_ask = 0.0
        
        for item in book:
            if item.type == mt5.BOOK_TYPE_SELL: # Ask
                if item.price < best_ask:
                    best_ask = item.price
                vol_ask += item.volume
            elif item.type == mt5.BOOK_TYPE_BUY: # Bid
                if item.price > best_bid:
                    best_bid = item.price
                vol_bid += item.volume
                
        if vol_bid + vol_ask == 0.0 or best_bid == 0.0 or best_ask == float('inf'):
            return 0.0
            
        p_micro = ((best_ask * vol_bid) + (best_bid * vol_ask)) / (vol_bid + vol_ask)
        return p_micro
        
    finally:
        if subscribed:
            mt5.market_book_release(symbol)
