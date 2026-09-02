"""Consumes telemetry events off SQS and writes them to Postgres. Lands in M7.

Runs as a separate process (see docker-compose) so a slow database never touches
the audio path.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("Telemetry pipeline lands in M7")


if __name__ == "__main__":
    main()
