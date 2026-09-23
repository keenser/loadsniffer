version = '2.0'

# Apply the SSDP header-parsing compatibility shim as early as possible so it
# is in effect before any SsdpListener is constructed (mrc.py imports aioupnp
# before creating RendererRegistry).
from . import _ssdp_compat  # noqa: E402,F401
from . import _gena_compat  # noqa: E402,F401

_ssdp_compat.apply()
_gena_compat.apply()

from .renderer import RendererRegistry  # noqa: E402
from .mediaserver import MediaServer  # noqa: E402