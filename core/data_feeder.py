"""
core/data_feeder.py  —  Omega Architecture: Process 1 (Data Feeder)
═══════════════════════════════════════════════════════════════════════
Spawned as an independent OS process (bypasses GIL).

Responsibilities:
  - Calls mt5.initialize() independently (MT5 connections are NOT cross-process).
  - Fetches live symbol ticks via mt5.symbol_info_tick() at maximum speed.
  - Packs tick structs (bid, ask, last, volume_real, time, flags, spread) into a
    multiprocessing.shared_memory block shared with the Alpha Engine.
  - Uses a poison-pill sentinel (stop_event) for clean shutdown.

Shared Memory Layout per symbol (8 × float64 = 64 bytes):
  [0] bid
  [1] ask
  [2] last
  [3] volume_real
  [4] time (unix epoch float)
  [5] flags
  [6] spread (points)
  [7] RESERVED (future use)
"""

import time
import logging
import multiprocessing as mp
import numpy as np
from multiprocessing.shared_memory import SharedMemory

# ── Constants ─────────────────────────────────────────────────────────────────
FIELDS_PER_SYMBOL = 8          # float64 values stored per symbol
BYTES_PER_SYMBOL  = FIELDS_PER_SYMBOL * 8   # 64 bytes each
SHM_POLL_HZ       = 100        # tick polls per second (10 ms loop)
MT5_INIT_RETRIES  = 3          # connection attempts before giving up
MT5_INIT_BACKOFF  = 2.0        # seconds between retry attempts


# ── Logger (subprocess-safe: no QueueHandler dependency) ──────────────────────
def _make_logger() -> logging.Logger:
    log = logging.getLogger("data_feeder")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [DataFeeder] %(levelname)s — %(message)s"
        ))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


# ── MT5 connection helper with retry ──────────────────────────────────────────
def _connect_mt5(login: int, password: str, server: str, logger: logging.Logger) -> bool:
    """
    Attempts mt5.initialize() up to MT5_INIT_RETRIES times with exponential
    backoff. Returns True on success, False after all attempts exhausted.
    Critical: must be called INSIDE the spawned process — MT5 connections are
    not inheritable across process boundaries.
    """
    import MetaTrader5 as mt5
    for attempt in range(1, MT5_INIT_RETRIES + 1):
        logger.info(f"MT5 init attempt {attempt}/{MT5_INIT_RETRIES}…")
        if mt5.initialize(login=int(login), password=password, server=server):
            logger.info("MT5 connection established.")
            return True
        err = mt5.last_error()
        logger.warning(f"mt5.initialize() failed: {err}")
        if attempt < MT5_INIT_RETRIES:
            time.sleep(MT5_INIT_BACKOFF * attempt)   # back-off: 2s, 4s
    logger.critical(f"MT5 init FAILED after {MT5_INIT_RETRIES} attempts — process exiting.")
    return False


# ── Main process entry ────────────────────────────────────────────────────────
def data_feeder_process(
    symbols:      list,
    shm_name:     str,
    symbol_index: dict,
    stop_event:   mp.Event,
    mt5_login:    int,
    mt5_password: str,
    mt5_server:   str,
    ready_event:  mp.Event,
):
    """
    Entry point for the Data Feeder process.

    Parameters
    ----------
    symbols       : list of symbol names to track
    shm_name      : name of the already-created SharedMemory block
    symbol_index  : {symbol: row_index} mapping into shm
    stop_event    : mp.Event — set() to terminate the loop cleanly
    mt5_login     : MT5 account number
    mt5_password  : MT5 password string
    mt5_server    : MT5 broker server string
    ready_event   : mp.Event — set() once the first full snapshot is written
    """
    import MetaTrader5 as mt5

    logger = _make_logger()
    logger.info(f"DataFeeder PID={mp.current_process().pid} | tracking {len(symbols)} symbols")

    # ── Independent MT5 initialisation (CRITICAL — per-process) ──────────────
    if not _connect_mt5(mt5_login, mt5_password, mt5_server, logger):
        return   # nothing to clean up yet

    # ── Attach to the pre-allocated shared memory block ───────────────────────
    try:
        shm = SharedMemory(name=shm_name)
    except FileNotFoundError:
        logger.critical(f"SharedMemory '{shm_name}' not found — exiting.")
        mt5.shutdown()
        return

    buf = np.ndarray(
        shape=(len(symbols), FIELDS_PER_SYMBOL),
        dtype=np.float64,
        buffer=shm.buf,
    )

    logger.info("Shared memory attached. Entering tick loop.")
    _ready_signalled = False
    interval = 1.0 / SHM_POLL_HZ   # 10 ms target loop

    while not stop_event.is_set():
        t_start = time.perf_counter()

        for sym in symbols:
            idx = symbol_index[sym]
            try:
                tick = mt5.symbol_info_tick(sym)
                if tick:
                    buf[idx, 0] = tick.bid
                    buf[idx, 1] = tick.ask
                    buf[idx, 2] = tick.last
                    buf[idx, 3] = tick.volume_real
                    buf[idx, 4] = float(tick.time)
                    buf[idx, 5] = float(tick.flags)
                    info = mt5.symbol_info(sym)
                    buf[idx, 6] = float(info.spread) if info else 0.0
                    buf[idx, 7] = 0.0   # reserved
            except Exception as exc:
                logger.warning(f"Tick error [{sym}]: {exc}")

        # Signal the AlphaEngine only after the first complete snapshot
        if not _ready_signalled:
            ready_event.set()
            _ready_signalled = True
            logger.info("First snapshot written — signalling AlphaEngine.")

        elapsed   = time.perf_counter() - t_start
        sleep_for = max(0.0, interval - elapsed)
        time.sleep(sleep_for)

    logger.info("Stop event received — releasing resources.")
    shm.close()
    mt5.shutdown()
    logger.info("DataFeeder exited cleanly.")
