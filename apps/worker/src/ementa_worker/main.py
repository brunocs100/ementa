"""Entry point: `uv run ementa-worker` runs the queued jobs until stopped (Ctrl+C)."""

import argparse
import asyncio
import logging.config

from arq.logs import default_log_config
from arq.worker import Worker

from ementa_kit.config import get_settings
from ementa_kit.telemetry import setup
from ementa_worker.worker import worker_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ementa-worker", description="Runs the jobs queued in Redis (arq)."
    )
    parser.add_argument("--burst", action="store_true", help="stop once the queue is empty")
    args = parser.parse_args()

    settings = get_settings()
    if "service_name" not in settings.model_fields_set:
        settings = settings.model_copy(update={"service_name": "ementa-worker"})
    provider = setup(settings)
    logging.config.dictConfig(default_log_config(verbose=False))  # arq: job start, end, retry
    asyncio.set_event_loop(asyncio.new_event_loop())  # arq's Worker runs on the current loop
    worker = Worker(**worker_settings(settings), burst=args.burst)
    try:
        worker.run()
    except KeyboardInterrupt:  # on Windows the event loop has no signal handlers
        pass
    finally:
        provider.shutdown()  # send the spans still in the batch


if __name__ == "__main__":
    main()
