from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from docket.config import get_settings
from docket.database import configure_database, get_session_factory
from docket.services.soak import SoakService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docket-soak")
    parser.add_argument("command", choices=("start", "status", "complete"))
    arguments = parser.parse_args(argv)
    settings = get_settings()
    configure_database(settings.database_url)
    service = SoakService(get_session_factory(), settings)
    if arguments.command == "start":
        status = service.start()
    elif arguments.command == "complete":
        status = service.complete()
    else:
        status = service.status()
    print(json.dumps(status.as_dict(), indent=2, sort_keys=True))
    if arguments.command == "complete" and status.completed_at is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
