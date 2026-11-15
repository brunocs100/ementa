"""Entry point: `uv run ementa-api` serves the API; `--openapi` prints the contract."""

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="ementa-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--openapi", action="store_true", help="print the OpenAPI document and exit"
    )
    args = parser.parse_args()

    from ementa_api.app import create_app

    app = create_app()
    if args.openapi:
        document = json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
        sys.stdout.buffer.write(document.encode("utf-8"))  # UTF-8 and LF on any OS
        return

    import uvicorn
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    from ementa_kit.config import get_settings
    from ementa_kit.telemetry import setup

    setup(get_settings())
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
