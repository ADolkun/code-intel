"""Which bytes each module of this package was compiled from.

A running server keeps the code it imported, so after an edit (an editable
install, a ``git pull``) it serves the old code. ``analysis_runtime.CodeWatch``
compares the files on disk with ``IMPORTED``: for every package module the
SHA-256 of the exact bytes it was compiled from, by resolved path.

``install`` runs from the package's ``__init__``, before any other submodule
is imported. From then on every ``code_intel.*`` module is compiled by
``_RecordingLoader`` from bytes it hashes itself. The default loader may run a
``.pyc`` validated only by the source's mtime and size, and reads the source
again to compile it, so neither says which bytes run. The two modules that run
before the recorder exists (``__init__`` and this one) are recorded by
comparing their running code with the code compiled from the file on disk.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import sys
import time
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import CodeType, ModuleType

PACKAGE = __name__.rpartition(".")[0]
PACKAGE_DIR = Path(__file__).resolve().parent

# Taken before any module this package watches is compiled through the
# recorder, so the code watch can tell which files may have changed between
# their load and its digest.
LOADED_AT_NS = time.time_ns()
_OWN_CODE = sys._getframe(0).f_code

IMPORTED: dict[Path, str] = {}
# Recorded for a module whose running code differs from its file on disk.
CHANGED_BEFORE_THE_WATCH = "changed between its load and the code watch"
# Recorded for a module whose reload raised while running.
PARTIALLY_RELOADED = "partially reloaded"
# Reading a file again (OSError) or compiling it (SyntaxError, or ValueError
# for a NUL byte).
_OWN_SOURCE_ERRORS: tuple[type[Exception], ...] = (OSError, SyntaxError, ValueError)


def digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=False).hexdigest()


class _RecordingLoader(importlib.machinery.SourceFileLoader):
    """Compiles a package module from the bytes whose digest it records."""

    _compiled_digest: str | None = None

    def get_code(self, fullname: str) -> CodeType:
        path = self.get_filename(fullname)
        data = self.get_data(path)
        code = self.source_to_code(data, path)
        self._compiled_digest = digest(data)
        return code

    def exec_module(self, module: ModuleType) -> None:
        """Records the digest only once the module ran: a failed first import
        leaves nothing loaded (the import system drops the module), so it
        leaves no record. A reload that fails while running leaves the module
        half old, half new, which no digest describes, so it is recorded as
        changed; one that fails to compile leaves the old code and its record."""
        path = Path(self.get_filename(module.__name__)).resolve()
        reloading = path in IMPORTED
        self._compiled_digest = None
        try:
            super().exec_module(module)
        except BaseException:
            if not reloading:
                IMPORTED.pop(path, None)
            elif self._compiled_digest is not None:
                IMPORTED[path] = PARTIALLY_RELOADED
            raise
        if self._compiled_digest is not None:
            IMPORTED[path] = self._compiled_digest


class _RecordingFinder:
    """Gives this package's submodules the recording loader; every other
    import is left to the finders after it."""

    @classmethod
    def find_spec(
        cls, name: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if path is None or not name.startswith(PACKAGE + "."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        if (
            spec is None
            or spec.origin is None
            or type(spec.loader) is not importlib.machinery.SourceFileLoader
            or not Path(spec.origin).resolve().is_relative_to(PACKAGE_DIR)
        ):
            return None
        spec.loader = _RecordingLoader(name, spec.origin)
        return spec


def record_running_source(file: str, code: CodeType) -> None:
    """Record ``file`` (a module that ran before the recorder existed) by its
    digest when ``code``, its running module code, equals the code compiled
    from the file on disk now: equal code objects run the same, so the disk
    bytes stand for the ones loaded. Unequal code, or a file that no longer
    reads or compiles, means it changed after it was read."""
    path = Path(file).resolve()
    try:
        data = path.read_bytes()
        same = compile(data, str(path), "exec", dont_inherit=True) == code
    except _OWN_SOURCE_ERRORS:
        same, data = False, b""
    IMPORTED[path] = digest(data) if same else CHANGED_BEFORE_THE_WATCH


def install(init_file: str, init_code: CodeType) -> None:
    """Placed just before the path finder, so earlier hooks (a test runner's
    import rewriting) keep their modules, and only once per process."""
    record_running_source(__file__, _OWN_CODE)
    record_running_source(init_file, init_code)
    if any(finder is _RecordingFinder for finder in sys.meta_path):
        return
    position = next(
        (i for i, finder in enumerate(sys.meta_path) if finder is importlib.machinery.PathFinder), len(sys.meta_path)
    )
    sys.meta_path.insert(position, _RecordingFinder)
