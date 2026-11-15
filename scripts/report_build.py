"""Compatibility alias for the default pharma industry plugin."""
import sys
from industry_plugins.pharma import reporting as implementation

if __name__ == "__main__":
    raise SystemExit(implementation.main())

sys.modules[__name__] = implementation
