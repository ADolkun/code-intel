"""FastAPI oracle cases for the router graph: routes, prefixes, includes, mounts.

Each case is a small app package; its expected routes are whatever FastAPI
builds when ``fastapi_oracle.py`` imports it, never hand-written. Sources use
``APP_PKG`` for the case's own package name, so every case imports as
``case_<id>`` and one oracle process can load them all.

Ids ``c06``-``c27`` are edge cases of FastAPI's routing rules; ``g``-ids are
router-graph scenarios: import order, factories and functions called at import,
mounts, router subclasses, values changed in place, aliases. ``INEXACT`` lists
the cases the analyzer cannot match exactly, with the limitations it must report.
Dependency-semantics cases (``c01``-``c05``, ``c10``, ``c14``, ``c16``, ``c17``,
``c25``, ``d``-ids) live in ``fastapi_dependency_cases.py``.
"""

from __future__ import annotations

import textwrap

from code_intel.oracles import Case, case_package

APP_TOKEN = "APP_PKG"

_COMMON = {
    "__init__.py": "",
    "api/__init__.py": "",
    "api/routes/__init__.py": "",
    "deps.py": """
        from fastapi import Depends, Security
        def get_db():
            yield "db"
        def get_user(db=Depends(get_db)):
            return "u"
        def get_admin(user=Depends(get_user)):
            return user
        def verify_key():
            return True
        def audit():
            return True
        """,
}


def _case(case_id: str, files: dict[str, str], *, app: str = "main:app", allow: frozenset[str] = frozenset()) -> Case:
    """A case package: the common modules plus ``files`` (paths relative to the package)."""
    package = case_package(case_id)
    out: dict[str, str] = {}
    for rel, source in {**_COMMON, **files}.items():
        out[f"{package}/{rel}"] = textwrap.dedent(source).lstrip("\n").replace(APP_TOKEN, package)
    module, _, attribute = app.partition(":")
    return Case(files=out, app=f"{package}.{module}:{attribute}", allow=frozenset(allow))


def _chain(length: int) -> str:
    """``l1()`` at import calls ``l2()``, ..., ``l{length}()``, which registers ``/leaf``."""
    lines = ["from fastapi import APIRouter, FastAPI", "router = APIRouter()"]
    lines += [f"def l{length}():", "    router.add_api_route('/leaf', lambda: 1)"]
    for k in range(length - 1, 0, -1):
        lines += [f"def l{k}():", f"    l{k + 1}()"]
    return "\n".join([*lines, "l1()", "app = FastAPI()", "app.include_router(router)", ""])


def _fan_out(width: int, depth: int) -> str:
    """``l1()`` at import; each of ``l1``..``l{depth}`` calls the next ``width``
    times, and ``l{depth + 1}()`` registers ``/leaf``: ``width ** depth`` routes."""
    lines = ["from fastapi import APIRouter, FastAPI", "router = APIRouter()"]
    lines += [f"def l{depth + 1}():", "    router.add_api_route('/leaf', lambda: 1)"]
    for k in range(depth, 0, -1):
        lines += [f"def l{k}():", *([f"    l{k + 1}()"] * width)]
    return "\n".join([*lines, "l1()", "app = FastAPI()", "app.include_router(router)", ""])


CASES: dict[str, Case] = {}

# c06_app_deps
CASES["c06"] = _case(
    "c06",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import verify_key
            app = FastAPI(dependencies=[Depends(verify_key)])
            app.include_router(a_router)
            """,
    },
)

# c07_nested_prefix
CASES["c07"] = _case(
    "c07",
    {
        "api/routes/pkg/__init__.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_user, verify_key
            from .leaf import router as leaf_router
            router = APIRouter(prefix="/v1", dependencies=[Depends(get_user)])
            router.include_router(leaf_router, prefix="/c", dependencies=[Depends(verify_key)])
            """,
        "api/routes/pkg/leaf.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            router = APIRouter(prefix="/items", dependencies=[Depends(get_db)])
            @router.get("/x")
            def leaf_x(): ...
            """,
        "api/routes/parent.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import verify_key
            from APP_PKG.api.routes.child import router as child_router
            router = APIRouter(prefix="/parent")
            router.include_router(child_router, prefix="/kid", dependencies=[Depends(verify_key)])
            @router.get("/own")
            def own(): ...
            """,
        "api/routes/child.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/x")
            def child_x(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.pkg import router as pkg_router
            from APP_PKG.api.routes.parent import router as parent_router
            from APP_PKG.deps import get_admin
            app = FastAPI()
            app.include_router(pkg_router, prefix="/api", dependencies=[Depends(get_admin)])
            app.include_router(parent_router, prefix="/p")
            """,
    },
)

# c08_programmatic
CASES["c08"] = _case(
    "c08",
    {
        "api/routes/prog.py": """
            from fastapi import APIRouter, Depends, WebSocket
            from APP_PKG.deps import get_db, verify_key
            router = APIRouter(prefix="/prog")
            def handler(db=Depends(get_db)): ...
            router.add_api_route("/r", handler, methods=["GET", "POST"], dependencies=[Depends(verify_key)])
            async def ws(websocket: WebSocket, db=Depends(get_db)): ...
            router.add_api_websocket_route("/ws", ws)
            """,
        "main.py": """
            from fastapi import FastAPI, WebSocket, Depends
            from fastapi.staticfiles import StaticFiles
            from APP_PKG.api.routes.prog import router as prog_router
            from APP_PKG.deps import get_user
            app = FastAPI()
            app.include_router(prog_router)
            async def ws_app(websocket: WebSocket, u=Depends(get_user)): ...
            app.add_api_websocket_route("/ws-app", ws_app)
            sub = FastAPI()
            @sub.get("/ping")
            def ping(u=Depends(get_user)): ...
            app.mount("/sub", sub)
            app.mount("/static", StaticFiles(directory="/tmp", check_dir=False))
            """,
    },
    # StaticFiles is not a FastAPI app, so its routes are out of reach.
    allow=frozenset({"asgi_mount"}),
)

# c09_routeclass_apiroute_app
CASES["c09"] = _case(
    "c09",
    {
        "api/routes/rc.py": """
            from fastapi import APIRouter, Depends
            from fastapi.routing import APIRoute
            from APP_PKG.deps import verify_key
            class TimedRoute(APIRoute):
                pass
            router = APIRouter(route_class=TimedRoute, prefix="/rc")
            @router.api_route("/multi", methods=["GET", "POST"])
            def multi(): ...
            @router.api_route("/default")
            def default(): ...
            @router.get("/x", response_model=dict, tags=["t"], dependencies=[Depends(verify_key)])
            def x(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.rc import router as rc_router
            from APP_PKG.deps import get_db
            app = FastAPI()
            app.include_router(rc_router)
            @app.get("/health")
            def health(db=Depends(get_db)): ...
            @app.api_route("/app-multi", methods=["PUT", "PATCH"])
            def app_multi(): ...
            app.dependency_overrides[get_db] = lambda: "fake"
            """,
    },
)

# c11_dup_mount
CASES["c11"] = _case(
    "c11",
    {
        "api/routes/v.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/items")
            def items(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.v import router as v_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(v_router, prefix="/v1")
            app.include_router(v_router, prefix="/v2", dependencies=[Depends(verify_key)])
            """,
    },
)

# c12_constants
CASES["c12"] = _case(
    "c12",
    {
        "api/paths.py": """
            PREFIX = "/k"
            ITEMS_PATH = "/items"
            API_PREFIX = "/api"
            """,
        "api/routes/k.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.api.paths import ITEMS_PATH, PREFIX
            from APP_PKG.deps import verify_key, audit
            LOCAL_PATH = "/local"
            COMMON = [Depends(verify_key)]
            router = APIRouter(prefix=PREFIX, dependencies=COMMON)
            @router.get(LOCAL_PATH)
            def a(): ...
            @router.get(ITEMS_PATH)
            def b(): ...
            @router.get(path="/kwpath")
            def c(): ...
            DEC = [Depends(audit)]
            @router.get("/deps-var", dependencies=DEC)
            def d(): ...
            @router.get("/deps-concat", dependencies=COMMON + [Depends(audit)])
            def e(): ...
            """,
        "api/routes/k2.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/z")
            def z(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.paths import API_PREFIX
            from APP_PKG.api.routes.k import router as k_router
            from APP_PKG.api.routes.k2 import router as k2_router
            from APP_PKG.deps import audit
            VERSION = "v9"
            GUARDS = [Depends(audit)]
            app = FastAPI()
            app.include_router(k_router, prefix=API_PREFIX)
            app.include_router(k2_router, prefix=f"/api/{VERSION}", dependencies=GUARDS)
            """,
    },
)

# c13_app_factory
CASES["c13"] = _case(
    "c13",
    {
        "api/routes/items.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/i")
            def i(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.items import router as items_router
            from APP_PKG.deps import verify_key
            def create_app() -> FastAPI:
                application = FastAPI()
                application.include_router(items_router, prefix="/items", dependencies=[Depends(verify_key)])
                return application
            app = create_app()
            """,
    },
)

# c15_outside_routes_dir
CASES["c15"] = _case(
    "c15",
    {
        "health.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            router = APIRouter(prefix="/health")
            @router.get("/live")
            def live(db=Depends(get_db)): ...
            """,
        "main.py": """
            from fastapi import FastAPI, APIRouter, Depends
            from APP_PKG.health import router as health_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(health_router, dependencies=[Depends(verify_key)])
            local = APIRouter(prefix="/local")
            @local.get("/l")
            def l(): ...
            app.include_router(local, dependencies=[Depends(verify_key)])
            """,
    },
)

# c18_include_attr_in_pkg
CASES["c18"] = _case(
    "c18",
    {
        "api/routes/grp/__init__.py": """
            from fastapi import APIRouter
            from . import one, two
            router = APIRouter()
            router.include_router(one.router, prefix="/one")
            router.include_router(two.router, prefix="/two")
            """,
        "api/routes/grp/one.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/x")
            def one_x(): ...
            """,
        "api/routes/grp/two.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/x")
            def two_x(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.grp import router as grp_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(grp_router, prefix="/grp", dependencies=[Depends(verify_key)])
            """,
    },
)

# c19_router_factory_and_ws
CASES["c19"] = _case(
    "c19",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends, WebSocket
            from APP_PKG.deps import verify_key, get_user
            def make_router(**kw) -> APIRouter:
                return APIRouter(**kw)
            router = make_router(prefix="/made")
            @router.get("/m")
            def m(): ...
            ws_router = APIRouter(prefix="/ws", dependencies=[Depends(verify_key)])
            @ws_router.websocket("/live", dependencies=[Depends(get_user)])
            async def live(websocket: WebSocket): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.a import router as a_router, ws_router
            app = FastAPI()
            app.include_router(a_router)
            app.include_router(ws_router)
            """,
    },
)

# c20_unpack_and_dict
CASES["c20"] = _case(
    "c20",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import verify_key, audit
            router = APIRouter()
            @router.get("/a", dependencies=[*[Depends(verify_key)], Depends(audit)])
            def a(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            MOUNT = dict(prefix="/kw", dependencies=[Depends(verify_key)])
            app.include_router(a_router, **MOUNT)
            """,
    },
)

# c21_dead_router
CASES["c21"] = _case(
    "c21",
    {
        "api/routes/live.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/live")
            def live(): ...
            """,
        "api/routes/dead.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            router = APIRouter(prefix="/dead")
            @router.get("/d")
            def d(db=Depends(get_db)): ...
            def register(r: APIRouter):
                @r.get("/registered")
                def registered(): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.live import router as live_router
            app = FastAPI()
            app.include_router(live_router)
            """,
    },
    # The dead router is reported, never live.
    allow=frozenset({"unmounted_router"}),
)

# c22_shared_router
CASES["c22"] = _case(
    "c22",
    {
        "api/routes/shared/__init__.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/shared")
            from . import a, b  # noqa: E402  register routes on the shared router
            """,
        "api/routes/shared/a.py": """
            from . import router
            @router.get("/a")
            def sa(): ...
            """,
        "api/routes/shared/b.py": """
            from APP_PKG.api.routes.shared import router
            @router.get("/b")
            def sb(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.shared import router as shared_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(shared_router, prefix="/api", dependencies=[Depends(verify_key)])
            """,
    },
)

# c23_same_file_nesting
CASES["c23"] = _case(
    "c23",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import verify_key
            _PREFIX = "/a"
            router = APIRouter(prefix=_PREFIX)
            sub = APIRouter()
            @sub.get("/s")
            def s(): ...
            @router.get("/own")
            def own(): ...
            router.include_router(sub, prefix="/sub", dependencies=[Depends(verify_key)])
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.a import router as a_router
            app = FastAPI()
            app.include_router(a_router, prefix="/x" + "/y")
            """,
    },
)

# c24_reexport_router_pkg_init_routes
CASES["c24"] = _case(
    "c24",
    {
        "api/routes/r/__init__.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/r")
            @router.get("/init-route")
            def init_route(): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.r import router as r_router
            app = FastAPI()
            app.include_router(r_router, prefix="/api")
            """,
    },
)

# c26_file_default_false_guard
CASES["c26"] = _case(
    "c26",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            open_router = APIRouter()
            @router.get("/guarded")
            def guarded(): ...
            @open_router.get("/public")
            def public(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes import a as a_mod
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(a_router, prefix="/admin", dependencies=[Depends(verify_key)])
            app.include_router(a_mod.open_router, prefix="/open")
            """,
    },
)

# c27_file_default_nested_same_file
CASES["c27"] = _case(
    "c27",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import audit
            router = APIRouter()
            sub = APIRouter()
            @sub.get("/s")
            def s(): ...
            router.include_router(sub, prefix="/sub", dependencies=[Depends(audit)])
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(a_router, prefix="/a", dependencies=[Depends(verify_key)])
            """,
    },
)


# ── Ported scenarios ────────────────────────────────────────────────────

# g01: route packages, re-export barrels and two routers in one module, as in
# api/routes/{appointments,admin,articles}.
CASES["g01"] = _case(
    "g01",
    {
        "guards.py": """
            def require(permission):
                def check():
                    return permission
                return check
            """,
        "api/routes/squads.py": """
            from fastapi import APIRouter
            router = APIRouter(tags=["squads"])
            @router.get("/squads")
            def list_squads(): ...
            """,
        "api/routes/appointments/__init__.py": """
            from fastapi import APIRouter
            from .approval import router as approval_router
            from .detail import router as detail_router
            router = APIRouter()
            _PREFIX = "/appointments"
            router.include_router(approval_router, prefix=_PREFIX)
            router.include_router(detail_router, prefix=_PREFIX)
            """,
        "api/routes/appointments/approval.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.post("/{appointment_id}/approve")
            def approve_appointment(): ...
            """,
        "api/routes/appointments/detail.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/{item_id}")
            def get_item(): ...
            """,
        "api/routes/users/__init__.py": """
            from fastapi import APIRouter
            from .detail import router as detail_router
            router = APIRouter()
            _PREFIX = "/users"
            router.include_router(detail_router, prefix=_PREFIX)
            """,
        "api/routes/users/detail.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/{item_id}")
            def get_item(): ...
            """,
        "api/routes/admin/__init__.py": """
            from .emails import router as emails_router
            __all__ = ["emails_router"]
            """,
        "api/routes/admin/emails.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/admin/failed-emails")
            @router.get("")
            def list_dead_letter_entries(): ...
            """,
        "api/routes/articles/__init__.py": """
            from fastapi import APIRouter
            from .lifecycle import router as lifecycle_router, root_router as lifecycle_root_router
            router = APIRouter()
            _PREFIX = "/articles"
            router.include_router(lifecycle_router, prefix=_PREFIX)
            router.include_router(lifecycle_root_router)
            """,
        "api/routes/articles/lifecycle.py": """
            from fastapi import APIRouter
            router = APIRouter()
            root_router = APIRouter()
            @router.get("/{article_id}")
            def get_article(): ...
            @root_router.get("/camo-types")
            def list_camo_types(): ...
            """,
        "api/routes/star/__init__.py": """
            from .base import *
            """,
        "api/routes/star/base.py": """
            from fastapi import APIRouter
            __all__ = ["router"]
            router = APIRouter(prefix="/star")
            helper = APIRouter(prefix="/hidden")
            @router.get("/s")
            def star(): ...
            """,
        "api/routes/explicit/__init__.py": """
            from .base import router
            """,
        "api/routes/explicit/base.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/base")
            @router.get("/e")
            def explicit(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.squads import router as squads_router
            from APP_PKG.api.routes.appointments import router as appointments_router
            from APP_PKG.api.routes.users import router as users_router
            from APP_PKG.api.routes.admin import emails_router as admin_emails_router
            from APP_PKG.api.routes.articles import router as articles_router
            from APP_PKG.api.routes import star, explicit
            from APP_PKG.deps import verify_key, audit
            from APP_PKG.guards import require
            app = FastAPI()
            app.include_router(squads_router, dependencies=[Depends(verify_key)])
            app.include_router(appointments_router, dependencies=[Depends(verify_key)])
            app.include_router(users_router)
            app.include_router(
                admin_emails_router,
                dependencies=[Depends(require("auth.manage")), Depends(verify_key)],
            )
            app.include_router(articles_router, dependencies=[Depends(audit)])
            app.include_router(star.router, prefix="/api", dependencies=[Depends(verify_key)])
            app.include_router(explicit.router, prefix="/api")
            """,
    },
)

# g02: names rebound before or after the include, at import time, in every
# binding form Python has: the include sees whatever the name holds when it runs.
CASES["g02"] = _case(
    "g02",
    {
        "routers.py": """
            from fastapi import APIRouter
            def _make(tag):
                r = APIRouter()
                @r.get("/" + tag)
                def h(): ...
                return r
            orig = APIRouter()
            @orig.get("/orig")
            def o(): ...
            other = APIRouter()
            @other.get("/other")
            def t(): ...
            """,
        "skip.py": """
            other = 1
            """,
        "main.py": """
            from typing import Any
            from fastapi import FastAPI, APIRouter
            from APP_PKG.routers import orig, other
            from APP_PKG.routers import orig as q3, orig as q7, orig as q8, orig as q8b, orig as q9
            from APP_PKG.routers import orig as q10a, orig as q10b, orig as q11c, orig as star_kept
            app = FastAPI()

            q3 = other  # rebound before the include
            app.include_router(q3, prefix="/q3")

            app.include_router(q7, prefix="/q7")
            q7 = other  # rebound after the include

            q8: APIRouter  # an annotation alone binds nothing
            app.include_router(q8, prefix="/q8")

            q8b: Any = other
            app.include_router(q8b, prefix="/q8b")

            if (q9 := other) is None:
                pass
            app.include_router(q9, prefix="/q9")

            def default(x=(q10a := other)): ...
            app.include_router(q10a, prefix="/q10a")

            def keep(_):
                return lambda f: f

            @keep(q10b := other)
            def decorated(): ...
            app.include_router(q10b, prefix="/q10b")

            class C:
                global q11c
                q11c = other
            app.include_router(q11c, prefix="/q11c")

            from APP_PKG.skip import *  # noqa: F403  binds only `other`
            app.include_router(star_kept, prefix="/star")
            """,
    },
)

# g03: one router included more than once, under one name or two aliases,
# and two same-named routers from different modules.
CASES["g03"] = _case(
    "g03",
    {
        "api/routes/one.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/x")
            def one(): ...
            """,
        "api/routes/two.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/x")
            def two(): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.one import router as first, router as second
            from APP_PKG.api.routes import one, two
            app = FastAPI()
            app.include_router(first, prefix="/a")
            app.include_router(second, prefix="/b")
            app.include_router(one.router, prefix="/c")
            app.include_router(two.router, prefix="/c")
            """,
    },
)

# g04: includes in positions that run at import although they look nested:
# a default, a decorator argument, the outermost comprehension iterable, a
# bare try body and a with body.
CASES["g04"] = _case(
    "g04",
    {
        "api/routes/child.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/c")
            def child(): ...
            """,
        "main.py": """
            import contextlib
            from fastapi import FastAPI
            from APP_PKG.api.routes.child import router as child
            app = FastAPI()

            def with_default(x=app.include_router(child, prefix="/default")): ...

            def keep(_):
                return lambda f: f

            @keep(app.include_router(child, prefix="/decorator"))
            def decorated(): ...

            _ = [0 for _ in [app.include_router(child, prefix="/iterable")]]

            try:
                app.include_router(child, prefix="/try")
            except ValueError:
                pass

            with contextlib.nullcontext():
                app.include_router(child, prefix="/with")
            """,
    },
)

# g05: includes inside functions and class bodies. A class body runs at import
# with its own names; a function's local import and parameters shadow the
# module's names.
CASES["g05"] = _case(
    "g05",
    {
        "api/routes/public.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/public")
            def public(): ...
            """,
        "api/routes/secure.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/secure")
            def secure(): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.public import router as child
            app = FastAPI()

            class Mounts:
                from APP_PKG.api.routes.secure import router as child
                app.include_router(child, prefix="/class")

            app.include_router(child, prefix="/module")

            def unrelated():
                from APP_PKG.api.routes.public import router as child
                return child

            def install():
                from APP_PKG.api.routes.secure import router as child
                app.include_router(child, prefix="/fn")

            install()
            """,
    },
    # Function bodies are not followed from their call sites, so the
    # function's include is listed (last, as it runs last) and reported.
    allow=frozenset({"deferred_router_mount"}),
)

# g06: package initializers that rebind or assemble the name main imports:
# an import replaced by a local router, and a function-local import that only
# shares the name.
CASES["g06"] = _case(
    "g06",
    {
        "api/routes/things/__init__.py": """
            from fastapi import APIRouter
            from .base import router
            from .leaf import router as leaf_router
            router = APIRouter()
            router.include_router(leaf_router, prefix="/things")
            """,
        "api/routes/things/base.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/obsolete")
            def obsolete_route(): ...
            """,
        "api/routes/things/leaf.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/live")
            def live_route(): ...
            """,
        "api/routes/feature/__init__.py": """
            from fastapi import APIRouter
            from .child import router as child_router
            router = APIRouter()
            router.include_router(child_router)
            def unrelated():
                from .other import router
                return router
            """,
        "api/routes/feature/child.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/thing")
            def child_route(): ...
            """,
        "api/routes/feature/other.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/other")
            def other_route(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.things import router as things_router
            from APP_PKG.api.routes.feature import router as feature_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(things_router, dependencies=[Depends(verify_key)])
            app.include_router(feature_router, prefix="/api")
            """,
    },
    # things.base and feature.other are never included.
    allow=frozenset({"unmounted_router"}),
)

# g07: router prefixes built from a constant, an f-string and **options, and
# stacked route decorators that each carry their own guards.
CASES["g07"] = _case(
    "g07",
    {
        "api/routes/stacked.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import audit, verify_key
            VERSION = "v1"
            OPTIONS = {"prefix": "/k", "dependencies": [Depends(audit)]}
            named = APIRouter(prefix="/" + VERSION)
            formatted = APIRouter(prefix=f"/{VERSION}/x")
            spread = APIRouter(**OPTIONS)
            plain = APIRouter(prefix="/things")
            @named.get("/a")
            def a(): ...
            @formatted.get("/b")
            def b(): ...
            @spread.get("/c")
            def c(): ...
            @plain.get("/read", dependencies=[Depends(verify_key)])
            @plain.post("/write", dependencies=[Depends(audit)])
            @plain.delete("/open")
            def handle(): ...
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.stacked import formatted, named, plain, spread
            app = FastAPI()
            app.include_router(named)
            app.include_router(formatted)
            app.include_router(spread)
            app.include_router(plain)
            """,
    },
)

# g08: a route added to an imported router after the include is not copied.
CASES["g08"] = _case(
    "g08",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/early")
            def early(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            app.include_router(router)
            @router.get("/late")
            def late(): return 1
            """,
    },
)

# g09: a module nothing imports registers a route on an imported router: it never runs.
CASES["g09"] = _case(
    "g09",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "extra.py": """
            from .a import router
            @router.get("/extra")
            def extra(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unimported_router_change"}),
)

# g10: a module nothing imports includes a router into the app: it never runs.
CASES["g10"] = _case(
    "g10",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "wiring.py": """
            from .main import app
            from .a import router
            app.include_router(router)
            """,
        "main.py": """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get("/root")
            def root(): return 1
            """,
    },
    allow=frozenset({"unimported_router_change", "unmounted_router"}),
)

# g11: a module imported after the include registers on the included router: too late.
CASES["g11"] = _case(
    "g11",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "b.py": """
            from .a import router
            @router.get("/b")
            def b(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            app.include_router(router)
            from . import b  # noqa
            """,
    },
)

# g12: a module imported before a same-module include registers on its router (circular import).
CASES["g12"] = _case(
    "g12",
    {
        "extra.py": """
            from .main import router
            @router.get("/e")
            def e(): return 1
            """,
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            from . import extra  # noqa
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g13: a module the app module imports includes into the app through app.router.
CASES["g13"] = _case(
    "g13",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "wiring.py": """
            from .main import app
            from .a import router
            app.router.include_router(router, prefix="/w")
            """,
        "main.py": """
            from fastapi import FastAPI
            app = FastAPI()
            from . import wiring  # noqa
            """,
    },
)

# g14: include_router copies routes but drops a mount made on the included router.
CASES["g14"] = _case(
    "g14",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            sub = FastAPI()
            @sub.get("/s")
            def s(): return 1
            router = APIRouter()
            router.mount("/m", sub)
            @router.get("/r")
            def r(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unmounted_router"}),
)

# g15: a mount path's trailing slash is stripped.
CASES["g15"] = _case(
    "g15",
    {
        "main.py": """
            from fastapi import FastAPI
            sub = FastAPI()
            @sub.get("/s")
            def s(): return 1
            app = FastAPI()
            app.mount("/sub/", sub)
            """,
    },
)

# g16: nested FastAPI mounts, each with its own app dependencies.
CASES["g16"] = _case(
    "g16",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import verify_key, audit
            inner = FastAPI(dependencies=[Depends(audit)])
            @inner.get("/i")
            def i(): return 1
            mid = FastAPI(dependencies=[Depends(verify_key)])
            mid.mount("/in", inner)
            @mid.get("/m")
            def m(): return 1
            app = FastAPI(dependencies=[Depends(verify_key)])
            app.mount("/mid", mid)
            """,
    },
)

# g17: including a FastAPI sub-app's .router.
CASES["g17"] = _case(
    "g17",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import verify_key, audit
            sub = FastAPI(dependencies=[Depends(audit)])
            @sub.get("/s")
            def s(): return 1
            app = FastAPI(dependencies=[Depends(verify_key)])
            app.include_router(sub.router, prefix="/sub")
            """,
    },
)

# g18: a mount is by reference: routes added to the sub-app later are live.
CASES["g18"] = _case(
    "g18",
    {
        "main.py": """
            from fastapi import FastAPI
            sub = FastAPI()
            app = FastAPI()
            app.mount("/sub", sub)
            @sub.get("/later")
            def later(): return 1
            """,
    },
)

# g19: a router subclass whose __init__ sets the prefix and dependencies.
CASES["g19"] = _case(
    "g19",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key
            class VersionedRouter(APIRouter):
                def __init__(self, **kw):
                    super().__init__(prefix="/v1", dependencies=[Depends(verify_key)], **kw)
            router = VersionedRouter()
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"custom_router_class", "unresolved_dependencies", "unresolved_route_prefix"}),
)

# g20: a router subclass imported from another module.
CASES["g20"] = _case(
    "g20",
    {
        "base.py": """
            from fastapi import APIRouter
            class MyRouter(APIRouter):
                pass
            """,
        "a.py": """
            from .base import MyRouter
            router = MyRouter(prefix="/a")
            @router.get("/x")
            def x(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g21: a router built by a classmethod of a router subclass.
CASES["g21"] = _case(
    "g21",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            class R(APIRouter):
                @classmethod
                def make(cls):
                    return cls(prefix="/made")
            router = R.make()
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_include", "unresolved_route_router"}),
)

# g22: FastAPI(prefix=...) is not a FastAPI parameter and is ignored.
CASES["g22"] = _case(
    "g22",
    {
        "main.py": """
            from fastapi import FastAPI
            app = FastAPI(prefix="/api")
            @app.get("/x")
            def x(): return 1
            """,
    },
)

# g23: redirect_slashes from a module constant.
CASES["g23"] = _case(
    "g23",
    {
        "main.py": """
            from fastapi import FastAPI
            REDIRECT = False
            app = FastAPI(redirect_slashes=REDIRECT)
            @app.get("/x")
            def x(): return 1
            """,
    },
)

# g24: APIRouter(routes=[...]) adds routes the analysis does not read.
CASES["g24"] = _case(
    "g24",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            from fastapi.routing import APIRoute
            def h(): return 1
            router = APIRouter(routes=[APIRoute("/ctor", h)])
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unsupported_router_argument"}),
)

# g25: a route decorator bound to a name first (get = router.get).
CASES["g25"] = _case(
    "g25",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            get = router.get
            @get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"opaque_router_call"}),
)

# g26: include_router bound to a name first.
CASES["g26"] = _case(
    "g26",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            include = app.include_router
            include(router, prefix="/x")
            """,
    },
    allow=frozenset({"opaque_router_call", "unmounted_router"}),
)

# g27: a methods= name rebound after the route is registered.
CASES["g27"] = _case(
    "g27",
    {
        "main.py": """
            from fastapi import FastAPI
            M = ["GET"]
            app = FastAPI()
            @app.api_route("/x", methods=M)
            def h(): return 1
            M = ["POST"]
            """,
    },
)

# g28: a dependencies list changed in place before the router is built.
CASES["g28"] = _case(
    "g28",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            COMMON.append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g29: an include's **options dict changed by item assignment.
CASES["g29"] = _case(
    "g29",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            OPTS = {"prefix": "/a"}
            OPTS["prefix"] = "/b"
            router = APIRouter()
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router, **OPTS)
            """,
    },
    allow=frozenset({"unresolved_dependencies", "unresolved_include_prefix"}),
)

# g30: a prefix read from an instance attribute its __init__ sets.
CASES["g30"] = _case(
    "g30",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            class Cfg:
                prefix = "/default"
                def __init__(self, prefix=None):
                    if prefix:
                        self.prefix = prefix
            cfg = Cfg("/custom")
            router = APIRouter(prefix=cfg.prefix)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_route_prefix"}),
)

# g31: an app factory includes a module router; a route added after the factory call is not copied.
CASES["g31"] = _case(
    "g31",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            @router.get("/early")
            def early(): return 1
            def create_app():
                app = FastAPI()
                app.include_router(router)
                return app
            app = create_app()
            @router.get("/late")
            def late(): return 1
            """,
    },
)

# g32: a route registered in an app factory's body comes after earlier module-level routes.
CASES["g32"] = _case(
    "g32",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            def create_app():
                @router.get("/b")
                def b(): return 1
                app = FastAPI()
                app.include_router(router)
                return app
            app = create_app()
            """,
    },
)

# g33: a function called at import registers routes before the include.
CASES["g33"] = _case(
    "g33",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            def add_routes():
                @router.get("/f")
                def f(): return 1
            add_routes()
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g34: a router built and included inside a function called at import.
CASES["g34"] = _case(
    "g34",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            app = FastAPI()
            def setup():
                r = APIRouter(prefix="/s")
                @r.get("/x")
                def x(): return 1
                app.include_router(r)
            setup()
            """,
    },
)

# g35: a package function called at import registers a route on the app it is passed.
CASES["g35"] = _case(
    "g35",
    {
        "extras.py": """
            def register(app):
                @app.get("/extra")
                def extra(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .extras import register
            app = FastAPI()
            register(app)
            """,
    },
)

# g36: a router factory that includes another router factory.
CASES["g36"] = _case(
    "g36",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            def make_inner():
                r = APIRouter(prefix="/in")
                @r.get("/x")
                def x(): return 1
                return r
            def make_outer():
                o = APIRouter(prefix="/out")
                o.include_router(make_inner())
                return o
            app = FastAPI()
            app.include_router(make_outer())
            """,
    },
)

# g37: an app factory calling a router factory from another module.
CASES["g37"] = _case(
    "g37",
    {
        "factories.py": """
            from fastapi import APIRouter
            def make(prefix):
                r = APIRouter(prefix=prefix)
                @r.get("/x")
                def x(): return 1
                return r
            """,
        "main.py": """
            from fastapi import FastAPI
            from .factories import make
            def create_app():
                app = FastAPI()
                app.include_router(make("/a"))
                app.include_router(make(prefix="/b"))
                return app
            app = create_app()
            """,
    },
)

# g38: the same, with the factory module sorting after the app module.
CASES["g38"] = _case(
    "g38",
    {
        "zfactories.py": """
            from fastapi import APIRouter
            def make(prefix):
                r = APIRouter(prefix=prefix)
                @r.get("/x")
                def x(): return 1
                return r
            """,
        "main.py": """
            from fastapi import FastAPI
            from .zfactories import make
            def create_app():
                app = FastAPI()
                app.include_router(make("/a"))
                return app
            app = create_app()
            """,
    },
)

# g39: one factory called three times on one line.
CASES["g39"] = _case(
    "g39",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            def make(p):
                r = APIRouter(prefix=p)
                @r.get("/x")
                def x(): return 1
                return r
            app = FastAPI()
            app.include_router(make("/a")); app.include_router(make("/b")); app.include_router(make("/c"))
            """,
    },
)

# g40: an app factory includes a module router returned by a helper.
CASES["g40"] = _case(
    "g40",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter(prefix="/r")
            @router.get("/x")
            def x(): return 1
            def build():
                return router
            def create_app():
                app = FastAPI()
                app.include_router(build())
                return app
            app = create_app()
            """,
    },
)

# g41: a factory parameter's default is the prefix, and a module constant has the same name.
CASES["g41"] = _case(
    "g41",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            prefix = "/module"
            def make(prefix="/default"):
                r = APIRouter(prefix=prefix)
                @r.get("/x")
                def x(): return 1
                return r
            app = FastAPI()
            app.include_router(make())
            """,
    },
)

# g42: a factory-local path variable hides a module constant.
CASES["g42"] = _case(
    "g42",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            PATH = "/module"
            def make():
                PATH = "/local"
                r = APIRouter()
                @r.get(PATH)
                def x(): return 1
                return r
            app = FastAPI()
            app.include_router(make())
            """,
    },
)

# g43: an app factory's local prefix variable hides a module constant.
CASES["g43"] = _case(
    "g43",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            prefix = "/old"
            def create_app():
                prefix = "/api"
                app = FastAPI()
                app.include_router(router, prefix=prefix)
                return app
            app = create_app()
            """,
    },
)

# g44: a router used through a star import.
CASES["g44"] = _case(
    "g44",
    {
        "routes.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/r")
            @router.get("/x")
            def x(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .routes import *
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g45: a router re-exported through two levels of star imports.
CASES["g45"] = _case(
    "g45",
    {
        "api/__init__.py": """
            from .v1 import *
            """,
        "api/v1/__init__.py": """
            from .users import *
            """,
        "api/v1/users.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/users")
            @router.get("/me")
            def me(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .api import router
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g46: an include under if __name__ == "__main__" never runs on import.
CASES["g46"] = _case(
    "g46",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/a")
            def a(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            if __name__ == "__main__":
                app.include_router(router)
            @app.get("/live")
            def live(): return 1
            """,
    },
    allow=frozenset({"unmounted_router"}),
)

# g47: a TYPE_CHECKING if/else import of the router.
CASES["g47"] = _case(
    "g47",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/a")
            @router.get("/x")
            def x(): return 1
            """,
        "b.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/b")
            @router.get("/x")
            def x(): return 1
            """,
        "main.py": """
            from typing import TYPE_CHECKING
            from fastapi import FastAPI
            if TYPE_CHECKING:
                from .a import router
            else:
                from .b import router
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unmounted_router"}),
)

# g48: a lambda endpoint.
CASES["g48"] = _case(
    "g48",
    {
        "main.py": """
            from fastapi import FastAPI
            app = FastAPI()
            app.add_api_route("/l", lambda: 1)
            """,
    },
)

# g49: app.host(...) of a sub-app is a Starlette route the analysis reports, not follows.
CASES["g49"] = _case(
    "g49",
    {
        "main.py": """
            from fastapi import FastAPI
            sub = FastAPI()
            @sub.get("/s")
            def s(): return 1
            app = FastAPI()
            app.host("api.example.com", sub)
            @app.get("/x")
            def x(): return 1
            """,
    },
    allow=frozenset({"unmounted_router", "unsupported_route_registration"}),
)

# g50: a recursive function called at module level in a router module.
CASES["g50"] = _case(
    "g50",
    {
        "main.py": """
            from fastapi import FastAPI
            def again(n):
                if n < 0:
                    raise ValueError(n)
                return again(n - 1)
            try:
                value = again(2)
            except ValueError:
                value = None
            app = FastAPI()
            @app.get("/x")
            def x(): return 1
            """,
    },
)

# g51: app routes interleaved with includes keep Starlette's dispatch order.
CASES["g51"] = _case(
    "g51",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/items/{x}")
            def a(x: str): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .a import router
            app = FastAPI()
            @app.get("/items/special")
            def special(): return 1
            app.include_router(router)
            @app.get("/items/late")
            def late(): return 1
            """,
    },
)

# g52: the same router included twice with a route added between.
CASES["g52"] = _case(
    "g52",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            router = APIRouter()
            @router.get("/one")
            def one(): return 1
            app = FastAPI()
            app.include_router(router, prefix="/v1", dependencies=[Depends(verify_key)])
            @router.get("/two")
            def two(): return 1
            app.include_router(router, prefix="/v2", dependencies=[Depends(audit)])
            """,
    },
)

# g53: a route decorator on a method in a class body.
CASES["g53"] = _case(
    "g53",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import get_db
            router = APIRouter()
            class Views:
                @router.get("/v")
                def v(self, db=Depends(get_db)): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g54: a walrus include, then routes added to that router.
CASES["g54"] = _case(
    "g54",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            app = FastAPI()
            app.include_router(r := APIRouter(prefix="/w"))
            @r.get("/x")
            def x(): return 1
            @app.get("/y")
            def y(): return 1
            """,
    },
)

# g55: a WebSocket route on an included router with include dependencies and Security.
CASES["g55"] = _case(
    "g55",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends, Security, WebSocket
            from .deps import verify_key, audit, get_user
            router = APIRouter(dependencies=[Security(verify_key)])
            @router.websocket("/ws")
            async def ws(websocket: WebSocket, u=Depends(get_user)): pass
            app = FastAPI()
            app.include_router(router, prefix="/p", dependencies=[Depends(audit)])
            """,
    },
)

# g56: fastapi.routing.APIRouter and aliased constructors.
CASES["g56"] = _case(
    "g56",
    {
        "main.py": """
            import fastapi.routing
            from fastapi import FastAPI as F, APIRouter as R
            a = fastapi.routing.APIRouter(prefix="/a")
            b = R(prefix="/b")
            @a.get("/x")
            def ax(): return 1
            @b.get("/x")
            def bx(): return 1
            app = F()
            app.include_router(a)
            app.include_router(b)
            """,
    },
)

# g57: a router reached as a module attribute after import pkg.mod.
CASES["g57"] = _case(
    "g57",
    {
        "api/__init__.py": """

            """,
        "api/v.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/v")
            @router.get("/x")
            def x(): return 1
            """,
        "main.py": """
            import APP_PKG.api.v
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(APP_PKG.api.v.router)
            """,
    },
)

# g58: paths and prefixes from another module's constants.
CASES["g58"] = _case(
    "g58",
    {
        "config.py": """
            API = "/api"
            VERSION = "v2"
            """,
        "main.py": """
            from fastapi import APIRouter, FastAPI
            from . import config
            router = APIRouter(prefix=f"{config.API}/{config.VERSION}")
            @router.get(config.API + "/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router, prefix=f"/{config.VERSION}")
            """,
    },
)

# g59: a decorator route registered in a module-level for loop.
CASES["g59"] = _case(
    "g59",
    {
        "main.py": """
            from fastapi import FastAPI
            app = FastAPI()
            for name in ("a", "b"):
                @app.get(f"/{name}")
                def h(): return 1
            """,
    },
    allow=frozenset({"unresolved_route_path"}),
)

# g60: dependencies= built from another module's constant tuple through list(...).
CASES["g60"] = _case(
    "g60",
    {
        "guards.py": """
            from fastapi import Depends, Security
            from .deps import verify_key, audit
            GUARDS = (Depends(verify_key),)
            MORE = [*GUARDS, Security(audit)]
            """,
        "main.py": """
            from fastapi import FastAPI, APIRouter
            from .guards import MORE, GUARDS
            router = APIRouter(dependencies=list(GUARDS))
            @router.get("/x")
            def x(): return 1
            app = FastAPI(dependencies=MORE)
            app.include_router(router)
            """,
    },
)

# g61: a router imported in try/except ImportError with a fallback router.
CASES["g61"] = _case(
    "g61",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/a")
            @router.get("/x")
            def x(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI, APIRouter
            try:
                from .a import router
            except ImportError:
                router = APIRouter(prefix="/fallback")
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unmounted_router", "unresolved_include"}),
)

# g62: a router name deleted and bound again.
CASES["g62"] = _case(
    "g62",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter(prefix="/one")
            @router.get("/x")
            def x(): return 1
            del router
            router = APIRouter(prefix="/two")
            @router.get("/y")
            def y(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unmounted_router"}),
)

# g63: two functions called at import that call each other until a guard stops them.
CASES["g63"] = _case(
    "g63",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            def ping(n):
                if n <= 0:
                    @router.get("/done")
                    def done(): return 1
                    return
                pong(n - 1)
            def pong(n):
                ping(n)
            ping(3)
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"recursive_router_call"}),
)

# g64: a helper called at import that calls itself four times, one level deep.
CASES["g64"] = _case(
    "g64",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            def build(n, path):
                if n <= 0:
                    router.add_api_route(path, lambda: 1)
                    return
                build(n - 1, path + "/a")
                build(n - 1, path + "/b")
                build(n - 1, path + "/c")
                build(n - 1, path + "/d")
            build(1, "")
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"recursive_router_call"}),
)

# g65: an if/else at module level whose branches import different modules that register routes.
CASES["g65"] = _case(
    "g65",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            """,
        "b.py": """
            from .a import router
            @router.get("/b")
            def b(): return 1
            """,
        "c.py": """
            from .a import router
            @router.get("/c")
            def c(): return 1
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            if os.environ.get("NOPE_NOT_SET"):
                from . import b  # noqa
            else:
                from . import c  # noqa
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"conditional_import"}),
)

# g66: a module that registers a route, imported under a branch that does not run.
CASES["g66"] = _case(
    "g66",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            """,
        "b.py": """
            from .a import router
            @router.get("/b")
            def b(): return 1
            """,
        "main.py": """
            import sys
            from fastapi import FastAPI
            from .a import router
            if sys.version_info < (3, 0):
                from . import b  # noqa
            app = FastAPI()
            app.include_router(router)
            @app.get("/live")
            def live(): return 1
            """,
    },
    allow=frozenset({"conditional_import"}),
)

# g67: a dependencies list changed in place through a second name before a router reads it.
CASES["g67"] = _case(
    "g67",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            ALIAS = COMMON
            ALIAS.append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g68: a second name for a dependencies list, extended by another module.
CASES["g68"] = _case(
    "g68",
    {
        "guards.py": """
            from fastapi import Depends
            from .deps import verify_key
            COMMON = [Depends(verify_key)]
            EXTRA = COMMON
            """,
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import audit
            from .guards import EXTRA, COMMON
            EXTRA.extend([Depends(audit)])
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g69: a prefix changed with setattr on the module that defines it.
CASES["g69"] = _case(
    "g69",
    {
        "config.py": """
            PREFIX = "/old"
            """,
        "main.py": """
            from fastapi import APIRouter, FastAPI
            from . import config
            setattr(config, "PREFIX", "/new")
            router = APIRouter(prefix=config.PREFIX)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_route_prefix"}),
)

# g70: a router's dependencies list bound to a name and changed through it.
CASES["g70"] = _case(
    "g70",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key
            router = APIRouter()
            deps = router.dependencies
            deps.append(Depends(verify_key))
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"opaque_router_change"}),
)

# g71: a router subclass whose add_api_route adds a dependency and calls super().
CASES["g71"] = _case(
    "g71",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key
            class Guarded(APIRouter):
                def add_api_route(self, path, endpoint, **kw):
                    kw["dependencies"] = [*(kw.get("dependencies") or []), Depends(verify_key)]
                    return super().add_api_route(path, endpoint, **kw)
            router = Guarded()
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"custom_router_class"}),
)

# g72: a router built through a subclass, from another module, of a class whose __init__ sets the prefix.
CASES["g72"] = _case(
    "g72",
    {
        "routers.py": """
            from fastapi import APIRouter
            class Base(APIRouter):
                def __init__(self, **kw):
                    kw.setdefault("prefix", "/base")
                    super().__init__(**kw)
            class Leaf(Base):
                pass
            """,
        "main.py": """
            from fastapi import FastAPI
            from .routers import Leaf
            router = Leaf()
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"custom_router_class", "unresolved_dependencies", "unresolved_route_prefix"}),
)

# g73: routes= passed to APIRouter through a **mapping.
CASES["g73"] = _case(
    "g73",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            from fastapi.routing import APIRoute
            def h(): return 1
            OPTS = {"routes": [APIRoute("/ctor", h)], "prefix": "/p"}
            router = APIRouter(**OPTS)
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unsupported_router_argument"}),
)

# g74: a function that registers a route, called at import from a loop over literal paths.
CASES["g74"] = _case(
    "g74",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            def add(path):
                @router.get(path)
                def h(): return 1
            for p in ("/x", "/y"):
                add(p)
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_route_path"}),
)

# g75: a prefix read from a class attribute that module-level code rebinds.
CASES["g75"] = _case(
    "g75",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            class Cfg:
                prefix = "/old"
            Cfg.prefix = "/new"
            router = APIRouter(prefix=Cfg.prefix)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_route_prefix"}),
)


# g76: four calls per body, six levels deep: 5,461 call chains, past the budget.
CASES["g76"] = _case(
    "g76",
    {
        "main.py": _fan_out(4, 6),
    },
    allow=frozenset({"router_call_limit"}),
)

# g77: a chain of 17 functions called at import, one past the depth limit.
CASES["g77"] = _case(
    "g77",
    {
        "main.py": _chain(17),
    },
    allow=frozenset({"deferred_route_registration", "router_call_limit"}),
)

# g78: a chain of 16 functions called at import, at the depth limit.
CASES["g78"] = _case(
    "g78",
    {
        "main.py": _chain(16),
    },
)

# g79: a module imported under a branch that imports a registering module unconditionally.
CASES["g79"] = _case(
    "g79",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            """,
        "b.py": """
            from .a import router
            @router.get('/b')
            def b(): return 1
            """,
        "barrel.py": """
            from . import b  # noqa
            X = 1
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            if os.environ.get("NOPE_NOT_SET"):
                from .barrel import X  # noqa
            app = FastAPI()
            app.include_router(router)
            @app.get("/live")
            def live(): return 1
            """,
    },
    allow=frozenset({"conditional_import"}),
)

# g80: a registering module imported under a branch and again unconditionally, both before the include.
CASES["g80"] = _case(
    "g80",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            """,
        "b.py": """
            from .a import router
            @router.get('/b')
            def b(): return 1
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            if os.environ.get("NOPE_NOT_SET"):
                from . import b  # noqa
            from . import b as b2  # noqa
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"conditional_import"}),
)

# g81: a registering module imported under a branch before the include and unconditionally after it.
CASES["g81"] = _case(
    "g81",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get('/a')
            def a(): return 1
            """,
        "b.py": """
            from .a import router
            @router.get('/b')
            def b(): return 1
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            if os.environ.get("NOPE_NOT_SET"):
                from . import b  # noqa
            app = FastAPI()
            app.include_router(router)
            from . import b as b2  # noqa
            """,
    },
    allow=frozenset({"conditional_import"}),
)

# g82: a dependencies list bound to a second name in another module and changed in a third.
CASES["g82"] = _case(
    "g82",
    {
        "guards.py": """
            from fastapi import Depends
            from .deps import verify_key
            COMMON = [Depends(verify_key)]
            """,
        "alias.py": """
            from .guards import COMMON as C2
            ALIAS = C2
            """,
        "mut.py": """
            from fastapi import Depends
            from .deps import audit
            from .alias import ALIAS
            ALIAS.append(Depends(audit))
            """,
        "main.py": """
            from fastapi import APIRouter, FastAPI
            from . import mut  # noqa
            from .guards import COMMON
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g83: a dependencies list bound to a second name by tuple unpacking, then changed.
CASES["g83"] = _case(
    "g83",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            A, B = COMMON, 1
            A.append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g84: a dependencies list returned by a function whose result is changed.
CASES["g84"] = _case(
    "g84",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            def common():
                return COMMON
            common().append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g85: a dependencies list stored in a dict and changed through it.
CASES["g85"] = _case(
    "g85",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            REG = {"c": COMMON}
            REG["c"].append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g86: a dependencies list bound by a walrus and changed through it.
CASES["g86"] = _case(
    "g86",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            (alias := COMMON).append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g87: a dependencies list changed through a loop variable.
CASES["g87"] = _case(
    "g87",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI, Depends
            from .deps import verify_key, audit
            COMMON = [Depends(verify_key)]
            for lst in (COMMON,):
                lst.append(Depends(audit))
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g88: an app's routes passed to FastAPI's get_openapi and to a package function that clears them.
CASES["g88"] = _case(
    "g88",
    {
        "util.py": """
            def get_openapi(routes):
                routes.clear()
            """,
        "main.py": """
            from fastapi import FastAPI
            from fastapi.openapi.utils import get_openapi as real_openapi
            from .util import get_openapi
            app = FastAPI()
            @app.get("/x")
            def x(): return 1
            real_openapi(title="t", version="1", routes=app.routes)
            get_openapi(routes=app.routes)
            @app.get("/live")
            def live(): return 1
            """,
    },
    allow=frozenset({"opaque_router_change"}),
)

# g89: a router's routes changed through list.extend.
CASES["g89"] = _case(
    "g89",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            other = APIRouter()
            @other.get("/o")
            def o(): return 1
            router = APIRouter()
            list.extend(router.routes, other.routes)
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unmounted_router", "opaque_router_change"}),
)

# g90: includes in an if/elif/else chain with a walrus and TYPE_CHECKING, and in try/except/else/finally.
CASES["g90"] = _case(
    "g90",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix='/a')
            @router.get('/x')
            def x(): return 1
            """,
        "b.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix='/b')
            @router.get('/x')
            def x(): return 1
            """,
        "main.py": """
            from typing import TYPE_CHECKING
            from fastapi import FastAPI
            from . import a, b
            app = FastAPI()
            if TYPE_CHECKING:
                chosen = None
            elif (r := a.router) is not None:
                chosen = r
            else:
                chosen = b.router
            try:
                app.include_router(chosen)
            except Exception:
                pass
            else:
                app.include_router(b.router, prefix="/else")
            finally:
                app.include_router(a.router, prefix="/fin")
            """,
    },
    allow=frozenset({"unresolved_include", "conditional_router_mount"}),
)

# g91: an app passed through two package functions, the second including a router.
CASES["g91"] = _case(
    "g91",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get('/a')
            def a(): return 1
            """,
        "wire.py": """
            from .a import router
            def inner(app):
                app.include_router(router, prefix="/w")
            def outer(app):
                inner(app)
            """,
        "main.py": """
            from fastapi import FastAPI
            from .wire import outer
            app = FastAPI()
            outer(app)
            """,
    },
)

# g92: a module imported in a try body calls a registering function at import.
CASES["g92"] = _case(
    "g92",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get('/a')
            def a(): return 1
            def add():
                @router.get('/added')
                def added(): return 1
            """,
        "b.py": """
            from .a import add
            add()
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            try:
                from . import b  # noqa
            except ImportError:
                pass
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g93: a chain of 20 functions called at import, past the depth limit.
CASES["g93"] = _case(
    "g93",
    {
        "main.py": _chain(20),
    },
    allow=frozenset({"deferred_route_registration", "router_call_limit"}),
)

# g94: a chain of 40 functions called at import, far past the depth limit.
CASES["g94"] = _case(
    "g94",
    {
        "main.py": _chain(40),
    },
    allow=frozenset({"deferred_route_registration", "router_call_limit"}),
)

# g95: two calls per body, eleven levels deep: 4,095 call chains, within the budget.
CASES["g95"] = _case(
    "g95",
    {
        "main.py": _fan_out(2, 11),
    },
)

# g96: an app passed to one package function that includes a router.
CASES["g96"] = _case(
    "g96",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get('/a')
            def a(): return 1
            """,
        "wire.py": """
            from .a import router
            def inner(app):
                app.include_router(router, prefix='/w')
            """,
        "main.py": """
            from fastapi import FastAPI
            from .wire import inner
            app = FastAPI()
            inner(app)
            """,
    },
)

# g97: a router left empty in its module, filled by a module imported under a branch before the include.
CASES["g97"] = _case(
    "g97",
    {
        "a.py": """
            from fastapi import APIRouter
            router = APIRouter()
            """,
        "b.py": """
            from .a import router
            @router.get('/b')
            def b(): return 1
            """,
        "main.py": """
            import os
            from fastapi import FastAPI
            from .a import router
            if os.environ.get('NOPE_NOT_SET'):
                from . import b  # noqa
            app = FastAPI()
            app.include_router(router)
            from . import b as b2  # noqa
            @app.get("/live")
            def live(): return 1
            """,
    },
    allow=frozenset({"conditional_import"}),
)


# g98: a class attribute list changed through an instance bound to a name.
CASES["g98"] = _case(
    "g98",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            class G:
                deps = [Depends(verify_key)]
            g = G()
            g.deps.append(Depends(audit))
            router = APIRouter(dependencies=G.deps)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g99: a class attribute list changed through an instance never bound to a name.
CASES["g99"] = _case(
    "g99",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            class G:
                deps = [Depends(verify_key)]
            G().deps.append(Depends(audit))
            router = APIRouter(dependencies=G.deps)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g100: a class attribute list changed by a method through self.
CASES["g100"] = _case(
    "g100",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            class G:
                deps = [Depends(verify_key)]
                def add(self):
                    self.deps.append(Depends(audit))
            G().add()
            router = APIRouter(dependencies=G.deps)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g101: a package function named like a route method changes the list passed as dependencies=.
CASES["g101"] = _case(
    "g101",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            COMMON = [Depends(verify_key)]
            def get(path, dependencies):
                dependencies.append(Depends(audit))
            get("/unused", dependencies=COMMON)
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g102: a method named like a route method, on an object that is not a router, changes dependencies=.
CASES["g102"] = _case(
    "g102",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            COMMON = [Depends(verify_key)]
            class Helpers:
                def get(self, path, dependencies):
                    dependencies.append(Depends(audit))
            helpers = Helpers()
            helpers.get("/unused", dependencies=COMMON)
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g103: a module function named like a reading builtin changes its argument.
CASES["g103"] = _case(
    "g103",
    {
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            COMMON = [Depends(verify_key)]
            def sorted(x):
                x.append(Depends(audit))
                return x
            sorted(COMMON)
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g104: an imported function named like a reading builtin changes its argument.
CASES["g104"] = _case(
    "g104",
    {
        "util.py": """
            from fastapi import Depends
            from .deps import audit
            def sorted(x):
                x.append(Depends(audit))
                return x
            """,
        "main.py": """
            from fastapi import APIRouter, Depends, FastAPI
            from .deps import audit, verify_key
            COMMON = [Depends(verify_key)]
            from .util import sorted
            sorted(COMMON)
            router = APIRouter(dependencies=COMMON)
            @router.get("/x")
            def x(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_dependencies"}),
)

# g105: an include copies the child's routes; a function called at import after
# it registers a route and includes another router on the child, which the
# app never gets.
CASES["g105"] = _case(
    "g105",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            grand = APIRouter()
            @child.get("/early")
            def early(): return 1
            @grand.get("/g")
            def g(): return 1
            def register(router):
                @router.get("/late")
                def late(): return 1
                router.include_router(grand, prefix="/grand")
            app = FastAPI()
            app.include_router(child, prefix="/c")
            register(child)
            """,
    },
)

# g106: the same function called at import before the include: the app has its routes.
CASES["g106"] = _case(
    "g106",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            grand = APIRouter()
            @grand.get("/g")
            def g(): return 1
            def register(router):
                @router.get("/late")
                def late(): return 1
                router.include_router(grand, prefix="/grand")
            register(child)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
)

# g107: functions nothing calls at import register on a child the app included
# at import, directly, through a function they call, and by including another
# router: if they ever run, it is after the include copied the child's routes.
CASES["g107"] = _case(
    "g107",
    {
        "later.py": """
            from .routers import child, grand
            def add_nested():
                @child.get("/nested")
                def nested(): return 1
            def later():
                @child.get("/late")
                def late(): return 1
                add_nested()
                child.include_router(grand, prefix="/grand")
            """,
        "routers.py": """
            from fastapi import APIRouter
            child = APIRouter()
            grand = APIRouter()
            @child.get("/early")
            def early(): return 1
            @grand.get("/g")
            def g(): return 1
            """,
        "main.py": """
            from fastapi import FastAPI
            from .routers import child
            from . import later
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration", "deferred_router_mount"}),
)

# g108: a lambda registers a route and is called at import before the include:
# the analysis cannot place lazy code, so it lists the route as deferred.
CASES["g108"] = _case(
    "g108",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            hook = lambda: child.add_api_route("/hooked", handler)
            hook()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g109: the same lambda called after the include: FastAPI never copies the route.
CASES["g109"] = _case(
    "g109",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            hook = lambda: child.add_api_route("/hooked", handler)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            hook()
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g110: a chain of 17 functions called at import after the include, one past the
# depth limit: the registration it ends in runs after the include, but the
# analysis does not follow the call, so it cannot place it.
CASES["g110"] = _case(
    "g110",
    {
        "main.py": "\n".join(
            [
                "from fastapi import APIRouter, FastAPI",
                "router = APIRouter()",
                "router.add_api_route('/early', lambda: 1)",
                "def l17():",
                "    router.add_api_route('/leaf', lambda: 1)",
                *(line for k in range(16, 0, -1) for line in (f"def l{k}():", f"    l{k + 1}()")),
                "app = FastAPI()",
                "app.include_router(router)",
                "l1()",
                "",
            ]
        ),
    },
    allow=frozenset({"deferred_route_registration", "router_call_limit"}),
)

# g111: a function run at import through an alias, before the include: FastAPI serves its route.
CASES["g111"] = _case(
    "g111",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            go = later
            go()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g112: a function handed to another function that calls it at import, before the include.
CASES["g112"] = _case(
    "g112",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            def call(f):
                f()
            call(later)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g113: a package decorator that calls the function it decorates, at import, before the include.
CASES["g113"] = _case(
    "g113",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def run_now(fn):
                fn()
                return fn
            @run_now
            def later():
                child.add_api_route("/late", handler)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g114: functions run at import from a list, before the include.
CASES["g114"] = _case(
    "g114",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            HOOKS = [later]
            for hook in HOOKS:
                hook()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g115: a function run at import through globals(), before the include.
CASES["g115"] = _case(
    "g115",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            globals()["la" + "ter"]()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g116: another module runs the function at import through getattr with a computed name, before the include.
CASES["g116"] = _case(
    "g116",
    {
        "hooks.py": """
            from fastapi import APIRouter
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            """,
        "runner.py": """
            from . import hooks
            NAME = "la" + "ter"
            getattr(hooks, NAME)()
            """,
        "main.py": """
            from fastapi import FastAPI
            from . import hooks, runner
            from .hooks import child
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g117: a startup handler registers on a child the app included at import: FastAPI runs it after the include, so the app never serves its route.
CASES["g117"] = _case(
    "g117",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            @app.on_event("startup")
            def later():
                child.add_api_route("/late", handler)
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g118: a lifespan registers on a child the app included: it runs after the include, but the analysis cannot place a function handed to FastAPI(lifespan=...).
CASES["g118"] = _case(
    "g118",
    {
        "main.py": """
            from contextlib import asynccontextmanager
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            @asynccontextmanager
            async def lifespan(app):
                child.add_api_route("/late", handler)
                yield
            app = FastAPI(lifespan=lifespan)
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g119: functions found by inspecting the module object run at import, before the include.
CASES["g119"] = _case(
    "g119",
    {
        "main.py": """
            import inspect
            import sys
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
                if name.startswith("lat"):
                    fn()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g120: a function named by a module:name string runs at import, before the include.
CASES["g120"] = _case(
    "g120",
    {
        "main.py": """
            import pkgutil
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            pkgutil.resolve_name("APP_PKG.main:later")()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g121: a function named by a dotted string runs at import, before the include.
CASES["g121"] = _case(
    "g121",
    {
        "main.py": """
            import pkgutil
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            pkgutil.resolve_name("APP_PKG.main.later")()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g122: a function run through an alias before the include and called directly after it: FastAPI serves its route.
CASES["g122"] = _case(
    "g122",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            go = later
            go()
            app = FastAPI()
            app.include_router(child, prefix="/c")
            later()
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g123: a module passed as a value to a function that runs what it holds, before the include.
CASES["g123"] = _case(
    "g123",
    {
        "hooks.py": """
            from fastapi import APIRouter
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            """,
        "registry.py": """
            def register_all(mod):
                for name, value in list(vars(mod).items()):
                    if name.startswith("lat"):
                        value()
            """,
        "main.py": """
            from fastapi import FastAPI
            from . import hooks
            from .registry import register_all
            register_all(hooks)
            app = FastAPI()
            app.include_router(hooks.child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g124: a string annotation evaluated at import (typing.get_type_hints) calls the
# function before the include: FastAPI serves its route.
CASES["g124"] = _case(
    "g124",
    {
        "main.py": """
            import typing
            from fastapi import APIRouter, FastAPI
            child = APIRouter()
            def handler(): return 1
            child.add_api_route("/early", handler)
            def later():
                child.add_api_route("/late", handler)
            def g(x: "later()"): return x
            typing.get_type_hints(g)
            app = FastAPI()
            app.include_router(child, prefix="/c")
            """,
    },
    allow=frozenset({"deferred_route_registration"}),
)

# g125: methods=None is FastAPI's default (GET), written out, through a name,
# on add_api_route and api_route; an empty collection gives a route with no
# method (FastAPI's default unique id asserts on one, so these set their own);
# a list through a name is upper-cased.
CASES["g125"] = _case(
    "g125",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            NONE = None
            POST = ["post"]
            def h(): return 1
            router.add_api_route("/none", h, methods=None)
            router.add_api_route("/name-none", h, methods=NONE)
            router.add_api_route("/empty", h, methods=[], generate_unique_id_function=lambda route: route.name)
            router.add_api_route("/name-list", h, methods=POST)
            @router.api_route("/decorated-none", methods=None)
            def decorated(): return 1
            @router.api_route("/decorated-name-none", methods=NONE)
            def decorated_name(): return 1
            @router.api_route("/decorated-empty", methods=(), generate_unique_id_function=lambda route: route.name)
            def decorated_empty(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g126: methods= from **kwargs: a mapping that sets methods, sets it to None,
# or leaves it out.
CASES["g126"] = _case(
    "g126",
    {
        "main.py": """
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            NONE_OPTS = {"methods": None}
            OTHER_OPTS = {"name": "other"}
            def h(): return 1
            router.add_api_route("/kw", h, **{"methods": ["PUT", "PATCH"]})
            router.add_api_route("/kw-none", h, **NONE_OPTS)
            router.add_api_route("/kw-other", h, **OTHER_OPTS)
            @router.api_route("/kw-decorated", **{"methods": ["DELETE"]})
            def decorated(): return 1
            app = FastAPI()
            app.include_router(router)
            """,
    },
)

# g127: a methods= value chosen at runtime: the analysis reports the route as ANY.
CASES["g127"] = _case(
    "g127",
    {
        "main.py": """
            import os
            from fastapi import APIRouter, FastAPI
            router = APIRouter()
            def h(): return 1
            router.add_api_route("/either", h, methods=["POST"] if os.environ.get("CASE_G127") else None)
            router.add_api_route("/plain", h)
            app = FastAPI()
            app.include_router(router)
            """,
    },
    allow=frozenset({"unresolved_route_methods"}),
)

# g128: Starlette registrations with methods=None (a function endpoint is GET
# and HEAD, an HTTPEndpoint class any method it defines) are not analyzed and
# are reported; the FastAPI route next to them is.
CASES["g128"] = _case(
    "g128",
    {
        "main.py": """
            from fastapi import FastAPI
            from starlette.endpoints import HTTPEndpoint
            from starlette.responses import PlainTextResponse
            app = FastAPI()
            def plain(request): return PlainTextResponse("x")
            class Endpoint(HTTPEndpoint):
                async def post(self, request): return PlainTextResponse("x")
            app.add_route("/starlette-fn", plain, methods=None)
            app.add_route("/starlette-cls", Endpoint, methods=None)
            @app.api_route("/api", methods=None)
            def api(): return 1
            """,
    },
    allow=frozenset({"unsupported_route_registration"}),
)

# A star import's names when ``__all__`` is not one unconditional literal: a
# branch, an in-place change here or in another module. The analysis must not
# rule a name out on one possible value, so each router stays included.
CASES["g129"] = _case(
    "g129",
    {
        "branch.py": """
            import os
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/branch")
            def branch(): return 1
            if not os.environ.get("CASE_G129_HIDE"):
                __all__ = ["router"]
            else:
                __all__ = []
            """,
        "grown.py": """
            from fastapi import APIRouter
            grown = APIRouter()
            @grown.get("/grown")
            def grown_route(): return 1
            __all__ = []
            __all__ += ["grown"]
            """,
        "appended.py": """
            from fastapi import APIRouter
            appended = APIRouter()
            @appended.get("/appended")
            def appended_route(): return 1
            __all__ = []
            __all__.append("appended")
            """,
        "elsewhere.py": """
            from fastapi import APIRouter
            elsewhere = APIRouter()
            @elsewhere.get("/elsewhere")
            def elsewhere_route(): return 1
            __all__ = []
            """,
        "main.py": """
            from fastapi import FastAPI
            import APP_PKG.elsewhere
            APP_PKG.elsewhere.__all__.append("elsewhere")
            from APP_PKG.branch import *  # noqa: F403
            from APP_PKG.grown import *  # noqa: F403
            from APP_PKG.appended import *  # noqa: F403
            from APP_PKG.elsewhere import *  # noqa: F403
            app = FastAPI()
            app.include_router(router)  # noqa: F405
            app.include_router(grown)  # noqa: F405
            app.include_router(appended)  # noqa: F405
            app.include_router(elsewhere)  # noqa: F405
            """,
    },
)

# Cases where FastAPI builds what the analysis cannot read from source (a
# constructor override, routes=, a method bound to a name, a value changed in
# place or handed on, a loop, recursion, a call past the call chain budget,
# an import under a branch, lazy code or an unfollowed call that registers a
# route after an include): the analyzer differs
# from the runtime there, and must report these limitation kinds, which name
# what it could not follow.
INEXACT: dict[str, frozenset[str]] = {
    "g19": frozenset({"custom_router_class", "unresolved_dependencies", "unresolved_route_prefix"}),
    "g21": frozenset({"unresolved_include", "unresolved_route_router"}),
    "g24": frozenset({"unsupported_router_argument"}),
    "g25": frozenset({"opaque_router_call"}),
    "g26": frozenset({"opaque_router_call"}),
    "g28": frozenset({"unresolved_dependencies"}),
    "g29": frozenset({"unresolved_include_prefix"}),
    "g30": frozenset({"unresolved_route_prefix"}),
    "g59": frozenset({"unresolved_route_path"}),
    "g61": frozenset({"unresolved_include"}),
    "g64": frozenset({"recursive_router_call"}),
    "g65": frozenset({"conditional_import"}),
    "g66": frozenset({"conditional_import"}),
    "g67": frozenset({"unresolved_dependencies"}),
    "g68": frozenset({"unresolved_dependencies"}),
    "g69": frozenset({"unresolved_route_prefix"}),
    "g70": frozenset({"opaque_router_change"}),
    "g71": frozenset({"custom_router_class"}),
    "g72": frozenset({"custom_router_class", "unresolved_route_prefix"}),
    "g73": frozenset({"unsupported_router_argument"}),
    "g74": frozenset({"unresolved_route_path"}),
    "g75": frozenset({"unresolved_route_prefix"}),
    "g76": frozenset({"router_call_limit"}),
    "g79": frozenset({"conditional_import"}),
    "g81": frozenset({"conditional_import"}),
    "g82": frozenset({"unresolved_dependencies"}),
    "g83": frozenset({"unresolved_dependencies"}),
    "g84": frozenset({"unresolved_dependencies"}),
    "g85": frozenset({"unresolved_dependencies"}),
    "g86": frozenset({"unresolved_dependencies"}),
    "g87": frozenset({"unresolved_dependencies"}),
    "g88": frozenset({"opaque_router_change"}),
    "g89": frozenset({"opaque_router_change"}),
    "g90": frozenset({"unresolved_include"}),
    "g97": frozenset({"conditional_import"}),
    "g98": frozenset({"unresolved_dependencies"}),
    "g99": frozenset({"unresolved_dependencies"}),
    "g100": frozenset({"unresolved_dependencies"}),
    "g101": frozenset({"unresolved_dependencies"}),
    "g102": frozenset({"unresolved_dependencies"}),
    "g103": frozenset({"unresolved_dependencies"}),
    "g104": frozenset({"unresolved_dependencies"}),
    "g109": frozenset({"deferred_route_registration"}),
    "g110": frozenset({"deferred_route_registration", "router_call_limit"}),
    "g118": frozenset({"deferred_route_registration"}),
    "g127": frozenset({"unresolved_route_methods"}),
}

# Cases the newest FastAPI releases refuse to import, with the error they give:
# the runtime comparison runs only where FastAPI still accepts the case.
IMPORT_ERRORS_ON_NEWER_FASTAPI: dict[str, str] = {
    "g22": "unexpected keyword argument 'prefix'",
}

# Cases the analyzer cannot match exactly under a FastAPI whose include_router
# keeps the child router live (0.137 and later), with the limitation kinds it
# must report there. A case inexact under the copy model may be exact here (a
# registration after the include is served), and the other way round.
INEXACT_LIVE: dict[str, frozenset[str]] = {
    **{case_id: kinds for case_id, kinds in INEXACT.items() if case_id not in ("g81", "g97", "g109", "g110")},
    # Functions nothing calls at import, and a startup handler: the oracle only
    # imports the app, while the live include would serve what they add.
    "g107": frozenset({"deferred_route_registration", "deferred_router_mount"}),
    "g117": frozenset({"deferred_route_registration"}),
    # go() runs later() a second time without a call the analysis follows.
    "g122": frozenset({"deferred_route_registration"}),
}
