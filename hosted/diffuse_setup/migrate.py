"""Explicit schema initialization command for the hosted integration database."""

from .store import initialize_schema


def main() -> None:
    initialize_schema()


if __name__ == "__main__":
    main()
