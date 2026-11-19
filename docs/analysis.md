# Analysis notes

## How the backend analysis works

Backend analysis runs in-process on `backend.root/backend.package`, without importing it.

**One router graph** serves both DI and routes. Every `APIRouter(...)` and `FastAPI(...)` call is a node with its own `prefix=` and `dependencies=`; every `include_router(...)` in any module is an edge with its own prefix and dependencies, resolved through imports, aliases, `module.router` attributes and re-export barrels (`from .a import router as a_router`, `from .a import *`). Routes are the paths through the graph from the app. A router built inside a factory function belongs to that function, and each include of the factory's result adds an edge. `app.mount(path, sub_app)` of a FastAPI sub-app continues the walk; any other mount is an `asgi_mount` limitation. Registrations come from the route decorators (`get`, `post`, `put`, `patch`, `delete`, `options`, `head`, `trace`, `api_route`, `websocket`) and from `add_api_route` and `add_api_websocket_route`. Router factories and package functions called at import are followed, including nested ones. Mounts on a router reached through `include_router` are dropped, as FastAPI does. Star-import barrels are resolved; a module's `__all__` limits them only when it is one unconditional literal list or tuple of strings that no module changes in place. Chains of plain re-exports are followed in a loop; other nested lookups stop at 100 levels with a limitation.

**Include models.** What an include serves depends on the FastAPI version `backend.lock` locks. The analysis reads it on every build, the DI and routes JSON name it in their `app` block (`fastapi_version`, `include_model`, and `include_model_reason`, `null` unless the model is `unknown`), and `status` shows it. Order is the app's import order: a module's imports run first, depth first (a package's `__init__` before its submodules), and a function called at import runs at its call.

- `copy`, FastAPI before 0.137.0: `include_router` copies the child's routes when it runs, so an include sees exactly the registrations on the child that ran before it. A registration made after the include, in the same module or in a module imported later, is not part of that include, and neither is a mount on an included router (FastAPI drops it).
- `live`, FastAPI 0.137.0 and later: the parent keeps the child router, so every registration on it is served through the include, with the include's prefix and dependencies, whenever it runs. Code that runs after the import (an `on_event` handler, a `lifespan`) or may never run is listed as `deferred` and reported. A mount on an included router is served under the include's prefix.
- `unknown`, when the lock is missing or unreadable, locks no `fastapi` or several versions, or the version is not a plain release (`0.137.0rc1`): no registration is dropped on order grounds. One only `live` serves is listed as `deferred` and reported, with the reason the version is unknown in the message.

Under `copy`, a registration in a function that can only run after the import, on a router an include copies at import, is not part of that include either, but only when no way of reaching the function object is left open (it is a module-level function defined once, decorated by nothing but route decorators or `on_event`; every read of its name is the callee of a call the analysis follows; no string names it; its module is never used as a value; and the package has no dynamic lookup that may reach any name: `getattr` with a computed name, `vars`, `globals`, `sys.modules`, `__dict__`, `eval`, `exec`, `__import__`, `importlib.import_module`, …). Such a registration is left out of that include and reported (`deferred_route_registration` or `deferred_router_mount`, naming the include). Any other function may run at import through an alias, an argument, a container, a decorator, a string or a dynamic lookup, so its routes are listed as `deferred` and reported. A router included twice yields two route sets. A change made in a module the app never imports is reported as `unimported_router_change`.

**Values** are evaluated statically: string constants, module and imported constants, `+`, f-strings whose parts are known, list and tuple concatenation, starred sequences and `**kwargs` dictionaries. A value that cannot be evaluated becomes a limitation (`unresolved_route_path`, `unresolved_include_prefix`, `unresolved_route_prefix`, `unresolved_dependencies`), never a silent empty string. A list, dict or set can be changed through any reference to it, so rather than follow each reference the analysis treats every use of a container name as a possible change except a use known to only read it (the `dependencies=`, `prefix=`, `tags=` and `responses=` values of route, include, `APIRouter` and `FastAPI` calls confirmed by import from `fastapi`; a loop or comprehension over it; the reading builtins; comparisons; indexing; and a few read-only methods). A container name with another use is unknown ("may be changed in place") and reported. A value changed through `setattr`, an instance attribute assigned by a method, or a local bound more than once is also treated as unknown and reported, never guessed.

**When code runs.** Module-level code, `if`/`try`/`with`/`for`/`while`/`match` bodies, app or router factories and package functions called at import (`setup()` or `setup(app)` at module level) run at import. An include under a branch, a loop, an `except` handler or a conditional expression is marked `conditional` with a `conditional_router_mount` limitation. Functions nothing calls at import are still read: an include or a route registration inside one is marked `deferred`. A function called at import runs once per call chain, up to 16 calls deep and 4,096 call chains in all; a recursive call is not followed and is reported as `recursive_router_call`, a call past either bound as `router_call_limit`. A module whose first import may be skipped is conditional even if it is imported again later, with a `conditional_import` limitation at the import statement.

**Known gaps**, each reported rather than guessed:

- A router or app passed to code outside the package (`add_pagination(app)`) is an `opaque_router_call`, because that code may add routes or dependencies. A router changed in place (`app.router.routes.clear()`, `app.dependencies = []`) or whose route list is bound to a name or passed on is an `opaque_router_change`.
- **App factories.** `app = "module:factory()"` validates but is not analysed yet: it is an `unresolved_app` diagnostic. [`runtime-check`](verification.md) can still import such an app.
- **Library routers and dependencies.** Installed code is not read. Without a [library capture](#library-routers-and-dependencies), a router built by an installed library's factory (`include_router(auth_lib.get_users_router())`) is an `unresolved_include` limitation and its routes are missing from the route tools and the docs check, and a `Depends(name)` that resolves to a module-level name bound to a call result (`current_user = auth_lib.current_user(active=True)`) is an `external` dependency, identified by the defining binding (`module:current_user`) with the bound expression shown, and an `unresolved_dependency` limitation because its sub-dependencies are unknown. The capture fills both.
- A name bound by a `match` capture pattern is not evaluated, so an include of it is `unresolved_include`; so is an include of a loop variable.
- A `global` rebinding of a router name inside any function makes uses of that module-level name ambiguous, so the registrations or includes that read it are reported.
- A route registered under a module-level branch is listed with `mount: "conditional"` and no limitation of its own.
- A container changed through reflection (`globals()`, `vars(module)`, `sys.modules[...]`, `exec`/`eval`, `module.__dict__`) is not seen: its value is listed as if unchanged, with no limitation.

**Routes are listed in Starlette order**, the order the app dispatches them (the first full match wins), with the `def` line of the handler and its `module:qualname` id.

**Dependency order** is FastAPI's: the app's own dependencies, then each include's dependencies and each router's own dependencies from the app down to the registering router, then the route decorator's `dependencies=`, then the handler's parameters. Duplicates are kept, as FastAPI keeps them.

**Parameters** follow FastAPI's `analyze_param`: a dependency is a `Depends(...)` or `Security(...)` from `fastapi` (a local function named `Depends` is not one), in the parameter default or inside `Annotated[...]`, with aliases such as `DbSession = Annotated[Session, Depends(get_db)]` resolved across modules and re-exports. Inside one `Annotated`, only the last FastAPI marker counts: `Annotated[str, Query(), Depends(x)]` depends on `x`, and `Annotated[str, Depends(x), Query()]` declares no dependency. A marker in both the default and `Annotated` is an `unresolved_dependency` limitation, because FastAPI rejects it. `Depends()` with no argument uses the annotation's type.

A target the analysis cannot see into is an `unresolved_dependency` limitation instead of a guess: a name bound to a wrapper call (`get_settings = lru_cache(_settings)` makes the wrapper's result the dependency, not `_settings`), a callable defined outside the analyzed package, or a factory whose returned callable is unknown.

**Identity and display.** A dependency is identified by `module:qualname` (a lambda as `module:<lambda>@L<line>`) and displayed by its short name. Classes contribute their `__init__` parameters and instances (`Depends(RoleChecker("admin"))`, or a module-level `checker = RoleChecker(...)`) their `__call__` parameters, so an instance is its own target, `module:RoleChecker()`, next to the class `module:RoleChecker`. A plain method through an instance (`Depends(service.get_items)`) is bound, so FastAPI skips its first parameter, and is `module:Service().get_items`; through the class it is `module:Service.get_items`. A factory call (`Depends(require_role("admin"))`) is identified by the factory and displayed with its arguments; its sub-dependencies come only from the callable it returns. A module-level guard alias (`admin_only = require_role("admin")`) resolves to the factory and is shown as `require_role(…) [as admin_only]`.

Security scopes, `use_cache`, the `scope=` of `Depends` and `app.dependency_overrides` are ignored: none of them changes which dependencies a route declares. A class whose `__init__` comes from a base outside the package (a pydantic model, for one) contributes no sub-dependencies, and no limitation says so. Dataclass dependencies are supported (their fields are the parameters); any other class decorator is reported as `unresolved_dependency`. A `functools.partial` is identified as its function, with the parameters FastAPI reads from it. A package decorator is transparent when it returns the function itself or a wrapper that copies the function's metadata with `functools.wraps` or `functools.update_wrapper`; any other package decorator is reported as `opaque_decorator`.

## Library routers and dependencies

Static analysis reads the repository, never installed code. Two things in installed code decide what a route is and cannot be read from its source: a router a library builds when it runs (`app.include_router(fastapi_users.get_users_router(...))`), and the dependencies of a callable whose signature the library sets at runtime (`current_user = fastapi_users.current_user(active=True)`, then `Depends(current_user)`). `stack-intel capture` imports the app once, under the interpreter of `[backend.capture]`, and records only those facts in `.stack-intel/library-capture.json`. The analysis applies that file without running anything, so the MCP server, `doc-check` and every other command give the same answer on every machine.

**Workflow.**

1. Add `[backend.capture]` to the config. Install the backend's locked dependencies (`uv sync --locked` in the backend) so the interpreter matches the lock.
2. Run `stack-intel capture`. It finds the gaps statically (each `unresolved_include`, each `external` dependency binding, each installed callable the package uses as a dependency), imports the app, records what the running app shows at exactly those places, and writes the file. A second run writes the same bytes. A gap it cannot fill stays a gap; its reason is printed (`capture: not captured, include_router at backend/app/main.py:28 (…): pkg.main:own_handler is defined in the backend package`) and the gap is recorded in the file's `unfilled` key, without the reason, which can hold a path of the machine that captured. The same goes for an entry the analysis would not apply (a package function that returns a closure it defines, for one): it is not written as an entry but recorded as unfilled, and the message of a gap recorded this way says that the capture could not fill it, not that it is missing from the file. A dependency that shows only once the entries before it are applied is captured in a further run of the app, however long the chain.
3. Commit the file. `capture --check` (and the `library-capture` check of `verify`) fails when the file differs from what the app builds now; run it in CI with the backend's locked environment. After a lock bump of FastAPI or of a captured library, a change to an `include_router` of installed code or to a library dependency, or when `library_capture_stale` appears, run `capture` again; two branches that both recapture conflict, and running it again resolves that.

`capture --if-stale` runs the freshness checks below with the file applied and starts the app only when one of them finds the file stale; otherwise it prints `capture: <file> is up to date (static check)` and exits 0, having imported nothing. When it does start the app it does what `capture` does, including the refusal below. It needs no interpreter when the file is fresh. What the static check cannot see is anything the library call's result depends on other than the text of the include's router argument or of the binding's own expression: an argument whose value changes in another module, or the object the call is made on (the `fastapi_users` of `current_user = fastapi_users.current_user()`, configured elsewhere). `--if-stale` can report a file fresh that `capture --check` reports different; keep `--check` in CI.

`capture` refuses (exit 2, nothing written) when an installed version of `fastapi` or of any captured distribution differs from the lock, naming both versions and `uv sync --locked`, because the file would describe code the lock does not install.

**When code runs.** Repository code runs in exactly three commands: `capture`, the `library-capture` check of `verify`, and `runtime-check`. Each starts the configured interpreter on the packaged oracle in a subprocess, from an empty temporary directory, with a 300 s timeout (on expiry the subprocess's whole process group is killed) and a clean environment: `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL` when set, plus `[backend.capture] env`. `status`, `summary`, `dump`, `doc-check`, `config-check` and every MCP tool never start it.

**What the file holds** (`format: 1`):

- `includes`: for each `include_router` call whose child comes wholly from installed code, the child's routes (method, path relative to the child, handler id and definition, route-level dependencies and their `via`, parameter dependencies), keyed by the call's file, the text of its router argument (`ast.unparse`, so reformatting does not matter) and its occurrence among the calls of that file with the same text;
- `dependencies`: the dependency tree of each external binding (`binding`: `module:name`, with `bound_to`, the text of the expression the name is bound to) and of each installed callable named in the package (`external`: a dotted name). A node with `sub` is a callable of installed code and the capture owns its subtree; a node without it is a callable of the backend package, which the analysis reads from source on every call;
- `callables`: where each installed callable is defined (distribution, file inside it, line), and `distributions` the versions installed;
- `unfilled` (only when there is a gap `capture` could not fill or whose entry the analysis would not apply; never empty): `includes` and `dependencies`, holding only the key of each gap, as the entry would have had it (an include by file, router text and occurrence; a dependency by `binding` with `bound_to`, or by `external`). The analysis ignores it apart from the wording of a gap's message; `capture --if-stale` reads it to tell a gap already attempted from a new one. A gap whose key changed (a binding bound to another expression, a second include of the same router) is new. A file written before the key existed has none: the first `--if-stale` over a repository with such a gap starts the app once and writes it, and `capture --check` fails once after an upgrade for such a repository, until it captures again.

Everything the repository owns is still read from source on every call: an include's prefix and dependencies, the app's dependencies, dispatch order and the sub-dependencies of the repository's own callables. A repository edit therefore rarely stales the capture.

**What it lifts.** A captured router is a router node mounted exactly like a repository router: it takes the include's own prefix and dependencies and its place in dispatch order, and its routes are live routes (`library` set, `file:line` the include) in DI, routes, docs and `runtime-check`. No `unresolved_include` is reported for it. A captured binding keeps its id and the expression it is bound to, gains the captured sub-dependencies and the `runtime_id` FastAPI reports for it, and is no longer an `unresolved_dependency`; an installed callable named in the package becomes a `library` target. `di_reverse` therefore reaches every route a library dependency guards, where it used to stop short by the routes behind `current_user`.

**Freshness.** The capture file is part of the backend fingerprint, so editing or regenerating it rebuilds DI, routes and docs. On every build the analysis checks, without importing anything:

- each captured distribution's version against the lock: on a difference the capture is still applied and a `library_capture_stale` limitation names the distribution, both versions and `stack-intel capture`;
- an `includes` entry that matches no unresolved `include_router` call, an entry for another `app`, and an entry that names a callable of the package the analysis cannot find: not applied, and reported as `library_capture_stale`;
- a `dependencies` entry that no dependency of the package matches, or a `binding` entry whose `bound_to` differs from the expression the package binds the name to now (a rebound name): reported the same way;
- a gap with no entry while `[backend.capture]` is configured keeps its own limitation, which adds `not in .stack-intel/library-capture.json; run stack-intel capture`; when the file records the gap as `unfilled` it adds `the capture could not fill it; stack-intel capture prints why` instead.

A malformed file is a `parse_failed` diagnostic on it. What static checks cannot see is a change to anything the library call's result depends on other than the text of its router argument or binding expression (a value passed in from another module, or the object the call is made on); the `library-capture` check catches it.

`capture --if-stale` treats the file as stale when any of these holds, and as fresh otherwise: the file is malformed; the analysis reports `library_capture_stale`; a gap `capture` can fill has no entry and no `unfilled` record; an `unfilled` record matches no gap any more; or the file does not exist, whether or not there are gaps (`capture --check` and `status` already report a missing file).

### Keeping the capture fresh

A pre-commit or [prek](https://github.com/j178/prek) local hook can run `capture --if-stale`, so the committed file does not go stale and a commit that changes nothing it depends on pays only the static check:

```yaml
- repo: local
  hooks:
    - id: stack-intel-capture
      name: refresh the stack-intel library capture
      entry: uv run --no-project --python 3.14 --with-requirements .stack-intel/requirements.txt stack-intel capture --if-stale
      language: system
      pass_filenames: false
      files: ^(backend/.*\.py|backend/uv\.lock|\.stack-intel/)
```

Set `files` to the backend's Python files, its lockfile and `.stack-intel/`. A hook that rewrites a file fails the commit, as every pre-commit hook does: stage the file and commit again. Starting the app needs the backend's locked environment (`uv sync --locked`); when the file is fresh the hook needs no interpreter. CI should still run `capture --check`, which also catches what the static check cannot.

**Limits.** A router that is not wholly from installed code (a repository handler passed into the factory), or whose include did not run once while the app was imported, is not captured. A package callable that returns a closure it defines is identified by FastAPI as its factory, so its parameters cannot be told from the closure's and the entry is not applied. A library that adds routes to a router it is handed (an app-level `get_app()`) is not captured. Library routes are never reported as backend-only or counted in coverage.

## How the frontend analysis works

Frontend analysis runs the TypeScript 7.1 compiler (a native executable that the helper drives through its JavaScript API) through a Node helper (`ts/helper.cjs`), one process per batch, with the request on standard input and the result on standard output. It has two modes:

- **`facts`**: each file is parsed on its own, without a program, and yields its JSX elements and declarations (and, for React Router pages, the string attributes of the configured tags). Results are cached per file, so after an edit only the changed files are parsed again. JSX usage, TypeScript symbols and pages use this mode. A file that cannot be read (`read_failed`), that the parser itself fails on (`parse_failed`) or whose facts cannot be extracted (`analysis_failed`, step `extract facts`) is reported by name, and the rest of the batch is unaffected.
- **`routes`**: one program and type checker over the frontend, built from `frontend.tsconfig` (paths, base URL, JSX) but hermetic: no default library, no type packages, and no file under any `node_modules`. Results do not depend on installed packages. The hermetic module resolution is a static table: before each program is built, every import of every file in it is resolved with TypeScript's own resolution, only the ones that land in the repository outside `node_modules` go into the table, and the program is rebuilt until no new file appears. The checker resolves imports, aliases, namespace imports, re-export barrels and constants across files. It runs in a worker thread while the backend graph is built. Per-file failures name a step: `read file`, `read tsconfig`, `parse file` (TypeScript syntax errors; the file is still read from the recovered tree) and `extract calls`. A file the parser or binder cannot handle (the native compiler crashing, which the helper survives by restarting it for the next file) is a `parse_failed` diagnostic and is left out of the program. Bytes that are not UTF-8 are a `decode_failed` diagnostic, and the file is read with U+FFFD in their place.

**Clients.** See [`[[frontend.clients]]`](configuration.md#frontendclients). Client and builder rows carry `callee` (the callee as written) and the line and column it starts at, `callee_line` and `callee_column`, which can be a later line than the call's own `line` (`api\n  .get(…)`). WebSocket rows have no callee, so those three are `null`, and their `column` is the start of the URL expression. Calls inside a client's own definition are not consumers. Calls in test files are found but not matched (`test_calls_skipped`).

**The evaluator** handles string literals, templates, `+`, conditionals (`?:` and `??`, one variant per branch), `as const`, `satisfies`, non-null assertions, `const` declarations including imported ones, members of `const` objects and calls to functions in the program, followed through builders that call builders. A value it cannot evaluate becomes a hole in the URL. A URL that does not start with `/` is an `unresolved_frontend_path` diagnostic; more than 16 variants is a `url_variants_truncated` limitation. A client's `base_path` is prepended before matching.

**Holes** are `param` (inside a path segment), `query` (after a literal `?`, or a trailing value that is always a query string or empty) or `unknown` (a trailing value the evaluator cannot see).

**Method.** The `method` property at the top level of the init object (quoted, shorthand or a computed key), after resolving an identifier to its object literal; the member name for an `instance` verb call; `GET` (or the client's `default_method`) when there is no init or no `method`. A computed key is evaluated; one the evaluator cannot read makes the method `UNKNOWN` with an `unresolved_http_method` limitation. A `method` written after a spread wins over the spread. The method never comes from a type annotation.

**Prop flows.** When a URL hole is a parameter or destructured prop of the enclosing component or hook, the string literals passed for it at call sites and in JSX attributes become variants with `confidence: "possible"` and reason `prop_flow`. Sites that omit the prop use the parameter's default. Sites are followed through `const X = Y`, `memo`, `forwardRef`, default exports, and `dynamic`/`lazy(() => import(…))`. A non-literal value at any call site, or a site that cannot be traced to the function, adds an `unresolved_prop_value` limitation and keeps the value unknown.

**WebSockets.** Every string expression in code (never in comments) whose evaluated text contains `/ws/` is a WebSocket call with method `WS`; the path is the part from `/ws/` on. Text containing whitespace is never a URL.

**Known gaps.** A URL string that is never passed to a client call (a `const pdfUrl = "/pdf"` handed to an `<a href>` or an `<img src>`) is not a call: the route it names stays backend-only. A call through a client the config does not name is not found.

## Matching

Backend paths become Starlette's path regexes (`{name}`, `{name:path}`, `{name:int}`, `{name:float}`, `{name:uuid}`, `{name:str}`), tried in Starlette order. Each frontend URL variant is matched separately, with query holes and anything after a literal `?` dropped.

- A literal URL matches the first route whose regex and method match: `exact`.
- A URL with holes is matched segment by segment. A literal segment matches a backend literal or parameter (`exact`). A `param` hole matches a backend parameter (`exact`) or a backend literal (`possible`, `hole_on_literal`). A mixed segment whose holes each start it or follow `/` (`${id}.csv`) matches a parameter (`exact`) or a literal that fits (`possible`).
- A hole glued to text or another hole inside a segment, or one that ends the URL, may be empty or hold `/`. Such a URL is matched character by character against the route's pattern, so convertors still apply. Every route that some value of its holes reaches is listed as `possible`, with reason `mixed_segment`, or `trailing_hole` when the URL ends in a hole, and none shadows another.
- An exact match that fits every value the holes can take shadows every later route, because Starlette always dispatches there; a possible match never shadows.
- `{name:path}` absorbs one or more segments, and the backend segments after it must match the end of the URL.
- An `UNKNOWN` method matches any method (`possible`).
- `WS` calls match only WebSocket routes, and HTTP calls never do.
- Trailing slashes are significant when the app sets `redirect_slashes=False`. With `redirect_slashes=True` a slash-variant match is `possible` (`redirect`).

A match row's `reason` is one of `literal`, `param`, `literal_on_param`, `hole_on_literal`, `mixed_segment`, `trailing_hole`, `method_unknown`, `redirect` or `prop_flow` (an otherwise exact match of a prop-flow variant). Duplicate calls are merged only when file, line, column, method and path are all equal.

## JSX usage

Elements come from the TypeScript parser, so comments, strings, type arguments (`useState<Row>`, `<DataTable<Row> … />`) and generics are never mistaken for tags, and `tag_kind` is exact however long the tag.

### Parent rule

`parent_function` is the innermost *named function* whose span, from its parameter list to its end, contains the element; elements in a parameter default belong to that function. A named function is a function declaration or a function expression with a name, or an arrow or anonymous function expression that initializes a variable, seen through `as`/`satisfies`/parentheses and through any calls it is an argument of (`const X = () => …`, `memo(forwardRef(…))`, `withA(B)(…)`). So:

- `const X = function Y() {…}` is `Y`;
- `const rows = items.map((r) => <Row />)` is `rows`;
- `dynamic(() => import("…"), { loading: () => <Spinner /> })` does not name the `loading` arrow, which is not a direct argument;
- methods, class-field arrows and object-property arrows (`cell: () => <Badge />`) are not named functions, so their elements take the next named function outward, or `null`.

Fragments (`<>…</>`) have no tag and cannot be queried. JSX usage scans `.tsx`, `.jsx`, `.js`, `.mjs` and `.cjs` files; `.ts`, `.mts` and `.cts` cannot contain JSX (`<Foo>x` is a type assertion there). A test file is one under a `__tests__`, `__mocks__`, `test`, `tests` or `e2e` directory, or named `*.test.*` or `*.spec.*` (the rule is shared with the route analysis). A file with syntax errors is reported as a `typescript_syntax_error` diagnostic and analyzed from TypeScript's recovered tree; a file that cannot be read is a `read_failed` diagnostic. Both name the file.

## Symbols

**Kinds:** `function`, `class`, `component`, `method`, `const`, `variable`, `type_alias`, `interface`, `enum` and `namespace`. **Scopes:** `module`, `namespace`, `class`, `object` (object-literal members) and `local`.

**TypeScript and JavaScript** (every `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs` and `.cjs` file under the frontend root, from the TypeScript parser):

- function declarations, and `const`/`let`/`var`/`using` bindings, including `let x!: T`, destructuring patterns and `for … of/in` headers;
- `component` for a PascalCase name (with a lowercase letter) whose own body contains JSX outside nested functions, or that is initialized by `memo`, `forwardRef`, `lazy` or `dynamic` (also as `React.memo`); SCREAMING_CASE names are never components;
- classes, class methods, accessors and arrow-valued class fields (`method`), object-literal properties whose value is an arrow or function and object shorthand methods; the name of a named class expression (`x = class Name {}`) is `local`, since it is bound only inside the class;
- interfaces, type aliases, enums and `namespace`/`module` blocks with an identifier name; members of nested namespaces have the dotted container (`A.B`);
- `export default x;` and `export = x;` mark `x` exported;
- JSDoc tags in JavaScript files declare nothing: TypeScript 7 parses `@typedef`, `@private` and `@type` into nodes of their own, and the helper skips them.

Not indexed: parameters, catch bindings, plain class fields, constructors, interface and type members, `declare module "x"`, anonymous default exports, `export default <expr>`, export specifiers, `export * as` and the own name of a named function expression.

**Python** (every `.py` file under `backend.root`, from `ast`): module-level functions, classes and assignments, including those inside module-level `if`, `try`, `with`, `for`, `while` and `match` blocks, `for` targets and walrus targets; class bodies (methods, nested classes, class-level assignments); and functions and classes nested inside functions (`local`). `Enum` subclasses are `enum`, `Protocol` subclasses `interface`, and type-alias assignments (`TypeAlias`, `type X = …`, `TypeVar`, `ParamSpec`, `NewType`, `TypeAliasType`, generic subscripts and `|` unions of types) `type_alias`. Function-local variables are not indexed: Python has no declaration keyword, so every assignment in a function body would become a "definition"; use text search for those.

**Ranking.** Base score 0.5 for a path under one of `[symbols] primary_prefixes`, 0.4 elsewhere under the frontend or backend root, 0.1 otherwise; minus 0.3 for a test path (including mocks and `tests` directories), 0.2 for `object` scope and 0.1 for `local` or `class` scope; never below 0. A `file_hint` match scores 1.0. Ties are broken by scope (module first), path depth, file and line, so neither language wins ties by default.

### Calls through a Protocol

Python language servers resolve a call on a variable typed as a `typing.Protocol` (`gateway: JobGateway`) to the Protocol's method, not to the concrete class's method, so references and callers of `JobGatewayAdapter.active_jobs` list only the calls typed as the adapter. For a Python `method` candidate the reply therefore also says which Protocol methods to query:

- `protocol_method` is `true` when the method is declared in a class that subclasses `Protocol` (text: `in JobGateway (Protocol)`), `false` for every other candidate.
- `protocols` lists, for a method of any other class, each Protocol that declares a method of the same name and that the class matches: `{"name", "id" (module:qualname of the Protocol), "file", "line" (of the Protocol's same-name method)}`, sorted by file and line; `[]` when none, and for every candidate that is not a Python method. Text output adds one line under the candidate: `calls typed as a Protocol it matches (by member names) resolve to: JobGateway.active_jobs (backend/…/processor.py:168), …`.

A class matches a Protocol when it names it as a base, or when every member the Protocol declares is a member of the class. A base counts as naming the Protocol only when it resolves to the Protocol's `module:qualname`, directly or through the bases of an in-repo base class; an unrelated imported class that shares the Protocol's short name does not. Members are methods, annotated or assigned class attributes and nested classes, plus attributes assigned on `self` in `__init__` and `__post_init__`, of the class and of its in-repo base classes. The match is by names only, as runtime Protocol checks are: no signature, annotation or return type is compared. Attributes set elsewhere than `__init__` and `__post_init__`, `__getattr__` and `__slots__` are not seen.

## React Router limits

The `react-router` page provider reads only what the TypeScript parser sees in the configured `modules`:

- a `path={expr}` that is not a string literal is invisible: the route yields no page;
- an `index` route has no `path`, so it yields no page of its own (its parent's path is the page);
- object routers (`createBrowserRouter([{ path: "/x", … }])`) and routes declared in modules that are not listed are unsupported;
- a route whose path is only `*` is the not-found handler and is skipped;
- a listed module that is not a tracked file is a `read_failed` diagnostic.

## Freshness and the restart rule

Every cache is keyed by a fingerprint of exactly the files it reads (and of the config file), so any edit, addition or deletion is picked up by the next call; `refresh` is only for troubleshooting.

- **TypeScript facts** are cached per file by stamp. The cache as a whole is keyed by the Node version, path and binary stamp, the TypeScript directory and version, the stamp of the compiler file the helper loads, and a digest of the helper scripts, so upgrading TypeScript or Node, or reinstalling or patching the same version, drops it automatically.
- **The backend model** is keyed by the stamps of every `.py` file under the backend package, the Python version file, the lock and the library capture file; a rebuild re-parses and re-analyzes only the changed modules, then reassembles the graph. DI is built from the model.
- **The frontend routes scan** is keyed by the stamps of every frontend source file plus the tsconfig and the lockfile, and the toolchain stamp. A missing frontend or an unusable toolchain is cached like a scan until that key changes; a crashed helper is not cached as an answer, so the next routes call runs it again.
- **Routes** are keyed by the backend fingerprint and the scan's key. After a backend-only edit the cached scan is matched against the rebuilt backend model, so the helper does not run again.
- **Stamps.** A file's stamp is its modification time, size, inode and status-change time (ctime). As with git's `core.trustctime`, ctime is what catches a same-size rewrite whose modification time was set back: the kernel updates ctime on every content or metadata change, and nothing can set it back. On Windows ctime is the creation time, so there only the other three fields count.
- **Racy-clean rule.** As in git, a file changed (by modification time or ctime) within two seconds of being stamped may change again without changing its stamp. The content of those files is hashed when the fingerprint is taken, and a cache built from them is kept only while their content still matches: while the window lasts it stays loaded but not `fresh`, a real edit inside the window drops it and the next call rebuilds, and after the window equal content confirms it.
- **The toolchain** is probed again on every call while it is unavailable, and whenever the TypeScript package's `package.json` or the compiler file it names changes, or the node a probe would choose is a different path or file.
- **Docs** (`doc-check` and `doc_refs`): each doc's Markdown facts and each tracked code file's identifier words are cached per file by stamp, under the racy-clean rule, so after an edit only that file is read again. The file list comes from git on every call, DI and the symbol index come from their own caches, and resolution, reachability and the ignore entries run on every call.

**The restart rule.** Python modules are imported once, so a running server keeps the code it started with. The server records a digest of the exact bytes each of its modules was compiled from, and of the repository's `.stack-intel/requirements.txt`. **After a pin bump (or a `git pull` of an editable install), restart the stack-intel MCP server in your client.** Until then every reply leads with `⚠ stack-intel changed since this server started (its code, or the version pinned in .stack-intel/requirements.txt); restart the stack-intel MCP server in your client to load it. These results come from the old code.` and JSON carries `"analyzer_code_stale": true`. A module that was modified so close to its load that its bytes cannot be compared makes replies lead with `⚠ Cannot confirm that the analyzer code this server loaded matches the files on disk; restart the stack-intel MCP server in your client to be sure.`, with `"analyzer_code_stale": null` and `analyzer_code_unconfirmed` listing each `{"file", "reason"}`. The TypeScript helper (`ts/*.cjs`) and the TypeScript package need no restart. Config edits need no restart: the file is read on every call.

The runtime serializes access to its caches with a lock, so concurrent tool calls cannot corrupt them.

### Old interpreter

When the interpreter is older than the backend's (`backend.python_version_file`), backend source cannot be parsed reliably, so the server refuses instead of returning results that silently miss files:

- `di_deps`, `di_reverse`, `route_match`, `route_unmatched`, `doc_refs`, `refresh`, and `symbol_resolve` with `side="backend"` or `"both"` return `runtime_incompatible` with the running and required versions, before any analysis;
- `status` keeps working and names the interpreter; with `load_counts=true` it runs nothing and returns `counts: null` with `counts_error`;
- `jsx_usage` and `symbol_resolve` with `side="frontend"` keep working.

The tool itself requires Python 3.14, so this refusal applies when the backend's version file names a newer interpreter. `doc-check`, `verify`, `summary`, `runtime-check` and the `dump` commands refuse the same way. Without `[backend]` the floor is the tool's own 3.14, so nothing is refused; a tool that needs `[backend]` answers `not_configured` before the interpreter is considered.

## Performance

Measured with the launcher's Python 3.14.7, Node 26.13.0 and TypeScript 7.1.0-beta on an Apple M4 laptop, on a real-world repository of the shape this tool targets: 301 backend files (369 routes, 14 dependency targets), 468 frontend source files (about 11,000 JSX elements and 12,000 declarations), 30 docs with 543 references, 1,407 tracked files in all. Each figure is the median of three runs, in-process, with a warm OS file cache. "Cold" is a fresh runtime; "after an edit" appends a line to one file and calls again 2.2 s later, past the racy-clean window:

| Analysis | Cold | Warm | After editing one file |
| --- | --- | --- | --- |
| DI | 2.21 s | 0.04 s | 0.25 s (a backend `.py`) |
| Routes (alone) | 2.64 s | 0.04 s | 1.05 s (a frontend `.ts`) |
| JSX usage | 0.57 s | 0.04 s | 0.14 s (a `.tsx`) |
| Symbol resolve (`side="both"`) | 0.97 s | 0.06 s | 0.15 s (a `.tsx`) |
| Docs report (DI, symbols and every scope doc) | 5.87 s | 0.19 s | 0.22 s (a scope `.md`) |

Routes cost about the same with or without a prior DI call: the TypeScript helper runs beside the backend build, so the slower of the two sets the time. The `routes` helper builds one program over the whole frontend, so a frontend edit runs it again; a backend edit reuses its cached scan and pays only for the incremental model rebuild and matching. DI, JSX and symbols re-read only the changed file.

End to end through the launcher, every command starts cold (wall time, and the peak memory of the Python process; the TypeScript helper runs as a child process with its own memory):

| Command | Wall time | Peak memory |
| --- | --- | --- |
| `doc-check` | 6.3 s | 361 MB |
| `dump routes --json` | 3.2 s | 298 MB |
| `summary` | 8.0 s | 361 MB |
| `verify` | 12.6 s | 372 MB |

`status` shows each analyzer's last run time for the running server and the facts cache's hit and helper-run counters.
