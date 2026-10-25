"""ASI orchestrator: Stage 1 gathers facts, Stage 2 proposes fixes for the owner to approve with CH."""
import os as _os
import sys as _sys

__version__ = "0.1.0"


def _demote_bare_base_dir():
    """On Windows the bare Python install directory (e.g. C:\\...\\miniconda3) is on sys.path ahead of
    site-packages. A stray module there, such as a loose typing_extensions.py, then shadows the installed
    package and breaks anyio/httpx at runtime. Move that directory to the end of the search path."""
    base = _os.path.normcase(_os.path.abspath(_sys.base_prefix))
    bare = [p for p in _sys.path if p and _os.path.normcase(_os.path.abspath(p)) == base]
    if bare:
        _sys.path[:] = [p for p in _sys.path if p not in bare] + bare
        mod = _sys.modules.get("typing_extensions")
        if mod is not None and _os.path.normcase(_os.path.dirname(_os.path.abspath(mod.__file__ or ""))) == base:
            del _sys.modules["typing_extensions"]          # loaded from the stray copy: reload from site-packages


_demote_bare_base_dir()
