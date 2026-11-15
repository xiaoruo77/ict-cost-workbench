"""Compatibility alias for pharma period aggregation."""
import sys
from industry_plugins.pharma import periods as implementation

if __name__ == "__main__":
    raise SystemExit(implementation._main())

sys.modules[__name__] = implementation
