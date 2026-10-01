"""InFlow Python SDK."""

from importlib.metadata import version

from .errors import InflowApiError
from .options import ClientOptions

__version__ = version("inflowpay")

__all__ = ["ClientOptions", "InflowApiError", "__version__"]
