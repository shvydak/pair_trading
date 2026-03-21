import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = logging.DEBUG if os.getenv("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO
    logger.setLevel(level)

    ch = logging.StreamHandler()
    ch.setFormatter(_FMT)
    logger.addHandler(ch)

    fh = RotatingFileHandler(
        os.path.join(_LOG_DIR, "pair_trading.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(_FMT)
    logger.addHandler(fh)

    return logger
