"""Start the app:  python -m energy_tracker"""

from __future__ import annotations

import logging

import uvicorn

from .config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request at INFO, which would flood the log at every poll.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    uvicorn.run(
        "energy_tracker.api:app",
        host="0.0.0.0",  # noqa: S104 - inside a container; access is limited in api.py
        port=settings.port,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
