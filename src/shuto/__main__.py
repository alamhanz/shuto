"""Allow `python -m shuto`."""

import sys

from .cli import main

sys.exit(main())
