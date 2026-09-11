"""FastAPI boundary for Telegram, Hermes publishers, and workers."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    AuthenticationError, RateLimiter, issue_session, validate_session, validate_tailscale_identity,
)
from .config import Settings
from .database import ConflictError, Database, NotFoundError, StateError
from .workers import Workers


class CardResponse(BaseModel):
    card_version: int = Field(ge=1)
    outcome: Literal["recommended", "alternative", "rejected", "deferred", "abstained"]
    selected_option_id: str | None = None
    note: str = Field(default="", max_length=4000)


class SubmitRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ApplyRequest(BaseModel):
    expected_version: int = Field(ge=1)


class SocketHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale = []
        for client in list(self.clients):
            try:
                await client.send_json(payload)
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(
        settings.database_path,
        settings.manifest_root,
        settings.default_expiry_days,
        auto_resume=settings.auto_resume,
        weekly_only=settings.weekly_only,
    )
    db.initialize()
    workers = Workers(db, settings)
    hub = SocketHub()
    auth_limiter = RateLimiter(limit=12, window_seconds=60)
    mutation_limiter = RateLimiter(limit=120, window_seconds=60)
    worker_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal worker_task
        if settings.worker_enabled:
            worker_task = asyncio.create_task(workers.run())
        yield
        workers.stop()
        if worker_task:
            await worker_task

    app = FastAPI(title="Hermes Weekly Wiki Review", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.hub = hub
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.mini_app_url, "http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "If-Match"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; connect-src 'self' wss: https:; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:"
        )
        return response

    def publisher_profile(authorization: Annotated[str | None, Header()] = None) -> str:
        token = (authorization or "").removeprefix("Bearer ").strip()
        matches = [profile for profile, expected in settings.publish_tokens.items()
                   if token and __import__("hmac").compare_digest(token, expected)]
        if len(matches) != 1:
            raise HTTPException(status_code=401, detail="invalid publisher credential")
        return matches[0]

    def owner(request: Request, authorization: Annotated[str | None, Header()] = None) -> int:
        token = (authorization or "").removeprefix("Bearer ").strip()
        try:
            user_id = validate_session(token, settings.signing_key, settings.telegram_owner_id)
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="not authorized") from None
        if not mutation_limiter.allow(f"owner:{user_id}:{request.url.path}"):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return user_id

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StateError)
    async def bad_state(_: Request, exc: StateError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_value(_: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/api/auth/tailscale")
    async def authenticate(request: Request):
        key = request.headers.get("Tailscale-User-Login") or (request.client.host if request.client else "unknown")
        if not auth_limiter.allow(key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        try:
            login = validate_tailscale_identity(
                request.headers.get("Tailscale-User-Login", ""), settings.tailscale_owner_login)
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="not authorized") from None
        return {
            "token": issue_session(settings.telegram_owner_id, settings.signing_key),
            "expires_in": 900,
            "login": login,
        }

    @app.get("/api/inbox")
    async def inbox(tab: str = Query("new", pattern="^(new|deferred|completed)$"), _: int = Depends(owner)):
        return {"items": db.inbox(tab)}

    @app.get("/api/decisions/{decision_id}")
    async def decision(decision_id: str, _: int = Depends(owner)):
        return db.get_decision(decision_id)

    @app.put("/api/cards/{card_id}/response")
    async def respond(card_id: str, payload: CardResponse, user_id: int = Depends(owner)):
        result = db.respond(card_id, payload.card_version, payload.outcome,
                            payload.selected_option_id, payload.note, user_id)
        await hub.broadcast({"type": "decision_updated", "decision_id": result["decision_id"]})
        return result

    @app.post("/api/decisions/{decision_id}/submit")
    async def submit(decision_id: str, payload: SubmitRequest, user_id: int = Depends(owner)):
        result = db.submit(decision_id, payload.expected_version, user_id)
        await hub.broadcast({"type": "decision_submitted", "decision_id": decision_id})
        return result

    @app.post("/api/manifests/{manifest_id}/apply")
    async def apply_manifest(manifest_id: str, payload: ApplyRequest, _: int = Depends(owner)):
        if not settings.wiki_executor_enabled:
            raise HTTPException(status_code=503, detail="Wiki executor is disabled")
        result = db.queue_apply(manifest_id, payload.expected_version)
        return result

    @app.get("/api/executions/{execution_id}")
    async def execution(execution_id: str, _: int = Depends(owner)):
        return db.get_execution(execution_id)

    @app.post("/internal/v1/decisions")
    async def publish(payload: dict[str, Any], profile: str = Depends(publisher_profile)):
        try:
            result = db.publish(payload, profile)
        except PermissionError:
            raise HTTPException(status_code=403, detail="publisher profile mismatch") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        await hub.broadcast({"type": "decision_created", "decision_id": result["decision_id"]})
        return result

    @app.get("/internal/v1/decisions/{decision_id}")
    async def internal_decision(decision_id: str, profile: str = Depends(publisher_profile)):
        result = db.get_decision(decision_id)
        if profile != "*" and result["source_profile"] != profile:
            raise HTTPException(status_code=403, detail="publisher profile mismatch")
        return result

    @app.websocket("/api/ws")
    async def websocket(websocket: WebSocket, token: str = Query(...)):
        try:
            validate_session(token, settings.signing_key, settings.telegram_owner_id)
        except AuthenticationError:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.clients.discard(websocket)

    @app.get("/healthz")
    async def health(request: Request, deep: bool = False):
        db_ok = True
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            db_ok = False
        result: dict[str, Any] = {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "error",
            "telegram": "configured" if settings.telegram_bot_token else "unconfigured",
            "hermes_api": "configured" if settings.hermes_profile_api_keys else "unconfigured",
        }
        if deep:
            source = request.client.host if request.client else ""
            trusted_local = source in {"127.0.0.1", "::1"}
            trusted_owner = request.headers.get("Tailscale-User-Login", "").strip().lower() == settings.tailscale_owner_login
            if not trusted_local and not trusted_owner:
                raise HTTPException(status_code=403, detail="deep health is private")
            async with httpx.AsyncClient(timeout=5) as client:
                if settings.telegram_bot_token:
                    try:
                        response = await client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe")
                        result["telegram"] = "reachable" if response.is_success else "error"
                    except Exception:
                        result["telegram"] = "error"
                if settings.hermes_profile_api_keys:
                    profile_checks: dict[str, str] = {}
                    for profile, key in settings.hermes_profile_api_keys.items():
                        try:
                            response = await client.get(
                                workers.hermes._url(profile, "/health"),
                                headers={"Authorization": f"Bearer {key}"},
                            )
                            profile_checks[profile] = "reachable" if response.is_success else "error"
                        except Exception:
                            profile_checks[profile] = "error"
                    result["hermes_profiles"] = profile_checks
                    result["hermes_api"] = (
                        "reachable" if all(value == "reachable" for value in profile_checks.values()) else "error"
                    )
        return result

    web_dist = Path(__import__("os").environ.get("DECISION_INBOX_WEB_DIST", "web/dist"))
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


app = create_app()
