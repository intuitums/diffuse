"""Explicit schema initialization command for the Diffuse Relay database."""

from .store import initialize_schema


def main() -> None:
    initialize_schema()


if __name__ == "__main__":
    main()
