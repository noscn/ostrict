"""Deprecated compatibility alias: this package was renamed to ``ostrict`` in v0.3.0.

``import strict_semantic`` and every ``strict_semantic.<submodule>`` import keep
working and resolve to the *same* module objects as their ``ostrict``
counterparts (one copy of every class, so isinstance and equality behave).
Update imports when convenient.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings

from ostrict import *  # noqa: F403 - re-export the public API
from ostrict import __all__ as __all__
from ostrict import __version__ as __version__


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real: str):
        self._real = real

    def create_module(self, spec):
        return importlib.import_module(self._real)  # the ostrict module object itself

    def exec_module(self, module):
        pass  # already executed under its real name


class _AliasFinder(importlib.abc.MetaPathFinder):
    _ostrict_alias = True  # idempotence marker, survives re-execution of this module

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("strict_semantic."):
            return None
        real = "ostrict" + fullname.removeprefix("strict_semantic")
        if importlib.util.find_spec(real) is None:
            return None  # let the normal ImportError surface
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(real))


if not any(getattr(f, "_ostrict_alias", False) for f in sys.meta_path):
    sys.meta_path.append(_AliasFinder())

warnings.warn(
    "'strict_semantic' was renamed to 'ostrict' in v0.3.0; this alias keeps working, "
    "but update imports when convenient",
    DeprecationWarning,
    stacklevel=2,
)
