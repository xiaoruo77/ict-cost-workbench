"""Compatibility alias for the default pharma industry plugin."""
import sys
from industry_plugins.pharma import grounding as implementation

sys.modules[__name__] = implementation
