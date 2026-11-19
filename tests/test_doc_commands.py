"""The commands a code span or shell line runs (``doc_commands``): one row per
runner form of the plan's runner appendix, with the verdict it gives.

``check`` resolves to ok or missing; ``unproven`` is ok when the name is
defined and not_checked otherwise; ``unchecked`` is not_checked without a
lookup; ``skipped`` is a placeholder or a wildcard. A form that runs nothing
the repository defines gives no mention."""

from __future__ import annotations

import pytest

from stack_intel.doc_commands import command_mentions, fence_command_mentions

Row = tuple[str, str, str | None, str]


def _mentions(text: str) -> list[Row]:
    return [(m.runner, m.name, m.directory, m.verdict) for m in command_mentions(text)]


NPM: list[tuple[str, list[Row]]] = [
    ("npm run build", [("npm", "build", None, "check")]),
    ("npm run-script build", [("npm", "build", None, "check")]),
    ("npm rum build", [("npm", "build", None, "check")]),
    ("npm urn build", [("npm", "build", None, "check")]),
    ("npm test", [("npm", "test", None, "check")]),
    ("npm t", [("npm", "test", None, "check")]),
    ("npm tst", [("npm", "test", None, "check")]),
    ("npm stop", [("npm", "stop", None, "check")]),
    ("npm start", [("npm", "start", None, "check")]),
    ("npm run start", [("npm", "start", None, "check")]),
    ("npm restart", []),
    ("npm run restart", []),
    ("npm run env", []),
    ("npm run build --if-present", [("npm", "build", None, "unproven")]),
    ("npm run build -w web", [("npm", "build", None, "unchecked")]),
    ("npm -w web run build", [("npm", "build", None, "unchecked")]),
    ("npm run build --workspace=web", [("npm", "build", None, "unchecked")]),
    ("npm run build --workspaces", [("npm", "build", None, "unchecked")]),
    ("npm run build -ws", [("npm", "build", None, "unchecked")]),
    ("npm --silent run build", [("npm", "build", None, "check")]),
    ("npm run build -- --watch", [("npm", "build", None, "check")]),
    ("npm run build --prefix web", [("npm", "build", "web", "check")]),
    ("npm run build --prefix=web/", [("npm", "build", "web", "check")]),
    ("npm --prefix ./web test", [("npm", "test", "web", "check")]),
    ("npm run build --prefix", [("npm", "build", None, "check")]),
    ("npm install", []),
    ("npm ci", []),
    ("npm run", []),
    ("npm run -- build", []),
    ("npm run-all build", []),
    ("npm-run-all build", []),
    ("./npm run build", []),
    ("npm.cmd run build", []),
    ("npm run <script>", [("npm", "<script>", None, "skipped")]),
    ("npm run $TARGET", [("npm", "$TARGET", None, "skipped")]),
    ("npm run build:*", [("npm", "build:*", None, "skipped")]),
    ("npm run ...", [("npm", "...", None, "skipped")]),
]

PNPM: list[tuple[str, list[Row]]] = [
    ("pnpm run build", [("pnpm", "build", None, "check")]),
    ("pnpm run-script build", [("pnpm", "build", None, "check")]),
    ("pnpm build", [("pnpm", "build", None, "unproven")]),
    ("pnpm test", [("pnpm", "test", None, "unproven")]),
    ("pnpm t", [("pnpm", "test", None, "unproven")]),
    ("pnpm start", [("pnpm", "start", None, "check")]),
    ("pnpm run start", [("pnpm", "start", None, "check")]),
    ("pnpm run --if-present build", [("pnpm", "build", None, "unproven")]),
    ("pnpm run build --if-present", [("pnpm", "build", None, "check")]),
    ("pnpm -r run build", [("pnpm", "build", None, "unchecked")]),
    ("pnpm --filter web run build", [("pnpm", "build", None, "unchecked")]),
    ("pnpm --filter=web build", [("pnpm", "build", None, "unchecked")]),
    ("pnpm -w run build", [("pnpm", "build", None, "unchecked")]),
    ("pnpm -C web run build", [("pnpm", "build", "web", "check")]),
    ("pnpm --dir web run build", [("pnpm", "build", "web", "check")]),
    ("pnpm run /^build:.*/", [("pnpm", "/^build:.*/", None, "unchecked")]),
    ("pnpm install", []),
    ("pnpm i", []),
    ("pnpm exec vitest", []),
    ("pnpm dlx create-vite", []),
    ("pnpm restart", []),
    ("pnpm run", []),
]

YARN_BUN: list[tuple[str, list[Row]]] = [
    ("yarn run build", [("yarn", "build", None, "unproven")]),
    ("yarn build", [("yarn", "build", None, "unproven")]),
    ("yarn test", [("yarn", "test", None, "unproven")]),
    ("yarn start", [("yarn", "start", None, "unproven")]),
    ("yarn --cwd web build", [("yarn", "build", "web", "unproven")]),
    ("yarn workspace web run build", [("yarn", "build", None, "unchecked")]),
    ("yarn workspace web build", [("yarn", "build", None, "unchecked")]),
    ("yarn workspaces foreach -A run build", [("yarn", "build", None, "unchecked")]),
    ("yarn", []),
    ("yarn install", []),
    ("yarn add react", []),
    ("yarn dlx create-vite", []),
    ("yarn run", []),
    ("bun run build", [("bun", "build", None, "unproven")]),
    ("bun build", []),
    ("bun dev", [("bun", "dev", None, "unproven")]),
    ("bun --cwd web run dev", [("bun", "dev", "web", "unproven")]),
    ("bun --filter web dev", [("bun", "dev", None, "unchecked")]),
    ("bun test", []),
    ("bun test src/a.test.ts", []),
    ("bun index.ts", []),
    ("bun run index.ts", []),
    ("bun run ./scripts/seed", []),
    ("bun install", []),
    ("bun x vite", []),
]

MAKE: list[tuple[str, list[Row]]] = [
    ("make", []),
    ("make lint", [("make", "lint", None, "check")]),
    ("gmake lint", [("make", "lint", None, "check")]),
    ("make lint test", [("make", "lint", None, "check"), ("make", "test", None, "check")]),
    ("make test V=1", [("make", "test", None, "check")]),
    ("make -C tools lint", [("make", "lint", "tools", "check")]),
    ("make -Ctools lint", [("make", "lint", "tools", "check")]),
    ("make --directory=tools lint", [("make", "lint", "tools", "check")]),
    ("make --directory tools lint", [("make", "lint", "tools", "check")]),
    ("make -C a -C b lint", [("make", "lint", "a/b", "check")]),
    ("make -j 4 test", [("make", "test", None, "check")]),
    ("make -j test", [("make", "test", None, "check")]),
    ("make -j4 test", [("make", "test", None, "check")]),
    ("make -l 2.5 test", [("make", "test", None, "check")]),
    ("make --jobs=4 test", [("make", "test", None, "check")]),
    ("make -sk test", [("make", "test", None, "check")]),
    ("make -s -k --no-print-directory test", [("make", "test", None, "check")]),
    ("make -Otarget test", [("make", "test", None, "check")]),
    ("make --output-sync=line test", [("make", "test", None, "check")]),
    ("make -f other.mk test", [("make", "test", None, "unchecked")]),
    ("make --file=other.mk test", [("make", "test", None, "unchecked")]),
    ("make --makefile other.mk test", [("make", "test", None, "unchecked")]),
    ("make -I include test", [("make", "test", None, "unchecked")]),
    ("make --include-dir=include test", [("make", "test", None, "unchecked")]),
    ("make --eval='x: ; @true' x", [("make", "x", None, "unchecked")]),
    ("make -o prep test", [("make", "test", None, "unchecked")]),
    ("make -W src.c test", [("make", "test", None, "unchecked")]),
    ("make -E 'X = 1' test", [("make", "test", None, "unchecked")]),
    ("make -Z test", [("make", "test", None, "unchecked")]),
    ("make --frobnicate test", [("make", "test", None, "unchecked")]),
    ("make -- test", [("make", "test", None, "check")]),
    ("make --help", []),
    ("make -v", []),
    ("make --version", []),
    ("make <target>", [("make", "<target>", None, "skipped")]),
    ("make $(TARGET)", [("make", "$(TARGET)", None, "skipped")]),
]

MISE: list[tuple[str, list[Row]]] = [
    ("mise run build", [("mise", "build", None, "check")]),
    ("mise r build", [("mise", "build", None, "check")]),
    ("mise tasks run build", [("mise", "build", None, "check")]),
    ("mise run build --release", [("mise", "build", None, "check")]),
    ("mise run build ::: lint", [("mise", "build", None, "check"), ("mise", "lint", None, "check")]),
    ("mise run build arg ::: lint -- x", [("mise", "build", None, "check"), ("mise", "lint", None, "check")]),
    ("mise backend:test", [("mise", "backend:test", None, "check")]),
    ("mise //backend:test", [("mise", "//backend:test", None, "check")]),
    ("mise :test", [("mise", ":test", None, "check")]),
    ("mise -C backend run test", [("mise", "test", "backend", "check")]),
    ("mise --cd backend run test", [("mise", "test", "backend", "check")]),
    ("mise run -C backend test", [("mise", "test", "backend", "check")]),
    ("mise -q run test", [("mise", "test", None, "check")]),
    ("mise -vy run test", [("mise", "test", None, "check")]),
    ("mise --yes run test", [("mise", "test", None, "check")]),
    ("mise run --force test", [("mise", "test", None, "unchecked")]),
    ("mise --jobs=4 run test", [("mise", "test", None, "unchecked")]),
    ("mise run node:web#build", [("mise", "node:web#build", None, "unchecked")]),
    ("mise run ./scripts/build.sh", [("mise", "./scripts/build.sh", None, "unchecked")]),
    ("mise run ../build.sh", [("mise", "../build.sh", None, "unchecked")]),
    ("mise services/api:serve", [("mise", "services/api:serve", None, "check")]),
    ("mise run 'test:*'", [("mise", "test:*", None, "skipped")]),
    ("mise run 'test:**'", [("mise", "test:**", None, "skipped")]),
    ("mise run test:?", [("mise", "test:?", None, "skipped")]),
    ("mise run {a,b}", [("mise", "{a,b}", None, "skipped")]),
    ("mise //...:install", [("mise", "//...:install", None, "skipped")]),
    ("mise //projects/...:build", [("mise", "//projects/...:build", None, "skipped")]),
    ("mise run <task>", [("mise", "<task>", None, "skipped")]),
    ("mise run", []),
    ("mise install", []),
    ("mise use node@26", []),
    ("mise exec -- node -v", []),
    ("mise test", []),
]

LINES: list[tuple[str, list[Row]]] = [
    ("cd web && npm run build", [("npm", "build", "web", "check")]),
    ("(cd web && npm test)", [("npm", "test", "web", "check")]),
    ("cd . && npm test", [("npm", "test", "", "check")]),
    ("cd web && npm ci && npm run build", [("npm", "build", "web", "check")]),
    ("cd web; make lint", [("make", "lint", "web", "check")]),
    ("cd web | make lint", [("make", "lint", "web", "check")]),
    ("cd web && cd ui && npm run build", [("npm", "build", "web/ui", "check")]),
    ("(cd web && npm test) && npm run build", [("npm", "test", "web", "check"), ("npm", "build", None, "check")]),
    ("cd web && npm run build --prefix ui", [("npm", "build", "web/ui", "check")]),
    ("cd web && make -C ui lint", [("make", "lint", "web/ui", "check")]),
    ("cd .. && npm run build", [("npm", "build", None, "unproven")]),
    ("cd $APP && cd web && npm run build", [("npm", "build", None, "unproven")]),
    ("cd && npm run build", [("npm", "build", None, "unproven")]),
    ("npm run build --prefix ../other", [("npm", "build", None, "unproven")]),
    ("make -C /opt/app lint", [("make", "lint", None, "unproven")]),
    (
        "npm run a && npm run b; npm test | cat",
        [("npm", "a", None, "check"), ("npm", "b", None, "check"), ("npm", "test", None, "check")],
    ),
    ("$ npm ci && npm run build", [("npm", "build", None, "check")]),
    ("sudo npm run build", [("npm", "build", None, "check")]),
    ("sudo -E make install", [("make", "install", None, "check")]),
    ("CI=1 make test", [("make", "test", None, "check")]),
    ("time make test", [("make", "test", None, "check")]),
    ("make lint # and the tests", [("make", "lint", None, "check")]),
    ("make lint 2>&1 | tee log", [("make", "lint", None, "check")]),
    ("make lint > lint.log 2> errors.log", [("make", "lint", None, "check")]),
    ("make lint >lint.log < /dev/null", [("make", "lint", None, "check")]),
    ("make $(cat goals.txt) lint", [("make", "$(cat goals.txt)", None, "skipped"), ("make", "lint", None, "check")]),
    ("echo make lint", []),
    ('git commit -m "make lint pass; npm run deploy"', []),
    ("# make lint", []),
    ("uv run pytest", []),
]


@pytest.mark.parametrize(("text", "expected"), [*NPM, *PNPM, *YARN_BUN, *MAKE, *MISE, *LINES])
def test_command_mentions(text: str, expected: list[Row]) -> None:
    assert _mentions(text) == expected


def test_the_text_runs_from_the_runner_to_the_end_of_its_command() -> None:
    assert [m.text for m in command_mentions("$ cd web && npm run build -- --watch # dev")] == [
        "npm run build -- --watch"
    ]
    assert [m.text for m in command_mentions("make lint test && echo ok")] == ["make lint test", "make lint test"]


def test_a_make_command_line_records_the_variables_it_sets() -> None:
    (mention,) = command_mentions("make deploy ENV=prod DRY_RUN:=1")
    assert mention.overrides == frozenset({"ENV", "DRY_RUN"})


def test_every_unproven_or_unchecked_form_says_why() -> None:
    rows = [text for text, expected in [*NPM, *PNPM, *YARN_BUN, *MAKE, *MISE] if expected]
    for text in rows:
        for mention in command_mentions(text):
            assert (mention.reason is None) == (mention.verdict == "check"), text


def _fence(info: str, text: str) -> list[tuple[int, str, str, str]]:
    return [(offset, m.name, m.verdict, m.text) for offset, m in fence_command_mentions(info, text.split("\n"))]


@pytest.mark.parametrize(
    ("info", "text", "expected"),
    [
        ("sh", "make -j8 \\\n  build", [(0, "build", "check", "make -j8 build")]),
        ("sh", "make build \\\n  V=1", [(0, "build", "check", "make build V=1")]),
        ("sh", "npm run \\\n  build", [(0, "build", "check", "npm run build")]),
        ("sh", "mise run \\\n  build", [(0, "build", "check", "mise run build")]),
        ("sh", "make lint\nmake test", [(0, "lint", "check", "make lint"), (1, "test", "check", "make test")]),
        (
            "console",
            "$ make help\nmake build    Build the app\nmake test     Run the tests",
            [(0, "help", "check", "make help")],
        ),
        ("console", "# make install\nmake: Nothing to be done", [(0, "install", "check", "make install")]),
        ("sh", "% make lint\nmake[1]: Entering directory", [(0, "lint", "check", "make lint")]),
        ("sh", "# install\nmake install", [(1, "install", "check", "make install")]),
        (
            "",
            "make build    - build the app",
            [
                (0, "build", "unproven", "make build    - build the app"),
                (0, "-", "skipped", "make build    - build the app"),
                (0, "build", "unproven", "make build    - build the app"),
                (0, "the", "unproven", "make build    - build the app"),
                (0, "app", "unproven", "make build    - build the app"),
            ],
        ),
        ("sh", "cat <<EOF > x.sh\nmake deploy\nEOF\nmake lint", [(3, "lint", "check", "make lint")]),
        ("sh", "cat <<-'END'\n\tmake deploy\n\tEND\nmake lint", [(3, "lint", "check", "make lint")]),
        ("sh", "cat <<< 'make deploy'\nmake lint", [(1, "lint", "check", "make lint")]),
        (
            "sh",
            "git clone https://example.com/tool && cd tool\nmake install",
            [(1, "install", "unproven", "make install")],
        ),
        ("sh", "mkdir build && cd build\nmake", []),
        ("sh", "cd web\nnpm run build", [(1, "build", "unproven", "npm run build")]),
        ("sh", "pushd web\nmake lint\npopd", [(1, "lint", "unproven", "make lint")]),
    ],
)
def test_fence_command_mentions(info: str, text: str, expected: list[tuple[int, str, str, str]]) -> None:
    assert _fence(info, text) == expected


def test_an_export_on_an_earlier_fence_line_sets_the_variable_for_make() -> None:
    ((_, mention),) = fence_command_mentions("sh", ["export PREFIX=x", "make x-build"])
    assert mention.overrides == frozenset({"PREFIX"})


@pytest.mark.parametrize(
    ("text", "overrides", "verdict"),
    [
        ("PREFIX=x make x-build", {"PREFIX"}, "check"),
        ("env PREFIX=x make x-build", {"PREFIX"}, "check"),
        ("make -e x-build", {"-e"}, "check"),
        ("MAKEFLAGS=-r make foo", set(), "unchecked"),
        ("make MAKEFILES=x.mk foo", set(), "unchecked"),
    ],
)
def test_variables_a_make_command_sets(text: str, overrides: set[str], verdict: str) -> None:
    (mention,) = command_mentions(text)
    assert mention.verdict == verdict
    if verdict == "check":
        assert mention.overrides == overrides


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("npm run --loglevel warn build", "unproven"),
        ("npm run --loglevel=warn build", "check"),
        ("npm --silent run build", "check"),
        ("npm run build --loglevel warn", "check"),
        ("pnpm run --reporter silent build", "unproven"),
        ("pnpm --workspace-concurrency 2 run build", "unproven"),
        ("pnpm run build --filter web", "unchecked"),
        ("pnpm run build -r", "unchecked"),
        ("npm_config_if_present=true npm run build", "unproven"),
        ("MISE_CONFIG_FILE=x.toml mise run build", "unchecked"),
        ("MISE_ENV=ci mise run build", "check"),
    ],
)
def test_options_and_variables_that_leave_a_name_uncertain(text: str, verdict: str) -> None:
    assert [m.verdict for m in command_mentions(text)] == [verdict]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("make my-target", [("my-target", "skipped")]),
        ("make example", [("example", "skipped")]),
        ("npm run my_script", [("my_script", "skipped")]),
        ("make build \\", [("build", "check")]),
    ],
)
def test_example_names_and_a_trailing_backslash(text: str, names: list[tuple[str, str]]) -> None:
    assert [(m.name, m.verdict) for m in command_mentions(text)] == names
