"""
core/db_writer.py  —  Omega Architecture: Phase 5 — Zero-Blocking Post-Trade I/O
══════════════════════════════════════════════════════════════════════════════════
An isolated background daemon (daemon=True thread inside the Execution Router
process, or optionally a separate process) that:

  1. Reads trade result dicts from io_queue (multiprocessing.Queue).
  2. Executes INSERT INTO trades SQL — the ONLY thread that ever touches the DB.
  3. The main trading loop NEVER waits for a hard-drive write.

This eliminates the "blocking I/O on execution thread" bottleneck described in
Phase 5 of the Omega Architecture specification.

Table schema (trades):
    kelly       REAL      (Kelly fraction used)
    z_score     REAL      (Spread Z-Score at signal time)
    pnl         REAL      (filled in on close — initially NULL)
"""

import time
import logging
import sqlite3
import queue        # queue.Empty for timeout-based get
from datetime import datetime, timedelta
from multiprocessing import Queue as MpQueue
from utils.logger import setup_logger


DB_PATH = "logs/trades.db"


def _make_logger() -> logging.Logger:
    """Uses the global setup_logger to ensure DB logs are captured in rotating files."""
    return setup_logger("db_writer")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            symbol    TEXT,
            direction REAL,
            lot       REAL,
            price     REAL,
            ticket    INTEGER,
            tp        REAL,
            sl        REAL,
            kelly     REAL,
            z_score   REAL,
            pnl       REAL,
            basket_id TEXT,
            leg_role  TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_symbol
        ON trades (symbol, timestamp DESC)
    """)
    conn.commit()


def _defragment_db(conn, logger) -> None:
    """
    Run SQLite maintenance: defragmentation + statistics regeneration.
    
    VACUUM reclaims unused pages (from deletes/overwrites).
    PRAGMA optimize regenerates query statistics for the query planner.
    
    Cost: ~50ms on 1M-row DB (runs once per 50K trades or 30 days).
    """
    try:
        logger.info("DB maintenance: running VACUUM...")
        conn.execute("VACUUM")
        
        logger.info("DB maintenance: running PRAGMA optimize...")
        conn.execute("PRAGMA optimize")
        
        logger.info("DB maintenance: running PRAGMA analyze...")
        conn.execute("PRAGMA analyze")
        
        # Persist maintenance timestamp in control_state
        now_str = datetime.now().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO control_state (key, value) VALUES ('LAST_MAINTENANCE', ?)",
            (now_str,)
        )
        
        conn.commit()
        logger.info(f"DB maintenance: VACUUM + PRAGMA optimize + analyze complete (Timestamp: {now_str})")
    except Exception as exc:
        logger.error(f"DB maintenance failed: {exc}", exc_info=True)


def db_writer_daemon(io_queue: MpQueue, stop_event) -> None:
    """
    Main function for the DB Writer daemon.

    Designed to run as a daemon thread inside the Execution Router process:

        import threading
        t = threading.Thread(target=db_writer_daemon, args=(io_queue, stop_event), daemon=True)
        t.start()

    Or as a separate multiprocessing.Process if desired.
    """
    logger = _make_logger()
    logger.info("DB Writer daemon started.")

    conn = sqlite3.connect(DB_PATH, check_same_thread=True)
    conn.execute("PRAGMA journal_mode=WAL")     # Write-Ahead Log: concurrent reads + no blocking
    conn.execute("PRAGMA synchronous=NORMAL")   # Faster than FULL; still crash-safe on most OSes
    _ensure_schema(conn)

    BATCH_SIZE    = 10      # flush in micro-batches
    FLUSH_TIMEOUT = 2.0     # seconds — flush even if batch not full
    MAINTENANCE_THRESHOLD_TRADES = 50000 
    MAINTENANCE_INTERVAL_DAYS    = 30

    pending_inserts: list[dict] = []
    pending_updates: list[dict] = []
    last_flush = time.monotonic()
    trades_since_maintenance = 0
    
    # ── Initial Maintenance Check (persistent) ───────────────────────────────
    last_m_str = None
    try:
        cur = conn.execute("SELECT value FROM control_state WHERE key = 'LAST_MAINTENANCE'")
        row = cur.fetchone()
        if row:
            last_m_str = row[0]
    except Exception:
        pass

    if last_m_str:
        last_m_dt = datetime.fromisoformat(last_m_str)
    else:
        last_m_dt = datetime.now() - timedelta(days=MAINTENANCE_INTERVAL_DAYS + 1) # Force first run

    logger.info(f"Last DB maintenance was: {last_m_dt.isoformat()}")

    while not stop_event.is_set():
        try:
            item = io_queue.get(timeout=0.5)
            if item.get("action") == "update_pnl":
                pending_updates.append(item)
            else:
                pending_inserts.append(item)
        except Exception:
            pass   # queue.Empty or mp.Queue timeout — normal

        now = time.monotonic()
        should_flush = (
            (len(pending_inserts) + len(pending_updates)) >= BATCH_SIZE or
            ((pending_inserts or pending_updates) and (now - last_flush) >= FLUSH_TIMEOUT)
        )

        if should_flush:
            # 1. Flush Inserts
            if pending_inserts:
                try:
                    conn.executemany("""
                        INSERT INTO trades
                            (timestamp, symbol, direction, lot, price, ticket,
                             tp, sl, kelly, z_score, pnl, basket_id, leg_role)
                        VALUES
                            (:timestamp, :symbol, :direction, :lot, :price, :ticket,
                             :tp, :sl, :kelly, :z_score, NULL, :basket_id, :leg_role)
                    """, pending_inserts)
                    conn.commit()
                    trades_inserted = len(pending_inserts)
                    trades_since_maintenance += trades_inserted
                    logger.info(f"DB Writer: flushed {trades_inserted} trade records (total since maintenance: {trades_since_maintenance}).")
                    pending_inserts.clear()
                except Exception as exc:
                    logger.error(f"DB Writer: INSERT failed — {exc}", exc_info=True)
                
                # Trigger maintenance window if threshold exceeded
                now_dt = datetime.now()
                time_since_m = now_dt - last_m_dt
                
                if (trades_since_maintenance >= MAINTENANCE_THRESHOLD_TRADES or 
                    time_since_m.days >= MAINTENANCE_INTERVAL_DAYS):
                    
                    reason = "volume" if trades_since_maintenance >= MAINTENANCE_THRESHOLD_TRADES else "time (30 days)"
                    logger.info(f"Triggering scheduled DB maintenance (Reason: {reason}).")
                    
                    def _async_maintenance():
                        import threading
                        def task():
                            try:
                                temp_conn = sqlite3.connect(DB_PATH)
                                _defragment_db(temp_conn, logger)
                                temp_conn.close()
                            except Exception as e:
                                logger.error(f"Async maintenance failed: {e}")
                        threading.Thread(target=task, daemon=True).start()
                        
                    _async_maintenance()
                    trades_since_maintenance = 0
                    last_m_dt = now_dt

            # 2. Flush Updates
            if pending_updates:
                try:
                    conn.executemany("""
                        UPDATE trades 
                        SET pnl = :pnl 
                        WHERE ticket = :ticket
                    """, pending_updates)
                    conn.commit()
                    logger.info(f"DB Writer: updated PNL for {len(pending_updates)} records.")
                    pending_updates.clear()
                except Exception as exc:
                    logger.error(f"DB Writer: UPDATE failed — {exc}", exc_info=True)
                    
            last_flush = now

    # ── Drain remaining on shutdown ───────────────────────────────────────────
    while not io_queue.empty():
        try:
            item = io_queue.get_nowait()
            if item.get("action") == "update_pnl":
                pending_updates.append(item)
            else:
                pending_inserts.append(item)
        except Exception:
            break

    if pending_inserts:
        try:
            conn.executemany("""
                INSERT INTO trades
                    (timestamp, symbol, direction, lot, price, ticket,
                     tp, sl, kelly, z_score, pnl, basket_id, leg_role)
                VALUES
                    (:timestamp, :symbol, :direction, :lot, :price, :ticket,
                     :tp, :sl, :kelly, :z_score, NULL, :basket_id, :leg_role)
            """, pending_inserts)
            conn.commit()
            logger.info(f"DB Writer: shutdown flush {len(pending_inserts)} records.")
        except Exception as exc:
            logger.error(f"DB Writer: shutdown flush failed — {exc}")

    if pending_updates:
        try:
            conn.executemany("""
                UPDATE trades 
                SET pnl = :pnl 
                WHERE ticket = :ticket
            """, pending_updates)
            conn.commit()
            logger.info(f"DB Writer: shutdown update {len(pending_updates)} records.")
        except Exception as exc:
            logger.error(f"DB Writer: shutdown update failed — {exc}")
    
    # ── Final maintenance before shutdown ─────────────────────────────────────
    logger.info("DB Writer: running final maintenance before shutdown...")
    _defragment_db(conn, logger)

    conn.close()
    logger.info("DB Writer daemon exited.")
