"""Allow ``python -m playtest`` to run the driver's CLI."""

from playtest.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
