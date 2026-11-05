"""The library capture file format: reading, writing, include keys and comparing."""

from __future__ import annotations

import ast
import copy
import json
from typing import Any

import pytest

from code_intel import library_capture
from code_intel.library_capture import CaptureFormatError


def location(line: int = 10) -> dict[str, Any]:
    return {"dist": "extlib", "file": "extlib/__init__.py", "line": line}


def sample() -> dict[str, Any]:
    route = {
        "method": "GET",
        "path": "/me",
        "kind": "http",
        "handler_id": "extlib:make.<locals>.me",
        "location": location(46),
        "route_deps": [{"id": "extlib:audit", "via": "router", "sub": []}],
        "param_deps": [
            {
                "id": "extlib:current",
                "param": "user",
                "sub": [{"id": "pkg.deps:get_manager", "param": "manager"}],
            }
        ],
    }
    return {
        "format": 1,
        "app": "pkg.main:app",
        "distributions": {"extlib": "1.2.3", "fastapi": "0.162.0"},
        "includes": [{"file": "backend/pkg/main.py", "router": "extlib.make()", "occurrence": 1, "routes": [route]}],
        "dependencies": [
            {
                "binding": "pkg.main:current",
                "bound_to": "extlib.current(active=True)",
                "id": "extlib:current",
                "sub": [{"id": "extlib:audit", "sub": []}],
            },
            {"external": "extlib.Settings", "id": "extlib:Settings", "sub": []},
        ],
        "callables": {
            "extlib:audit": location(9),
            "extlib:current": location(35),
            "extlib:Settings": location(17),
        },
    }


def with_unfilled() -> dict[str, Any]:
    return {
        **sample(),
        "unfilled": {
            "includes": [
                {
                    "file": "backend/pkg/main.py",
                    "router": "extlib.mixed(handler)",
                    "occurrence": 1,
                    "reason": "pkg.main:handler is defined in the backend package",
                }
            ],
            "dependencies": [
                {
                    "binding": "pkg.main:mine",
                    "bound_to": "extlib.pass_through(dep)",
                    "reason": "a callable of the package",
                },
                {"external": "extlib.Missing", "reason": "no module named 'extlib'"},
            ],
        },
    }


def test_a_capture_survives_a_write_and_a_read_with_the_same_bytes() -> None:
    capture = library_capture.from_json(sample())
    text = library_capture.dumps(capture)
    assert text.endswith("}\n")
    assert library_capture.loads(text) == capture
    assert library_capture.dumps(library_capture.loads(text)) == text


def test_the_text_does_not_depend_on_the_order_keys_were_built_in() -> None:
    shuffled = json.loads(json.dumps(sample()))
    shuffled["includes"][0]["routes"][0] = dict(reversed(shuffled["includes"][0]["routes"][0].items()))
    assert library_capture.dumps(library_capture.from_json(shuffled)) == library_capture.dumps(
        library_capture.from_json(sample())
    )


def test_an_unknown_top_level_key_is_ignored_and_not_written_back() -> None:
    data = {**sample(), "registrations": [1]}
    assert "registrations" not in library_capture.dumps(library_capture.from_json(data))


def test_a_capture_without_unfilled_gaps_writes_no_unfilled_key() -> None:
    assert "unfilled" not in library_capture.from_json(sample())
    assert "unfilled" not in library_capture.dumps(library_capture.from_json(sample()))
    assert library_capture.unfilled_of(library_capture.from_json(sample())) == {"includes": [], "dependencies": []}


def test_unfilled_gaps_survive_a_write_and_a_read_with_the_same_bytes() -> None:
    capture = library_capture.from_json(with_unfilled())
    text = library_capture.dumps(capture)
    assert library_capture.loads(text) == capture
    assert library_capture.dumps(library_capture.loads(text)) == text
    assert library_capture.unfilled_of(capture)["dependencies"][1] == {
        "external": "extlib.Missing",
        "reason": "no module named 'extlib'",
    }


def mutated(change) -> dict[str, Any]:
    data = copy.deepcopy(sample())
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(unfilled=[]), "unfilled: must be an object"),
        (lambda d: d["unfilled"].pop("includes"), "unfilled: missing 'includes'"),
        (lambda d: d["unfilled"].pop("dependencies"), "unfilled: missing 'dependencies'"),
        (lambda d: d["unfilled"].update(extra=[]), "unfilled: unknown key 'extra'"),
        (lambda d: d["unfilled"].update(includes={}), "unfilled.includes: must be a list"),
        (lambda d: d["unfilled"]["includes"][0].pop("reason"), "unfilled.includes[0]: missing 'reason'"),
        (lambda d: d["unfilled"]["includes"][0].update(reason=""), "unfilled.includes[0].reason: must be a non-empty"),
        (lambda d: d["unfilled"]["includes"][0].update(occurrence=0), "includes[0].occurrence: must be a positive"),
        (lambda d: d["unfilled"]["includes"][0].update(routes=[]), "unfilled.includes[0]: unknown key 'routes'"),
        (lambda d: d["unfilled"]["dependencies"][0].pop("bound_to"), "dependencies[0]: missing 'bound_to'"),
        (lambda d: d["unfilled"]["dependencies"][1].update(bound_to="x"), "dependencies[1]: unknown key 'bound_to'"),
        (lambda d: d["unfilled"]["dependencies"][0].update(external="x"), "exactly one of 'binding' and 'external'"),
        (lambda d: d["unfilled"]["dependencies"][1].pop("external"), "exactly one of 'binding' and 'external'"),
        (lambda d: d["unfilled"]["dependencies"][0].update(id="x"), "dependencies[0]: unknown key 'id'"),
        (lambda d: d["unfilled"]["dependencies"][0].update(reason=3), "dependencies[0].reason: must be a non-empty"),
    ],
)
def test_a_malformed_unfilled_key_is_refused_with_where_and_why(change, message: str) -> None:
    data = copy.deepcopy(with_unfilled())
    change(data)
    with pytest.raises(CaptureFormatError, match=message.replace("[", r"\[").replace("]", r"\]")):
        library_capture.from_json(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(format=2), "format: is 2"),
        (lambda d: d.update(format=True), "format: is True"),
        (lambda d: d.pop("includes"), "missing 'includes'"),
        (lambda d: d["includes"][0].update(occurrence=0), "includes[0].occurrence: must be a positive integer"),
        (lambda d: d["includes"][0].pop("router"), "includes[0]: missing 'router'"),
        (lambda d: d["includes"][0].update(extra=1), "includes[0]: unknown key 'extra'"),
        (lambda d: d["includes"][0]["routes"][0].update(method="TRACE"), "routes[0].method: must be one of"),
        (lambda d: d["includes"][0]["routes"][0].update(kind="grpc"), "routes[0].kind: must be one of"),
        (lambda d: d["includes"][0]["routes"][0]["route_deps"][0].update(via="app"), "via: must be one of"),
        (lambda d: d["includes"][0]["routes"][0]["location"].update(line=0), "location.line"),
        (lambda d: d["includes"][0]["routes"][0]["route_deps"][0].pop("via"), "route_deps[0]: missing 'via'"),
        (lambda d: d["includes"][0]["routes"][0]["param_deps"][0].pop("param"), "param_deps[0]: missing 'param'"),
        (
            lambda d: d["includes"][0]["routes"][0]["location"].update(dist="extlib-userz"),
            "no version for 'extlib-userz'",
        ),
        (lambda d: d["dependencies"][0].pop("bound_to"), "dependencies[0]: missing 'bound_to'"),
        (lambda d: d["dependencies"][1].update(bound_to="x"), "dependencies[1]: unknown key 'bound_to'"),
        (lambda d: d["dependencies"][0].update(external="x"), "exactly one of 'binding' and 'external'"),
        (lambda d: d["dependencies"][1].pop("external"), "exactly one of 'binding' and 'external'"),
        (lambda d: d["callables"].pop("extlib:audit"), "no location for 'extlib:audit'"),
        (lambda d: d["distributions"].pop("extlib"), "no version for 'extlib'"),
        (lambda d: d["distributions"].update(extlib=3), "distributions.extlib: must be a non-empty string"),
    ],
)
def test_a_malformed_capture_is_refused_with_where_and_why(change, message: str) -> None:
    with pytest.raises(CaptureFormatError, match=message.replace("[", r"\[").replace("]", r"\]")):
        library_capture.from_json(mutated(change))


@pytest.mark.parametrize("text", ["", "{", "[]", "3", '{"app": "x"}'])
def test_text_that_is_not_a_capture_is_refused(text: str) -> None:
    with pytest.raises(CaptureFormatError):
        library_capture.loads(text)


# ── Include keys ─────────────────────────────────────────────────────────

SOURCE = """\
app.include_router(make(1), prefix="/a")
app.include_router(
    other(),
    prefix="/b",
)
app.include_router(make(1), prefix="/c")
app.include_router(router=make(1))
"""


def sites(source: str) -> list[tuple[int, int, str, int]]:
    return [(s.line, s.end_line, s.router, s.occurrence) for s in library_capture.include_sites(ast.parse(source))]


def test_include_sites_give_the_span_the_router_text_and_the_occurrence_in_source_order() -> None:
    assert sites(SOURCE) == [
        (1, 1, "make(1)", 1),
        (2, 5, "other()", 1),
        (6, 6, "make(1)", 2),
        (7, 7, "make(1)", 3),
    ]


def test_include_keys_do_not_change_when_the_call_is_reformatted() -> None:
    reformatted = SOURCE.replace("make(1)", "make( 1 )").replace('"/a"', "'/a'")
    assert [s[2:] for s in sites(reformatted)] == [s[2:] for s in sites(SOURCE)]


def test_an_include_without_a_router_argument_has_no_site() -> None:
    assert sites("app.include_router()\nplain()\nobj.other(make())\n") == []


# ── Comparing ────────────────────────────────────────────────────────────


def test_differences_name_each_added_removed_and_changed_entry() -> None:
    old = library_capture.from_json(sample())
    assert library_capture.differences(old, old) == []
    data = sample()
    data["distributions"]["extlib"] = "1.2.4"
    data["includes"][0]["routes"][0]["path"] = "/you"
    data["dependencies"].pop(1)
    data["dependencies"].append(
        {"binding": "pkg.main:other", "bound_to": "extlib.other()", "id": "extlib:audit", "sub": []}
    )
    assert library_capture.differences(old, library_capture.from_json(data)) == [
        "distribution extlib: 1.2.3 -> 1.2.4",
        "include backend/pkg/main.py extlib.make() #1: changed",
        "dependency external extlib.Settings: removed",
        "dependency pkg.main:other: added",
    ]


def test_differences_name_each_added_removed_and_changed_unfilled_gap() -> None:
    old = library_capture.from_json(with_unfilled())
    data = with_unfilled()
    data["unfilled"]["includes"][0]["reason"] = "the call ran 2 times"
    data["unfilled"]["dependencies"][0]["bound_to"] = "extlib.pass_through(other)"
    data["unfilled"]["dependencies"].pop(1)
    assert library_capture.differences(old, library_capture.from_json(data)) == [
        "unfilled include backend/pkg/main.py extlib.mixed(handler) #1: changed",
        "unfilled dependency external extlib.Missing: removed",
        "unfilled dependency pkg.main:mine: changed",
    ]
    assert library_capture.differences(library_capture.from_json(sample()), old) == [
        "unfilled include backend/pkg/main.py extlib.mixed(handler) #1: added",
        "unfilled dependency external extlib.Missing: added",
        "unfilled dependency pkg.main:mine: added",
    ]
