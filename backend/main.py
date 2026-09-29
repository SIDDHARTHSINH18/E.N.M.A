import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.api.audit import router as audit_router
from backend.api.auth import router as auth_router
from backend.api.chat import router as chat_router
from backend.api.graph import router as graph_router
from backend.api.memory import router as memory_router
from backend.api.skills import router as skills_router
from backend.api.tasks import router as tasks_router
from backend.api.upload import router as upload_router
from backend.core.security import (
    get_auth_password,
    get_rate_limits,
    rate_limiter,
    session_store,
)
from backend.core.services import (
    orchestrator,
    provider_status,
)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(name)s "
        "%(levelname)s "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "ghost.main",
)


# ============================================================
# PUBLIC ROUTES (no session required)
# ============================================================

PUBLIC_PATHS = {
    "/",
    # /health stays public: the desktop shell and local
    # operators need a no-credential liveness check.
    # /health/provider is protected — it discloses the
    # active provider/model and triggers an outbound
    # provider call.
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/auth/login",
    "/api/auth/setup",
}


_warned_non_loopback = False


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforces bearer-token authentication on every route
    except PUBLIC_PATHS. CORS preflight (OPTIONS) passes
    through because browsers never attach Authorization
    headers to preflight requests.
    """

    async def dispatch(
        self,
        request: Request,
        call_next,
    ):
        global _warned_non_loopback

        # ------------------------------------------------
        # One-time warning for non-loopback access (M2):
        # the API is designed for the local machine; a
        # request from another host deserves a loud note
        # in the server log.
        # ------------------------------------------------

        client_host = (
            request.client.host
            if request.client
            else None
        )

        if (
            client_host
            and client_host
            not in ("127.0.0.1", "::1")
            # Starlette's TestClient identifies itself
            # as "testclient"; tests are loopback by
            # definition.
            and client_host != "testclient"
            and not _warned_non_loopback
        ):

            logger.warning(
                "GHOST API is being accessed from a "
                "non-loopback address (%s). If this is "
                "not intentional, stop the server and "
                "bind to 127.0.0.1.",
                client_host,
            )

            _warned_non_loopback = True

        # ------------------------------------------------
        # Public paths and CORS preflight
        # ------------------------------------------------

        if (
            request.method == "OPTIONS"
            or request.url.path in PUBLIC_PATHS
        ):
            return await call_next(request)

        # ------------------------------------------------
        # Bearer token validation
        # ------------------------------------------------

        authorization = request.headers.get(
            "Authorization",
            "",
        )

        token = (
            authorization[len("Bearer "):].strip()
            if authorization.startswith("Bearer ")
            else ""
        )

        session = (
            session_store.get_valid(token)
            if token
            else None
        )

        if session is None:

            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Authentication required."
                    ),
                },
                headers={
                    "WWW-Authenticate": "Bearer",
                },
            )

        # ------------------------------------------------
        # General per-session rate limit (chat/upload/
        # login apply their own tighter limits on top)
        # ------------------------------------------------

        limits = get_rate_limits()

        allowed, retry_after = rate_limiter.check(
            f"session:{token}",
            limits["general"],
        )

        if not allowed:

            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        "Rate limit exceeded. "
                        "Slow down."
                    ),
                },
                headers={
                    "Retry-After": str(
                        int(retry_after),
                    ),
                },
            )

        request.state.session_token = token

        return await call_next(request)


def validate_startup_config():
    """
    Fail fast on a half-configured backend.

    A missing provider key or GHOST_AUTH_PASSWORD previously
    surfaced only as errors on first use; GHOST refuses to
    start instead. The provider checked is the orchestrator's
    default, or the provider named by ENMA_DEFAULT_PROVIDER —
    the desktop layer sets that to the provider the app
    actually talks to (Groq), so the packaged config template
    and this check agree.
    """

    status = provider_status(
        os.getenv("ENMA_DEFAULT_PROVIDER") or None
    )

    if not status["api_key_present"]:

        raise RuntimeError(
            "\n"
            "========================================\n"
            f"{status['provider'].upper()}_API_KEY is not "
            "configured.\n"
            "\n"
            "Fix:\n"
            "  1. Open the ENMA configuration file (or .env)\n"
            f"  2. Set {status['provider'].upper()}_API_KEY in it\n"
            "  3. Restart the backend\n"
            "\n"
            "GHOST refuses to start with a\n"
            "half-configured provider.\n"
            "========================================"
        )

    if not get_auth_password():

        raise RuntimeError(
            "\n"
            "========================================\n"
            "GHOST_AUTH_PASSWORD is not configured.\n"
            "\n"
            "Since M2 the API requires a passphrase.\n"
            "\n"
            "Fix:\n"
            "  1. Open the ENMA configuration file (or .env)\n"
            "  2. Set GHOST_AUTH_PASSWORD to a strong\n"
            "     passphrase of your choice\n"
            "  3. Restart the backend\n"
            "\n"
            "GHOST refuses to start without it —\n"
            "an unauthenticated API is not a safe\n"
            "default.\n"
            "========================================"
        )

    return status


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Fail fast on a half-configured backend.
    """

    status = validate_startup_config()

    logger.info(
        "Provider '%s' configured "
        "(model=%s, base_url=%s)",
        status["provider"],
        status["model"],
        status["base_url"],
    )

    yield


app = FastAPI(
    title="GHOST — Personal AI Operating System",
    version="0.2.0",
    lifespan=lifespan,
)


app.add_middleware(SessionAuthMiddleware)

# CORS must run OUTERMOST (added last): Starlette runs
# the last-added middleware first on the request path,
# so this ordering lets even 401/429 responses from the
# auth middleware carry CORS headers — otherwise the
# browser could not read them.
app.add_middleware(
    CORSMiddleware,
   allow_origins=[
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:5177",
    "http://127.0.0.1:5177",
],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Ghost-Sources"],
)


@app.get("/")
def root():
    return {
        "message": "GHOST API is running"
    }


@app.get("/health")
def health():
    """
    Configuration check only — deliberately no
    network call, so health stays fast and works
    offline. /health/provider does a live ping.
    """

    return {
        "status": "ok",
        "provider": provider_status(),
    }


@app.get("/health/provider")
async def health_provider(http_request: Request):
    """
    Live reachability check for the configured
    provider (GET /models). Any HTTP response
    counts as reachable; this is a network check,
    not an authentication check.
    """

    # The UI reports the provider chat actually uses: accept an
    # explicit ?provider= request, falling back to the default.
    requested = (
        http_request.query_params.get("provider")
        if http_request.query_params.get("provider")
        in orchestrator.providers
        else None
    )

    provider = orchestrator.get_provider(requested)

    status = provider_status(requested)

    # Canonical provider health interface; returns a plain
    # bool. Any exception means "not reachable", never a
    # failed request.
    try:
        reachable = bool(await provider.health_check())
    except Exception:
        reachable = False

    # Diagnostic reason for the UI (no secrets): distinguish a
    # quota/rate-limit rejection from an outage or a connection
    # error, using the metadata the provider recorded during its
    # probe.
    reason = "ok" if reachable else "unreachable"

    if not reachable:
        status_code = getattr(
            provider,
            "last_health_status_code",
            None,
        )

        if status_code == 429:
            reason = "rate_limited"
        elif status_code == 503:
            reason = "unavailable"
        elif status_code is not None:
            reason = "error"

    return {
        "provider": status["provider"],
        "model": status["model"],
        "online": reachable,
        "reachable": reachable,
        "reason": reason,
        "status_code": getattr(
            provider,
            "last_health_status_code",
            None,
        ),
    }


app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(upload_router)
app.include_router(graph_router)
app.include_router(memory_router)
app.include_router(tasks_router)
app.include_router(skills_router)
app.include_router(audit_router)
