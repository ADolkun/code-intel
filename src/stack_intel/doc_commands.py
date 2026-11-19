"""The commands a code span or a shell line runs: ``package.json`` scripts
through npm, pnpm, yarn or bun, make targets and mise tasks.

Each runner's arguments are read the way that runner reads them (from its
sources and documentation: npm 11, pnpm 10-12, yarn 1 and 4, bun 1.3+, GNU make
4.4, mise 2026.x) into ``CommandMention``s, one per script, target or task. A form that runs nothing the repository defines
(``npm ci``, a bare ``make``) gives no mention. A form whose outcome the
definition files cannot settle carries the reason, so it ends ``not_checked``
and never ``missing``. ``doc_resolvers.CommandResolver`` resolves the mentions.

The line is split into commands at ``&&``, ``||``, ``;``, ``|``, ``&`` and the
parentheses of a subshell. A ``cd <dir>`` applies to the commands after it until
the subshell it is in closes.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from stack_intel.doc_rules import is_placeholder_path

PACKAGE_MANIFEST = "package.json"

Runner = Literal["npm", "pnpm", "yarn", "bun", "make", "mise"]
PACKAGE_RUNNERS: tuple[Runner, ...] = ("npm", "pnpm", "yarn", "bun")

# How a mention resolves: `check` is ok or missing; `unproven` is ok when the
# name is defined, else not_checked; `unchecked` is not_checked without a
# lookup; `skipped` (a placeholder or a wildcard) is never looked up.
Verdict = Literal["check", "unproven", "unchecked", "skipped"]


@dataclass(frozen=True)
class CommandMention:
    """One script, target or task a command runs.

    ``text``: the command as written, from the runner to the end of the command.
    ``directory``: the repository-relative directory a ``cd``, ``--prefix``,
    ``-C``, ``--dir``, ``--cwd`` or ``--cd`` names (``""`` for the repository
    root), None when it names none or the name is not a repository path (``..``,
    ``~/x``, ``$APP``). ``overrides``: the variables a make command line sets.
    """

    runner: Runner
    name: str
    directory: str | None
    text: str
    verdict: Verdict = "check"
    reason: str | None = None
    overrides: frozenset[str] = field(default=frozenset())


# ── Shell lines ──────────────────────────────────────────────────────────

_QUOTES = "\"'"
_PROMPTS = frozenset({"$", "%"})
# Words that run the command after them (with their own options).
_WRAPPERS = frozenset({"sudo", "time", "exec", "command", "nohup", "env", "nice"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_RE = re.compile(r"\d*(?:&>>?|>>?&?|>\||<<<|<<-?|<>|<&?)")
_PLACEHOLDER_WORD_RE = re.compile(r"<[^<>\s]+>")

# A `cd` to a path that is not a repository path: later relative `cd`s stay unknown.
_LOST = "\0"


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in _QUOTES:
        return token[1:-1]
    return token


def _repo_dir(raw: str) -> str | None:
    """A repository-relative directory, None for one outside the repository or not literal."""
    path = posixpath.normpath(_unquote(raw))
    if path.startswith(("/", "~", "..")) or is_placeholder_path(path) or any(ch in path for ch in "\"'`$%*?"):
        return None
    return "" if path == "." else path


def _join_dir(cwd: str | None, raw: str) -> str:
    """The directory ``raw`` names from ``cwd`` (None: wherever the reader is)."""
    inner = _repo_dir(raw)
    if inner is None or cwd == _LOST:
        return _LOST
    if not cwd:
        return inner
    joined = posixpath.normpath(posixpath.join(cwd, inner)) if inner else cwd
    return _LOST if joined.startswith("..") else joined


def _final_dir(directory: str | None) -> str | None:
    return None if directory == _LOST else directory


@dataclass
class _Command:
    """One command of a line: its words, where each starts and ends, and where it runs."""

    words: list[str]
    spans: list[tuple[int, int]]
    line: str
    cwd: str | None

    def text_from(self, index: int) -> str:
        return self.line[self.spans[index][0] : self.spans[-1][1]]


def _separator_at(line: str, index: int) -> str | None:
    char = line[index]
    pair = line[index : index + 2]
    if pair in ("&&", "||", "|&"):
        return pair
    if char in ";|()`":
        return char
    if char == "&" and line[index - 1 : index] not in ("<", ">") and line[index + 1 : index + 2] != ">":
        return char
    return None


def _scan(line: str) -> Iterator[tuple[str, int, int] | str]:
    """The words of ``line`` as ``(word, start, end)`` and its separators as
    strings. Quotes and ``$(...)`` keep spaces and separators inside one word;
    an unquoted ``#`` that starts a word ends the line."""
    index = 0
    start: int | None = None
    quote: str | None = None
    while index < len(line):
        char = line[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "$" and line[index + 1 : index + 2] in ("(", "{"):
            start = index if start is None else start
            index = _closing(line, index + 1) + 1
            continue
        separator = _separator_at(line, index)
        if char.isspace() or separator is not None:
            if start is not None:
                yield line[start:index], start, index
                start = None
            if separator is not None:
                yield separator
                index += len(separator)
            else:
                index += 1
            continue
        if char == "#" and start is None:
            return
        if start is None:
            start = index
        if char in _QUOTES:
            quote = char
        index += 1
    if start is not None:
        yield line[start:], start, len(line)


def _closing(line: str, index: int) -> int:
    """The index of the bracket closing the one at ``index``, or the line's last index."""
    opener = line[index]
    closer = ")" if opener == "(" else "}"
    depth = 0
    for position in range(index, len(line)):
        if line[position] == opener:
            depth += 1
        elif line[position] == closer:
            depth -= 1
            if depth == 0:
                return position
    return len(line) - 1


def _commands(line: str) -> Iterator[_Command]:
    cwd: str | None = None
    stack: list[str | None] = []
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    redirect_target = False
    for item in [*_scan(line), ";"]:
        if not isinstance(item, str):
            word = item[0]
            redirect = _REDIRECT_RE.match(word) if _PLACEHOLDER_WORD_RE.fullmatch(word) is None else None
            if redirect_target or redirect is not None:
                redirect_target = redirect is not None and redirect.end() == len(word)
                continue
            words.append(word)
            spans.append((item[1], item[2]))
            continue
        redirect_target = False
        if words and words[0] == "cd":
            cwd = _join_dir(cwd, words[1]) if len(words) == 2 else _LOST
        elif words:
            yield _Command(words, spans, line, cwd)
        words, spans = [], []
        if item == "(":
            stack.append(cwd)
        elif item == ")":
            cwd = stack.pop() if stack else cwd


def _runner_at(words: Sequence[str]) -> int | None:
    """The index of the runner word, when the command runs one: after a prompt,
    variable assignments and wrappers such as ``sudo``."""
    index = 1 if words and words[0] in _PROMPTS else 0
    wrapped = False
    while index < len(words):
        word = words[index]
        if word in _PARSERS:
            return index
        if word in _WRAPPERS:
            wrapped = True
        elif not (_ASSIGNMENT_RE.match(word) or (wrapped and word.startswith("-"))):
            return None
        index += 1
    return None


# ── Names ────────────────────────────────────────────────────────────────

_WILDCARD_CHARS = "*?[{"


def _placeholder(name: str) -> bool:
    return is_placeholder_path(name) or name.startswith("$") or name in ("-", "--")


def _named(
    runner: Runner,
    name: str,
    directory: str | None,
    text: str,
    verdict: Verdict = "check",
    reason: str | None = None,
    overrides: frozenset[str] = frozenset(),
) -> CommandMention:
    name = _unquote(name)
    if verdict != "unchecked" and (_placeholder(name) or any(ch in name for ch in _WILDCARD_CHARS)):
        verdict, reason = "skipped", "a placeholder or a wildcard, not a name"
    elif directory == _LOST and verdict == "check":
        verdict, reason = "unproven", "runs in a directory outside the repository or not written literally"
    return CommandMention(runner, name, _final_dir(directory), text, verdict, reason, overrides)


def _option_value(words: Sequence[str], index: int, flag: str) -> tuple[str | None, int]:
    """The value of ``flag`` at ``words[index]`` (``--flag=v`` or ``--flag v``) and the next index."""
    word = words[index]
    if word.startswith(flag + "="):
        return word[len(flag) + 1 :], index + 1
    if index + 1 < len(words):
        return words[index + 1], index + 2
    return None, index + 1


# ── npm ──────────────────────────────────────────────────────────────────

_NPM_RUN = frozenset({"run", "run-script", "rum", "urn"})
_NPM_LIFECYCLE = {"test": "test", "t": "test", "tst": "test", "start": "start", "stop": "stop"}
# npm defines these itself when package.json does not.
_NPM_BUILT_IN_SCRIPTS = frozenset({"restart", "env"})
_NPM_WORKSPACE_VALUED = frozenset({"-w", "--workspace"})
_NPM_WORKSPACE_FLAGS = frozenset({"--workspaces", "-ws", "--include-workspace-root"})


def _npm(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    command = name = None
    directory = cwd
    workspace = if_present = False
    index = 0
    while index < len(words):
        word = words[index]
        flag = word.split("=", 1)[0]
        if word == "--":
            break
        if flag in _NPM_WORKSPACE_VALUED:
            workspace = True
            index += 1 if "=" in word else 2
            continue
        if flag in _NPM_WORKSPACE_FLAGS:
            workspace = True
        elif flag == "--if-present":
            if_present = True
        elif flag == "--prefix":
            value, index = _option_value(words, index, "--prefix")
            if value is not None:
                directory = _join_dir(cwd, value)
            continue
        elif word.startswith("-"):
            pass
        elif command is None:
            command = word
            if word in _NPM_LIFECYCLE:
                name = _NPM_LIFECYCLE[word]
            elif word not in _NPM_RUN:
                return []
        elif name is None:
            name = word
        index += 1
    if name is None or name in _NPM_BUILT_IN_SCRIPTS:
        return []
    if workspace:
        return [_named("npm", name, directory, text, "unchecked", "runs in the workspaces' own package.json files")]
    if if_present:
        return [_named("npm", name, directory, text, "unproven", "--if-present passes when the script is missing")]
    return [_named("npm", name, directory, text)]


# ── pnpm ─────────────────────────────────────────────────────────────────

_PNPM_RUN = frozenset({"run", "run-script"})
_PNPM_TEST = frozenset({"test", "t", "tst"})
_PNPM_SELECTORS = frozenset({"-r", "--recursive", "-w", "--workspace-root", "--filter", "-F", "--filter-prod"})
_PNPM_VALUED_SELECTORS = frozenset({"--filter", "-F", "--filter-prod"})
_PNPM_COMMANDS = frozenset(
    """add install i update up upgrade remove rm uninstall un link ln unlink import rebuild rb prune fetch
    install-test it patch patch-commit patch-remove dedupe audit list ls ll la outdated why licenses exec dlx
    create store root bin setup init deploy pack publish server env config c get set doctor self-update
    approve-builds ignored-builds cat-file cat-index find-hash help recursive multi m sbom cache completion
    login logout whoami version view info owner dist-tag docs bugs search ping star unstar stars token
    access profile unpublish deprecate undeprecate adduser fund restart catalog runtime with""".split()
)


def _pnpm(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    directory = cwd
    selected = if_present = False
    command = None
    index = 0
    while index < len(words):
        word = words[index]
        flag = word.split("=", 1)[0]
        if word == "--":
            break
        if flag in ("-C", "--dir"):
            value, index = _option_value(words, index, flag)
            if value is not None:
                directory = _join_dir(cwd, value)
            continue
        if flag in _PNPM_SELECTORS:
            selected = True
            index += 2 if flag in _PNPM_VALUED_SELECTORS and "=" not in word else 1
            continue
        if flag == "--if-present":
            if_present = True
        elif word.startswith("-"):
            pass
        elif command is None:
            command = word
            if word not in _PNPM_RUN:
                break
        else:
            return [_pnpm_mention(word, directory, text, selected, if_present)]
        index += 1
    if command is None or command in _PNPM_RUN:
        return []
    if command in _PNPM_COMMANDS:
        return []
    if selected:
        return [_named("pnpm", command, directory, text, "unchecked", "runs in the selected workspace projects")]
    if command in _PNPM_TEST:
        return [_named("pnpm", "test", directory, text, "unproven", "pnpm test does not report a missing script")]
    if command == "start":
        return [_named("pnpm", "start", directory, text)]
    return [_named("pnpm", command, directory, text, "unproven", "pnpm runs `pnpm exec` when no script has that name")]


def _pnpm_mention(name: str, directory: str | None, text: str, selected: bool, if_present: bool) -> CommandMention:
    if selected:
        return _named("pnpm", name, directory, text, "unchecked", "runs in the selected workspace projects")
    if len(name) > 1 and name.startswith("/") and name.endswith("/"):
        return _named("pnpm", name, directory, text, "unchecked", "a /regex/ selects scripts by pattern")
    if if_present:
        return _named("pnpm", name, directory, text, "unproven", "--if-present passes when the script is missing")
    return _named("pnpm", name, directory, text)


# ── yarn and bun ─────────────────────────────────────────────────────────

_YARN_COMMANDS = frozenset(
    """add audit autoclean bin cache check config constraints create dedupe dlx exec explain generate-lock-entry
    global help import info init install licenses link list login logout node npm outdated owner pack patch
    patch-commit plugin policies publish rebuild remove search set stage tag team unlink unplug up upgrade
    upgrade-interactive version versions why""".split()
)
_YARN_REASON = "yarn runs a dependency's binary of that name too"
_BUN_COMMANDS = frozenset(
    """test x exec repl install i add a remove rm update outdated audit info why link unlink publish patch pm
    build init create c upgrade help completions ci feedback""".split()
)
_BUN_REASON = "bun runs a source file, a package binary or a system command of that name too"


def _first_name(words: Sequence[str], start: int) -> str | None:
    for word in words[start:]:
        if word == "--":
            return None
        if not word.startswith("-"):
            return word
    return None


def _yarn(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    directory = cwd
    index = 0
    while index < len(words):
        word = words[index]
        if word.split("=", 1)[0] == "--cwd":
            value, index = _option_value(words, index, "--cwd")
            if value is not None:
                directory = _join_dir(cwd, value)
            continue
        if word == "--":
            return []
        if not word.startswith("-"):
            break
        index += 1
    else:
        return []
    command = words[index]
    if command in ("workspace", "workspaces"):
        if command == "workspace":
            rest = list(words[index + 2 :])
            name = _first_name(rest[1:] if rest[:1] == ["run"] else rest, 0)
        else:
            name = _first_name(words, words.index("run", index) + 1) if "run" in words[index:] else None
        if name is None:
            return []
        return [_named("yarn", name, directory, text, "unchecked", "runs in another workspace's package.json")]
    if command in _YARN_COMMANDS:
        return []
    name = _first_name(words, index + 1) if command == "run" else command
    if name is None:
        return []
    return [_named("yarn", name, directory, text, "unproven", _YARN_REASON)]


def _bun(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    directory = cwd
    selected = False
    index = 0
    while index < len(words):
        word = words[index]
        flag = word.split("=", 1)[0]
        if flag == "--cwd":
            value, index = _option_value(words, index, "--cwd")
            if value is not None:
                directory = _join_dir(cwd, value)
            continue
        if flag in ("--filter", "-F"):
            selected = True
            index += 1 if "=" in word else 2
            continue
        if word == "--":
            return []
        if not word.startswith("-"):
            break
        index += 1
    else:
        return []
    command = words[index]
    if command in _BUN_COMMANDS:
        return []
    name = _first_name(words, index + 1) if command == "run" else command
    if name is None or "/" in name or posixpath.splitext(name)[1]:
        return []
    if selected:
        return [_named("bun", name, directory, text, "unchecked", "runs in the selected workspace packages")]
    return [_named("bun", name, directory, text, "unproven", _BUN_REASON)]


# ── make ─────────────────────────────────────────────────────────────────

_MAKE_FLAGS = frozenset("BdeiknpqrRsStwL")
_MAKE_LONG_FLAGS = frozenset(
    """--always-make --environment-overrides --ignore-errors --keep-going --just-print --dry-run --recon
    --print-data-base --question --no-builtin-rules --no-builtin-variables --silent --quiet --no-silent --stop
    --no-keep-going --touch --trace --print-directory --no-print-directory --warn-undefined-variables
    --check-symlink-times --debug --output-sync --jobserver-style --shuffle""".split()
)
_MAKE_COUNTED = frozenset({"--jobs", "--load-average", "--max-load"})
_MAKE_OPAQUE = frozenset(
    """--file --makefile --include-dir --eval --old-file --assume-old --what-if --new-file --assume-new""".split()
)
_MAKE_OPAQUE_SHORT = frozenset("fIoWE")
_MAKE_INFO = frozenset({"--help", "--version", "-h", "-v"})
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class _MakeArgs:
    """``make``'s arguments: goals, the ``-C`` directory, the variables set,
    and the first option that makes its makefile unknowable."""

    def __init__(self, words: Sequence[str], cwd: str | None) -> None:
        self.goals: list[str] = []
        self.directory = cwd
        self.overrides: set[str] = set()
        self.opaque: str | None = None
        self.info = False
        self._read(words)

    def _set_dir(self, value: str) -> None:
        self.directory = _join_dir(self.directory, value)

    def _unknowable(self, option: str, reason: str) -> None:
        if self.opaque is None:
            self.opaque = f"{option} {reason}"

    def _read(self, words: Sequence[str]) -> None:
        index = 0
        options = True
        while index < len(words):
            word = words[index]
            index += 1
            if options and word == "--":
                options = False
            elif options and word.startswith("--"):
                index = self._long(word, words, index)
            elif options and word.startswith("-") and len(word) > 1:
                index = self._short(word, words, index)
            elif "=" in word:
                self.overrides.add(word.split("=", 1)[0].rstrip(":+?!"))
            else:
                self.goals.append(word)

    def _long(self, word: str, words: Sequence[str], index: int) -> int:
        flag = word.split("=", 1)[0]
        if flag in _MAKE_INFO:
            self.info = True
        elif flag == "--directory":
            value, index = _option_value(words, index - 1, flag)
            if value is not None:
                self._set_dir(value)
        elif flag in _MAKE_OPAQUE:
            self._unknowable(flag, "names makefiles or files the docs check cannot read")
            if "=" not in word:
                index += 1
        elif flag in _MAKE_COUNTED:
            if "=" not in word and index < len(words) and _NUMBER_RE.fullmatch(words[index]):
                index += 1
        elif flag not in _MAKE_LONG_FLAGS:
            self._unknowable(flag, "is an option the docs check does not know")
        return index

    def _short(self, word: str, words: Sequence[str], index: int) -> int:
        for position, char in enumerate(word[1:], start=1):
            rest = word[position + 1 :]
            if char in "hv":
                self.info = True
            elif char in _MAKE_FLAGS:
                continue
            elif char in "jl":
                if not rest and index < len(words) and _NUMBER_RE.fullmatch(words[index]):
                    index += 1
                elif rest and not _NUMBER_RE.fullmatch(rest):
                    continue
                return index
            elif char == "O":
                return index
            elif char == "C":
                value = rest or (words[index] if index < len(words) else None)
                if not rest:
                    index += 1
                if value is not None:
                    self._set_dir(value)
                return index
            elif char in _MAKE_OPAQUE_SHORT:
                self._unknowable(f"-{char}", "names makefiles or files the docs check cannot read")
                return index if rest else index + 1
            else:
                self._unknowable(f"-{char}", "is an option the docs check does not know")
                return index
        return index


def _make(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    args = _MakeArgs(words, cwd)
    if args.info:
        return []
    overrides = frozenset(args.overrides)
    if args.opaque is not None:
        return [_named("make", goal, args.directory, text, "unchecked", args.opaque) for goal in args.goals]
    return [_named("make", goal, args.directory, text, overrides=overrides) for goal in args.goals]


# ── mise ─────────────────────────────────────────────────────────────────

_MISE_RUN = frozenset({"run", "r"})
_MISE_QUIET = frozenset({"-q", "--quiet", "-v", "--verbose", "-y", "--yes"})
_MISE_SHORT_QUIET = frozenset("qvy")


class _MiseArgs:
    def __init__(self, cwd: str | None) -> None:
        self.directory = cwd
        self.option: str | None = None

    def option_at(self, words: Sequence[str], index: int) -> int | None:
        """The index after the option at ``index``, None when it is not an option."""
        word = words[index]
        if not word.startswith("-") or word == "-":
            return None
        flag = word.split("=", 1)[0]
        if flag in ("-C", "--cd"):
            value, after = _option_value(words, index, flag)
            if value is not None:
                self.directory = _join_dir(self.directory, value)
            return after
        if flag.startswith("-C") and not flag.startswith("--"):
            self.directory = _join_dir(self.directory, word[2:])
            return index + 1
        if flag in _MISE_QUIET or (not flag.startswith("--") and set(flag[1:]) <= _MISE_SHORT_QUIET):
            return index + 1
        if self.option is None:
            self.option = flag
        return index + 1


def _is_task_shorthand(word: str) -> bool:
    return ":" in word or word.startswith("//")


def _mise_task(name: str, args: _MiseArgs, text: str) -> CommandMention:
    name = _unquote(name)
    if "..." in name or _placeholder(name) or any(ch in name for ch in _WILDCARD_CHARS):
        return CommandMention("mise", name, _final_dir(args.directory), text, "skipped", "a wildcard or a placeholder")
    if name.startswith("node:"):
        return _named("mise", name, args.directory, text, "unchecked", "a workspace-graph task from a package.json")
    if name.startswith(("./", "../")) or (name.startswith("/") and not name.startswith("//")):
        return _named("mise", name, args.directory, text, "unchecked", "runs a file, not a task")
    if args.option is not None:
        return _named(
            "mise", name, args.directory, text, "unchecked", f"{args.option} is an option the docs check does not know"
        )
    return _named("mise", name, args.directory, text)


def _mise(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    args = _MiseArgs(cwd)
    index = 0
    while index < len(words) and (after := args.option_at(words, index)) is not None:
        index = after
    if index >= len(words):
        return []
    command = words[index]
    if command in _MISE_RUN:
        start = index + 1
    elif command in ("tasks", "task") and index + 1 < len(words) and words[index + 1] in _MISE_RUN:
        start = index + 2
    elif _is_task_shorthand(command):
        start = index
    else:
        return []
    out: list[CommandMention] = []
    expecting = True
    index = start
    while index < len(words):
        word = words[index]
        if word == ":::":
            expecting = True
            index += 1
        elif word == "--":
            expecting = False
            index += 1
        elif expecting and (after := args.option_at(words, index)) is not None:
            index = after
        elif expecting:
            out.append(_mise_task(word, args, text))
            expecting = False
            index += 1
        else:
            index += 1
    return out


# ── Lines ────────────────────────────────────────────────────────────────

_PARSERS: dict[str, Callable[[Sequence[str], str | None, str], list[CommandMention]]] = {
    "npm": _npm,
    "pnpm": _pnpm,
    "yarn": _yarn,
    "bun": _bun,
    "make": _make,
    "gmake": _make,
    "mise": _mise,
}


def command_mentions(text: str) -> list[CommandMention]:
    """Every script, target or task a code span or a shell line runs."""
    out: list[CommandMention] = []
    for line in text.split("\n"):
        for command in _commands(line):
            index = _runner_at(command.words)
            if index is not None:
                parse = _PARSERS[command.words[index]]
                out.extend(parse(command.words[index + 1 :], command.cwd, command.text_from(index)))
    return out
