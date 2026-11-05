"""FastAPI oracle cases for dependency semantics.

Each case is a small app package; its expected dependencies are whatever FastAPI
builds when ``fastapi_oracle.py`` imports it, never hand-written. Sources use
``APP_PKG`` for the case's own package name, so every case imports as
``case_<id>`` and one oracle process can load them all.

Ids ``c01``-``c25`` are edge cases of FastAPI's dependency rules; ``d``-ids are
DI scenarios (dataclass dependencies, decorated handlers and dependencies,
wrapper and instance factories, ``functools.partial``, ``TYPE_CHECKING``
aliases, classes and their instances, bound methods, one id shared by forms
FastAPI reads differently). ``INEXACT_DEPENDENCIES`` lists the cases the analyzer cannot match
exactly, with the limitations it must report. Router-graph cases live in
``fastapi_cases.py``. Scenarios FastAPI rejects at import (a dependency in both
``Annotated`` and the default, ``Annotated[()]``) are unit tests in
``test_dependency_resolver.py`` instead.
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

_MAIN_A = """
    from fastapi import FastAPI
    from APP_PKG.api.routes.a import router as a_router
    app = FastAPI()
    app.include_router(a_router)
    """


def _case(case_id: str, files: dict[str, str], *, app: str = "main:app", allow: frozenset[str] = frozenset()) -> Case:
    """A case package: the common modules plus ``files`` (paths relative to the package)."""
    package = case_package(case_id)
    out: dict[str, str] = {}
    for rel, source in {**_COMMON, **files}.items():
        out[f"{package}/{rel}"] = textwrap.dedent(source).lstrip("\n").replace(APP_TOKEN, package)
    module, _, attribute = app.partition(":")
    return Case(files=out, app=f"{package}.{module}:{attribute}", allow=frozenset(allow))


DEPENDENCY_CASES: dict[str, Case] = {}

# c01_security: Security in a default, in Annotated, in dependencies= and in a sub-dependency.
DEPENDENCY_CASES["c01"] = _case(
    "c01",
    {
        "api/routes/a.py": """
            from typing import Annotated
            from fastapi import APIRouter, Depends, Security
            from APP_PKG.deps import get_user, get_admin, verify_key, audit
            router = APIRouter()
            @router.get("/sec-default")
            def a(u=Security(get_user, scopes=["read"])): ...
            @router.get("/sec-annot")
            def b(u: Annotated[str, Security(get_admin)]): ...
            @router.get("/sec-dec", dependencies=[Security(verify_key)])
            def c(): ...
            def sub(x=Security(audit)): ...
            @router.get("/sec-sub")
            def d(s=Depends(sub)): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Security
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(a_router, prefix="/c01", dependencies=[Security(verify_key)])
            """,
    },
)

# c02_depends_forms: dependency=, use_cache=, fastapi.Depends, import aliases, module attributes.
DEPENDENCY_CASES["c02"] = _case(
    "c02",
    {
        "api/routes/a.py": """
            import fastapi
            from fastapi import APIRouter, Depends
            from APP_PKG import deps
            from APP_PKG.deps import get_db, get_user as current_user
            router = APIRouter()
            @router.get("/kw")
            def a(db=Depends(dependency=get_db)): ...
            @router.get("/nocache")
            def b(db=Depends(get_db, use_cache=False)): ...
            @router.get("/fastapi-attr")
            def c(db=fastapi.Depends(get_db)): ...
            @router.get("/alias-import")
            def d(u=Depends(current_user)): ...
            @router.get("/module-attr")
            def e(u=Depends(deps.get_user)): ...
            @router.get("/module-attr-dec", dependencies=[Depends(deps.verify_key)])
            def f(): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c03_class_deps: class dependencies read __init__; instances read __call__.
DEPENDENCY_CASES["c03"] = _case(
    "c03",
    {
        "deps_cls.py": """
            from fastapi import Depends
            from APP_PKG.deps import get_db, get_user
            class Pager:
                def __init__(self, db=Depends(get_db), limit: int = 10):
                    self.limit = limit
            class RoleChecker:
                def __init__(self, role: str):
                    self.role = role
                def __call__(self, user=Depends(get_user)):
                    return user
            admin_only = RoleChecker("admin")
            """,
        "api/routes/a.py": """
            from typing import Annotated
            from fastapi import APIRouter, Depends
            from APP_PKG.deps_cls import Pager, RoleChecker, admin_only
            router = APIRouter()
            @router.get("/class")
            def a(p: Pager = Depends(Pager)): ...
            @router.get("/class-bare")
            def b(p: Pager = Depends()): ...
            @router.get("/class-annot-bare")
            def c(p: Annotated[Pager, Depends()]): ...
            @router.get("/instance")
            def d(u=Depends(RoleChecker("admin"))): ...
            @router.get("/instance-alias", dependencies=[Depends(admin_only)])
            def e(): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c04_method_lambda_gen: bound, static and class methods, a lambda, an async generator.
DEPENDENCY_CASES["c04"] = _case(
    "c04",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db, get_user
            class Svc:
                def get_items(self, db=Depends(get_db)):
                    return []
                @staticmethod
                def static_dep(user=Depends(get_user)):
                    return user
                @classmethod
                def cls_dep(cls, db=Depends(get_db)):
                    return db
            svc = Svc()
            router = APIRouter()
            @router.get("/bound")
            def a(x=Depends(svc.get_items)): ...
            @router.get("/static")
            def b(x=Depends(Svc.static_dep)): ...
            @router.get("/classm")
            def c(x=Depends(Svc.cls_dep)): ...
            @router.get("/lambda")
            def d(x=Depends(lambda: 1)): ...
            async def agen(db=Depends(get_db)):
                yield 1
            @router.get("/agen")
            def e(x=Depends(agen)): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c05_annotated_reexport: Annotated aliases through a package re-export, module attributes and chains.
DEPENDENCY_CASES["c05"] = _case(
    "c05",
    {
        "deps2/__init__.py": """
            from .db import DbSession, CurrentUser
            from .admin import AdminUser
            """,
        "deps2/db.py": """
            from typing import Annotated
            from fastapi import Depends
            def get_db2():
                yield "db"
            DbSession = Annotated[str, Depends(get_db2)]
            def get_user2(db: DbSession):
                return "u"
            CurrentUser = Annotated[str, Depends(get_user2)]
            """,
        "deps2/admin.py": """
            from typing import Annotated
            from fastapi import Depends
            from .db import CurrentUser
            def check_admin(u: CurrentUser):
                return u
            AdminUser = Annotated[str, Depends(check_admin)]
            """,
        "api/routes/a.py": """
            from fastapi import APIRouter
            from APP_PKG.deps2 import DbSession, CurrentUser, AdminUser
            from APP_PKG import deps2
            import APP_PKG.deps2 as d2
            router = APIRouter()
            @router.get("/a")
            def a(db: DbSession): ...
            @router.get("/b")
            def b(u: deps2.CurrentUser): ...
            @router.get("/c")
            def c(u: d2.AdminUser): ...
            @router.get("/d")
            def d(u: AdminUser): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c10_alias_cond: include guards through module aliases and a conditional include. The
# corpus also defined `router` in an if/else on an environment variable; which branch runs
# is unknowable statically, so that half stays a router-graph unit test (an exact
# comparison cannot hold for it).
DEPENDENCY_CASES["c10"] = _case(
    "c10",
    {
        "api/routes/things.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/things")
            @router.get("/t")
            def t(): ...
            """,
        "api/routes/other.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/o")
            def o(): ...
            """,
        "api/routes/cond.py": """
            from fastapi import APIRouter
            router = APIRouter(prefix="/stable")
            @router.get("/c")
            def c(): ...
            """,
        "main.py": """
            import os
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes import things as things_mod
            import APP_PKG.api.routes.other as other_mod
            from APP_PKG.api.routes.cond import router as cond_router
            from APP_PKG.deps import verify_key
            app = FastAPI()
            app.include_router(things_mod.router, prefix="/api", dependencies=[Depends(verify_key)])
            app.include_router(other_mod.router, prefix="/o")
            if not os.environ.get("DISABLE_COND"):
                app.include_router(cond_router, prefix="/cond", dependencies=[Depends(verify_key)])
            """,
    },
    # Sound: the include runs only when the branch is taken, which it is under the oracle.
    allow=frozenset({"conditional_router_mount"}),
)

# c14_name_collision: same-named dependencies in two modules, and a method named like a dependency.
DEPENDENCY_CASES["c14"] = _case(
    "c14",
    {
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            def get_ctx(db=Depends(get_db)):
                return db
            router = APIRouter()
            @router.get("/a")
            def a(c=Depends(get_ctx)): ...
            """,
        "api/routes/b.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import verify_key
            def get_ctx(k=Depends(verify_key)):
                return k
            router = APIRouter()
            @router.get("/b")
            def b(c=Depends(get_ctx)): ...
            """,
        "api/routes/u.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_user
            router = APIRouter()
            @router.get("/u")
            def u(user=Depends(get_user)): ...
            """,
        "services/__init__.py": "",
        "services/users.py": """
            class UserRepo:
                def get_user(self, user_id: int):
                    return user_id
            """,
        "main.py": """
            from fastapi import FastAPI
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.api.routes.b import router as b_router
            from APP_PKG.api.routes.u import router as u_router
            app = FastAPI()
            app.include_router(a_router)
            app.include_router(b_router)
            app.include_router(u_router)
            """,
    },
)

# c16_factory_inner: a factory result takes its deps from the returned closure only.
DEPENDENCY_CASES["c16"] = _case(
    "c16",
    {
        "factories.py": """
            from fastapi import Depends
            from APP_PKG.deps import get_db, get_user, get_admin
            def require_scope(scope: str):
                def unused(admin=Depends(get_admin)):
                    return admin
                async def _inner(db=Depends(get_db)):
                    return scope
                return _inner
            def require_role(role: str, user=Depends(get_user)):
                def checker(db=Depends(get_db)):
                    return role
                return checker
            """,
        "api/routes/a.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.factories import require_scope, require_role as rr
            router = APIRouter()
            @router.get("/scope")
            def a(x=Depends(require_scope("s"))): ...
            @router.get("/role")
            def b(x=Depends(rr("admin"))): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c17_multi_annotated: only the last FastAPI marker in Annotated counts.
DEPENDENCY_CASES["c17"] = _case(
    "c17",
    {
        "api/routes/a.py": """
            from typing import Annotated
            from fastapi import APIRouter, Depends, Query
            from APP_PKG.deps import get_db, get_user, verify_key
            CurrentUser = Annotated[str, Depends(get_user)]
            Keyed = Annotated[CurrentUser, Depends(verify_key)]
            router = APIRouter()
            @router.get("/two")
            def a(x: Annotated[str, Depends(get_db), Depends(get_user)]): ...
            @router.get("/nested")
            def b(x: Keyed): ...
            @router.get("/query-last")
            def c(x: Annotated[str, Depends(get_db), Query()] = "q"): ...
            """,
        "main.py": _MAIN_A,
    },
)

# c25_dep_class_in_routes_and_mixed: a later local def shadows an import; nested defs stay local.
DEPENDENCY_CASES["c25"] = _case(
    "c25",
    {
        "api/routes/a.py": """
            from typing import Annotated
            from fastapi import APIRouter, Depends, Request
            from APP_PKG.deps import get_db, get_user
            router = APIRouter()
            def get_db():
                yield "local-shadow"
            @router.get("/shadow")
            def shadow(db=Depends(get_db)): ...
            def outer():
                def get_user():
                    return 1
                return get_user
            @router.get("/u")
            def u(x=Depends(get_user)): ...
            """,
        "main.py": _MAIN_A,
    },
)

# d01 (old alias-backend scenario): every Annotated alias spelling FastAPI accepts.
DEPENDENCY_CASES["d01"] = _case(
    "d01",
    {
        "core/__init__.py": """
            from .database import DbSession as ReexportedDb
            """,
        "core/database.py": """
            from typing import Annotated, TypeAlias
            from fastapi import Depends
            def get_db():
                yield "db"
            DbSession = Annotated[str, Depends(get_db)]
            DbTyped: TypeAlias = Annotated[str, Depends(get_db)]
            """,
        "core/policy.py": """
            from typing import Annotated
            from fastapi import Depends
            def get_policy_context():
                return "ctx"
            def require_permission(permission: str):
                def _check(ctx=Depends(get_policy_context)):
                    return ctx
                return _check
            type PolicyCtx = Annotated[str, Depends(get_policy_context)]
            AdminCtx = Annotated[str, Depends(require_permission("admin.manage"))]
            """,
        "services.py": """
            def get_thing_service():
                return "svc"
            """,
        "api/routes/a.py": """
            from typing import Annotated
            from fastapi import APIRouter, Depends, Query
            import APP_PKG.core.database as database
            from APP_PKG.core import ReexportedDb
            from APP_PKG.core.database import DbSession, DbTyped
            from APP_PKG.core.database import DbSession as RenamedDb
            from APP_PKG.core.policy import AdminCtx, PolicyCtx
            from APP_PKG.services import get_thing_service
            router = APIRouter()
            ThingDep = Annotated[str, Depends(get_thing_service)]
            ChainedDb = DbSession
            QueryDb = Annotated[DbSession, Query()]
            NotADep = Annotated[str, Query(max_length=5)]
            @router.get("/imported")
            def imported(db: DbSession, ctx: PolicyCtx): ...
            @router.get("/local")
            def local(svc: ThingDep): ...
            @router.get("/renamed")
            def renamed(db: RenamedDb): ...
            @router.get("/reexported")
            def reexported(db: ReexportedDb): ...
            @router.get("/typed")
            def typed(db: DbTyped): ...
            @router.get("/chained")
            def chained(db: ChainedDb): ...
            @router.get("/attribute")
            def attribute(db: database.DbSession): ...
            @router.get("/quoted")
            def quoted(db: "DbSession"): ...
            @router.get("/nested")
            def nested(db: QueryDb = "x", q: NotADep = "y"): ...
            @router.get("/factory")
            def factory(ctx: AdminCtx): ...
            @router.get("/posonly/{a}")
            def posonly(a: int, /, c=Depends(get_thing_service), *, e=Depends(database.get_db)): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.a import router as a_router
            from APP_PKG.deps import get_user
            app = FastAPI()
            app.include_router(a_router, dependencies=[Depends(get_user)])
            """,
    },
)

# d02 (old route-guard scenario): decorator guards, guard aliases (local and imported),
# APIRouter constructor guards and a package router's guards over its children.
DEPENDENCY_CASES["d02"] = _case(
    "d02",
    {
        "core/__init__.py": "",
        "core/policy.py": """
            from fastapi import Depends
            def get_policy_context():
                return "ctx"
            def require_any_permission(*permissions: str):
                def _check(ctx=Depends(get_policy_context)):
                    return ctx
                return _check
            def require_perm(permission: str):
                def dep(ctx=Depends(get_policy_context)):
                    return ctx
                return dep
            """,
        "core/guards.py": """
            from APP_PKG.core.policy import require_any_permission
            shared_guard = require_any_permission("shared.view")
            """,
        "api/routes/things.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.core.guards import shared_guard
            from APP_PKG.core.policy import require_any_permission, require_perm
            from APP_PKG.deps import get_db
            router = APIRouter()
            _guard = require_any_permission("thing.view")
            @router.get("/aliased", dependencies=[Depends(_guard)])
            def aliased(db=Depends(get_db)): ...
            @router.get("/factory", dependencies=[Depends(require_perm("y.view"))])
            def factory(): ...
            @router.get("/imported", dependencies=[Depends(shared_guard)])
            def imported(): ...
            @router.get("/plain")
            def plain(): ...
            """,
        "api/routes/absences.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_user
            def _flag_enabled():
                return True
            router = APIRouter(
                prefix="/absences",
                dependencies=[Depends(get_user), Depends(_flag_enabled)],
            )
            @router.get("")
            def list_absences(): ...
            @router.post("/{absence_id}")
            def update_absence(absence_id: str): ...
            """,
        "api/routes/nested/__init__.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import verify_key
            from .child import router as child_router
            router = APIRouter(dependencies=[Depends(verify_key)])
            router.include_router(child_router, prefix="/nested")
            """,
        "api/routes/nested/child.py": """
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/leaf")
            def nested_leaf(): ...
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.things import router as things_router
            from APP_PKG.api.routes.absences import router as absences_router
            from APP_PKG.api.routes.nested import router as nested_router
            from APP_PKG.deps import audit
            app = FastAPI()
            app.include_router(things_router, dependencies=[Depends(audit)])
            app.include_router(absences_router)
            app.include_router(nested_router)
            """,
    },
)

# d03 (old nested-package, re-export, dual-router and package-collision scenarios): include
# guards reach routes through package routers, re-export barrels and both routers of one
# module, and same-named sub-routers in two packages stay distinct.
DEPENDENCY_CASES["d03"] = _case(
    "d03",
    {
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
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            router = APIRouter()
            @router.post("/{appointment_id}/approve")
            def approve_appointment(db=Depends(get_db)): ...
            """,
        "api/routes/appointments/detail.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_user
            router = APIRouter()
            @router.get("/{item_id}")
            def get_appointment(u=Depends(get_user)): ...
            """,
        "api/routes/users/__init__.py": """
            from fastapi import APIRouter
            from .detail import router as detail_router
            router = APIRouter()
            _PREFIX = "/users"
            router.include_router(detail_router, prefix=_PREFIX)
            """,
        "api/routes/users/detail.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_admin
            router = APIRouter()
            @router.get("/{item_id}")
            def get_user_detail(a=Depends(get_admin)): ...
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
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_admin, get_db
            router = APIRouter()
            root_router = APIRouter(dependencies=[Depends(get_admin)])
            @router.get("/{article_id}")
            def get_article(db=Depends(get_db)): ...
            @root_router.get("/camo-types")
            def list_camo_types(db=Depends(get_db)): ...
            """,
        "api/routes/admin/__init__.py": """
            from .emails import router as emails_router
            __all__ = ["emails_router"]
            """,
        "api/routes/admin/emails.py": """
            from fastapi import APIRouter, Depends
            from APP_PKG.deps import get_db
            router = APIRouter(prefix="/admin/failed-emails")
            @router.get("")
            def list_dead_letter_entries(db=Depends(get_db)): ...
            """,
        "core/__init__.py": "",
        "core/policy.py": """
            from fastapi import Depends
            from APP_PKG.deps import get_user
            def require_global_permission(permission: str):
                def _check(user=Depends(get_user)):
                    return user
                return _check
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from APP_PKG.api.routes.admin import emails_router as admin_emails_router
            from APP_PKG.api.routes.appointments import router as appointments_router
            from APP_PKG.api.routes.users import router as users_router
            from APP_PKG.api.routes.articles import router as articles_router
            from APP_PKG.core.policy import require_global_permission
            from APP_PKG.deps import verify_key
            AUTH_BINDING_MANAGE = "auth.binding.manage"
            app = FastAPI()
            app.include_router(appointments_router, dependencies=[Depends(verify_key)])
            app.include_router(users_router)
            app.include_router(articles_router, dependencies=[Depends(verify_key)])
            app.include_router(
                admin_emails_router,
                dependencies=[
                    Depends(require_global_permission(AUTH_BINDING_MANAGE)),
                    Depends(verify_key),
                ],
            )
            """,
    },
)

# d04: a @dataclass dependency with a Depends field default.
DEPENDENCY_CASES["d04"] = _case(
    "d04",
    {
        "main.py": """
            from dataclasses import dataclass
            from fastapi import FastAPI, Depends
            from .deps import get_db
            @dataclass
            class Params:
                q: str = ""
                db: str = Depends(get_db)
            app = FastAPI()
            @app.get("/x")
            def h(p: Params = Depends()): return 1
            """,
    },
)

# d05: a handler wrapped by a package decorator without functools.wraps.
DEPENDENCY_CASES["d05"] = _case(
    "d05",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import get_db
            def logged(fn):
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper
            app = FastAPI()
            @app.get("/x")
            @logged
            def h(db=Depends(get_db)): return 1
            """,
    },
    allow=frozenset({"opaque_decorator"}),
)

# d06: a handler wrapped by a package decorator with functools.wraps.
DEPENDENCY_CASES["d06"] = _case(
    "d06",
    {
        "main.py": """
            import functools
            from fastapi import FastAPI, Depends
            from .deps import get_db
            def logged(fn):
                @functools.wraps(fn)
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper
            app = FastAPI()
            @app.get("/x")
            @logged
            def h(db=Depends(get_db)): return 1
            """,
    },
)

# d07: an Annotated alias imported only under TYPE_CHECKING.
DEPENDENCY_CASES["d07"] = _case(
    "d07",
    {
        "aliases.py": """
            from typing import Annotated
            from fastapi import Depends
            from .deps import get_db
            DbDep = Annotated[str, Depends(get_db)]
            """,
        "main.py": """
            from __future__ import annotations
            from typing import TYPE_CHECKING
            from fastapi import FastAPI
            if TYPE_CHECKING:
                from .aliases import DbDep
            app = FastAPI()
            @app.get("/x")
            def x(db: DbDep): return 1
            """,
    },
)

# d08: a dependency wrapped by a decorator that changes its signature.
DEPENDENCY_CASES["d08"] = _case(
    "d08",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import get_db, verify_key
            def needs_verify(fn):
                def wrapper(ok=Depends(verify_key)):
                    return fn()
                return wrapper
            @needs_verify
            def current(db=Depends(get_db)):
                return 1
            app = FastAPI()
            @app.get("/x")
            def x(c=Depends(current)): return 1
            """,
    },
    allow=frozenset({"opaque_decorator"}),
)

# d09: add_api_route with a bound method of a module-level instance.
DEPENDENCY_CASES["d09"] = _case(
    "d09",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import get_db
            class Svc:
                def handle(self, db=Depends(get_db)): return 1
            svc = Svc()
            app = FastAPI()
            app.add_api_route("/m", svc.handle)
            """,
    },
)

# d10: pydantic Field after Depends in Annotated, and Annotated Depends with a Query default.
DEPENDENCY_CASES["d10"] = _case(
    "d10",
    {
        "main.py": """
            from typing import Annotated
            from pydantic import Field
            from fastapi import FastAPI, Depends, Query
            from .deps import get_db, verify_key
            app = FastAPI()
            @app.get("/f")
            def f(db: Annotated[str, Depends(get_db), Field(min_length=1)]): return 1
            @app.get("/q")
            def q(v: Annotated[str, Depends(verify_key)] = Query("x")): return 1
            """,
    },
)

# d11: Depends on methods inherited from a package base class.
DEPENDENCY_CASES["d11"] = _case(
    "d11",
    {
        "base.py": """
            from fastapi import Depends
            from .deps import get_db
            class Base:
                def fetch(self, db=Depends(get_db)): return 1
                @classmethod
                def cm(cls, db=Depends(get_db)): return 1
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from .base import Base
            class Child(Base):
                pass
            child = Child()
            app = FastAPI()
            @app.get("/i")
            def i(x=Depends(child.fetch)): return 1
            @app.get("/c")
            def c(x=Depends(Child.cm)): return 1
            """,
    },
)

# d12: an Annotated alias keeps the dependency bound when it was written.
DEPENDENCY_CASES["d12"] = _case(
    "d12",
    {
        "main.py": """
            from typing import Annotated
            from fastapi import FastAPI, Depends
            def get_x(): return 1
            X = Annotated[int, Depends(get_x)]
            def get_x(q: int = 0): return 2
            app = FastAPI()
            @app.get("/x")
            def h(x: X, y=Depends(get_x)): return 1
            """,
    },
)

# d13: a dependency rebound through a functools.wraps wrapper factory.
DEPENDENCY_CASES["d13"] = _case(
    "d13",
    {
        "main.py": """
            import functools
            from fastapi import FastAPI, Depends
            from .deps import get_db
            def with_retry(fn):
                @functools.wraps(fn)
                def inner(*a, **k):
                    return fn(*a, **k)
                return inner
            def get_repo(db=Depends(get_db)): return 1
            get_repo = with_retry(get_repo)
            app = FastAPI()
            @app.get("/x")
            def x(r=Depends(get_repo)): return 1
            """,
    },
)

# d14: PEP 695 Annotated aliases and nested Annotated with Query last.
DEPENDENCY_CASES["d14"] = _case(
    "d14",
    {
        "main.py": """
            from typing import Annotated
            from fastapi import FastAPI, Depends, Query
            from .deps import get_db, verify_key
            type DbDep = Annotated[str, Depends(get_db)]
            type Outer = Annotated[DbDep, Depends(verify_key)]
            Plain = Annotated[str, Depends(get_db)]
            app = FastAPI()
            @app.get("/a")
            def a(db: DbDep): return 1
            @app.get("/b")
            def b(db: Outer): return 1
            @app.get("/c")
            def c(db: Annotated[Plain, Query()]): return 1
            @app.get("/d")
            def d(db: Annotated[DbDep, Depends(verify_key)]): return 1
            """,
    },
)

# d15: a dependency through a star-import barrel with __all__.
DEPENDENCY_CASES["d15"] = _case(
    "d15",
    {
        "core/__init__.py": """
            from .db import *
            """,
        "core/db.py": """
            __all__ = ["get_session"]
            def get_session():
                yield "s"
            def _private(): pass
            """,
        "main.py": """
            from fastapi import FastAPI, Depends
            from .core import get_session
            app = FastAPI()
            @app.get("/x")
            def x(s=Depends(get_session)): return 1
            """,
    },
)

# d16: a dependency factory returning a callable-class instance.
DEPENDENCY_CASES["d16"] = _case(
    "d16",
    {
        "main.py": """
            from fastapi import FastAPI, Depends
            from .deps import get_user
            class RoleChecker:
                def __init__(self, role): self.role = role
                def __call__(self, user=Depends(get_user)): return user
            def require(role):
                return RoleChecker(role)
            app = FastAPI()
            @app.get("/x")
            def x(u=Depends(require("admin"))): return 1
            """,
    },
)

# d17: a class dependency whose constructor comes from pydantic.
DEPENDENCY_CASES["d17"] = _case(
    "d17",
    {
        "main.py": """
            from pydantic import BaseModel
            from fastapi import FastAPI, Depends
            class Q(BaseModel):
                a: int = 0
            app = FastAPI()
            @app.get("/x")
            def x(q: Q = Depends()): return 1
            """,
    },
)

# d18: a functools.partial dependency.
DEPENDENCY_CASES["d18"] = _case(
    "d18",
    {
        "main.py": """
            import functools
            from fastapi import FastAPI, Depends
            from .deps import get_db
            def paged(limit: int, db=Depends(get_db)): return 1
            app = FastAPI()
            @app.get("/x")
            def x(p=Depends(functools.partial(paged, limit=10))): return 1
            """,
    },
)

# d19: a dataclass dependency inheriting Depends fields from a dataclass in another module.
DEPENDENCY_CASES["d19"] = _case(
    "d19",
    {
        "base.py": """
            from dataclasses import dataclass
            from fastapi import Depends
            from .deps import get_db
            @dataclass
            class Base:
                db: str = Depends(get_db)
            """,
        "main.py": """
            from dataclasses import dataclass
            from fastapi import FastAPI, Depends
            from .deps import verify_key
            from .base import Base
            @dataclass
            class Params(Base):
                ok: bool = Depends(verify_key)
            app = FastAPI()
            @app.get("/x")
            def h(p: Params = Depends()): return 1
            """,
    },
)

# d20: an inherited dataclass field whose dependency name is bound differently in the subclass's module.
DEPENDENCY_CASES["d20"] = _case(
    "d20",
    {
        "base.py": """
            from dataclasses import dataclass
            from fastapi import Depends
            from .deps import get_db
            @dataclass
            class Base:
                db: str = Depends(get_db)
            """,
        "main.py": """
            from dataclasses import dataclass
            from fastapi import FastAPI, Depends
            from .base import Base
            def get_db():
                return "other"
            @dataclass
            class Params(Base):
                q: int = 0
            app = FastAPI()
            @app.get("/x")
            def h(p: Params = Depends()): return 1
            """,
    },
)

# d21: a handler decorator that copies the function's metadata with functools.update_wrapper.
DEPENDENCY_CASES["d21"] = _case(
    "d21",
    {
        "main.py": """
            import functools
            from fastapi import FastAPI, Depends
            from .deps import get_db
            def logged(fn):
                def wrapper(*a, **k):
                    return fn(*a, **k)
                functools.update_wrapper(wrapper, fn)
                return wrapper
            app = FastAPI()
            @app.get("/x")
            @logged
            def h(db=Depends(get_db)): return 1
            """,
    },
)

# d22: a decorator class whose __init__ calls functools.update_wrapper on the instance.
DEPENDENCY_CASES["d22"] = _case(
    "d22",
    {
        "main.py": """
            import functools
            from fastapi import FastAPI, Depends
            from .deps import get_db
            class logged:
                def __init__(self, fn):
                    self.fn = fn
                    functools.update_wrapper(self, fn)
                def __call__(self, *a, **k):
                    return self.fn(*a, **k)
            app = FastAPI()
            @app.get("/x")
            @logged
            def h(db=Depends(get_db)): return 1
            """,
    },
)


# d23: functools.partial dependencies binding a dependency parameter by keyword
# (dropped, or replaced by another Depends), by position, nested, and over an
# Annotated dependency, next to a direct use of the same function.
DEPENDENCY_CASES["d23"] = _case(
    "d23",
    {
        "main.py": """
            import functools
            from typing import Annotated
            from fastapi import FastAPI, Depends
            from .deps import audit, get_db, verify_key
            def paged(limit: int = 1, db=Depends(get_db), key=Depends(verify_key)): return 1
            def ann(limit: int = 1, db: Annotated[str, Depends(get_db)] = ""): return 1
            app = FastAPI()
            @app.get("/kw-none")
            def kw_none(p=Depends(functools.partial(paged, db=None))): return 1
            @app.get("/kw-other")
            def kw_other(p=Depends(functools.partial(paged, db=Depends(audit)))): return 1
            @app.get("/positional")
            def positional(p=Depends(functools.partial(paged, 10, None))): return 1
            @app.get("/nested")
            def nested(p=Depends(functools.partial(functools.partial(paged, 10), key=None))): return 1
            @app.get("/annotated")
            def annotated(p=Depends(functools.partial(ann, db=None))): return 1
            @app.get("/plain")
            def plain(p=Depends(paged)): return 1
            """,
    },
    # The partials read other parameters than paged itself, under paged's id.
    allow=frozenset({"shared_dependency_id"}),
)

# d24_class_and_instance: Depends(C) reads __init__, Depends(C()) reads __call__,
# so the class and its instances are distinct targets with their own sub-dependencies.
DEPENDENCY_CASES["d24"] = _case(
    "d24",
    {
        "checkers.py": """
            from fastapi import Depends
            from APP_PKG.deps import audit, get_db, get_user, verify_key
            class Checker:
                def __init__(self, db=Depends(get_db)):
                    self.db = db
                def __call__(self, user=Depends(get_user)):
                    return user
            class KeyChecker(Checker):
                def __init__(self, key=Depends(verify_key)):
                    self.key = key
            class Plain:
                def __call__(self, a=Depends(audit)):
                    return a
            shared = Checker()
            """,
        "main.py": """
            from fastapi import Depends, FastAPI
            from APP_PKG.checkers import Checker, KeyChecker, Plain, shared
            app = FastAPI()
            @app.get("/instance")
            def instance(c=Depends(Checker())): return 1
            @app.get("/class")
            def klass(c=Depends(Checker)): return 1
            @app.get("/alias")
            def alias(c=Depends(shared)): return 1
            @app.get("/route-level", dependencies=[Depends(Checker()), Depends(Checker)])
            def route_level(): return 1
            @app.get("/inherited-call")
            def inherited_call(c=Depends(KeyChecker())): return 1
            @app.get("/inherited-class")
            def inherited_class(c=Depends(KeyChecker)): return 1
            @app.get("/plain-class")
            def plain_class(p=Depends(Plain)): return 1
            @app.get("/plain-instance")
            def plain_instance(p=Depends(Plain())): return 1
            """,
    },
)

# d25_bound_methods: a plain method through an instance is bound (FastAPI skips
# its first parameter) and through the class is a function (FastAPI reads it);
# static and class methods are the same callable either way.
DEPENDENCY_CASES["d25"] = _case(
    "d25",
    {
        "svc.py": """
            from fastapi import Depends
            from APP_PKG.deps import audit, get_db, get_user, verify_key
            class Base:
                def inherited(first=Depends(audit), second=Depends(get_db)):
                    return second
            class Svc(Base):
                def method(first=Depends(get_user), second=Depends(verify_key)):
                    return second
                @staticmethod
                def static(user=Depends(get_user)):
                    return user
                @classmethod
                def klass(cls, db=Depends(get_db)):
                    return db
            svc = Svc()
            """,
        "main.py": """
            from fastapi import Depends, FastAPI
            from APP_PKG.svc import Svc, svc
            app = FastAPI()
            @app.get("/function")
            def function(x=Depends(Svc.method)): return 1
            @app.get("/bound")
            def bound(x=Depends(svc.method)): return 1
            @app.get("/inherited-function")
            def inherited_function(x=Depends(Svc.inherited)): return 1
            @app.get("/inherited-bound")
            def inherited_bound(x=Depends(svc.inherited)): return 1
            @app.get("/static")
            def static(x=Depends(Svc.static), y=Depends(svc.static)): return 1
            @app.get("/classmethod")
            def classmethod_(x=Depends(Svc.klass), y=Depends(svc.klass)): return 1
            """,
    },
)

# d26_shared_ids: forms FastAPI identifies by one id but reads differently: a
# partial and its function, a class and its partial, a factory and the closure
# or local-class instance it returns. Route closures are exact; the registry
# entry lists one form and the others are reported.
DEPENDENCY_CASES["d26"] = _case(
    "d26",
    {
        "main.py": """
            import functools
            from fastapi import Depends, FastAPI
            from APP_PKG.deps import audit, get_db, get_user, verify_key
            def paged(db=Depends(get_db), key=Depends(verify_key)): return 1
            class Pager:
                def __init__(self, db=Depends(get_db), key=Depends(verify_key)):
                    self.db = db
            def make_closure(a=Depends(audit)):
                def inner(user=Depends(get_user)): return user
                return inner
            def make_instance(a=Depends(audit)):
                class Local:
                    def __call__(self, db=Depends(get_db)): return db
                return Local()
            app = FastAPI()
            @app.get("/paged")
            def plain(p=Depends(paged)): return 1
            @app.get("/paged-partial")
            def partial(p=Depends(functools.partial(paged, key=Depends(audit)))): return 1
            @app.get("/pager")
            def pager(p=Depends(Pager)): return 1
            @app.get("/pager-partial")
            def pager_partial(p=Depends(functools.partial(Pager, db=None))): return 1
            @app.get("/closure")
            def closure(c=Depends(make_closure()), f=Depends(make_closure)): return 1
            @app.get("/local-instance")
            def local_instance(c=Depends(make_instance()), f=Depends(make_instance)): return 1
            """,
    },
    allow=frozenset({"shared_dependency_id"}),
)

# d27_redefined_function: both definitions are main:get_x, but each reference
# reads the definition bound when it runs, with its own sub-dependencies.
DEPENDENCY_CASES["d27"] = _case(
    "d27",
    {
        "main.py": """
            from fastapi import Depends, FastAPI
            from APP_PKG.deps import get_db, get_user
            def get_x(db=Depends(get_db)): return 1
            def early_dep(x=Depends(get_x)): return x
            def get_x(user=Depends(get_user)): return 2
            app = FastAPI()
            @app.get("/early")
            def early(x=Depends(early_dep)): return 1
            @app.get("/late")
            def late(x=Depends(get_x)): return 1
            """,
    },
    allow=frozenset({"shared_dependency_id"}),
)

# d28_factory_forms: make(flag) returns a local class's method bound or
# unbound, both identified as main:make; which one depends on flag at runtime,
# so the analysis gives every call both forms' sub-dependencies and reports it.
DEPENDENCY_CASES["d28"] = _case(
    "d28",
    {
        "main.py": """
            from fastapi import Depends, FastAPI
            from APP_PKG.deps import get_db, get_user
            def make(flag):
                class Local:
                    def m(first=Depends(get_user), second=Depends(get_db)): return 1
                if flag:
                    return Local().m
                return Local.m
            app = FastAPI()
            @app.get("/bound")
            def bound(x=Depends(make(True))): return 1
            @app.get("/function")
            def function(x=Depends(make(False))): return 1
            """,
    },
    allow=frozenset({"shared_dependency_id"}),
)

# Cases where FastAPI reads a signature the analysis cannot see (a decorator
# without functools.wraps) or a callable chosen at runtime (a factory returning
# different forms under one id): the analyzer must report these limitation kinds.
INEXACT_DEPENDENCIES: dict[str, frozenset[str]] = {
    "d05": frozenset({"opaque_decorator"}),
    "d08": frozenset({"opaque_decorator"}),
    "d28": frozenset({"shared_dependency_id"}),
}
