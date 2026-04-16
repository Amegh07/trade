"""
main.py  —  Omega Architecture Supervisor
══════════════════════════════════════════════════════════════════════
Orchestrates three independent OS processes, each with its own
MT5 connection (bypasses GIL entirely — true hardware parallelism):

  Process 1 (DataFeeder)  : tick fetching → SharedMemory @ 100 Hz
  Process 2 (AlphaEngine) : SharedMemory → vectorised math → signal_queue
  Process 3 (ExecRouter)  : signal_queue → Kelly sizing → MT5 orders → io_queue
  Daemon Thread(DBWriter)  : io_queue → SQLite (never blocks a trading thread)

Critical Windows note:
  multiprocessing requires `spawn` start method (default on Win).
  All process entry-points live in `core/` — fully importable.
"""

import os
import sys
import time
import signal
import atexit
import threading
import multiprocessing as mp
from multiprocessing.shared_memory import SharedMemory

from utils.config import (
    MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER, SYMBOLS,
)
from utils.logger import setup_logger, listener
from execution.mt5_client import initialize_mt5, get_active_symbols, shutdown_mt5
from core.data_feeder      import data_feeder_process,     FIELDS_PER_SYMBOL, BYTES_PER_SYMBOL
from core.alpha_engine     import alpha_engine_process
from core.execution_router import execution_router_process
from core.db_writer        import db_writer_daemon
from core.db_schema        import initialize_database
from core.shadow_optimizer import run_shadow_optimizer
import ctypes

logger = setup_logger("main")

# ── Supervisor tunables ───────────────────────────────────────────────────────
MAX_CONCURRENCY    = 20    # maximum simultaneous open trades
HEALTH_INTERVAL    = 30    # seconds between supervisor health logs
FEEDER_READY_TIMEOUT = 45  # seconds to wait for DataFeeder first snapshot


# ══════════════════════════════════════════════════════════════════════════════
# SUPERVISOR
# ══════════════════════════════════════════════════════════════════════════════

def supervisor_main() -> None:
    """
    Omega Supervisor:
      1. Connects to MT5 briefly (symbol discovery only).
      2. Creates the SharedMemory block.
      3. Spawns the three Omega processes.
      4. Starts the DB Writer daemon thread.
      5. Health-monitors everything in a blocking loop.
      6. Handles clean shutdown on KeyboardInterrupt or dead-process detection.
    """
    logger.info("═" * 66)
    logger.info("   Ω OMEGA ARCHITECTURE — Zero-Latency Execution Engine   ")
    logger.info("   Phase 1: Multiprocess  │ Phase 2: Vectorised NumPy      ")
    logger.info("   Phase 3: OFI Tick-Tape │ Phase 4: Half-Kelly Sizing     ")
    logger.info("   Phase 5: Zero-Blocking DB I/O                            ")
    logger.info("═" * 66)

    # ── Database Initialization ───────────────────────────────────────────────
    logger.info("Verifying database schema…")
    initialize_database()

    # ── Symbol discovery (main process only — then released immediately) ──────
    logger.info("Connecting to MT5 for symbol discovery…")
    if not initialize_mt5(login=MT5_ACCOUNT, password=MT5_PASSWORD, server=MT5_SERVER):
        logger.critical("MT5 connection failed — cannot discover symbols. Exiting.")
        listener.stop()
        return

    active_symbols = get_active_symbols()
    
    # ── StatArb Compliance ───────────────────────────────────────────────────
    # Ensure all symbols required for pairs trading are in the monitoring list
    from core.alpha_engine import TRADABLE_PAIRS
    pair_symbols = set()
    for a, b in TRADABLE_PAIRS:
        pair_symbols.add(a)
        pair_symbols.add(b)
    
    active_symbols = sorted(list(set(active_symbols) | pair_symbols))
    
    if not active_symbols:
        logger.warning(
            f"No dynamic symbols found. Falling back to config default: {SYMBOLS}"
        )
        active_symbols = sorted(list(set(SYMBOLS) | pair_symbols))

    # Release main-process MT5 — each subprocess owns its own connection
    shutdown_mt5()
    logger.info(f"Symbol discovery complete: {len(active_symbols)} symbols.")

    # ── SharedMemory block (created here, attached by name in subprocesses) ───
    n_syms   = len(active_symbols)
    shm_size = max(n_syms * BYTES_PER_SYMBOL, 64)   # minimum 64 bytes guard
    shm_name = f"omega_ticks_{os.getpid()}"

    # ── Unlink stale SharedMemory blocks on startup ────────────────────────────
    try:
        stale_shm = SharedMemory(name=shm_name)
        stale_shm.close()
        stale_shm.unlink()
        logger.info(f"Cleaned up stale SharedMemory: {shm_name}")
    except FileNotFoundError:
        pass  # No stale block, OK

    try:
        shm = SharedMemory(create=True, size=shm_size, name=shm_name)
    except Exception as exc:
        logger.critical(f"Failed to create SharedMemory: {exc}")
        listener.stop()
        return

    sym_index = {sym: i for i, sym in enumerate(active_symbols)}
    logger.info(
        f"[SHM] '{shm_name}' created — {shm_size:,} bytes "
        f"({n_syms} symbols × {BYTES_PER_SYMBOL} bytes each)"
    )

    # ── Inter-process primitives ───────────────────────────────────────────────
    stop_event   = mp.Event()    # broadcast stop: set() → all processes exit
    feeder_ready = mp.Event()    # set by DataFeeder after first tick snapshot
    signal_queue = mp.Queue(maxsize=1000)   # AlphaEngine → ExecRouter
    io_queue     = mp.Queue(maxsize=5000)   # ExecRouter  → DB Writer

    # ── Live Parameters (Shadow Optimizer → Alpha Engine) ──────────────────────
    # Array of 2 doubles: [0] = Z_ENTRY_THRESHOLD, [1] = Z_EXIT_THRESHOLD
    # Default seeds: Entry > 2.5, Exit < 0.2
    live_params = mp.Array(ctypes.c_double, [2.5, 0.2]) 

    # ── Process instantiation ─────────────────────────────────────────────────
    p_feeder = mp.Process(
        target = data_feeder_process,
        name   = "DataFeeder",
        daemon = False,
        args   = (
            active_symbols, shm_name, sym_index,
            stop_event,
            MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER,
            feeder_ready,
        ),
    )

    p_alpha = mp.Process(
        target = alpha_engine_process,
        name   = "AlphaEngine",
        daemon = False,
        args   = (
            active_symbols, shm_name, sym_index,
            signal_queue, stop_event,
            MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER,
            feeder_ready,
            live_params,
        ),
    )

    p_shadow = mp.Process(
        target = run_shadow_optimizer,
        name   = "ShadowOptimizer",
        daemon = False,
        args   = (live_params, stop_event),
    )

    p_router = mp.Process(
        target = execution_router_process,
        name   = "ExecRouter",
        daemon = False,
        args   = (
            signal_queue, io_queue, stop_event,
            MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER,
            MAX_CONCURRENCY,
        ),
    )

    # ── Phase 5: DB Writer daemon now lives natively inside ExecRouter ────────
    # Removing supervisor instantiation to prevent double-writes and SQLite locks.

    # ── Launch sequence (ordered: feeder → alpha → router → db writer) ────────
    logger.info("[PHASE 1] Starting DataFeeder process…")
    p_feeder.start()
    logger.info(f"[PHASE 1] DataFeeder PID={p_feeder.pid} ✓")

    logger.info(f"[PHASE 1] Waiting for DataFeeder first snapshot (timeout={FEEDER_READY_TIMEOUT}s)…")
    if not feeder_ready.wait(timeout=FEEDER_READY_TIMEOUT):
        logger.critical(
            f"DataFeeder did not become ready within {FEEDER_READY_TIMEOUT}s — "
            "possibly an MT5 connection failure inside the subprocess. Aborting."
        )
        stop_event.set()
        p_feeder.join(timeout=5)
        shm.close()
        shm.unlink()
        listener.stop()
        return

    logger.info("[PHASE 2/3] Starting AlphaEngine process…")
    p_alpha.start()
    logger.info(f"[PHASE 2/3] AlphaEngine PID={p_alpha.pid} ✓")

    logger.info("[PHASE 4/5] Starting ExecRouter process…")
    p_router.start()
    logger.info(f"[PHASE 4/5] ExecRouter PID={p_router.pid} ✓")

    logger.info("[SHADOW] Starting ShadowOptimizer process…")
    p_shadow.start()
    logger.info(f"[SHADOW] ShadowOptimizer PID={p_shadow.pid} ✓")

    logger.info("[PHASE 5] IO Queue initialized for ExecRouter DB writes.")

    logger.info("═" * 66)
    logger.info("Ω  ALL OMEGA PROCESSES LIVE. Supervisor entering health monitor.")
    logger.info("═" * 66)

    # ── Signal handlers for graceful shutdown ──────────────────────────────────
    def signal_handler(signum, frame):
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info(f"Signal {sig_name} ({signum}) received, initiating graceful shutdown.")
        stop_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # ── Supervisor health loop ─────────────────────────────────────────────────
    procs = [
        ("DataFeeder",      p_feeder),
        ("AlphaEngine",     p_alpha),
        ("ExecRouter",      p_router),
        ("ShadowOptimizer", p_shadow),
    ]

    try:
        while True:
            time.sleep(HEALTH_INTERVAL)

            dead = [name for name, p in procs if not p.is_alive()]
            if dead:
                logger.critical(
                    f"☠  Dead processes detected: {dead}. "
                    "Initiating emergency shutdown."
                )
                break

            logger.info(
                f"[HEALTH] DataFeeder={p_feeder.is_alive()} | "
                f"AlphaEngine={p_alpha.is_alive()} | "
                f"ExecRouter={p_router.is_alive()} | "
                f"SignalQ={signal_queue.qsize()} | "
                f"IOQ={io_queue.qsize()}"
            )

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — initiating graceful Omega shutdown.")

    # ── Graceful shutdown sequence ─────────────────────────────────────────────
    finally:
        logger.info("Broadcasting stop_event to all Omega processes…")
        stop_event.set()

        for name, proc in procs:
            proc.join(timeout=10)
            if proc.is_alive():
                logger.warning(f"{name} did not exit within 10 s — force-terminating.")
                proc.terminate()
                proc.join(timeout=3)
            logger.info(f"{name} shutdown complete.")

        logger.info("Omega processes stopped.")

        logger.info("Releasing SharedMemory…")
        try:
            shm.close()
            shm.unlink()
            logger.info(f"SharedMemory '{shm_name}' released.")
        except Exception as exc:
            logger.warning(f"SharedMemory cleanup warning: {exc}")

        logger.info("Ω  Omega Engine stopped cleanly. Goodbye.")
        listener.stop()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Windows requires `spawn` (the default) — make it explicit and idempotent
    mp.set_start_method("spawn", force=True)
    supervisor_main()
