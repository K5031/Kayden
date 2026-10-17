import logging
import sys


_HANDLER = None


class _KaydenFormatter(logging.Formatter):
    def format(self, record):
        record.component = record.name.removeprefix("kayden.")
        return super().format(record)


def _handler():
    global _HANDLER
    if _HANDLER is None:
        _HANDLER = logging.StreamHandler(sys.stdout)
        _HANDLER.setFormatter(_KaydenFormatter("[%(component)s] %(message)s"))
    return _HANDLER


def get_logger(name: str) -> logging.Logger:
    """Return an app logger without changing third-party logging levels."""
    logger = logging.getLogger(f"kayden.{name}")
    handler = _handler()
    if handler not in logger.handlers:
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
