import logging
import logging.handlers
import queue
import os

# ── Global Logging Infrastructure ──────────────────────
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "trading.log")

# Force explicit thread-safe Queue and Listener instance for the application
log_queue = queue.Queue(-1)

_formatter = logging.Formatter('%(asctime)s - %(module)s - %(levelname)s - %(message)s')

# Rotating File Handler (Max 10MB -> 10 * 1024 * 1024, keeping 5 backups)
_file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024, 
    backupCount=5,
    encoding='utf-8'
)
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

# Single Listener pushing logs synchronously out of the queue
listener = logging.handlers.QueueListener(log_queue, _file_handler, _console_handler)
listener.start()

def setup_logger(name="trading"):
    """
    Sets up a thread-safe professional logger funneling into a central QueueListener.
    """
    logger = logging.getLogger(name)
    
    # Avoid attaching handlers repeatedly if initialized multiple times
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        # We only attach the QueueHandler so multiple threads/processes don't fight over file rotation
        queue_handler = logging.handlers.QueueHandler(log_queue)
        logger.addHandler(queue_handler)

    return logger

# Generate a default project-level logger for broad imports
logger = setup_logger("main")
