"""FactsCache (per-file cache of the TypeScript helper's ``facts`` mode) and the helper's facts.

Cache tests drive ``FactsCache`` with a fake helper and an injected clock, so
they run without Node and never patch ``time`` globally. Helper tests run the
real ``ts/helper.cjs`` through ``ts_bridge`` (skipped, or failed under
``CODE_INTEL_REQUIRE_TOOLCHAIN=1``, when Node or TypeScript is missing).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from code_intel import code_paths, ts_bridge, ts_facts
from code_intel.code_paths import RACY_WINDOW_NS, SourceFile

NOW = 1_000_000_000_000_000_000  # injected clock value, far past every test file's mtime
# Cache tests set mtimes against NOW and rewrite files keeping their stamp to
# reach the racy content check; the kernel's ctime is past NOW and changes on
# every rewrite.
pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")


def _toolchain(node_version: str = "v24.0.0", available: bool = True) -> ts_bridge.Toolchain:
    problem = None if available else ts_bridge.ToolchainProblem("node_not_found", "node not found on PATH", "node", "x")
    return ts_bridge.Toolchain(
        node="/usr/bin/node" if available else None,
        node_version=node_version,
        typescript_dir=Path("/ts"),
        typescript_version="7.1.0-beta",
        typescript_source="env",
        lockfile_typescript_version="7.1.0-beta",
        problem=problem,
        warnings=(),
    )


def _entry(path: str, *, status: str = "ok", error: str | None = None, tag: str = "Widget", syntax: int = 0) -> dict:
    return {
        "path": path,
        "status": status,
        "error": error,
        "syntax_errors": syntax,
        "first_syntax_error": {"line": 2, "column": 5, "message": "'>' expected."} if syntax else None,
        "elements": []
        if status != "ok"
        else [{"tag": tag, "line": 1, "column": 1, "kind": "self_close", "parent": None, "snippet": f"<{tag} />"}],
        "decls": [],
    }


class FakeHelper:
    """Stands in for ``ts_bridge.run_helper``: records each request's files."""

    def __init__(self, entry: Callable[[str], dict] | None = None) -> None:
        self.requests: list[list[str]] = []
        self.attribute_tags: list[list[str]] = []
        self.entry = entry or (lambda path: _entry(path, tag=Path(path).read_text(encoding="utf-8").strip()))
        self.fail: Exception | None = None
        self.schema = ts_facts.FACTS_SCHEMA

    def __call__(self, toolchain, mode, request, *, timeout_s):
        assert mode == "facts"
        assert timeout_s == ts_bridge.FACTS_TIMEOUT_S
        self.requests.append(list(request["files"]))
        self.attribute_tags.append(request["attribute_tags"])
        if self.fail is not None:
            raise self.fail
        return {
            "protocol": 1,
            "mode": "facts",
            "typescript_version": "7.1.0-beta",
            "facts_schema": self.schema,
            "files": [self.entry(path) for path in request["files"]],
        }


def _walk(root: Path, repo_root: Path) -> list[SourceFile]:
    problems: list = []
    files = code_paths.iter_source_files(root, code_paths.TS_SUFFIXES, repo_root, problems)
    assert problems == []
    return files


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _set_mtime(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path / "frontend" / "a.tsx", "Alpha")
    _write(tmp_path / "frontend" / "b.tsx", "Beta")
    # Old enough that no entry is racy unless a test makes it so.
    for path in (tmp_path / "frontend").iterdir():
        _set_mtime(path, 1_000_000_000)
    return tmp_path


def _cache(repo: Path, helper: FakeHelper, *, clock: Callable[[], int] = lambda: NOW, toolchain=None):
    current = {"toolchain": toolchain or _toolchain()}
    cache = ts_facts.FactsCache(repo, lambda: current["toolchain"], clock=clock, run_helper=helper)
    return cache, current


# ── FactsCache ───────────────────────────────────────────────────────────


def test_facts_cache_one_helper_run_for_all_misses_then_hits(repo: Path) -> None:
    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)

    first = cache.facts_for(files)
    assert {rel: f.elements[0].tag for rel, f in first.items()} == {"frontend/a.tsx": "Alpha", "frontend/b.tsx": "Beta"}
    assert len(helper.requests) == 1 and len(helper.requests[0]) == 2

    second = cache.facts_for(files)
    assert second == first
    assert len(helper.requests) == 1
    stats = cache.stats()
    assert (stats["entries"], stats["hits"], stats["misses"], stats["helper_runs"]) == (2, 2, 2, 1)
    assert isinstance(stats["last_helper_ms"], float)


def test_facts_cache_rereads_only_the_edited_file(repo: Path) -> None:
    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    cache.facts_for(_walk(repo / "frontend", repo))
    edited = _write(repo / "frontend" / "a.tsx", "Alpha2")
    _set_mtime(edited, 2_000_000_000)

    facts = cache.facts_for(_walk(repo / "frontend", repo))
    assert helper.requests[-1] == [str(edited)]
    assert facts["frontend/a.tsx"].elements[0].tag == "Alpha2"
    assert facts["frontend/b.tsx"].elements[0].tag == "Beta"


def test_facts_cache_jsx_and_js_edits_and_any_root_invalidate(tmp_path: Path) -> None:
    """Every file is keyed by its own stamp, so `.jsx`/`.js` edits and roots
    outside frontend/ refresh on the next call."""
    helper = FakeHelper()
    cache, _ = _cache(tmp_path, helper)
    jsx = _write(tmp_path / "frontend" / "c.jsx", "Old")
    js = _write(tmp_path / "other" / "d.js", "OldJs")
    for path in (jsx, js):
        _set_mtime(path, 1_000_000_000)
    files = _walk(tmp_path / "frontend", tmp_path) + _walk(tmp_path / "other", tmp_path)
    cache.facts_for(files)

    _write(jsx, "NewJsx")
    _write(js, "NewJs")
    _set_mtime(jsx, 3_000_000_000)
    _set_mtime(js, 3_000_000_000)
    files = _walk(tmp_path / "frontend", tmp_path) + _walk(tmp_path / "other", tmp_path)
    facts = cache.facts_for(files)
    assert facts["frontend/c.jsx"].elements[0].tag == "NewJsx"
    assert facts["other/d.js"].elements[0].tag == "NewJs"


def test_facts_cache_racy_entry_detects_a_same_stamp_edit(repo: Path) -> None:
    """git's racy-clean rule with an injected clock: an entry cached within the
    window is hash-checked, so an edit that keeps the whole stamp (ctime
    follows mtime in this module, as within one ctime tick) is seen."""
    helper = FakeHelper()
    path = repo / "frontend" / "a.tsx"
    _set_mtime(path, NOW - RACY_WINDOW_NS // 2)
    cache, _ = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)
    cache.facts_for(files)

    stamp = path.stat()
    path.write_text("Gamma", encoding="utf-8")  # same length as "Alpha"
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    files = _walk(repo / "frontend", repo)
    assert files[0].stamp.mtime_ns == stamp.st_mtime_ns and files[0].stamp.size == stamp.st_size
    facts = cache.facts_for(files)
    assert facts["frontend/a.tsx"].elements[0].tag == "Gamma"
    assert helper.requests[-1] == [str(path)]


@pytest.mark.parametrize("when", ["after the helper reads", "after the hash, before the helper reads"])
def test_facts_cache_an_edit_during_a_fetch_is_seen(when: str, repo: Path) -> None:
    """A racy file is hashed before the helper reads it, so an edit at either
    point of a fetch only mismatches the stored hash: the next lookup re-reads."""
    path = repo / "frontend" / "a.tsx"
    _set_mtime(path, NOW - RACY_WINDOW_NS // 2)

    def edit() -> None:
        st = path.stat()
        path.write_text("Gamma", encoding="utf-8")  # same length as "Alpha"
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    # Its own MonkeyPatch, so ending the digest patch keeps the module's ctime pin.
    with pytest.MonkeyPatch.context() as monkeypatch:
        if when == "after the helper reads":
            read = helper.entry
            helper.entry = lambda p: (read(p), edit())[0] if p == str(path) else read(p)
        else:
            real = ts_facts.content_digest

            def digest(target: Path) -> str:
                value = real(target)
                if target == path:
                    edit()
                return value

            monkeypatch.setattr(ts_facts, "content_digest", digest)
        cache.facts_for(_walk(repo / "frontend", repo))
    facts = cache.facts_for(_walk(repo / "frontend", repo))
    assert facts["frontend/a.tsx"].elements[0].tag == "Gamma"
    assert helper.requests[-1] == [str(path)]


def test_facts_cache_racy_entry_becomes_stat_only_once_old(repo: Path) -> None:
    helper = FakeHelper()
    path = repo / "frontend" / "a.tsx"
    mtime = NOW - RACY_WINDOW_NS // 2
    _set_mtime(path, mtime)
    clock = {"now": NOW}
    cache, _ = _cache(repo, helper, clock=lambda: clock["now"])
    cache.facts_for(_walk(repo / "frontend", repo))
    entry = cache._entries[code_paths.cache_key(path)]
    assert entry.racy and entry.content_hash is not None

    clock["now"] = mtime + 2 * RACY_WINDOW_NS
    cache.facts_for(_walk(repo / "frontend", repo))
    assert (entry.racy, entry.content_hash) == (False, None)
    assert len(helper.requests) == 1


@pytest.mark.real_ctime
def test_facts_cache_sees_a_same_size_rewrite_with_its_mtime_restored_after_the_window(
    tmp_path: Path, rewrite_keeping_mtime: Callable[[Path, str], None]
) -> None:
    """An entry cached once its file is past the racy window is trusted by
    stamp alone; the stamp's ctime still catches a rewrite whose mtime, size
    and inode are all as before."""
    path = _write(tmp_path / "frontend" / "a.tsx", "Alpha")
    helper = FakeHelper()
    clock = {"now": 0}

    def settle() -> None:
        st = path.stat()
        clock["now"] = max(st.st_mtime_ns, st.st_ctime_ns) + 2 * RACY_WINDOW_NS

    settle()
    cache, _ = _cache(tmp_path, helper, clock=lambda: clock["now"])
    cache.facts_for(_walk(tmp_path / "frontend", tmp_path))
    assert not cache._entries[code_paths.cache_key(path)].racy

    rewrite_keeping_mtime(path, "Gamma")
    settle()
    facts = cache.facts_for(_walk(tmp_path / "frontend", tmp_path))
    assert facts["frontend/a.tsx"].elements[0].tag == "Gamma"
    assert helper.requests[-1] == [str(path)]


def test_facts_cache_toolchain_or_schema_change_drops_everything(repo: Path) -> None:
    helper = FakeHelper()
    cache, current = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)
    cache.facts_for(files)
    current["toolchain"] = _toolchain(node_version="v25.0.0")
    cache.facts_for(files)
    assert len(helper.requests) == 2 and len(helper.requests[1]) == 2


def test_facts_cache_evicts_deleted_files_under_the_walked_root(repo: Path) -> None:
    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    cache.facts_for(_walk(repo / "frontend", repo), root=repo / "frontend", suffixes=code_paths.TS_SUFFIXES)
    (repo / "frontend" / "b.tsx").unlink()
    cache.facts_for(_walk(repo / "frontend", repo), root=repo / "frontend", suffixes=code_paths.TS_SUFFIXES)
    assert sorted(cache._entries) == [code_paths.cache_key(repo / "frontend" / "a.tsx")]


def test_facts_cache_does_not_evict_other_suffixes_or_roots(repo: Path) -> None:
    helper = FakeHelper()
    _write(repo / "frontend" / "util.ts", "Util")
    _set_mtime(repo / "frontend" / "util.ts", 1_000_000_000)
    cache, _ = _cache(repo, helper)
    cache.facts_for(_walk(repo / "frontend", repo))
    jsx_only = [f for f in _walk(repo / "frontend", repo) if f.rel.endswith(".tsx")]
    cache.facts_for(jsx_only, root=repo / "frontend", suffixes=code_paths.JSX_SUFFIXES)
    assert len(cache._entries) == 3


def test_facts_cache_fetching_a_subset_of_a_walk_keeps_the_rest(repo: Path) -> None:
    """A JSX query reads only non-test files but walks them all, so the test
    files' entries (which the symbol index needs) stay cached."""
    _write(repo / "frontend" / "__tests__" / "c.test.tsx", "Gamma")
    _set_mtime(repo / "frontend" / "__tests__" / "c.test.tsx", 1_000_000_000)
    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    walked = _walk(repo / "frontend", repo)
    cache.facts_for(walked, root=repo / "frontend", suffixes=code_paths.TS_SUFFIXES)
    assert cache.stats()["entries"] == 3
    subset = [f for f in walked if "__tests__" not in f.rel]
    cache.facts_for(subset, root=repo / "frontend", suffixes=code_paths.TS_SUFFIXES, walked=walked)
    assert cache.stats()["entries"] == 3
    cache.facts_for(walked, root=repo / "frontend", suffixes=code_paths.TS_SUFFIXES)
    assert len(helper.requests) == 1


def test_facts_cache_read_failures_are_not_cached(repo: Path) -> None:
    """A permission fix leaves the stamp unchanged, so a cached failure would stick."""
    failing = {"on": True}

    def entry(path: str) -> dict:
        if failing["on"] and path.endswith("a.tsx"):
            return _entry(path, status="read_failed", error="EACCES: permission denied")
        return _entry(path, tag=Path(path).read_text(encoding="utf-8"))

    helper = FakeHelper(entry)
    cache, _ = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)
    first = cache.facts_for(files)
    assert first["frontend/a.tsx"].status == "read_failed"
    failing["on"] = False
    second = cache.facts_for(files)
    assert second["frontend/a.tsx"].elements[0].tag == "Alpha"
    assert helper.requests[-1] == [str(repo / "frontend" / "a.tsx")]


def test_facts_cache_failed_helper_run_caches_nothing(repo: Path) -> None:
    helper = FakeHelper()
    helper.fail = ts_bridge.HelperError("facts", "exit", "node exited with code 1: boom")
    cache, _ = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)
    with pytest.raises(ts_bridge.HelperError, match="step 'exit'"):
        cache.facts_for(files)
    assert cache.stats()["entries"] == 0
    helper.fail = None
    assert len(cache.facts_for(files)) == 2


@pytest.mark.parametrize(
    ("response_change", "step"),
    [
        ({"facts_schema": 99}, "protocol"),
        ({"files": "nope"}, "decode output"),
        ({"files": []}, "protocol"),
    ],
)
def test_facts_cache_rejects_malformed_responses(repo: Path, response_change: dict, step: str) -> None:
    def helper(toolchain, mode, request, *, timeout_s):
        body = {
            "protocol": 1,
            "mode": "facts",
            "facts_schema": ts_facts.FACTS_SCHEMA,
            "files": [_entry(p) for p in request["files"]],
        }
        body.update(response_change)
        return body

    cache = ts_facts.FactsCache(repo, _toolchain, clock=lambda: NOW, run_helper=helper)
    with pytest.raises(ts_bridge.HelperError) as info:
        cache.facts_for(_walk(repo / "frontend", repo))
    assert info.value.step == step
    assert cache.stats()["entries"] == 0


def test_facts_cache_malformed_entry_names_the_file(repo: Path) -> None:
    def helper(toolchain, mode, request, *, timeout_s):
        files = [_entry(p) for p in request["files"]]
        del files[0]["decls"]
        return {"protocol": 1, "mode": "facts", "facts_schema": ts_facts.FACTS_SCHEMA, "files": files}

    cache = ts_facts.FactsCache(repo, _toolchain, clock=lambda: NOW, run_helper=helper)
    with pytest.raises(ts_bridge.HelperError, match=r"frontend/a\.tsx: malformed entry \(KeyError"):
        cache.facts_for(_walk(repo / "frontend", repo))


def test_facts_cache_raises_toolchain_unavailable(repo: Path) -> None:
    helper = FakeHelper()
    cache, _ = _cache(repo, helper, toolchain=_toolchain(available=False))
    with pytest.raises(ts_facts.ToolchainUnavailable, match="node not found on PATH") as info:
        cache.facts_for(_walk(repo / "frontend", repo))
    assert info.value.problem.code == "node_not_found"
    assert helper.requests == []
    assert ts_facts.ToolchainUnavailable is ts_bridge.ToolchainUnavailable


def test_facts_cache_clear(repo: Path) -> None:
    helper = FakeHelper()
    cache, _ = _cache(repo, helper)
    files = _walk(repo / "frontend", repo)
    cache.facts_for(files)
    cache.clear()
    cache.facts_for(files)
    assert len(helper.requests) == 2


# ── facts_problems (one wording, named steps) ─────────────────────────────


def _facts(**fields) -> ts_facts.FileFacts:
    base: dict = dict(
        rel="frontend/x.tsx",
        status="ok",
        error=None,
        syntax_errors=0,
        first_syntax_error=None,
        elements=(),
        decls=(),
    )
    base.update(fields)
    return ts_facts.FileFacts(**base)


def test_facts_problems_name_the_file_and_step() -> None:
    read = ts_facts.facts_problems(_facts(status="read_failed", error="EACCES: permission denied"), stage="s")
    assert [(p.kind, p.step, p.file, p.message) for p in read] == [
        (
            "read_failed",
            "read file",
            "frontend/x.tsx",
            "frontend/x.tsx: could not read file (EACCES: permission denied)",
        )
    ]
    extract = ts_facts.facts_problems(_facts(status="extract_failed", error="RangeError: stack"), stage="s")
    assert [(p.kind, p.step, p.message) for p in extract] == [
        (
            "analysis_failed",
            "extract facts",
            "frontend/x.tsx: could not extract JSX and declarations (RangeError: stack)",
        )
    ]
    parse = ts_facts.facts_problems(
        _facts(status="parse_failed", error="RangeError: Maximum call stack size exceeded"), stage="s"
    )
    assert [(p.kind, p.step, p.message) for p in parse] == [
        (
            "parse_failed",
            "parse file",
            "frontend/x.tsx: could not parse file (RangeError: Maximum call stack size exceeded)",
        )
    ]
    assert ts_facts.facts_problems(_facts(), stage="s") == []


def test_facts_problems_decode_failed_keeps_the_facts_and_names_the_byte() -> None:
    facts = ts_facts.FileFacts(
        rel="frontend/latin1.tsx",
        status="decode_failed",
        error="byte 12 is not valid UTF-8; undecodable bytes were read as U+FFFD",
        syntax_errors=0,
        first_syntax_error=None,
        elements=(),
        decls=(),
    )
    [problem] = ts_facts.facts_problems(facts, stage="s")
    assert (problem.kind, problem.step, problem.file) == ("decode_failed", "decode file", "frontend/latin1.tsx")
    assert "byte 12 is not valid UTF-8" in problem.message


def test_facts_problems_typescript_syntax_errors_are_worded_consistently() -> None:
    """One wording, "TypeScript reported N syntax errors in <file>"."""
    one = ts_facts.facts_problems(
        _facts(syntax_errors=1, first_syntax_error={"line": 3, "column": 7, "message": "'}' expected."}), stage="s"
    )
    assert [(p.kind, p.step, p.line, p.is_diagnostic) for p in one] == [
        ("typescript_syntax_error", "parse file", 3, True)
    ]
    assert one[0].message == (
        "TypeScript reported 1 syntax error in frontend/x.tsx; first at line 3, column 7: '}' expected.; "
        "results come from TypeScript's recovered tree"
    )
    many = ts_facts.facts_problems(
        _facts(syntax_errors=4, first_syntax_error={"line": 1, "column": 1, "message": "x"}), stage="s"
    )
    assert many[0].message.startswith("TypeScript reported 4 syntax errors in frontend/x.tsx;")


# ── The real helper ──────────────────────────────────────────────────────


def test_helper_facts_lines_columns_snippets_and_encodings(facts_helper) -> None:
    """Lines break only at \\r\\n, \\r and \\n, as editors, rg and Python count
    them (U+2028 inside a line does not end it), columns count code points,
    snippets drop the line terminator, and UTF-16 files with a BOM are read."""
    run = facts_helper(
        {
            "frontend/lines.tsx": "const a = 1;\u2028const B = () => <Kid />;\r\nconst c = '\U0001f600'; const D = () => <Kid />;\n",
            "frontend/crlf.tsx": "export function Crlf() {\r\n  return <Kid />;\r\n}\r\n",
        }
    )
    lines = run.by_rel["frontend/lines.tsx"]
    assert lines["status"] == "ok"
    assert [(e["line"], e["column"], e["parent"]) for e in lines["elements"]] == [(1, 30, "B"), (2, 32, "D")]
    assert [(e["line"], e["snippet"]) for e in lines["elements"]][0] == (
        1,
        "const a = 1;\u2028const B = () => <Kid />;",
    )
    assert [(d["name"], d["line"], d["column"]) for d in lines["decls"]] == [
        ("a", 1, 7),
        ("B", 1, 20),
        ("c", 2, 7),
        ("D", 2, 22),
    ]
    lone_cr = facts_helper({"frontend/cr.tsx": "const a = 1;\rconst B = () => <Kid />;\n"}).by_rel["frontend/cr.tsx"]
    assert [(e["line"], e["column"]) for e in lone_cr["elements"]] == [(2, 17)]
    crlf = run.by_rel["frontend/crlf.tsx"]
    assert [e["snippet"] for e in crlf["elements"]] == ["return <Kid />;"]
    assert [(d["name"], d["line"], d["end_line"]) for d in crlf["decls"]] == [("Crlf", 1, 3)]


def test_helper_reads_utf16_bom_and_reports_read_failures(typescript_toolchain, tmp_path: Path) -> None:
    utf16 = tmp_path / "u16.tsx"
    utf16.write_bytes("\ufeffexport const U = () => <Kid />;\n".encode("utf-16-le"))
    missing = tmp_path / "missing.tsx"
    response = ts_bridge.run_helper(
        typescript_toolchain,
        "facts",
        {"files": [str(utf16), str(missing)]},
        timeout_s=ts_bridge.FACTS_TIMEOUT_S,
    )
    by_path = {f["path"]: f for f in response["files"]}
    assert [e["tag"] for e in by_path[str(utf16)]["elements"]] == ["Kid"]
    gone = by_path[str(missing)]
    assert gone["status"] == "read_failed"
    assert "ENOENT" in gone["error"] and str(missing) in gone["error"]


def test_helper_reads_odd_length_and_lone_surrogate_utf16(typescript_toolchain, tmp_path: Path) -> None:
    """An odd trailing byte is dropped and a lone surrogate replaced, as
    TypeScript 6's reader allowed: neither fails the file or the batch."""
    odd = tmp_path / "odd_be.tsx"
    odd.write_bytes("\ufeffexport const B = () => <Kid />;\n".encode("utf-16-be") + b"\x00")
    lone = tmp_path / "lone.tsx"
    lone.write_bytes(
        "\ufeffexport const s = '\ud800';\nexport const L = () => <Kid />;\n".encode("utf-16-le", "surrogatepass")
    )
    response = ts_bridge.run_helper(
        typescript_toolchain,
        "facts",
        {"files": [str(odd), str(lone)]},
        timeout_s=ts_bridge.FACTS_TIMEOUT_S,
    )
    by_path = {f["path"]: f for f in response["files"]}
    for source in (odd, lone):
        assert by_path[str(source)]["status"] == "ok"
        assert [e["tag"] for e in by_path[str(source)]["elements"]] == ["Kid"]
    assert [(e["line"], e["column"]) for e in by_path[str(lone)]["elements"]] == [(2, 24)]


def test_helper_parses_nesting_that_overflowed_the_typescript_6_parser(facts_helper) -> None:
    """The native parser has no stack limit at this depth (TypeScript 6 threw a
    RangeError), and the extractors walk the tree iteratively."""
    depth = 20_000
    run = facts_helper(
        {
            "frontend/deep.ts": f"export const x = {'[' * depth}{']' * depth};\n",
            "frontend/ok.tsx": "export const A = () => <b />;\n",
        }
    )
    deep = run.by_rel["frontend/deep.ts"]
    assert (deep["status"], deep["error"], deep["syntax_errors"]) == ("ok", None, 0)
    assert [(d["name"], d["kind"]) for d in deep["decls"]] == [("x", "const")]
    assert run.by_rel["frontend/ok.tsx"]["status"] == "ok"


def _run_probe(toolchain: ts_bridge.Toolchain, tmp_path: Path, script: str, *args: Path) -> dict:
    """Runs `script` with the helper directory and `args` as arguments and
    returns the JSON it prints."""
    probe = tmp_path / "probe.cjs"
    probe.write_text(script, encoding="utf-8")
    assert toolchain.node is not None
    proc = subprocess.run(
        [toolchain.node, str(probe), str(ts_bridge.HELPER_DIR), *map(str, args)],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


_PARSER_FAILURE_PROBE = r"""
const path = require("path");
const facts = require(path.join(process.argv[2], "facts.cjs"));
let apis = 0;
class FakeAPI {
  constructor() { apis += 1; }
  createSourceFile(file) { throw new Error(`panic in ${path.basename(file)}\ngoroutine 1 [running]:\n\tparser.go:1`); }
  close() {}
}
const ts = { version: "7.1.0-test", API: FakeAPI, ScriptKind: { TS: 3, TSX: 4, JS: 1, JSX: 2 } };
const response = facts.run(ts, { files: [process.argv[3], process.argv[4], process.argv[5]] });
process.stdout.write(JSON.stringify({ apis, files: response.files.map((f) => [f.status, f.error]) }));
"""


def test_helper_reports_a_parser_crash_as_one_parse_failed_file_and_carries_on(
    typescript_toolchain, tmp_path: Path
) -> None:
    """The compiler process can die on a file (a Go stack overflow on absurd
    nesting): that file is parse_failed with the first line of the error, the
    process is started again, and the rest of the batch is unaffected."""
    one = tmp_path / "one.ts"
    two = tmp_path / "two.ts"
    one.write_text("export const a = 1;\n", encoding="utf-8")
    two.write_text("export const b = 2;\n", encoding="utf-8")
    out = _run_probe(typescript_toolchain, tmp_path, _PARSER_FAILURE_PROBE, one, tmp_path / "gone.ts", two)
    assert out["apis"] == 3, "one at the start, one after each parser failure"
    assert out["files"][0] == ["parse_failed", "Error: panic in one.ts"]
    assert out["files"][1][0] == "read_failed"
    assert out["files"][2] == ["parse_failed", "Error: panic in two.ts"]


_COMPILER_START_FAILURE_PROBE = r"""
const path = require("path");
const { openCompiler } = require(path.join(process.argv[2], "tsapi.cjs"));
let opened = 0;
let closed = 0;
class FakeAPI {
  constructor() { opened += 1; }
  createModuleResolver() { throw new Error("resolver failed"); }
  close() { closed += 1; }
}
let error = null;
try {
  openCompiler({ API: FakeAPI }, { frontendRoot: process.cwd(), tsconfig: null, files: [] }, []);
} catch (err) {
  error = err.message;
}
process.stdout.write(JSON.stringify({ opened, closed, error }));
"""


def test_compiler_closes_the_api_it_opened_when_it_cannot_start(typescript_toolchain, tmp_path: Path) -> None:
    """An open API keeps the helper process alive until the bridge times out, so
    a compiler that fails while starting closes it before the error propagates."""
    out = _run_probe(typescript_toolchain, tmp_path, _COMPILER_START_FAILURE_PROBE)
    assert out == {"opened": 2, "closed": 2, "error": "resolver failed"}


def test_helper_reports_bytes_that_are_not_utf8_and_keeps_the_facts(typescript_toolchain, tmp_path: Path) -> None:
    latin1 = tmp_path / "latin1.tsx"
    latin1.write_bytes(b"// caf\xe9\nexport const L = () => <Kid />;\n")
    utf8 = tmp_path / "utf8.tsx"
    utf8.write_bytes("\ufeff// caf\u00e9 \U0001f600\nexport const U = () => <Kid />;\n".encode("utf-8"))
    response = ts_bridge.run_helper(
        typescript_toolchain, "facts", {"files": [str(latin1), str(utf8)]}, timeout_s=ts_bridge.FACTS_TIMEOUT_S
    )
    by_path = {f["path"]: f for f in response["files"]}
    bad = by_path[str(latin1)]
    assert (bad["status"], bad["error"]) == (
        "decode_failed",
        "byte 6 is not valid UTF-8; undecodable bytes were read as U+FFFD",
    )
    assert [d["name"] for d in bad["decls"]] == ["L"]
    good = by_path[str(utf8)]
    assert (good["status"], [d["name"] for d in good["decls"]]) == ("ok", ["U"])


def test_helper_declarations_exported_by_default_and_qualified_namespaces(facts_helper) -> None:
    run = facts_helper(
        {
            "frontend/default.ts": "const config = { a: 1 };\nexport default config;\n",
            "frontend/assign.ts": "const Bar = 1;\nexport = Bar;\n",
            "frontend/ns.ts": (
                "namespace Outer { namespace Inner { export const y = 1; } }\nnamespace A.B { export const x = 1; }\n"
            ),
        }
    )
    assert [(d["name"], d["exported"]) for d in run.by_rel["frontend/default.ts"]["decls"]] == [("config", True)]
    assert [(d["name"], d["exported"]) for d in run.by_rel["frontend/assign.ts"]["decls"]] == [("Bar", True)]
    ns = [(d["name"], d["scope"], d["container"]) for d in run.by_rel["frontend/ns.ts"]["decls"]]
    assert ns == [
        ("Outer", "module", None),
        ("Inner", "namespace", "Outer"),
        ("y", "namespace", "Outer.Inner"),
        ("A", "module", None),
        ("B", "namespace", "A"),
        ("x", "namespace", "A.B"),
    ]


def test_helper_class_expression_names_are_local_wherever_the_expression_sits(facts_helper) -> None:
    """A class expression's own name binds only inside its body, so it is never a
    module (or exported) name; members keep the class name as their container.
    A named function expression's own name is not indexed at all."""
    run = facts_helper(
        {
            "frontend/expr.ts": (
                "window.ResizeObserver = class ResizeObserver { observe() {} };\n"
                "const A = class B { run() {} };\n"
                "export const C = class D {};\n"
                "register(class E {});\n"
                "const f = function g() {};\n"
                "export { B };\n"
            ),
        }
    )
    rows = [
        (d["name"], d["kind"], d["scope"], d["container"], d["exported"])
        for d in run.by_rel["frontend/expr.ts"]["decls"]
    ]
    assert rows == [
        ("ResizeObserver", "class", "local", None, False),
        ("observe", "method", "class", "ResizeObserver", False),
        ("A", "const", "module", None, False),
        ("B", "class", "local", None, False),
        ("run", "method", "class", "B", False),
        ("C", "const", "module", None, True),
        ("D", "class", "local", None, False),
        ("E", "class", "local", None, False),
        ("f", "function", "module", None, False),
    ]


def test_helper_reports_syntax_errors_and_keeps_the_recovered_tree(facts_helper) -> None:
    run = facts_helper({"frontend/broken.tsx": "export function Broken() {\n  return <Kid>;\n}\n"})
    broken = run.by_rel["frontend/broken.tsx"]
    assert broken["status"] == "ok"
    assert broken["syntax_errors"] > 0
    assert broken["first_syntax_error"]["line"] >= 2
    assert [d["name"] for d in broken["decls"]] == ["Broken"]
    assert [e["tag"] for e in broken["elements"]] == ["Kid"]


def test_helper_javascript_jsdoc_does_not_change_declarations(facts_helper) -> None:
    """TypeScript 7 reparses JSDoc in JavaScript into nodes of its own (a `@type`
    becomes a return type, `@private` a modifier, `@typedef` a type alias). They
    sit in a comment, so signatures, members and kinds are what the code says."""
    run = facts_helper(
        {
            "frontend/jsdoc.js": (
                "/**\n * @type {Handler}\n */\n"
                "function typed(a, b) {\n  return a;\n}\n"
                "/** @typedef {{ x: number }} Point */\n"
                "class K {\n  /** @private */\n  hidden() {}\n  /** @type {Fn} */\n  #m(x) { return x; }\n}\n"
                "const o = { /** @type {Fn} */ run(x) { return x; } };\n"
            ),
            "frontend/weird.d.d.ts": "declare const X: number;\n",
        }
    )
    jsdoc = run.by_rel["frontend/jsdoc.js"]
    assert jsdoc["status"] == "ok"
    assert [(d["name"], d["kind"], d["signature"]) for d in jsdoc["decls"]] == [
        ("typed", "function", "function typed(a, b)"),
        ("K", "class", "class K"),
        ("hidden", "method", "hidden()"),
        ("#m", "method", "#m(x)"),
        ("o", "const", "const o = { /** @type {Fn} */ run(x) { return x; } }"),
        ("run", "function", "run(x)"),
    ]
    weird = run.by_rel["frontend/weird.d.d.ts"]
    assert (weird["status"], weird["syntax_errors"]) == ("ok", 0)


def test_helper_javascript_with_type_annotations_is_not_a_parse_error(facts_helper) -> None:
    """The parser accepts annotations in a `.js` file; "can only be used in
    TypeScript files" is a grammar check of the language service, not a parse error."""
    run = facts_helper({"frontend/flow.js": "export function f(a: number): string { return String(a); }\n"})
    flow = run.by_rel["frontend/flow.js"]
    assert (flow["status"], flow["syntax_errors"]) == ("ok", 0)
    assert [d["name"] for d in flow["decls"]] == ["f"]


def test_helper_snippet_and_signature_limits_count_code_points(facts_helper) -> None:
    long_name = "x" * 300
    run = facts_helper(
        {"frontend/long.tsx": f"export const {long_name} = () => <Kid a='{'é' * 600}' />;\n"},
        snippet_max=50,
        signature_max=40,
    )
    facts = run.by_rel["frontend/long.tsx"]
    assert len(facts["elements"][0]["snippet"]) == 50
    assert len(facts["decls"][0]["signature"]) == 40


def test_facts_cache_sends_the_attribute_tags_and_reads_attributes_and_ancestors(repo: Path) -> None:
    def entry(path: str) -> dict:
        row = {"tag": "Route", "line": 3, "column": 5, "kind": "open", "parent": "App", "snippet": "<Route>"}
        inner = {**row, "line": 4, "attributes": {"path": "assets"}, "ancestor": {"line": 3, "column": 5}}
        return {**_entry(path), "elements": [{**row, "attributes": {"path": "/"}, "ancestor": None}, inner]}

    helper = FakeHelper(entry)
    cache = ts_facts.FactsCache(
        repo, lambda: _toolchain(), run_helper=helper, attribute_tags=("Route", "Link", "Route")
    )
    outer, inner = cache.facts_for(_walk(repo / "frontend", repo))["frontend/a.tsx"].elements
    assert helper.attribute_tags == [["Link", "Route"]]
    assert (outer.attributes, outer.ancestor) == ({"path": "/"}, None)
    assert (inner.attributes, inner.ancestor) == ({"path": "assets"}, (3, 5))
    plain = ts_facts.FactsCache(repo, lambda: _toolchain(), run_helper=helper)
    plain.facts_for(_walk(repo / "frontend", repo))
    assert helper.attribute_tags[-1] == []


_ROUTES_SOURCE = """\
export const App = () => (
  <Routes>
    <Route path="/" element={<Layout />}>
      <Route index element={<Home />} />
      <Route path="assets" element={<Assets />} />
      <Route path={"items"}>
        <Route path={`:id`} element={<Item />} />
      </Route>
      <Route path={dynamic} element={<Dyn />} />
      <Route element={<Shell />}>
        <Route path="deep" element={<Route path="odd" />} />
      </Route>
    </Route>
    <Route path="*" element={<NotFound />} data-x='a "b"' xlink:href="#h" />
  </Routes>
);
"""


def test_helper_attribute_tags_give_string_attributes_and_the_nearest_same_tag_ancestor(facts_helper) -> None:
    """Only configured tags carry the extra fields. A literal attribute is a
    fact whatever its spelling; a dynamic one is not. The ancestor is the
    nearest enclosing element of the same tag: a self-closing element
    encloses nothing, a closed one no longer encloses, and an element inside
    an attribute is not inside the element's children."""
    run = facts_helper({"routes.tsx": _ROUTES_SOURCE}, attribute_tags=("Route",))
    elements = run.by_rel["routes.tsx"]["elements"]
    routes = [e for e in elements if e["tag"] == "Route"]
    assert all("attributes" not in e and "ancestor" not in e for e in elements if e["tag"] != "Route")
    assert [(e["line"], e["attributes"], e["ancestor"] and e["ancestor"]["line"]) for e in routes] == [
        (3, {"path": "/"}, None),
        (4, {}, 3),
        (5, {"path": "assets"}, 3),
        (6, {"path": "items"}, 3),
        (7, {"path": ":id"}, 6),
        (9, {}, 3),
        (10, {}, 3),
        (11, {"path": "deep"}, 10),
        (11, {"path": "odd"}, 10),
        (14, {"path": "*", "data-x": 'a "b"', "xlink:href": "#h"}, None),
    ]
    assert routes[4]["ancestor"] == {"line": 6, "column": 7}
    assert routes[1]["ancestor"] == {"line": routes[0]["line"], "column": routes[0]["column"]}


def test_helper_without_attribute_tags_adds_nothing_and_rejects_bad_tags(facts_helper, typescript_toolchain) -> None:
    run = facts_helper({"routes.tsx": _ROUTES_SOURCE})
    assert all("attributes" not in e and "ancestor" not in e for e in run.by_rel["routes.tsx"]["elements"])
    assert typescript_toolchain.typescript_dir is not None
    for bad in ("Route", [""], [1]):
        with pytest.raises(ts_bridge.HelperError, match="attribute_tags must be an array of non-empty tag names"):
            ts_bridge.run_helper(
                typescript_toolchain, "facts", {"files": [], "attribute_tags": bad}, timeout_s=ts_bridge.FACTS_TIMEOUT_S
            )
