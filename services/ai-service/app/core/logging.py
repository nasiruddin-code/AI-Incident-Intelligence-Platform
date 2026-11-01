"""
Logging setup.

Kept deliberately simple (stdlib `logging`) for Week 1. Week 4/5 will
replace or extend this with OpenTelemetry log export to the Collector —
because everything already logs through the standard `logging` module via
`get_logger()`, that swap happens in this one file and nowhere else.
"""
import logging
import sys



def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid duplicate handlers on reload (uvicorn --reload imports twice).
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
