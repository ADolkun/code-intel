"""The commands a code span or a shell line runs: ``package.json`` scripts
through npm, pnpm, yarn or bun, make targets and mise tasks.

Each runner's arguments are read the way that runner reads them (from its
sources and documentation: npm 11, pnpm 10-12, yarn 1 and 4, bun 1.3+,
GNU make 4.4, mise 2026.x) into ``CommandMention``s, one per script, target
or task. A form that runs nothing the repository defines
(``npm ci``, a bare ``make``) gives no mention. A form whose outcome the
definition files cannot settle carries the reason, so it ends ``not_checked``
and never ``missing``. ``doc_resolvers.CommandResolver`` resolves the mentions.

The line is split into commands at ``&&``, ``||``, ``;``, ``|``, ``&`` and the
parentheses of a subshell. A ``cd <dir>`` applies to the commands after it until
the subshell it is in closes. A fence is read as a whole
(``fence_command_mentions``): continued lines are joined, heredoc bodies are
skipped, a fence with prompts is read only on its prompt lines, and a ``cd`` or an
``export`` on one line leaves every later line unprovable for what it changes.

Whatever the reader cannot place is unprovable rather than modeled: a directory
it cannot name, an option it does not know, a variable the command sets.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    ``environments``: the mise environments the command selects (``-E`` or
    ``MISE_ENV``).
    """

    runner: Runner
    name: str
    directory: str | None
    text: str
    verdict: Verdict = "check"
    reason: str | None = None
    overrides: frozenset[str] = field(default=frozenset())
    environments: frozenset[str] = field(default=frozenset())


_QUOTES = "\"'"
_PROMPTS = frozenset({"$", "%"})
# Words that run the command after them (with their own options).
_WRAPPERS = frozenset({"sudo", "time", "exec", "command", "nohup", "env", "nice"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_RE = re.compile(r"\d*(?:&>>?|>>?&?|>\||<<<|<<-?|<>|<&?)")
_PLACEHOLDER_WORD_RE = re.compile(r"<[^<>\s]+>")
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# `$` and `%` only: a `#` line in a doc is far more often a comment than a root prompt.
_PROMPT_RE = re.compile(r"^\s*[$%]\s+")
# Environment variables that change which makefile or config is read.
_MAKE_ENVIRONMENT = frozenset({"MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKEFILES", "MAKEFILE_LIST"})

# A `cd` to a path that is not a repository path: later relative `cd`s stay unknown.
_LOST = "\0"


def _unquote(token: str) -> str:
    """``token`` as the shell passes it on: quotes removed wherever they sit
    (``te"st"`` is ``test``), and backslash escapes resolved."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(token):
        char = token[index]
        escaped = token[index + 1 : index + 2]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                out.append(char)
        elif char == quote:
            quote = None
        elif char == "\\" and escaped and (quote is None or escaped in '"\\$`'):
            out.append(escaped)
            index += 1
        elif quote is None and char in _QUOTES:
            quote = char
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _continues(line: str) -> bool:
    """The line ends in an unescaped backslash: an odd run of them."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _end_of_input(line: str) -> str:
    """A backslash that ends the input continues nothing, and bash drops it."""
    return line[:-1] if _continues(line) else line


def _repo_dir(raw: str) -> str | None:
    """A repository-relative directory, None for one outside the repository or not literal."""
    path = posixpath.normpath(raw)
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
    strings. Quotes, ``$(...)`` and a backslash keep spaces and separators inside one word;
    an unquoted ``#`` that starts a word ends the line."""
    index = 0
    start: int | None = None
    quote: str | None = None
    while index < len(line):
        char = line[index]
        if quote is not None:
            if quote == '"' and char == "\\" and line[index + 1 : index + 2] in ('"', "\\", "$", "`"):
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "$" and line[index + 1 : index + 2] in ("(", "{"):
            start = index if start is None else start
            index = _closing(line, index + 1) + 1
            continue
        if char == "\\" and index + 1 < len(line):
            start = index if start is None else start
            index += 2
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


@dataclass
class _LineState:
    """What a line's commands change for the commands after them: the
    directory (None: the reader's own), the variables exported (with their
    values, None when not set on the line), and whether the line changed
    directory at all."""

    cwd: str | None = None
    env: dict[str, str | None] = field(default_factory=dict)
    moved: bool = False


def _commands(line: str, state: _LineState) -> Iterator[_Command]:
    cwd = state.cwd
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
            if word != "\\":
                words.append(word)
                spans.append((item[1], item[2]))
            continue
        redirect_target = False
        if words and words[0] in ("cd", "pushd", "popd"):
            cwd = _join_dir(cwd, _unquote(words[1])) if len(words) == 2 and words[0] != "popd" else _LOST
            state.moved = True
        elif words and words[0] == "export":
            state.env.update(_assignment(word) for word in words[1:] if not word.startswith("-"))
        elif words:
            yield _Command(words, spans, line, cwd)
        words, spans = [], []
        if item == "(":
            stack.append(cwd)
        elif item == ")":
            cwd = stack.pop() if stack else cwd


def _assignment(word: str) -> tuple[str, str | None]:
    name, equals, value = word.partition("=")
    return name, _unquote(value) if equals else None


def _runner_at(words: Sequence[str]) -> tuple[int, dict[str, str | None]] | None:
    """The index of the runner word, when the command runs one (after a prompt,
    variable assignments and wrappers such as ``sudo``), and the variables
    those assignments set."""
    index = 1 if words and words[0] in _PROMPTS else 0
    wrapped = False
    assigned: dict[str, str | None] = {}
    while index < len(words):
        word = words[index]
        if word in _PARSERS:
            return index, assigned
        if word in _WRAPPERS:
            wrapped = True
        elif _ASSIGNMENT_RE.match(word):
            assigned.update([_assignment(word)])
        elif not (wrapped and word.startswith("-")):
            return None
        index += 1
    return None


_WILDCARD_CHARS = "*?[{"


def _placeholder(name: str) -> bool:
    """A placeholder, or a name the shell expands: any ``$`` form, ANSI-C
    ``$'…'`` and locale ``$"…"`` quoting included, which the reader does not model."""
    return is_placeholder_path(name) or "$" in name or name in ("-", "--")


def _named(
    runner: Runner,
    name: str,
    directory: str | None,
    text: str,
    verdict: Verdict = "check",
    reason: str | None = None,
    overrides: frozenset[str] = frozenset(),
) -> CommandMention:
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


_NPM_RUN = frozenset({"run", "run-script", "rum", "urn"})
_NPM_LIFECYCLE = {"test": "test", "t": "test", "tst": "test", "start": "start", "stop": "stop"}
# npm defines these itself when package.json does not.
_NPM_BUILT_IN_SCRIPTS = frozenset({"restart", "env"})
_NPM_WORKSPACE_VALUED = frozenset({"-w", "--workspace"})
_NPM_WORKSPACE_FLAGS = frozenset({"--workspaces", "-ws", "--include-workspace-root"})
_NPM_FLAGS = frozenset({"-s", "--silent", "-q", "--quiet", "-d", "-dd", "-ddd", "--verbose", "--foreground-scripts"})
# npm reads `-C` as `--prefix`.
_NPM_PREFIX = frozenset({"--prefix", "-C"})
# Options that change which package.json runs or whether a missing script
# fails; npm expands any unambiguous abbreviation of them (`--if-pres`).
_NPM_RESOLUTION = (
    "if-present",
    "prefix",
    "workspace",
    "workspaces",
    "include-workspace-root",
    "global",
    "location",
    "ws",
)
_PNPM_FLAGS = frozenset({"-s", "--silent", "--stream", "--parallel", "--sequential", "--aggregate-output"})


def _option_reason(option: str) -> str:
    return f"{option} is an option the docs check does not know, so the name after it is not certain"


def _unknown_option(word: str, known: frozenset[str]) -> bool:
    """An option that may take the next word as its value: one this reader
    does not know, written without ``=``."""
    return word.startswith("-") and "=" not in word and word not in known


def _long_option_reason(option: str) -> str:
    return f"{option} may abbreviate an npm option that changes which script runs or whether a missing one fails"


def _npm_resolution_option(flag: str) -> bool:
    name = flag.removeprefix("--")
    return bool(name) and any(option.startswith(name) for option in _NPM_RESOLUTION)


def _npm(words: Sequence[str], cwd: str | None, text: str) -> list[CommandMention]:
    command = name = unknown = unknown_long = bundled = None
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
        elif flag in _NPM_PREFIX:
            value, index = _option_value(words, index, flag)
            if value is not None:
                directory = _join_dir(cwd, value)
            continue
        elif word.startswith("--") and flag not in _NPM_FLAGS:
            # After the name an option is npm config, which matters only when
            # it may abbreviate an option that changes resolution.
            if unknown_long is None and name is None:
                unknown_long = _option_reason(flag)
            elif unknown_long is None and _npm_resolution_option(flag):
                unknown_long = _long_option_reason(flag)
        elif word.startswith("-") and not word.startswith("--") and "C" in word[1:]:
            # A bundle of short flags (`-sC web`): its -C is --prefix.
            if not word.endswith("C"):
                bundled = bundled or word
            else:
                value = words[index + 1] if index + 1 < len(words) else None
                if value is not None:
                    directory = _join_dir(cwd, value)
                index += 2
                continue
        elif word.startswith("-"):
            if name is None and unknown is None and _unknown_option(word, _NPM_FLAGS):
                unknown = word
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
    if unknown_long is not None:
        return [_named("npm", name, directory, text, "unchecked", unknown_long)]
    if bundled is not None:
        reason = f"{bundled} bundles -C with flags after it, so the directory is not certain"
        return [_named("npm", name, directory, text, "unchecked", reason)]
    if if_present:
        return [_named("npm", name, directory, text, "unproven", "--if-present passes when the script is missing")]
    if unknown is not None:
        return [_named("npm", name, directory, text, "unproven", _option_reason(unknown))]
    return [_named("npm", name, directory, text)]


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
    command = unknown = unknown_long = None
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
        elif word.startswith("--") and flag not in _PNPM_FLAGS:
            if unknown_long is None:
                unknown_long = flag
        elif word.startswith("-"):
            if unknown is None and _unknown_option(word, _PNPM_FLAGS):
                unknown = word
        elif command is None:
            command = word
            if word not in _PNPM_RUN:
                break
        else:
            later = words[index + 1 :]
            selected = selected or any(w.split("=", 1)[0] in _PNPM_SELECTORS for w in later)
            mention = _pnpm_mention(word, directory, text, selected, if_present)
            if unknown_long is not None and mention.verdict != "skipped":
                return [replace(mention, verdict="unchecked", reason=_option_reason(unknown_long))]
            if unknown is not None and mention.verdict == "check":
                return [replace(mention, verdict="unproven", reason=_option_reason(unknown))]
            return [mention]
        index += 1
    if command is None or command in _PNPM_RUN:
        return []
    if command in _PNPM_COMMANDS:
        return []
    if selected:
        return [_named("pnpm", command, directory, text, "unchecked", "runs in the selected workspace projects")]
    if unknown_long is not None:
        return [_named("pnpm", command, directory, text, "unchecked", _option_reason(unknown_long))]
    if command in _PNPM_TEST:
        return [_named("pnpm", "test", directory, text, "unproven", "pnpm test does not report a missing script")]
    if unknown is not None:
        return [_named("pnpm", command, directory, text, "unproven", _option_reason(unknown))]
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
        elif flag == "--environment-overrides":
            self.overrides.add(flag)
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
                if char == "e":
                    self.overrides.add("-e")
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


_MISE_RUN = frozenset({"run", "r"})
_MISE_QUIET = frozenset({"-q", "--quiet", "-v", "--verbose", "-y", "--yes"})
_MISE_SHORT_QUIET = frozenset("qvy")


class _MiseArgs:
    def __init__(self, cwd: str | None) -> None:
        self.directory = cwd
        self.option: str | None = None
        self.environments: frozenset[str] = frozenset()

    def _environments(self, value: str | None) -> None:
        envs = _mise_environments(value)
        if envs is None:
            self.option = self.option or "-E"
        else:
            self.environments = envs

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
        if flag in ("-E", "--env"):
            value, after = _option_value(words, index, flag)
            self._environments(value)
            return after
        if flag.startswith("-E") and not flag.startswith("--"):
            self._environments(word[2:])
            return index + 1
        if flag in _MISE_QUIET or (not flag.startswith("--") and set(flag[1:]) <= _MISE_SHORT_QUIET):
            return index + 1
        if self.option is None:
            self.option = flag
        return index + 1


def _mise_environments(value: str | None) -> frozenset[str] | None:
    """The environments a ``MISE_ENV`` or ``-E`` value names, None when it is not literal."""
    if value is None:
        return None
    if any(ch in value for ch in "$`*?"):
        return None
    return frozenset(env for env in value.split(",") if env)


def _is_task_shorthand(word: str) -> bool:
    return ":" in word or word.startswith("//")


def _mise_task(name: str, args: _MiseArgs, text: str) -> CommandMention:
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
    return replace(_named("mise", name, args.directory, text), environments=args.environments)


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


_PARSERS: dict[str, Callable[[Sequence[str], str | None, str], list[CommandMention]]] = {
    "npm": _npm,
    "pnpm": _pnpm,
    "yarn": _yarn,
    "bun": _bun,
    "make": _make,
    "gmake": _make,
    "mise": _mise,
}


def _in_environment(mention: CommandMention, env: Mapping[str, str | None], listing: bool) -> CommandMention:
    """``mention`` once the variables the command line exports are known, and
    whether its line may be a listing (an unprompted fence line)."""
    if mention.verdict in ("unchecked", "skipped"):
        return mention
    if mention.runner == "make":
        overrides = mention.overrides | env.keys()
        if set_here := sorted(overrides & _MAKE_ENVIRONMENT):
            return replace(mention, verdict="unchecked", reason=f"the command sets {set_here[0]}")
        mention = replace(mention, overrides=frozenset(overrides))
    elif mention.runner == "mise":
        if set_here := sorted(name for name in env if name.startswith("MISE_") and name != "MISE_ENV"):
            return replace(mention, verdict="unchecked", reason=f"the command sets {set_here[0]}")
        # `-E` takes precedence over MISE_ENV.
        if "MISE_ENV" in env and not mention.environments:
            envs = _mise_environments(env["MISE_ENV"])
            if envs is None:
                return replace(
                    mention, verdict="unchecked", reason="the command sets MISE_ENV to a value it cannot read"
                )
            mention = replace(mention, environments=envs)
    elif set_here := sorted(name for name in env if name.lower().startswith("npm_config_")):
        return replace(mention, verdict="unproven", reason=f"the command sets {set_here[0]}")
    if listing and mention.verdict == "check":
        reason = "an unprompted fence line naming several goals may be a listing, not a command"
        return replace(mention, verdict="unproven", reason=reason)
    return mention


def _line_mentions(line: str, state: _LineState, *, fence: bool) -> list[CommandMention]:
    out: list[CommandMention] = []
    for command in _commands(line, state):
        found = _runner_at(command.words)
        if found is None:
            continue
        index, assigned = found
        parse = _PARSERS[command.words[index]]
        # Unquoted once here, as the shell passes them: `'-bb'` is an option.
        arguments = [_unquote(word) for word in command.words[index + 1 :]]
        mentions = parse(arguments, command.cwd, command.text_from(index))
        listing = fence and command.words[index] in ("make", "gmake") and len(mentions) > 1
        out.extend(_in_environment(mention, {**state.env, **assigned}, listing) for mention in mentions)
    return out


def command_mentions(text: str) -> list[CommandMention]:
    """Every script, target or task a code span runs."""
    return _line_mentions(_end_of_input(text), _LineState(), fence=False)


def _joined(lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """``(offset of the first line, line)`` with backslash-continued lines joined."""
    index = 0
    while index < len(lines):
        start = index
        line = lines[index]
        while _continues(line) and index + 1 < len(lines):
            index += 1
            line = line[:-1].rstrip() + " " + lines[index].lstrip()
        index += 1
        yield start, _end_of_input(line) if index == len(lines) else line


def fence_command_mentions(lines: Sequence[str]) -> list[tuple[int, CommandMention]]:
    """Every script, target or task the lines of a shell fence run, as
    ``(line offset, mention)``. Where any line starts with a ``$`` or ``%``
    prompt, only the prompt lines are commands;
    the rest are output. A line after one that changed directory runs where
    the reader cannot tell."""
    prompt = _PROMPT_RE
    joined = list(_joined(lines))
    prompted = any(prompt.match(line) for _, line in joined)
    out: list[tuple[int, CommandMention]] = []
    env: dict[str, str | None] = {}
    moved = False
    heredocs: list[str] = []
    for offset, line in joined:
        if heredocs:
            if line.strip() == heredocs[0]:
                heredocs.pop(0)
            continue
        if prompted:
            match = prompt.match(line)
            if match is None:
                continue
            line = line[match.end() :]
        heredocs = [match.group(2) for match in _HEREDOC_RE.finditer(line)]
        state = _LineState(cwd=_LOST if moved else None, env=env)
        out.extend((offset, mention) for mention in _line_mentions(line, state, fence=not prompted))
        moved = moved or state.moved
    return out
