"""The Makefile reader (``make_targets``): target names from rule lines, and
every construct that makes the set unprovable."""

from __future__ import annotations

import textwrap

import pytest

from stack_intel.make_targets import make_targets_from_bytes, read_make_targets


def _read(text: str) -> tuple[set[str], str | None]:
    found = read_make_targets(textwrap.dedent(text).lstrip("\n"))
    return set(found.targets), found.opaque


def test_rule_lines_define_targets_and_recipes_and_assignments_do_not() -> None:
    targets, opaque = _read(
        """
        PYTHON := python3
        SRC = src
        FLAGS ?= -q
        FLAGS += -v
        LIST != ls
        .DEFAULT_GOAL := help

        help: ## Show this help
        \t@grep -E '^[a-z-]+:' $(MAKEFILE_LIST)
        lint test: deps
        \t$(PYTHON) -m ruff check $(SRC) && echo done: ok
        clean::
        \trm -rf build
        deps: ; @true
        """
    )
    assert targets == {"help", "lint", "test", "clean", "deps"}
    assert opaque is None


def test_default_goal_is_an_assignment_not_the_default_rule() -> None:
    targets, opaque = _read(".DEFAULT_GOAL := help\nhelp:\n\t@true\n")
    assert (targets, opaque) == ({"help"}, None)


def test_every_branch_of_a_conditional_counts() -> None:
    targets, opaque = _read(
        """
        ifeq ($(OS),Windows_NT)
        setup:
        \tcmd /c setup.bat
        else ifeq ($(CI),1)
        ci-setup:
        \t./ci.sh
        else
        unix-setup:
        \t./setup.sh
        endif
        ifdef VERBOSE
        verbose: ; @true
        endif
        """
    )
    assert (targets, opaque) == ({"setup", "ci-setup", "unix-setup", "verbose"}, None)


def test_continuation_lines_join_targets_and_comments_end_a_line() -> None:
    targets, opaque = _read(
        """
        one \\
          two: # three:
        \t@echo \\
        \t  not-a-target:
        four: ## five:
        """
    )
    assert (targets, opaque) == ({"one", "two", "four"}, None)


def test_tab_lines_after_a_rule_are_its_recipe_even_with_a_colon() -> None:
    targets, opaque = _read("build:\n\tdocker run -v a:b image\n\techo x: y\n")
    assert (targets, opaque) == ({"build"}, None)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("ifndef NOPE\n\tinclude extra.mk\nendif\n\nlint:\n\t@echo lint\n", "has include (line 2)"),
        ("lint:\n\t@echo lint\nX = 1\n\tinclude extra.mk\n", "has include (line 4)"),
        ("X = 1\n\t$(eval deploy: ; @echo d)\nlint:\n\t@echo lint\n", "has $(eval) (line 2)"),
    ],
)
def test_a_tab_indented_line_can_still_hide_definitions(text: str, reason: str) -> None:
    """GNU make reads a tab line as a recipe only while a rule is open, which
    the reader does not track, so these count wherever they appear."""
    assert _read(text)[1] == reason


@pytest.mark.parametrize(
    "text",
    [
        "PREFIX = app\nifeq ($(shell uname),Darwin)\n\tPREFIX = mac\nendif\n$(PREFIX)-build:\n\t@echo b\n",
        "PREFIX = app\nifeq ($(shell uname),Darwin)\n  PREFIX = mac\nendif\n$(PREFIX)-build:\n\t@echo b\n",
        "$(X)-build:\n\t@echo\nX = a\n\tX = b\n",
        "X = a\n$(X)-build:\n\t@echo\nX = b\n",
    ],
)
def test_a_variable_assigned_twice_anywhere_leaves_its_target_names_unknown(text: str) -> None:
    assert "a target name built from variables it cannot expand" in str(_read(text)[1])


def test_a_simple_assignment_expands_at_once_and_a_recursive_one_on_use() -> None:
    found = read_make_targets("A := x\nS := $(A)-s\nR = $(A)-r\n$(S) $(R):\n")
    assert (found.targets, found.opaque) == ({"x-s", "x-r"}, None)


def test_a_leading_dot_slash_is_not_part_of_a_target() -> None:
    assert _read("./x/y:\n\t@echo\n.PHONY: ./z\n")[0] == {"x/y", ".PHONY", "z"}


@pytest.mark.parametrize(
    ("text", "opaque"),
    [
        (".venv:\n\tpython -m venv .venv\nlint: .venv\n", False),
        (".env.local:\n\tcp .env .env.local\n", False),
        (".c.o:\n\tcc -c $<\n", True),
        (".SUFFIXES: .md .html\n.md.html:\n\tpandoc $< -o $@\n", True),
        (".md.html:\n\tpandoc $< -o $@\n", False),
    ],
)
def test_only_known_suffixes_make_a_suffix_rule(text: str, opaque: bool) -> None:
    assert (_read(text)[1] is not None) == opaque


def test_target_specific_variables_and_special_target_prerequisites_count() -> None:
    targets, opaque = _read(".PHONY: release docs\nrelease: VERSION = 1.0\nbuild: export X := 1\n")
    assert targets == {".PHONY", "release", "docs", "build"}
    assert opaque is None


def test_a_target_name_from_variables_is_expanded_when_they_are_known() -> None:
    found = read_make_targets(
        textwrap.dedent(
            """
            UI := web/ui
            STATE = $(UI)/node_modules/.stamp
            $(STATE): $(UI)/package.json
            \tnpm ci --prefix $(UI)
            ${UI}/dist: ; @true
            """
        ).lstrip("\n")
    )
    assert found.targets == {"web/ui/node_modules/.stamp", "web/ui/dist"}
    assert found.opaque is None
    assert found.variables == {"UI", "STATE"}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("include common.mk\nlint:\n", "has include (line 1)"),
        ("-include .env\nlint:\n", "has -include (line 1)"),
        ("sinclude local.mk\nlint:\n", "has sinclude (line 1)"),
        ("lint:\n%.o: %.c\n\tcc -c $<\n", "has a pattern rule (line 2)"),
        ("%: force\n\t@true\n", "has a pattern rule (line 1)"),
        ("OBJS = a.o b.o\n$(OBJS): %.o: %.c\n\tcc -c $<\n", "has a static pattern rule (line 2)"),
        ("objs: a.o b.o\na.o b.o: %.o: %.c\n", "has a static pattern rule (line 2)"),
        (".c.o:\n\tcc -c $<\n", "has an old-style suffix rule .c.o (line 1)"),
        (".sh:\n\tcp $< $@\n", "has an old-style suffix rule .sh (line 1)"),
        (".DEFAULT:\n\t@echo $@\n", "has a .DEFAULT rule (line 1)"),
        ("$(TARGET):\n\t@true\n", "has a target name built from variables it cannot expand (line 1)"),
        ("$(shell cat names):\n\t@true\n", "has a target name built from variables it cannot expand (line 1)"),
        ("X := a\nX := b\n$(X):\n", "has a target name built from variables it cannot expand (line 3)"),
        ("X := a\nX += b\n$(X):\n", "has a target name built from variables it cannot expand (line 3)"),
        ("ifdef CI\nX := a\nendif\n$(X):\n", "has a target name built from variables it cannot expand (line 4)"),
        ("define RULE\nx:\nendef\n", "has define (line 1)"),
        ("$(eval $(call RULE,x))\n", "has $(eval) (line 1)"),
        ("vpath %.c src\n", "has vpath (line 1)"),
        ("VPATH = src\n", "has VPATH (line 1)"),
        (".PHONY: $(TARGETS)\n", "has a target name built from variables it cannot expand (line 1)"),
        ("$(NAME)_DIR := x\n", "has a variable whose name is computed (line 1)"),
        ("load mylib.so\n", "has load (line 1)"),
    ],
)
def test_opaque_reasons(text: str, reason: str) -> None:
    assert _read(text)[1] == reason


def test_the_first_opaque_reason_is_kept() -> None:
    assert _read("include a.mk\n%.o: %.c\n")[1] == "has include (line 1)"


def test_a_makefile_that_is_not_utf8_is_a_decode_diagnostic() -> None:
    found, problems = make_targets_from_bytes("Makefile", b"lint:\n\t\xff\n")
    assert found is None
    assert [(p.kind, p.file) for p in problems] == [("decode_failed", "Makefile")]
