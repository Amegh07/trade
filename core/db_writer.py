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
    id          INTEGER PK AUTOINCREMENT
    timestamp   REAL      (unix epoch)
    symbol      TEXT
    direction   REAL      (+1 / -1)
    lot         REAL
    price       REAL
    ticket      INTEGER
    tp          REAL
    sl          REAL
    kelly       REAL      (Kelly fraction used)
    hurst       REAL      (Hurst exponent at signal time)
    pnl         REAL      (filled in on close — initially NULL)
"""

import time
import logging
import sqlite3
import queue        # queue.Empty for timeout-based get
from multiprocessing import Queue as MpQueue


DB_PATH = "logs/trades.db"


def _make_logger() -> logging.Logger:
    log = logging.getLogger("db_writer")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [DBWriter] %(levelname)s — %(message)s"
        ))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


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
            hurst     REAL,
            pnl       REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_symbol
        ON trades (symbol, timestamp DESC)
    """)
    conn.commit()


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

    pending: list[dict] = []
    last_flush = time.monotonic()

    while not stop_event.is_set():
        try:
            item = io_queue.get(timeout=0.5)
            pending.append(item)
        except Exception:
            pass   # queue.Empty or mp.Queue timeout — normal

        now = time.monotonic()
        should_flush = (
            len(pending) >= BATCH_SIZE or
            (pending and (now - last_flush) >= FLUSH_TIMEOUT)
        )

        if should_flush and pending:
            try:
                conn.executemany("""
                    INSERT INTO trades
                        (timestamp, symbol, direction, lot, price, ticket,
                         tp, sl, kelly, hurst, pnl)
                    VALUES
                        (:timestamp, :symbol, :direction, :lot, :price, :ticket,
                         :tp, :sl, :kelly, :hurst, NULL)
                """, pending)
                conn.commit()
                logger.info(f"DB Writer: flushed {len(pending)} trade records.")
                pending.clear()
                last_flush = now
            except Exception as exc:
                logger.error(f"DB Writer: INSERT failed — {exc}", exc_info=True)

    # ── Drain remaining on shutdown ───────────────────────────────────────────
    while not io_queue.empty():
        try:
            pending.append(io_queue.get_nowait())
        except Exception:
            break

    if pending:
        try:
            conn.executemany("""
                INSERT INTO trades
                    (timestamp, symbol, direction, lot, price, ticket,
                     tp, sl, kelly, hurst, pnl)
                VALUES
                    (:timestamp, :symbol, :direction, :lot, :price, :ticket,
                     :tp, :sl, :kelly, :hurst, NULL)
            """, pending)
            conn.commit()
            logger.info(f"DB Writer: shutdown flush {len(pending)} records.")
        except Exception as exc:
            logger.error(f"DB Writer: shutdown flush failed — {exc}")

    conn.close()
    logger.info("DB Writer daemon exited.")
