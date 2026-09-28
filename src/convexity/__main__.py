"""Allow `python -m convexity` (and `python -m convexity build-symbols`)."""

import sys

from convexity.cli import main

if __name__ == "__main__":
    sys.exit(main())
