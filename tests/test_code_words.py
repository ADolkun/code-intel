"""Code identifier words (``code_words``): what counts as a real use of a name."""

from __future__ import annotations

import textwrap
import time

import pytest

from stack_intel.code_words import (
    BUILTIN_WORDS,
    IMPORT_ROOTS,
    BoundModule,
    TopLevel,
    bound_by_module,
    code_words,
    code_words_from_bytes,
    is_code_file,
    module_bindings,
    open_module,
    package_bindings_from_bytes,
    plain_words,
    python_words,
)


def py(source: str):
    words, problems = python_words("backend/m.py", textwrap.dedent(source).lstrip("\n"))
    assert problems == []
    return words


def test_python_identifiers_of_every_kind_count() -> None:
    words = py(
        """
        import pkg.sub as alias_name
        from other.mod import thing as renamed

        class Config(Base):
            def method(self, arg_one, *rest, key_only=1, **extra):
                return call(keyword_arg=arg_one).attr_name

        try:
            pass
        except ValueError as caught:
            pass
        """
    )
    expected = {
        "pkg",
        "sub",
        "alias_name",
        "other",
        "mod",
        "thing",
        "renamed",
        "Config",
        "Base",
        "method",
        "self",
        "arg_one",
        "rest",
        "key_only",
        "extra",
        "call",
        "keyword_arg",
        "attr_name",
        "ValueError",
        "caught",
    }
    assert expected <= words.words


def test_comments_and_docstrings_do_not_count() -> None:
    words = py(
        '''
        """Module docstring names _stub_headers()."""

        def handler():
            """Calls removed_helper() first."""
            # legacy_name is gone
            return 1

        class Owner:
            """Has old_attribute."""
        '''
    )
    assert not {"_stub_headers", "removed_helper", "legacy_name", "old_attribute", "Module"} & words.words
    assert {"handler", "Owner"} <= words.words


def test_string_constants_count_word_by_word() -> None:
    words = py(
        """
        APP = "service_backend.main:app"
        settings.get("pg_max_retry_seconds")
        message = f"value {x} for some_setting"
        """
    )
    assert {"service_backend", "main", "app", "pg_max_retry_seconds", "some_setting", "for"} <= words.words
    assert "service_backend.main" in words.dotted


def test_attribute_chains_keep_every_dotted_window() -> None:
    words = py(
        """
        value = self.config.HeartbeatConfig.pg_max_retry_seconds
        result = make().first.second
        """
    )
    assert {
        "HeartbeatConfig.pg_max_retry_seconds",
        "self.config.HeartbeatConfig.pg_max_retry_seconds",
        "config.HeartbeatConfig",
        "first.second",
    } <= words.dotted
    # A chain on a call result has no name for its head.
    assert "make.first" not in words.dotted


def test_imports_give_dotted_texts() -> None:
    words = py(
        """
        import service_backend.core.db
        from service_backend.models import Appointment
        """
    )
    assert {"service_backend.core.db", "core.db", "service_backend.models.Appointment", "models.Appointment"} <= (
        words.dotted
    )


def test_python_syntax_error_falls_back_to_plain_words_with_a_diagnostic() -> None:
    words, problems = python_words("backend/broken.py", "def ok(:\n    # mentioned_in_comment\n")
    assert "mentioned_in_comment" in words.words
    (problem,) = problems
    assert (problem.kind, problem.step, problem.file, problem.line) == (
        "parse_failed",
        "parse file",
        "backend/broken.py",
        1,
    )


def test_python_bytes_decode_failure_is_a_diagnostic() -> None:
    words, problems = code_words_from_bytes("backend/bad.py", b"x = '\xff'\n")
    assert words.words == frozenset()
    assert [(p.kind, p.step) for p in problems] == [("decode_failed", "decode file")]


def test_python_bytes_follow_the_coding_cookie() -> None:
    words, problems = code_words_from_bytes("backend/l1.py", b"# -*- coding: latin-1 -*-\nname_\xe9 = 1\n")
    assert problems == []
    assert "name_é" in words.words


@pytest.mark.parametrize(
    ("rel", "text", "word"),
    [
        ("frontend/a.tsx", "// commentWord\nexport const fetchArticles = () => api.get('/x')", "commentWord"),
        ("backend/schema.sql", "CREATE TABLE article_appointments (id int);", "article_appointments"),
        ("deploy/app.yaml", "env:\n  FEATURE_FLAG_KEY: x\n", "FEATURE_FLAG_KEY"),
    ],
)
def test_non_python_files_give_plain_words(rel: str, text: str, word: str) -> None:
    words, problems = code_words(rel, text)
    assert problems == []
    assert word in words.words


def test_plain_words_keep_dotted_texts() -> None:
    words = plain_words("const x = api.client.get(); // HeartbeatConfig.value")
    assert {"api.client", "api.client.get", "client.get", "HeartbeatConfig.value"} <= words.dotted


def test_non_python_bytes_tolerate_undecodable_bytes() -> None:
    words, problems = code_words_from_bytes("frontend/a.ts", b"const good_name = '\xff';\n")
    assert problems == []
    assert "good_name" in words.words


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("backend/a.py", True),
        ("frontend/b.tsx", True),
        ("frontend/c.mjs", True),
        ("db/schema.sql", True),
        (".github/workflows/ci.yml", True),
        ("protos/x.proto", True),
        ("backend/Dockerfile", True),
        ("frontend/Dockerfile.dev", True),
        (".env.example", True),
        ("frontend/package-lock.json", False),
        ("backend/uv.lock", False),
        ("pnpm-lock.yaml", False),
        ("docs/guide.md", False),
        ("assets/logo.png", False),
        (".env", False),
    ],
)
def test_code_file_selection(rel: str, expected: bool) -> None:
    assert is_code_file(rel) is expected


def test_builtins_are_known_words() -> None:
    assert {"len", "isinstance", "ValueError", "print"} <= BUILTIN_WORDS


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", set()),
        ("__all__: list[str] = []\n", {"__all__"}),
        # `__all__` declares names a module `__getattr__` may serve.
        ('__all__ = ["Lazy", 1]\n__all__ += ("More",)\n', {"__all__", "Lazy", "More"}),
        ("from .mod import Thing\n", {"Thing"}),
        ("from .mod import Thing as Other\n", {"Other"}),
        ("import a.b\nimport c as d\n", {"a", "d"}),
        ("from .mod import *\n", set()),
        ("def f(): pass\nasync def g(): pass\nclass C: pass\n", {"f", "g", "C"}),
        ("x = 1\ny: int = 2\nz: int\na, (b, *c) = 1, (2, 3)\n", {"x", "y", "a", "b", "c"}),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .m import T\nelse:\n    U = 1\n",
            {"TYPE_CHECKING", "T", "U"},
        ),
        (
            "try:\n    from .m import A\nexcept ImportError:\n    B = None\nelse:\n    C = 1\nfinally:\n    D = 2\n",
            {"A", "B", "C", "D"},
        ),
        # A function body binds nothing at package level.
        ("def f():\n    from .m import Hidden\n", {"f"}),
    ],
)
def test_package_bindings(source: str, expected: set[str]) -> None:
    names, problems = package_bindings_from_bytes("backend/pkg/__init__.py", source.encode())
    assert (names, problems) == (frozenset(expected), [])


def test_package_bindings_of_a_file_that_does_not_parse_are_empty_with_a_diagnostic() -> None:
    names, problems = package_bindings_from_bytes("backend/pkg/__init__.py", b"def (:\n")
    assert names == frozenset()
    assert [p.kind for p in problems] == ["parse_failed"]


def test_a_python_file_carries_what_it_binds_at_top_level_and_other_files_nothing() -> None:
    source = b"from .state import AuthState\nfrom .models import *\n\n\ndef build():\n    inner = 1\n"
    words, _ = code_words_from_bytes("pkg/errors.py", source)
    assert words.top == TopLevel(names=frozenset({"AuthState", "build"}), stars=((1, "models"),))
    words, _ = code_words_from_bytes("web/a.ts", b"export const AuthState = 1;\n")
    assert words.top is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("x = 1\n", TopLevel(names=frozenset({"x"}))),
        ('__all__ = ["A"]\n', TopLevel(names=frozenset({"__all__", "A"}), all=frozenset({"A"}))),
        (
            "__all__ = sorted(_LAZY)\n",
            TopLevel(names=frozenset({"__all__"}), all=frozenset(), all_dynamic=True),
        ),
        (
            '__all__ = ["A"]\n__all__.extend(B)\n',
            TopLevel(names=frozenset({"__all__", "A"}), all=frozenset({"A"}), all_dynamic=True),
        ),
        ("def __getattr__(name):\n    pass\n", TopLevel(names=frozenset({"__getattr__"}), getattr=True)),
        ("from pkg.models import *\n", TopLevel(stars=((0, "pkg.models"),))),
        ("from .. import *\n", TopLevel(stars=((2, ""),))),
    ],
)
def test_top_level_records_all_star_imports_and_module_getattr(source: str, expected: TopLevel) -> None:
    words, problems = python_words("pkg/m.py", source)
    assert (words.top, problems) == (expected, [])


def _modules(
    files: dict[str, str], roots: tuple[str, ...] = IMPORT_ROOTS
) -> dict[tuple[str, ...], tuple[BoundModule, ...]]:
    tops = {}
    for rel, source in files.items():
        words, _ = python_words(rel, textwrap.dedent(source))
        assert words.top is not None
        tops[rel] = words.top
    return dict(module_bindings(tops, roots))


def test_star_imports_are_followed_to_the_tracked_module() -> None:
    modules = _modules(
        {
            "pkg/__init__.py": "from .models import *\nfrom pkg.events import *\n",
            "pkg/models.py": "class UserModel: pass\nclass _Hidden: pass\n",
            "pkg/events.py": '__all__ = ["Event"]\nclass Event: pass\nclass Other: pass\n',
            "pkg/cycle_a.py": "from .cycle_b import *\nA = 1\n",
            "pkg/cycle_b.py": "from .cycle_a import *\nB = 1\n",
        }
    )
    assert bound_by_module(["pkg", "UserModel"], modules) == "pkg/__init__.py"
    assert bound_by_module(["pkg", "Event"], modules) == "pkg/__init__.py"
    # A star import takes `__all__` when there is one, else the public names.
    assert bound_by_module(["pkg", "Other"], modules) is None
    assert bound_by_module(["pkg", "_Hidden"], modules) is None
    assert bound_by_module(["cycle_a", "B"], modules) == "pkg/cycle_a.py"
    assert open_module(["pkg", "Anything"], modules) is None


@pytest.mark.parametrize(
    "files",
    [
        {"lazy.py": "_LAZY = {}\n__all__ = sorted(_LAZY)\n\ndef __getattr__(name):\n    return _LAZY[name]\n"},
        {"lazy.py": "from os.path import *\n"},
        {"lazy.py": "from .dyn import *\n", "dyn.py": "__all__ = sorted(NAMES)\n"},
        {"lazy.py": "from models import *\n", "a/models.py": "X = 1\n", "b/models.py": "Y = 1\n"},
    ],
    ids=["getattr", "untracked-star", "dynamic-all-star", "ambiguous-star"],
)
def test_a_module_that_may_bind_names_at_run_time_is_open(files: dict[str, str]) -> None:
    modules = _modules(files)
    assert bound_by_module(["lazy", "HeavyThing"], modules) is None
    assert open_module(["lazy", "HeavyThing"], modules) == "lazy.py"


def test_an_absolute_star_import_resolves_only_from_an_import_root() -> None:
    # A tracked `lib/enum.py` is not the standard library's `enum`.
    modules = _modules({"std/__init__.py": "from enum import *\n", "lib/enum.py": "class Mine: pass\n"})
    assert bound_by_module(["std", "Mine"], modules) is None
    assert open_module(["std", "IntEnum"], modules) == "std/__init__.py"
    src = _modules({"app/__init__.py": "from pkg.models import *\n", "src/pkg/models.py": "class Model: pass\n"})
    assert bound_by_module(["app", "Model"], src) == "app/__init__.py"
    backend = {"svc/__init__.py": "from core.base import *\n", "backend/core/base.py": "class Base: pass\n"}
    assert bound_by_module(["svc", "Base"], _modules(backend, ("", "backend/"))) == "svc/__init__.py"
    assert bound_by_module(["svc", "Base"], _modules(backend)) is None


def test_star_import_chains_resolve_in_linear_time() -> None:
    count = 400
    files = {
        f"p/m{i:03}.py": "".join(f"from .m{j:03} import *\n" for j in (i + 1, i + 2) if j < count) + f"N{i} = 1\n"
        for i in range(count)
    }
    started = time.perf_counter()
    modules = _modules(files)
    assert time.perf_counter() - started < 2.0
    assert bound_by_module(["m000", f"N{count - 1}"], modules) == "p/m000.py"
