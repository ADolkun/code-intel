"""``code-intel doc-check``: the docs against the code, plus the agent instruction files.

Checks links, anchors, repository paths, backend routes, frontend pages, code
symbols and orphaned docs in the config's ``[docs]`` scope, and the files under
``[agents]``. It needs only Python, git and markdown-it-py, plus Node and the
shared TypeScript install (``code-intel setup``) for react-router pages and
frontend definition locations.

Standard output holds only the findings, one ``path:line: message`` line each
(or, with ``--json``, one JSON document). Everything else goes to standard
error: the incomplete banner, the analysis limitations (informational) and a
one-line summary.

Exit codes:

- 0: no findings (limitations do not count, a missing Node or TypeScript included);
- 1: findings;
- 2: the check could not run (interpreter older than the backend's, a git
  failure, a config error, an analyzer error) or the report is incomplete (a
  diagnostic: a file could not be read, decoded or parsed), whether or not
  there are findings, because an incomplete report can hide findings.

No GitHub workflow commands (``::error``) are printed: messages quote doc
text, which a pull request controls.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import TYPE_CHECKING, TextIO

from code_intel import analysis_runtime, doc_presenters, doc_queries, presenters
from code_intel.analysis_runtime import AnalysisRuntime
from code_intel.config import ConfigFile

if TYPE_CHECKING:
    from code_intel.cli import Context

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_CANNOT_RUN = 2

PROG = "doc-check"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as one JSON document")


def _summary(result: doc_queries.DocCheck) -> str:
    noun = "finding" if len(result.findings) == 1 else "findings"
    references = f"{result.reference_count} references, {result.checked_count} checked"
    state = "" if result.complete else "; INCOMPLETE"
    return f"{PROG}: {len(result.findings)} {noun} in {result.doc_count} docs ({references}){state}"


def check(runtime: AnalysisRuntime, out: TextIO, err: TextIO, *, as_json: bool = False) -> int:
    """Run the check on ``runtime``'s checkout and print it; returns the exit code."""
    try:
        if not runtime.runtime_compatible():
            running, required = runtime.runtime_versions()
            print(
                f"{PROG}: Python {running} is older than the backend's {required}; the backend's sources do not "
                f"parse on it, so routes and symbols would be missed. Run with Python >={required}.",
                file=err,
            )
            return EXIT_CANNOT_RUN
        found = runtime.get_doc_report()
        agent_findings = runtime.get_agent_findings()
    except analysis_runtime.AnalysisFailed as exc:
        print(f"{PROG}: {exc.message}", file=err)
        return EXIT_CANNOT_RUN
    except Exception as exc:
        # Anything else is a bug in the check, not drift in the docs: name the
        # crash frame so it can be fixed, and fail rather than pass.
        print(f"{PROG}: internal error: {type(exc).__name__}: {exc}", file=err)
        traceback.print_exception(exc, file=err)
        return EXIT_CANNOT_RUN

    result = doc_queries.doc_check(found.report, agent_findings)
    if as_json:
        body = doc_presenters.doc_check(result)
        print(presenters.envelope(body, "json", stale=False, analysis=result.analysis), file=out)
    else:
        findings, notes = doc_presenters.doc_check_lines(result)
        for line in findings:
            print(line, file=out)
        for line in notes:
            print(line, file=err)
    print(_summary(result), file=err)
    if not result.complete:
        return EXIT_CANNOT_RUN
    return EXIT_FINDINGS if result.findings else EXIT_OK


def run(args: argparse.Namespace, context: Context) -> int:
    config_file = ConfigFile.locate(context.repo_root, args.config)
    runtime = AnalysisRuntime(context.repo_root, config_file, [])
    return check(runtime, sys.stdout, sys.stderr, as_json=args.json)
