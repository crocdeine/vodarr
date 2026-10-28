"""FastAPI application: HTTP guards, authentication, routes (ADR-0004, ADR-0005)."""

import contextlib
import html
import json
import logging
import os
import re
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from pydantic.alias_generators import to_camel
from starlette.exceptions import HTTPException as StarletteHTTPException

from vodarr import __version__, auth, crossseed, downloads, pipeline, routes, settings
from vodarr.core import (
    MASK,
    MsgError,
    close_thread_db,
    open_db,
    register_secret,
    setup_logging,
)
from vodarr.schemas import ApiModel

log = logging.getLogger(__name__)

COOKIE = "vodarr_session"
MAX_BODY = 1024 * 1024
STATUS = {
    "auth.required": 401,
    "auth.setup_required": 401,
    "auth.invalid_credentials": 401,
    "auth.locked": 429,
    "auth.already_setup": 409,
    "release.busy": 409,
    "release.search_too_soon": 429,
    "queue.not_removable": 409,
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; base-uri 'none';"
        " form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _camel(value: Any) -> Any:
    if isinstance(value, dict):
        return {to_camel(k) if isinstance(k, str) else k: _camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


class CamelJSON(JSONResponse):
    """snake_case in Python, camelCase on the wire: converted once, here (ADR-0004)."""

    def render(self, content: Any) -> bytes:
        return json.dumps(_camel(content), ensure_ascii=False, separators=(",", ":")).encode()


def error(http_status: int, code: str, headers: dict | None = None, /, **params: Any) -> CamelJSON:
    """The single error shape (ADR-0004); security headers included, even before routing.
    Positional-only: message params may be named `status`, `code` or `headers`."""
    return CamelJSON(
        {"error": {"code": code, "params": params}},
        status_code=http_status,
        headers={**SECURITY_HEADERS, **(headers or {})},
    )


def host_page(host: str) -> HTMLResponse:
    """A browser reaching VODarr under a name it does not know: say which setting to change.
    The name is the request's own Host header: escaped, and only shown back to that browser."""
    name = html.escape(host[:255])
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="color-scheme" content="dark light"><title>VODarr</title></head><body>
<h1>VODarr</h1>
<p>This address is not allowed: <code>{name}</code>.<br>
Add it to <code>VODARR_ALLOWED_HOSTS</code> (comma-separated) and recreate the container.</p>
<p lang="fr">Cette adresse n'est pas autorisée : <code>{name}</code>.<br>
Ajoutez-la à <code>VODARR_ALLOWED_HOSTS</code> (séparée par des virgules) puis recréez le
conteneur.</p>
</body></html>"""
    return HTMLResponse(body, status_code=421, headers=SECURITY_HEADERS)


def status_for(code: str) -> int:
    if code in STATUS:
        return STATUS[code]
    return 404 if code.endswith("not_found") else 422


def allowed_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "[::1]", socket.gethostname()}
    with contextlib.suppress(OSError):
        hosts.add(socket.gethostbyname(socket.gethostname()))
    extra = os.environ.get("VODARR_ALLOWED_HOSTS", "")
    return hosts | {h.strip().lower() for h in extra.split(",") if h.strip()}


def client(request: Request) -> str:
    """Address the throttle counts against. Behind a reverse proxy every client shares the
    proxy's address (X-Forwarded-For is not trusted: anyone could forge it)."""
    return request.client.host if request.client else "?"


def csrf_ok(request: Request) -> bool:
    """Cookie-authenticated mutations must come from our own page, as JSON (AC-AUTH-09).
    API key calls carry no cookie; a cross-site page cannot add the X-Api-Key header."""
    mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
    if not mutating or not request.cookies.get(COOKIE) or request.headers.get("x-api-key"):
        return True
    origin = re.sub(r"^https?://", "", request.headers.get("origin") or "").lower()
    if origin != (request.headers.get("host") or "").lower():
        return False
    content_type = request.headers.get("content-type") or ""
    return request.method == "DELETE" or content_type.startswith("application/json")


# --- request models ---------------------------------------------------------------------------


Text = Field(None, max_length=4096)


class Credentials(ApiModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class SetupIn(Credentials):
    setup_code: str = Field(max_length=64)


class Password(ApiModel):
    password: str = Field(max_length=1024)


# Partial updates: an omitted field keeps its stored value (dumped with exclude_unset).
class ProwlarrIn(ApiModel):
    url: str | None = Text
    api_key: str | None = Text
    max_checks_per_tracker: int | None = Field(None, ge=0, le=100)
    research_days: int | None = Field(None, ge=0, le=365)


class QbitIn(ApiModel):
    url: str | None = Text
    username: str | None = Text
    password: str | None = Text


class TmdbIn(ApiModel):
    api_key: str | None = Text


class ServicesIn(ApiModel):
    prowlarr: ProwlarrIn | None = None
    qbittorrent: QbitIn | None = None
    tmdb: TmdbIn | None = None


class PathsIn(ApiModel):
    downloads: str | None = Text
    data_root: str | None = Text
    qbit_data_root: str | None = Text


class CrossSeedIn(ApiModel):
    link_dirs: list[str] | None = Field(None, max_length=20)
    link_type: str | None = Field(None, max_length=20)
    flat_linking: bool | None = None
    link_category: str | None = Field(None, min_length=1, max_length=100)


class UiIn(ApiModel):
    language: str | None = Field(None, max_length=10)
    theme: str | None = Field(None, max_length=10)


class SettingsIn(ApiModel):
    services: ServicesIn | None = None
    paths: PathsIn | None = None
    crossseed: CrossSeedIn | None = None
    ui: UiIn | None = None


class ServiceTest(ApiModel):
    service: Literal["prowlarr", "qbittorrent", "tmdb"]
    # Unsaved form values to test; omitted fields fall back to the saved settings.
    url: str | None = Text
    api_key: str | None = Text
    username: str | None = Text
    password: str | None = Text


class FilePath(ApiModel):
    path: str = Field(min_length=1, max_length=4096)


# --- application -----------------------------------------------------------------------------


def register_stored_secrets(conn) -> None:
    """Mask every stored secret in the logs from the first line on (AC-CORE-04)."""
    row = conn.execute("SELECT api_key FROM admin WHERE id = 1").fetchone()
    if row:
        register_secret(row["api_key"])
    for (config,) in conn.execute("SELECT config FROM sources"):
        cfg = json.loads(config)
        for key in ("password", "url", "user"):
            register_secret(cfg.get(key))
    services_cfg = settings.load(conn)["services"]
    register_secret(services_cfg["prowlarr"]["apiKey"])
    register_secret(services_cfg["qbittorrent"]["password"])


def _worker(name: str, step, config_dir: Path, stop: threading.Event, wake: threading.Event):
    """Loop: one step at a time; sleep until woken (new work) or 3 s when idle (ADR-0002)."""

    def run():
        try:
            conn = open_db(config_dir)
            while not stop.is_set():
                wake.clear()  # before the step: a wake-up during it is not lost
                try:
                    busy = step(conn)
                except Exception:
                    log.exception("%s worker step failed", name)
                    busy = False
                if not busy:
                    wake.wait(3)
        except Exception:
            log.exception("%s worker stopped", name)
        finally:
            close_thread_db()

    return threading.Thread(target=run, name=f"{name}-worker", daemon=True)


def create_app(
    config_dir: Path | None = None, hosts: set[str] | None = None, workers: bool | None = None
) -> FastAPI:
    config_dir = config_dir or Path(os.environ.get("VODARR_CONFIG", "/config"))
    dev = os.environ.get("VODARR_DEV") == "1"
    hosts = {h.lower() for h in (hosts or allowed_hosts())}
    if workers is None:
        workers = os.environ.get("VODARR_WORKERS", "1") != "0"
    wake = {"downloads": threading.Event(), "pipeline": threading.Event()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        threads = []
        if workers:
            conn = open_db(config_dir)  # interrupted work goes back to pending
            downloads.recover(conn)
            pipeline.recover(conn)
            close_thread_db()
            stage_handlers = pipeline.handlers()

            def download_step(c):
                done = downloads.process_next(c, config_dir)
                if done:
                    wake["pipeline"].set()
                return done

            threads = [
                _worker("download", download_step, config_dir, stop, wake["downloads"]),
                _worker(
                    "pipeline",
                    lambda c: (
                        pipeline.research_tick(c)
                        or pipeline.process_next(c, config_dir, stage_handlers)
                    ),
                    config_dir,
                    stop,
                    wake["pipeline"],
                ),
            ]
            for t in threads:
                t.start()
        try:
            yield
        finally:
            stop.set()
            for event in wake.values():
                event.set()
            for t in threads:
                t.join(timeout=10)

    app = FastAPI(
        lifespan=lifespan,
        title="VODarr",
        version=__version__,
        default_response_class=CamelJSON,
        docs_url="/docs" if dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if dev else None,
    )
    app.state.config_dir = config_dir
    if not logging.getLogger().handlers:
        setup_logging()
    register_stored_secrets(open_db(config_dir))
    # First run: a one-time code in the log, required to create the admin (ADR-0005).
    app.state.setup_code = None
    if not auth.is_setup(open_db(config_dir)):
        app.state.setup_code = os.environ.get("VODARR_SETUP_TOKEN", "").strip().lower() or (
            auth.new_setup_code()
        )
        log.warning("First run: open VODarr and enter the setup code %s", app.state.setup_code)
        if len(app.state.setup_code) < 12:
            log.warning("VODARR_SETUP_TOKEN is short: use at least 12 random characters")

    def db():
        return open_db(config_dir)

    @app.middleware("http")
    async def guards(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        host = re.sub(r":\d+$", "", host)
        if host not in hosts:
            if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(
                "/api/"
            ):
                return host_page(host)
            return error(421, "http.host_not_allowed")
        if any(part.startswith(".") for part in request.url.path.split("/")):
            return error(404, "http.not_found")  # never serve dotfiles (.DS_Store, .git…)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_BODY):
            return error(413, "http.body_too_large")
        if "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            return error(411, "http.length_required")  # the size limit needs a Content-Length
        if not csrf_ok(request):
            return error(403, "http.csrf")
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        # Revalidate the interface and never cache API answers: no stale UI after an upgrade.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "http.not_found", 405: "http.method_not_allowed"}.get(
            exc.status_code, "http.error"
        )
        return error(exc.status_code, code, getattr(exc, "headers", None))

    @app.exception_handler(MsgError)
    async def on_msg_error(request: Request, exc: MsgError):
        return error(status_for(exc.msg.code), exc.msg.code, **exc.msg.params)

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError):
        fields = [".".join(str(p) for p in e["loc"][1:]) or e["type"] for e in exc.errors()]
        return error(422, "http.invalid_request", fields=", ".join(fields))

    @app.exception_handler(Exception)
    async def on_internal(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error(500, "internal")

    def require_auth(request: Request) -> None:
        conn = db()
        key = request.headers.get("x-api-key")
        if key:
            if auth.api_key_valid(conn, key):
                return
            raise MsgError("auth.required")
        if not auth.is_setup(conn):
            raise MsgError("auth.setup_required")
        if not auth.session_valid(conn, request.cookies.get(COOKIE)):
            raise MsgError("auth.required")

    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    def set_session(request: Request, response: Response, token: str) -> None:
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            COOKIE,
            token,
            max_age=auth.SESSION_TTL,
            httponly=True,
            samesite="strict",
            secure=https,
            path="/",
        )

    @public.get("/health")
    def health():
        return {"status": "ok"}

    @public.get("/auth/status")
    def auth_status(request: Request):
        conn = db()
        setup = auth.is_setup(conn)
        return {
            "setup_required": not setup,
            "authenticated": setup and auth.session_valid(conn, request.cookies.get(COOKIE)),
        }

    @public.post("/auth/setup", status_code=201)
    def auth_setup(body: SetupIn, request: Request, response: Response):
        conn = db()
        if auth.is_setup(conn) or not app.state.setup_code:
            raise MsgError("auth.already_setup")
        auth.check_setup_code(app.state.setup_code, body.setup_code, client(request))
        set_session(request, response, auth.setup(conn, body.username, body.password))
        app.state.setup_code = None
        return {"ok": True}

    @public.post("/auth/login")
    def auth_login(body: Credentials, request: Request, response: Response):
        token = auth.login(db(), body.username, body.password, client=client(request))
        set_session(request, response, token)
        return {"ok": True}

    @public.post("/auth/logout")
    def auth_logout(request: Request, response: Response):
        auth.logout(db(), request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @api.get("/auth/api-key")
    def api_key_masked():
        return {"api_key": MASK}

    @api.post("/auth/api-key")
    def api_key_regenerate():
        auth.regenerate_api_key(db())
        return {"api_key": MASK}

    @api.post("/auth/api-key/reveal")
    def api_key_reveal(body: Password, request: Request):
        return {"api_key": auth.reveal_api_key(db(), body.password, client(request))}

    @api.get("/settings")
    def settings_get():
        return settings.masked(db())

    @api.put("/settings")
    def settings_put(body: SettingsIn):
        # Stored keys are the camelCase names the frontend and pipeline share.
        return settings.save(  # null means "keep", like an omitted field
            db(), body.model_dump(by_alias=True, exclude_unset=True, exclude_none=True)
        )

    @api.post("/settings/test")
    def settings_test(body: ServiceTest):
        values = body.model_dump(by_alias=True, exclude_unset=True, exclude={"service"})
        msg = settings.test_service(db(), body.service, values)
        return {"ok": msg is None, "error": msg.as_dict() if msg else None}

    @api.post("/settings/cross-seed-import")
    def settings_crossseed_import(body: FilePath):
        """Pre-fills the form; nothing is saved (AC-SET-06)."""
        return crossseed.import_file(body.path)

    routes.register(api, db, config_dir, wake)
    app.include_router(public)
    app.include_router(api)
    web = Path(os.environ.get("VODARR_WEB", Path(__file__).resolve().parents[2] / "web"))
    if web.is_dir():  # the interface: static files, no build (ADR-0008)
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
