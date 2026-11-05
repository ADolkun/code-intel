"""Match frontend URL variants to backend routes with Starlette's rules.

Backend paths compile to the regexes Starlette builds (``{name}``, ``{name:int}``,
``{name:path}``, ...) and routes are tried in ``LiveRoute.order``, which is
Starlette's dispatch order. A frontend URL with no holes is matched exactly
like a request: the first route whose path and method match wins. A URL with
holes stands for many requests, so it is matched segment by segment and each
segment comparison carries a confidence:

- a literal segment on a backend literal or param, or a param hole on a backend
  param, is ``exact``;
- a param hole on a backend literal, and a mixed segment (``${id}.csv``) on a
  backend literal it fits, are ``possible``.

Dispatch order decides between routes: an exact match that holds for every value
the holes can take (no ``int``/``float``/``uuid`` convertor under a hole)
shadows every later route, because Starlette always dispatches to it first. A
possible match never shadows. A ``{name:path}`` segment absorbs one or more
frontend segments and the backend segments after it must match the end of the
URL.

A hole glued to text before it in its segment (`/articles${suffix}/files`,
the ``m`` of ``${y}-${m}``) may be empty or hold ``/``, as a glued last hole
may: a URL with one is matched on its characters, so every route some value of
its holes reaches is listed, each as ``possible`` (``mixed_segment``, or
``trailing_hole`` when the URL ends in a hole), and convertors still apply:
``/items/n${n}`` never reaches ``/items/{id:int}``.

A variant with any exact match reports only its exact matches; possible matches
are reported only when nothing matches exactly, so a call to
``/squads/${id}`` lists ``/squads/{squad_id}`` and not every literal sibling.
A variant that is itself only possible (a value from a prop flow) caps every
match at ``possible``; an otherwise exact match then has reason ``prop_flow``.

The trailing slash is significant when the app sets ``redirect_slashes=False``;
with ``True`` (FastAPI's default) a match that needs the slash toggled is
``possible`` with reason ``redirect``. An ``UNKNOWN`` frontend method matches any
method as ``possible``. WebSocket calls match only WebSocket routes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from code_intel.backend_model import UNRESOLVED_PATH_PART, LiveRoute
from code_intel.result_types import Confidence, MatchReason
from code_intel.route_paths import PARAM_RE

# Starlette's convertor regexes (starlette/convertors.py).
CONVERTOR_REGEX = {
    "str": "[^/]+",
    "path": ".*",
    "int": "[0-9]+",
    "float": r"[0-9]+(\.[0-9]+)?",
    "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
}
# A convertor Starlette does not know fails at registration, so the route can never match.
_NEVER = "(?!)"

EXACT: Confidence = "exact"
POSSIBLE: Confidence = "possible"

# Reason precedence when several segments contribute: the weakest evidence names the match.
_EXACT_REASONS: tuple[MatchReason, ...] = ("literal_on_param", "mixed_segment", "param", "literal")
_POSSIBLE_REASONS: tuple[MatchReason, ...] = ("trailing_hole", "hole_on_literal", "mixed_segment")
# Convertors that accept every non-empty value without "/", so a hole always fits.
_TOTAL_CONVERTORS = frozenset({"str", "path"})


# ── Frontend side ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hole:
    kind: str  # "param" | "query" | "unknown"
    expr: str
    # Follows text or another hole in its segment, so its value may be empty
    # or hold "/": the helper reads `/articles${suffix}` the same way when the
    # hole comes last.
    glued: bool = False


Part = str | Hole


@dataclass(frozen=True)
class FrontendUrl:
    """One URL variant prepared for matching: no query, holes kept in place.

    ``segments`` split the path on ``/`` (the leading ``/`` dropped, a trailing
    ``/`` kept as an empty last segment). ``trailing`` is the hole of unknown
    value the URL ends in, if any; it is not part of ``segments`` and may absorb
    the rest of the last segment and any number of further segments.
    """

    method: str
    kind: str  # "http" | "websocket"
    segments: tuple[tuple[Part, ...], ...]
    trailing: Hole | None
    confidence: Confidence  # the variant's own confidence: "possible" only for values from a prop flow

    @property
    def literal(self) -> str | None:
        """The concrete path when the URL has no holes, else None."""
        if self.trailing is not None or any(isinstance(p, Hole) for seg in self.segments for p in seg):
            return None
        return "/" + "/".join(_plain(seg) for seg in self.segments)

    @property
    def has_glued_hole(self) -> bool:
        return any(isinstance(p, Hole) and p.glued for seg in self.segments for p in seg)


def prepare_url(parts: Sequence[dict], method: str, kind: str, confidence: Confidence = EXACT) -> FrontendUrl:
    """Turn helper URL parts into a ``FrontendUrl``.

    Query holes and everything from a literal ``?`` on are dropped. A trailing
    ``unknown`` hole becomes ``trailing``.
    """
    flat: list[Part] = []
    for part in parts:
        if "text" in part:
            text = part["text"]
            if "?" in text:
                flat.append(text.split("?", 1)[0])
                break
            flat.append(text)
            continue
        hole_kind = part.get("hole", "param")
        if hole_kind == "query":
            break
        flat.append(Hole(hole_kind, part.get("expr", "")))
    trailing: Hole | None = None
    last = flat[-1] if flat else None
    if isinstance(last, Hole) and last.kind == "unknown":
        trailing = last
        flat.pop()
    segments: list[list[Part]] = [[]]
    for item in flat:
        if isinstance(item, Hole):
            segments[-1].append(Hole(item.kind, item.expr, glued=bool(segments[-1])))
            continue
        pieces = item.split("/")
        for i, piece in enumerate(pieces):
            if i > 0:
                segments.append([])
            if piece:
                segments[-1].append(piece)
    # The leading "/" opens an empty first segment.
    if len(segments) > 1 and not segments[0]:
        segments = segments[1:]
    return FrontendUrl(
        method=method,
        kind=kind,
        segments=tuple(tuple(_merge_text(seg)) for seg in segments),
        trailing=trailing,
        confidence=confidence,
    )


def _merge_text(parts: Iterable[Part]) -> list[Part]:
    out: list[Part] = []
    for part in parts:
        if isinstance(part, str) and out and isinstance(out[-1], str):
            out[-1] = out[-1] + part
        else:
            out.append(part)
    return out


def display_path(url: FrontendUrl, *, render_hole=None) -> str:
    """The URL as a route-like path: holes render as ``{name}``."""
    render = render_hole or _hole_name
    body = "/".join("".join(p if isinstance(p, str) else "{" + render(p) + "}" for p in seg) for seg in url.segments)
    path = "/" + body
    if url.trailing is not None:
        path += "{" + render(url.trailing) + "}"
    return path


_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def _hole_name(hole: Hole) -> str:
    """An identifier for a hole, so ``route_paths.PARAM_RE`` reads it as a param."""
    name = _NAME_RE.sub("_", hole.expr).strip("_") or "param"
    return name if not name[0].isdigit() else f"_{name}"


# ── Backend side ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackendSegment:
    text: str  # the raw segment, e.g. "{squad_id}" or "{year}-{month}"
    kind: str  # "literal" | "param" | "mixed" | "path"
    regex: re.Pattern[str]
    convertor: str | None  # for kind "param"
    shape: tuple[str | None, ...]  # literal pieces with None for each param, for mixed segments
    # Every param in the segment accepts any value without "/" (str or path convertors).
    total: bool = True


@dataclass(frozen=True)
class CompiledRoute:
    route: LiveRoute
    regex: re.Pattern[str]  # the whole path, as Starlette compiles it
    segments: tuple[BackendSegment, ...]
    has_path_param: bool


def _param_regex(convertor: str | None) -> str:
    return CONVERTOR_REGEX.get(convertor or "str", _NEVER)


def _compile_segment(text: str) -> BackendSegment:
    params = list(PARAM_RE.finditer(text))
    if not params:
        return BackendSegment(text, "literal", re.compile(re.escape(text)), None, (text,))
    pieces: list[str] = []
    shape: list[str | None] = []
    last = 0
    for m in params:
        if m.start() > last:
            pieces.append(re.escape(text[last : m.start()]))
            shape.append(text[last : m.start()])
        pieces.append(f"(?:{_param_regex(m.group(2))})")
        shape.append(None)
        last = m.end()
    if last < len(text):
        pieces.append(re.escape(text[last:]))
        shape.append(text[last:])
    regex = re.compile("".join(pieces))
    total = all((m.group(2) or "str") in _TOTAL_CONVERTORS for m in params)
    if any(m.group(2) == "path" for m in params):
        return BackendSegment(text, "path", regex, "path", tuple(shape), total)
    if len(params) == 1 and params[0].start() == 0 and params[0].end() == len(text):
        return BackendSegment(text, "param", regex, params[0].group(2) or "str", (None,), total)
    return BackendSegment(text, "mixed", regex, None, tuple(shape), total)


def compile_path(path: str) -> re.Pattern[str]:
    """Starlette's path regex: each param replaced by its convertor's regex."""
    pieces: list[str] = []
    last = 0
    for m in PARAM_RE.finditer(path):
        pieces.append(re.escape(path[last : m.start()]))
        pieces.append(f"(?:{_param_regex(m.group(2))})")
        last = m.end()
    pieces.append(re.escape(path[last:]))
    return re.compile("".join(pieces))


def compile_backend(routes: Iterable[LiveRoute], *, redirect_slashes: bool) -> CompiledRoutes:
    """Compile live routes for matching, kept in dispatch order."""
    compiled = []
    for route in sorted(routes, key=lambda r: r.order):
        bare = route.path[1:] if route.path.startswith("/") else route.path
        segments = tuple(_compile_segment(s) for s in bare.split("/"))
        compiled.append(
            CompiledRoute(
                route=route,
                regex=compile_path(route.path),
                segments=segments,
                has_path_param=any(s.kind == "path" for s in segments),
            )
        )
    return CompiledRoutes(tuple(compiled), redirect_slashes)


# ── Matching ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Match:
    route: LiveRoute
    confidence: Confidence
    reason: MatchReason


@dataclass(frozen=True)
class _SegmentResult:
    confidence: Confidence
    reason: MatchReason
    # The segment matches whatever values its holes take; only then can the
    # route shadow later routes.
    total: bool


def _plain(parts: Sequence[Part]) -> str:
    """The text of parts that hold no hole."""
    return "".join(p for p in parts if isinstance(p, str))


def _shape(parts: Sequence[Part]) -> tuple[str | None, ...]:
    return tuple(p if isinstance(p, str) else None for p in parts)


def _sample(parts: Sequence[Part], filler: str) -> str:
    return "".join(p if isinstance(p, str) else filler for p in parts)


def _fits(regex: re.Pattern[str], parts: Sequence[Part]) -> bool:
    """Whether some value of the holes makes the segment match (sampled, not proven)."""
    return any(regex.fullmatch(_sample(parts, filler)) for filler in ("1", "x", "0a"))


def _match_segment(front: Sequence[Part], back: BackendSegment) -> _SegmentResult | None:
    """Compare one frontend segment with one backend segment."""
    holes = [p for p in front if isinstance(p, Hole)]
    front_pattern = "".join(re.escape(p) if isinstance(p, str) else "[^/]+" for p in front)
    if not holes:
        text = _plain(front)
        if back.kind == "literal":
            return _SegmentResult(EXACT, "literal", True) if text == back.text else None
        return _SegmentResult(EXACT, "literal_on_param", True) if back.regex.fullmatch(text) else None
    if len(front) == 1:  # one param hole fills the segment
        if back.kind in ("param", "path"):
            return _SegmentResult(EXACT, "param", back.total)
        if back.kind == "literal":
            return _SegmentResult(POSSIBLE, "hole_on_literal", False)
        return _SegmentResult(POSSIBLE, "mixed_segment", False)
    # A mixed frontend segment: text and holes, e.g. `${y}-${m}` or `${id}.csv`.
    if back.kind in ("param", "path"):
        if back.convertor in (None, "str", "path"):
            return _SegmentResult(EXACT, "mixed_segment", True)
        return _SegmentResult(POSSIBLE, "mixed_segment", False) if _fits(back.regex, front) else None
    if back.kind == "mixed" and _shape(front) == back.shape:
        return _SegmentResult(EXACT, "mixed_segment", back.total)
    if back.kind == "literal":
        return _SegmentResult(POSSIBLE, "mixed_segment", False) if re.fullmatch(front_pattern, back.text) else None
    return _SegmentResult(POSSIBLE, "mixed_segment", False) if _fits(back.regex, front) else None


def _path_param_tail(front: Sequence[Sequence[Part]], back: BackendSegment) -> _SegmentResult | None:
    """A ``{name:path}`` backend segment against the frontend segments it absorbs."""
    if any(isinstance(p, Hole) for seg in front for p in seg):
        return _SegmentResult(EXACT, "param", True)
    text = "/".join(_plain(seg) for seg in front)
    return _SegmentResult(EXACT, "literal_on_param", True) if back.regex.fullmatch(text) else None


def _is_path_param(segment: BackendSegment) -> bool:
    return segment.kind == "path" and segment.shape == (None,)


def _match_path(url: FrontendUrl, compiled: CompiledRoute) -> list[_SegmentResult] | None:
    """Per-segment results when every backend segment is accounted for, else
    None. URLs with a glued or trailing hole are matched on characters instead."""
    front = url.segments
    back = compiled.segments
    results: list[_SegmentResult] = []
    for i, bseg in enumerate(back):
        if i >= len(front):
            return None
        if _is_path_param(bseg):
            return _match_path_param(front[i:], bseg, back[i + 1 :], results)
        res = _match_segment(front[i], bseg)
        if res is None:
            return None
        results.append(res)
    return results if len(front) == len(back) else None


def _match_path_param(
    front: Sequence[Sequence[Part]],
    param: BackendSegment,
    rest: Sequence[BackendSegment],
    results: list[_SegmentResult],
) -> list[_SegmentResult] | None:
    """``{name:path}`` absorbs one or more frontend segments; the backend
    segments after it must match the frontend segments that remain."""
    absorbed = len(front) - len(rest)
    if absorbed < 1:
        return None
    head = _path_param_tail(front[:absorbed], param)
    if head is None:
        return None
    out = [*results, head]
    for fseg, bseg in zip(front[absorbed:], rest, strict=True):
        res = _match_segment(fseg, bseg)
        if res is None:
            return None
        out.append(res)
    return out


def _combine(results: Sequence[_SegmentResult]) -> tuple[Confidence, MatchReason]:
    possible = [r.reason for r in results if r.confidence == POSSIBLE]
    if possible:
        return POSSIBLE, next(r for r in _POSSIBLE_REASONS if r in possible)
    reasons = {r.reason for r in results}
    return EXACT, next((r for r in _EXACT_REASONS if r in reasons), "literal")


def _toggle_slash(url: FrontendUrl) -> FrontendUrl | None:
    if url.trailing is not None or not url.segments:
        return None
    if url.segments[-1] == ():
        segments = url.segments[:-1]
        if not segments:
            return None
    else:
        segments = url.segments + ((),)
    return FrontendUrl(url.method, url.kind, segments, url.trailing, url.confidence)


@dataclass(frozen=True)
class CompiledRoutes:
    routes: tuple[CompiledRoute, ...]
    redirect_slashes: bool

    def match(self, url: FrontendUrl) -> list[Match]:
        """Every route this URL variant reaches, in dispatch order."""
        found = self._match(url)
        if not found and self.redirect_slashes:
            toggled = _toggle_slash(url)
            if toggled is not None:
                found = [Match(m.route, POSSIBLE, "redirect") for m in self._match(toggled)]
        if url.confidence == POSSIBLE:
            found = [Match(m.route, POSSIBLE, "prop_flow" if m.confidence == EXACT else m.reason) for m in found]
        return found

    def _candidates(self, url: FrontendUrl) -> list[CompiledRoute]:
        want = "websocket" if url.kind == "websocket" else "http"
        return [c for c in self.routes if c.route.kind == want and UNRESOLVED_PATH_PART not in c.route.path]

    @staticmethod
    def _method_fits(url: FrontendUrl, route: LiveRoute) -> Confidence | None:
        """None when the methods differ, else the method confidence."""
        if url.kind == "websocket":
            return EXACT
        if url.method == "UNKNOWN" or route.method == "ANY":
            return POSSIBLE
        return EXACT if url.method == route.method else None

    def _match(self, url: FrontendUrl) -> list[Match]:
        literal = url.literal
        if literal is not None:
            return self._match_literal(url, literal)
        # A trailing hole is glued to the text before it, so it too is matched
        # on characters, where convertors (int, uuid, ...) still apply.
        if url.has_glued_hole or url.trailing is not None:
            return self._match_characters(url)
        rows: list[tuple[CompiledRoute, Confidence, MatchReason, list[_SegmentResult]]] = []
        for compiled in self._candidates(url):
            method_conf = self._method_fits(url, compiled.route)
            if method_conf is None:
                continue
            results = _match_path(url, compiled)
            if results is None:
                continue
            confidence, reason = _combine(results)
            if method_conf == POSSIBLE:
                confidence, reason = POSSIBLE, "method_unknown"
            rows.append((compiled, confidence, reason, results))
            # Rows come in dispatch order and only for routes whose method fits.
            # Starlette sends every request this URL can make to the first route
            # that matches it whatever the holes hold, so later routes are
            # unreachable from it. A possible match never shadows.
            if confidence == EXACT and all(r.total for r in results):
                break
        exact = [r for r in rows if r[1] == EXACT]
        kept = exact or rows
        return [Match(c.route, conf, reason) for c, conf, reason, _ in kept]

    def _match_characters(self, url: FrontendUrl) -> list[Match]:
        """Every route that some value of the URL's holes reaches, all ``possible``.

        A glued hole may be empty or span segments, so segment-by-segment
        matching does not apply; no route can be shown to take every value, so
        none shadows a later one.
        """
        pattern = _url_tokens(url)
        shape_reason = "trailing_hole" if url.trailing is not None else "mixed_segment"
        out: list[Match] = []
        for compiled in self._candidates(url):
            method_conf = self._method_fits(url, compiled.route)
            if method_conf is None:
                continue
            if any(_intersects(pattern, alternative) for alternative in _path_tokens(compiled.route.path)):
                out.append(
                    Match(compiled.route, POSSIBLE, "method_unknown" if method_conf == POSSIBLE else shape_reason)
                )
        return out

    def _match_literal(self, url: FrontendUrl, path: str) -> list[Match]:
        """A concrete request: the first route whose path and method match wins.

        With an ``UNKNOWN`` method each method's first route is a possible match.
        """
        out: list[Match] = []
        seen_methods: set[str] = set()
        for compiled in self._candidates(url):
            if not compiled.regex.fullmatch(path):
                continue
            method_conf = self._method_fits(url, compiled.route)
            if method_conf is None:
                continue
            key = compiled.route.method
            if key in seen_methods:
                continue
            seen_methods.add(key)
            reason = "literal" if all(s.kind == "literal" for s in compiled.segments) else "literal_on_param"
            if method_conf == POSSIBLE:
                out.append(Match(compiled.route, POSSIBLE, "method_unknown"))
            else:
                out.append(Match(compiled.route, EXACT, reason))
                if url.kind == "websocket" or url.method != "UNKNOWN":
                    break
        return out


# ── Character-level matching ─────────────────────────────────────────────

# A token is (character class, min repeats, max repeats or None for unbounded).
# A class is a one-character literal or one of the names below.
_Token = tuple[str, int, int | None]
_ANY, _NOT_SLASH, _DIGIT, _HEX = "any", "not_slash", "digit", "hex"
_CLASS_NAMES = frozenset({_ANY, _NOT_SLASH, _DIGIT, _HEX})
_HEX_CHARS = frozenset("0123456789abcdef")
# Starlette's convertors as token sequences; float's optional fraction gives two.
_CONVERTOR_TOKENS: dict[str, tuple[tuple[_Token, ...], ...]] = {
    "str": (((_NOT_SLASH, 1, None),),),
    "path": (((_ANY, 0, None),),),
    "int": (((_DIGIT, 1, None),),),
    "float": (((_DIGIT, 1, None),), ((_DIGIT, 1, None), (".", 1, 1), (_DIGIT, 1, None))),
    "uuid": (
        (
            (_HEX, 8, 8),
            ("-", 1, 1),
            (_HEX, 4, 4),
            ("-", 1, 1),
            (_HEX, 4, 4),
            ("-", 1, 1),
            (_HEX, 4, 4),
            ("-", 1, 1),
            (_HEX, 12, 12),
        ),
    ),
}
# One character per class of behaviour; the literals of both sides are added per check.
_REPRESENTATIVES = frozenset("/0ag-.")


def _accepts(cls: str, char: str) -> bool:
    if cls not in _CLASS_NAMES:
        return cls == char
    if cls == _ANY:
        return True
    if cls == _NOT_SLASH:
        return char != "/"
    if cls == _DIGIT:
        return "0" <= char <= "9"
    return char in _HEX_CHARS


def _literal_tokens(text: str) -> list[_Token]:
    return [(c, 1, 1) for c in text]


def _url_tokens(url: FrontendUrl) -> tuple[_Token, ...]:
    """The URL's path as tokens: a hole that fills its segment is one or more
    characters without ``/``; a glued or trailing hole is any text."""
    tokens: list[_Token] = []
    for seg in url.segments:
        tokens.extend(_literal_tokens("/"))
        for part in seg:
            if isinstance(part, str):
                tokens.extend(_literal_tokens(part))
            else:
                tokens.append((_ANY, 0, None) if part.glued else (_NOT_SLASH, 1, None))
    if url.trailing is not None:
        tokens.append((_ANY, 0, None))
    return tuple(tokens)


def _path_tokens(path: str) -> list[tuple[_Token, ...]]:
    """A backend path as token sequences, one per combination of convertor
    alternatives; none for a convertor Starlette rejects."""
    out: list[list[_Token]] = [[]]
    last = 0
    for m in PARAM_RE.finditer(path):
        literal = _literal_tokens(path[last : m.start()])
        options = _CONVERTOR_TOKENS.get(m.group(2) or "str", ())
        out = [[*prefix, *literal, *option] for prefix in out for option in options]
        last = m.end()
    tail = _literal_tokens(path[last:])
    return [tuple([*prefix, *tail]) for prefix in out]


def _intersects(left: Sequence[_Token], right: Sequence[_Token]) -> bool:
    """Whether one string matches both token sequences: a search over pairs of
    positions. Each repeat count is capped at its minimum (or its maximum when
    bounded), which is all that decides what may follow."""
    alphabet = _REPRESENTATIVES | {cls for cls, _, _ in (*left, *right) if cls not in _CLASS_NAMES}

    def cap(token: _Token) -> int:
        return token[2] if token[2] is not None else token[1]

    start = (0, 0, 0, 0)
    seen = {start}
    stack = [start]
    while stack:
        i, a, j, b = stack.pop()
        if i == len(left) and j == len(right):
            return True
        moves = []
        if i < len(left) and a >= left[i][1]:
            moves.append((i + 1, 0, j, b))
        if j < len(right) and b >= right[j][1]:
            moves.append((i, a, j + 1, 0))
        if i < len(left) and j < len(right):
            ltok, rtok = left[i], right[j]
            l_room = ltok[2] is None or a < ltok[2]
            r_room = rtok[2] is None or b < rtok[2]
            if l_room and r_room and any(_accepts(ltok[0], c) and _accepts(rtok[0], c) for c in alphabet):
                moves.append((i, min(a + 1, cap(ltok)), j, min(b + 1, cap(rtok))))
        for state in moves:
            if state not in seen:
                seen.add(state)
                stack.append(state)
    return False
