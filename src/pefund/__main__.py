"""Allow ``python -m pefund`` as an alternative to the ``pefund`` command."""

from .cli import main

raise SystemExit(main())
