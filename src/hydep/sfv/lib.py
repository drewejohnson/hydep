from pkg_resources import parse_version

minVersion = parse_version("0.3.1")
upperNotEqual = parse_version("0.3.2")

try:
    import sfv
except ImportError:
    raise ImportError("Reach out to developers for the sfv package")


__version__ = sfv.__version__

if not (minVersion <= parse_version(__version__) < upperNotEqual):
    raise ImportError(
        f"Expected sfv version >={minVersion!s},<{upperNotEqual!s}, "
        f"found {__version__}"
    )
del minVersion, upperNotEqual, parse_version

from sfv import applySFV, getAdjFwdEig  # noqa: F401 E402
