"""Apps that mount routers and read dependencies from the installed library ``extlib``
(``library_fixture``), one shape each, for the library oracle cases.

Each case runs ``capture`` and the analysis, then the runtime oracle, and the two must
agree on every route: path, order, handler, location, dependencies and closures.
``COMPLETE`` cases are filled entirely by the capture; ``GAPS`` cases keep an
``unresolved_include`` the capture cannot fill, and the oracle reports exactly the
routes that include would have added.
"""

from __future__ import annotations

from typing import NamedTuple


class LibraryCase(NamedTuple):
    main: str
    deps: str | None = None  # pkg/deps.py; the fixture's own when None


HEAD = """\
import extlib
from fastapi import APIRouter, Depends, FastAPI

from pkg.deps import get_manager

app = FastAPI()
"""

DEPS_WITH_SESSION = """\
from fastapi import Depends


def get_session():
    return "session"


def get_manager(session=Depends(get_session)):
    return "manager"
"""

COMPLETE: dict[str, LibraryCase] = {
    "generic_method_factory": LibraryCase(
        HEAD
        + """
users = extlib.Users[dict](get_manager)
app.include_router(users.get_router(dict), prefix="/users", dependencies=[Depends(get_manager)])
"""
    ),
    "module_factory": LibraryCase(
        HEAD
        + """
app.include_router(extlib.make_status_router(), prefix="/status")
"""
    ),
    "nested_library_router": LibraryCase(
        HEAD
        + """
app.include_router(extlib.make_nested_router(), prefix="/nested", dependencies=[Depends(get_manager)])
"""
    ),
    "signature_set_at_runtime": LibraryCase(
        HEAD
        + """
@app.get("/signed")
def signed(values=Depends(extlib.signed)):
    return values
"""
    ),
    "closure_dependency": LibraryCase(
        HEAD
        + """
users = extlib.Users[dict](get_manager)
current = users.current(active=True)


@app.get("/own")
def own(user=Depends(current)):
    return user


router = APIRouter(dependencies=[Depends(current)])


@router.get("/guarded")
def guarded():
    return 1


app.include_router(router, prefix="/g")
"""
    ),
    "library_class_as_depends": LibraryCase(
        HEAD
        + """
@app.get("/settings")
def settings(configured=Depends(extlib.Settings)):
    return configured
"""
    ),
    "consumer_callable_with_its_own_dependencies": LibraryCase(
        HEAD
        + """
users = extlib.Users[dict](get_manager)
app.include_router(users.get_router(dict), prefix="/users")
""",
        DEPS_WITH_SESSION,
    ),
    "routes_before_and_after_a_library_include": LibraryCase(
        HEAD
        + """
@app.get("/status/ping")
def own_ping():
    return "own"


app.include_router(extlib.make_status_router(), prefix="/status")


@app.get("/status/live")
def own_live():
    return "own"
"""
    ),
    "include_under_a_router_with_dependencies": LibraryCase(
        HEAD
        + """
api = APIRouter(prefix="/api", dependencies=[Depends(get_manager)])
api.include_router(extlib.make_status_router(), prefix="/v1", dependencies=[Depends(extlib.signed)])
app.include_router(api)
"""
    ),
    "two_includes_of_one_factory": LibraryCase(
        HEAD
        + """
app.include_router(extlib.make_status_router(), prefix="/a")
app.include_router(extlib.make_status_router(), prefix="/b")
"""
    ),
}

GAPS: dict[str, LibraryCase] = {
    "router_that_includes_a_repository_handler": LibraryCase(
        HEAD
        + """
def own_handler():
    return "own"


app.include_router(extlib.make_mixed_router(own_handler), prefix="/mixed")
"""
    ),
}
