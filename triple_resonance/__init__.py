"""Manual-only day-T assistant. Strategy profitability is not certified."""
from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version('triple-resonance')
except PackageNotFoundError:
    __version__ = '0.3.1'
