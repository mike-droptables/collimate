"""Allow ``python -m collimate.lint`` as an alias for ``collimate-lint``."""

from .cli import main

raise SystemExit(main())
