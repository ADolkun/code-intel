"""Route matching with Starlette semantics on synthetic ``LiveRoute`` lists."""

from __future__ import annotations

import ast
import itertools
import re

import pytest

from code_intel import route_matching as rmatch
from code_intel.backend_model import LiveRoute, Located, RouteKind
from code_intel.result_types import Confidence
from code_intel.route_paths import path_key

_ENDPOINT = Located("case", ast.Pass())


def route(method: str, path: str, order: int, *, kind: RouteKind = "http", handler: str | None = None) -> LiveRoute:
    return LiveRoute(
        method=method,
        path=path,
        kind=kind,
        handler=handler or f"h{order}",
        handler_id=f"case:h{order}",
        file="backend/case.py",
        line=order + 1,
        registered_file="backend/case.py",
        registered_line=order + 1,
        chain=("case:app",),
        mount="static",
        layers=(),
        order=order,
        endpoint=_ENDPOINT,
    )


def routes(*specs: tuple[str, str], kind: RouteKind = "http") -> list[LiveRoute]:
    return [route(m, p, i, kind=kind) for i, (m, p) in enumerate(specs)]


def text(t: str) -> dict:
    return {"text": t}


def hole(expr: str, kind: str = "param") -> dict:
    return {"hole": kind, "expr": expr}


def url(*parts: dict, method: str = "GET", kind: str = "http", confidence: Confidence = "exact") -> rmatch.FrontendUrl:
    return rmatch.prepare_url(list(parts), method, kind, confidence)


def lit(path: str, method: str = "GET", **kw) -> rmatch.FrontendUrl:
    return url(text(path), method=method, **kw)


def found(compiled: rmatch.CompiledRoutes, u: rmatch.FrontendUrl) -> list[tuple[str, str, str, str]]:
    return [(m.route.method, m.route.path, m.confidence, m.reason) for m in compiled.match(u)]


def compile_(rs: list[LiveRoute], *, redirect_slashes: bool = False) -> rmatch.CompiledRoutes:
    return rmatch.compile_backend(rs, redirect_slashes=redirect_slashes)


# ── Literal URLs: dispatch order ─────────────────────────────────────────


def test_literal_url_takes_the_first_route_in_dispatch_order() -> None:
    # Starlette tries routes in order: a param route registered first shadows the literal sibling.
    shadowing = compile_(routes(("GET", "/squads/{squad_id}"), ("GET", "/squads/options")))
    assert found(shadowing, lit("/squads/options")) == [("GET", "/squads/{squad_id}", "exact", "literal_on_param")]

    literal_first = compile_(routes(("GET", "/squads/options"), ("GET", "/squads/{squad_id}")))
    assert found(literal_first, lit("/squads/options")) == [("GET", "/squads/options", "exact", "literal")]


def test_literal_url_matches_a_param_route_exactly() -> None:
    compiled = compile_(routes(("GET", "/squads/{squad_id}/articles")))
    assert found(compiled, lit("/squads/default/articles")) == [
        ("GET", "/squads/{squad_id}/articles", "exact", "literal_on_param")
    ]


def test_literal_url_needs_the_same_method() -> None:
    compiled = compile_(routes(("POST", "/squads"), ("GET", "/squads")))
    assert found(compiled, lit("/squads", "GET")) == [("GET", "/squads", "exact", "literal")]
    assert found(compiled, lit("/squads", "DELETE")) == []


# ── Convertors ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("backend", "frontend", "matches"),
    [
        ("/items/{item_id:int}", "/items/12", True),
        ("/items/{item_id:int}", "/items/abc", False),
        ("/items/{price:float}", "/items/1.5", True),
        ("/items/{price:float}", "/items/x1", False),
        ("/items/{key:uuid}", "/items/123e4567-e89b-12d3-a456-426614174000", True),
        ("/items/{key:uuid}", "/items/not-a-uuid", False),
        ("/files/{rest:path}", "/files/a/b/c.txt", True),
        ("/files/{rest:path}", "/files/", True),
        ("/items/{name:str}", "/items/a", True),
        ("/items/{name:bogus}", "/items/a", False),  # Starlette rejects an unknown convertor
    ],
)
def test_convertors_follow_starlette(backend: str, frontend: str, matches: bool) -> None:
    compiled = compile_(routes(("GET", backend)))
    assert bool(compiled.match(lit(frontend))) is matches


def test_path_convertor_absorbs_hole_segments() -> None:
    compiled = compile_(routes(("GET", "/files/{rest:path}")))
    assert found(compiled, url(text("/files/"), hole("dir"), text("/"), hole("name"))) == [
        ("GET", "/files/{rest:path}", "exact", "param")
    ]


def test_a_path_convertor_before_more_segments_needs_them_at_the_end_of_the_url() -> None:
    compiled = compile_(routes(("GET", "/files/{p:path}/meta")))
    assert found(compiled, url(text("/files/"), hole("id"))) == []
    assert found(compiled, url(text("/files/"), hole("id"), text("/meta"))) == [
        ("GET", "/files/{p:path}/meta", "exact", "param")
    ]
    assert found(compiled, url(text("/files/"), hole("dir"), text("/"), hole("name"), text("/meta"))) == [
        ("GET", "/files/{p:path}/meta", "exact", "param")
    ]
    assert found(compiled, url(text("/files/a/b/meta"))) == [
        ("GET", "/files/{p:path}/meta", "exact", "literal_on_param")
    ]
    assert found(compiled, url(text("/files/"), hole("id"), text("/other"))) == []
    # The path param must absorb at least one segment.
    assert found(compiled, url(text("/files/meta"))) == []


# ── Trailing slashes ─────────────────────────────────────────────────────


def test_trailing_slash_is_significant_without_redirect_slashes() -> None:
    compiled = compile_(routes(("GET", "/appointments")), redirect_slashes=False)
    assert found(compiled, lit("/appointments/")) == []
    assert found(compiled, lit("/appointments")) == [("GET", "/appointments", "exact", "literal")]
    with_slash = compile_(routes(("GET", "/appointments/")), redirect_slashes=False)
    assert found(with_slash, lit("/appointments")) == []
    assert found(with_slash, url(text("/squads/"), hole("id"), text("/"))) == []


def test_redirect_slashes_turns_a_slash_variant_into_a_possible_redirect() -> None:
    compiled = compile_(routes(("GET", "/appointments")), redirect_slashes=True)
    assert found(compiled, lit("/appointments/")) == [("GET", "/appointments", "possible", "redirect")]
    reverse = compile_(routes(("GET", "/appointments/")), redirect_slashes=True)
    assert found(reverse, lit("/appointments")) == [("GET", "/appointments/", "possible", "redirect")]


# ── Holes ────────────────────────────────────────────────────────────────


def test_query_holes_and_literal_query_text_are_stripped() -> None:
    compiled = compile_(routes(("GET", "/squads")))
    assert found(compiled, url(text("/squads?"), hole("qs", "query"))) == [("GET", "/squads", "exact", "literal")]
    assert found(compiled, url(text("/squads?status=active&x="), hole("x", "query"))) == [
        ("GET", "/squads", "exact", "literal")
    ]


def test_param_hole_prefers_param_routes_over_literal_siblings() -> None:
    compiled = compile_(routes(("GET", "/squads/options"), ("GET", "/squads/{squad_id}")))
    assert found(compiled, url(text("/squads/"), hole("id"))) == [("GET", "/squads/{squad_id}", "exact", "param")]


def test_param_hole_on_a_backend_literal_is_possible() -> None:
    compiled = compile_(routes(("GET", "/squads/options")))
    assert found(compiled, url(text("/squads/"), hole("id"))) == [
        ("GET", "/squads/options", "possible", "hole_on_literal")
    ]


def test_an_earlier_route_that_always_matches_shadows_every_later_route() -> None:
    compiled = compile_(routes(("GET", "/squads/default/{item}"), ("GET", "/squads/{squad_id}/{item}")))
    assert found(compiled, url(text("/squads/default/"), hole("x"))) == [
        ("GET", "/squads/default/{item}", "exact", "param")
    ]
    # Registered first, the param route takes every request the URL can make:
    # Starlette never reaches the literal sibling.
    later = compile_(routes(("GET", "/squads/{squad_id}/{item}"), ("GET", "/squads/default/{item}")))
    assert found(later, url(text("/squads/default/"), hole("x"))) == [
        ("GET", "/squads/{squad_id}/{item}", "exact", "literal_on_param")
    ]
    param_first = compile_(routes(("GET", "/v/{x}/{y}"), ("GET", "/v/export/{id}")))
    assert found(param_first, url(text("/v/export/"), hole("id"))) == [
        ("GET", "/v/{x}/{y}", "exact", "literal_on_param")
    ]


def test_a_possible_earlier_match_never_hides_the_route_starlette_dispatches_to() -> None:
    # `/a/export/${id}` reaches `/a/export/special` only when id is "special";
    # every other value goes to `/a/{x}/{id}`.
    compiled = compile_(routes(("GET", "/a/export/special"), ("GET", "/a/{x}/{id}")))
    assert found(compiled, url(text("/a/export/"), hole("id"))) == [("GET", "/a/{x}/{id}", "exact", "literal_on_param")]


def test_only_routes_that_accept_every_hole_value_shadow() -> None:
    # An int convertor rejects most values, so the str route after it stays reachable.
    compiled = compile_(routes(("GET", "/items/{n:int}"), ("GET", "/items/{name}")))
    assert found(compiled, url(text("/items/"), hole("id"))) == [
        ("GET", "/items/{n:int}", "exact", "param"),
        ("GET", "/items/{name}", "exact", "param"),
    ]
    # A route for another method never shadows: Starlette keeps looking.
    methods = compile_(routes(("POST", "/items/{name}"), ("GET", "/items/{name}")))
    assert found(methods, url(text("/items/"), hole("id"))) == [("GET", "/items/{name}", "exact", "param")]


def test_mixed_segments() -> None:
    # `${m}` is glued to the "-" before it, so it may be empty or hold "/":
    # every route it can reach is possible.
    ym = url(text("/reports/"), hole("y"), text("-"), hole("m"), text("/x"))
    compiled = compile_(routes(("GET", "/reports/{year}-{month}/x")))
    assert found(compiled, ym) == [("GET", "/reports/{year}-{month}/x", "possible", "mixed_segment")]
    whole = compile_(routes(("GET", "/reports/{period}/x")))
    assert found(whole, ym) == [("GET", "/reports/{period}/x", "possible", "mixed_segment")]
    literal = compile_(routes(("GET", "/reports/2024-05/x")))
    assert found(literal, ym) == [("GET", "/reports/2024-05/x", "possible", "mixed_segment")]
    assert found(literal, url(text("/reports/"), hole("y"), text("_"), hole("m"), text("/x"))) == []
    suffix = compile_(routes(("GET", "/files/{file_id}.csv")))
    assert found(suffix, url(text("/files/"), hole("id"), text(".csv"))) == [
        ("GET", "/files/{file_id}.csv", "exact", "mixed_segment")
    ]
    assert found(suffix, lit("/files/abc.csv")) == [("GET", "/files/{file_id}.csv", "exact", "literal_on_param")]


def test_a_hole_glued_mid_path_may_be_empty_or_span_segments() -> None:
    """`/articles${suffix}/files` reaches `/articles/{id}/files` when the
    suffix is "/1" and `/articlesX/files` when it is "X"; neither shadows."""
    compiled = compile_(
        routes(
            ("GET", "/articles/{id}/files"),
            ("GET", "/articlesX/files"),
            ("GET", "/articles/files"),
            ("GET", "/articles/{id:int}/files/{n:int}"),
            ("GET", "/squads/{id}/files"),
            ("POST", "/articles/{id}/files"),
        )
    )
    glued = url(text("/articles"), hole("suffix"), text("/files"))
    assert found(compiled, glued) == [
        ("GET", "/articles/{id}/files", "possible", "mixed_segment"),
        ("GET", "/articlesX/files", "possible", "mixed_segment"),
        ("GET", "/articles/files", "possible", "mixed_segment"),
    ]
    longer = url(text("/articlesX"), hole("suffix"), text("/files"))
    assert found(compiled, longer) == [("GET", "/articlesX/files", "possible", "mixed_segment")]
    assert found(compiled, url(text("/articles"), hole("suffix"), text("/files"), method="UNKNOWN"))[-1] == (
        "POST",
        "/articles/{id}/files",
        "possible",
        "method_unknown",
    )
    # A hole that fills its segment is not glued: the usual segment rules apply.
    assert found(compiled, url(text("/articles/"), hole("id"), text("/files"))) == [
        ("GET", "/articles/{id}/files", "exact", "param")
    ]


@pytest.mark.parametrize(
    "path", ["/a/{x}", "/a/{x:int}/b", "/a/{x:float}", "/a/{x:path}", "/a1/{x}-{y}", "/a/b", "/a/{x:nope}"]
)
@pytest.mark.parametrize(
    "parts",
    [
        [text("/a"), hole("g")],
        [text("/a"), hole("g"), text("/b")],
        [text("/a/"), hole("p"), text("-"), hole("g")],
        [text("/a/"), hole("p"), text("1"), hole("g"), text(".5")],
        [text("/a/"), hole("p"), text("/"), hole("q", "unknown")],
    ],
)
def test_character_matching_agrees_with_starlettes_regex(path: str, parts: list[dict]) -> None:
    """Brute force over every string of "/a" plus up to 5 characters from a
    small alphabet: the token search finds a common string exactly when one
    exists."""
    u = url(*parts)
    front = "".join(
        re.escape(p) if isinstance(p, str) else (".*" if p.glued else "[^/]+")
        for seg in u.segments
        for p in ("/", *seg)
    ) + (".*" if u.trailing is not None else "")
    regex = rmatch.compile_path(path)
    candidates = ("/a" + "".join(t) for n in range(6) for t in itertools.product("/ab15-.", repeat=n))
    expected = any(regex.fullmatch(c) and re.fullmatch(front, c) for c in candidates)
    got = any(rmatch._intersects(rmatch._url_tokens(u), alt) for alt in rmatch._path_tokens(path))
    assert got == expected


def test_character_matching_reads_uuid_and_float_convertors() -> None:
    compiled = compile_(routes(("GET", "/a/{x:uuid}/b"), ("GET", "/a/{x:float}/c")))
    assert found(compiled, url(text("/a"), hole("g"), text("/b"))) == [
        ("GET", "/a/{x:uuid}/b", "possible", "mixed_segment")
    ]
    assert found(compiled, url(text("/a/1"), hole("g"), text("z/c"))) == []
    assert found(compiled, url(text("/a/1"), hole("g"), text("5/c"))) == [
        ("GET", "/a/{x:float}/c", "possible", "mixed_segment")
    ]


def test_trailing_unknown_hole_may_absorb_zero_or_more_segments() -> None:
    compiled = compile_(
        routes(("GET", "/squads/{squad_id}"), ("GET", "/squads/{squad_id}/articles"), ("GET", "/squads"))
    )
    u = url(text("/squads/"), hole("id"), hole("sub", "unknown"))
    assert found(compiled, u) == [
        ("GET", "/squads/{squad_id}", "possible", "trailing_hole"),
        ("GET", "/squads/{squad_id}/articles", "possible", "trailing_hole"),
    ]
    glued = compile_(routes(("GET", "/admin/export"), ("GET", "/admin/exports/all"), ("GET", "/admin/other")))
    assert found(glued, url(text("/admin/export"), hole("suffix", "unknown"))) == [
        ("GET", "/admin/export", "possible", "trailing_hole"),
        ("GET", "/admin/exports/all", "possible", "trailing_hole"),
    ]


def test_a_glued_trailing_hole_still_meets_the_convertors() -> None:
    compiled = compile_(
        routes(("GET", "/items/{id:int}"), ("GET", "/v/{id:uuid}"), ("GET", "/w/{id:uuid}/files"), ("GET", "/s/{x}"))
    )
    assert found(compiled, url(text("/items/n"), hole("n", "unknown"))) == []
    assert found(compiled, url(text("/v/x"), hole("id", "unknown"))) == []
    assert found(compiled, url(text("/w/a"), hole("id", "unknown"))) == [
        ("GET", "/w/{id:uuid}/files", "possible", "trailing_hole")
    ]
    assert found(compiled, url(text("/s/ab"), hole("id", "unknown"))) == [
        ("GET", "/s/{x}", "possible", "trailing_hole")
    ]


# ── Methods and kinds ────────────────────────────────────────────────────


def test_unknown_method_matches_every_method_as_possible() -> None:
    compiled = compile_(routes(("GET", "/squads/{squad_id}"), ("PATCH", "/squads/{squad_id}"), ("POST", "/squads")))
    assert found(compiled, url(text("/squads/"), hole("id"), method="UNKNOWN")) == [
        ("GET", "/squads/{squad_id}", "possible", "method_unknown"),
        ("PATCH", "/squads/{squad_id}", "possible", "method_unknown"),
    ]
    assert found(compiled, lit("/squads", "UNKNOWN")) == [("POST", "/squads", "possible", "method_unknown")]


def test_backend_any_method_is_possible() -> None:
    compiled = compile_(routes(("ANY", "/dynamic")))
    assert found(compiled, lit("/dynamic", "DELETE")) == [("ANY", "/dynamic", "possible", "method_unknown")]


def test_websocket_calls_match_only_websocket_routes() -> None:
    rs = [
        route("GET", "/ws/articles/{article_id}/heartbeat", 0),
        route("WS", "/ws/articles/{article_id}/heartbeat", 1, kind="websocket"),
    ]
    compiled = compile_(rs)
    ws = url(text("/ws/articles/"), hole("id"), text("/heartbeat"), method="WS", kind="websocket")
    assert found(compiled, ws) == [("WS", "/ws/articles/{article_id}/heartbeat", "exact", "param")]
    http = url(text("/ws/articles/"), hole("id"), text("/heartbeat"))
    assert found(compiled, http) == [("GET", "/ws/articles/{article_id}/heartbeat", "exact", "param")]


def test_a_prop_flow_variant_caps_the_match_with_reason_prop_flow() -> None:
    compiled = compile_(routes(("GET", "/admin/changes/export"), ("GET", "/admin/options")))
    assert found(compiled, lit("/admin/changes/export", confidence="possible")) == [
        ("GET", "/admin/changes/export", "possible", "prop_flow")
    ]
    # A match that is already possible keeps its own, more specific reason.
    assert found(compiled, url(text("/admin/"), hole("x"), confidence="possible")) == [
        ("GET", "/admin/options", "possible", "hole_on_literal")
    ]


# ── Presentation ─────────────────────────────────────────────────────────


def test_display_path_names_holes_so_path_key_reads_them_as_params() -> None:
    u = url(text("/squads/"), hole("params.squadId"), text("/files/"), hole("encodeURIComponent(id)"))
    shown = rmatch.display_path(u)
    assert shown == "/squads/{params_squadId}/files/{encodeURIComponent_id}"
    assert path_key(shown) == "/squads/{}/files/{}"
    trailing = url(text("/squads/"), hole("id"), hole("sub", "unknown"))
    assert rmatch.display_path(trailing) == "/squads/{id}{sub}"
    assert rmatch.display_path(lit("/appointments/")) == "/appointments/"
    assert rmatch.display_path(url(text("/x?"), hole("q", "query"))) == "/x"
