"""Static analysis of a FastAPI backend and its TypeScript frontend, served to coding agents."""

import sys as _sys

from code_intel import loaded_code as _loaded_code

# Before any other submodule loads, so the server knows the bytes of every module it runs.
_loaded_code.install(__file__, _sys._getframe(0).f_code)
