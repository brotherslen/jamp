"""Entry point for the standalone build: the same `jamp` command, frozen."""
import sys

from jamp.cli import main

if __name__ == "__main__":
    sys.exit(main())
