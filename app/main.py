"""
Metered AI gateway — OpenAI-compatible proxy that meters every request.

Forwards /v1/chat/completions (and any other /v1/* path) to an upstream
OpenAI-compatible API, records tokens/latency/cost per request into Postgres
(or SQLite locally), and exposes Prometheus metrics + a JSON usage summary.

Why it exists: if you resell or share AI access, you need per-key spend you can
prove. Railway's own usage numbers are per-service; this gives you per-request,
per-key, per-model accounting you can chart in Grafana or bill from.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "info").upper())
LOG = logging.getLogger("metered-gateway")

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
GATEWAY_API_KEYS = [k.strip() for k in os.getenv("GATEWAY_API_KEYS", "").split(",") if k.strip()]
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
REQUEST_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120"))

# Prices are USD per 1M tokens. Override with MODEL_PRICES as JSON, e.g.
# {"gpt-4o-mini": {"in": 0.15, "out": 0.6}}
DEFAULT_PRICE_IN = float(os.getenv("DEFAULT_PRICE_IN_PER_1M", "0"))
DEFAULT_PRICE_OUT = float(os.getenv("DEFAULT_PRICE_OUT_PER_1M", "0"))
try:
    MODEL_PRICES: dict[str, dict[str, float]] = json.loads(os.getenv("MODEL_PRICES", "{}"))
except json.JSONDecodeError:
    LOG.warning("MODEL_PRICES is not valid JSON, ignoring it")
    MODEL_PRICES = {}


def normalize_db_url(url: str) -> str:
    """Railway hands out postgres:// / postgresql:// — SQLAlchemy needs the driver."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


DATABASE_URL = normalize_db_url(os.getenv("DATABASE_URL", "sqlite:///./usage.db"))
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

metadata = MetaData()
usage_events = Table(
    "usage_events",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("client_key", String(64), nullable=False, index=True),
    Column("model", String(128), nullable=False, index=True),
    Column("path", String(128), nullable=False),
    Column("status", Integer, nullable=False),
    Column("streamed", Integer, nullable=False, default=0),
    Column("prompt_tokens", Integer, nullable=True),
    Column("completion_tokens", Integer, nullable=True),
    Column("total_tokens", Integer, nullable=True),
    Column("latency_ms", Float, nullable=False),
    Column("cost_usd", Float, nullable=False, default=0.0),
    Column("error", String(256), nullable=True),
)

def ensure_schema() -> None:
    metadata.create_all(engine)
    LOG.info("schema ready on %s", engine.url.render_as_string(hide_password=True))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_schema()
    yield


app = FastAPI(title="Metered AI Gateway", version="1.0.0", lifespan=lifespan)


def key_label(authorization: Optional[str]) -> str:
    """Stable, non-reversible label for a caller key. Never store the raw key."""
    raw = (authorization or "").removeprefix("Bearer ").strip()
    if not raw:
        return "anonymous"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def authorize(authorization: Optional[str]) -> None:
    if not GATEWAY_API_KEYS:
        return
    raw = (authorization or "").removeprefix("Bearer ").strip()
    if raw not in GATEWAY_API_KEYS:
        raise HTTPException(status_code=401, detail="invalid gateway key")


def require_admin(request: Request, token: Optional[str]) -> None:
    """Gate /usage and /metrics when ADMIN_TOKEN is set.

    Spend per model is business data; on a public domain it should not be world
    readable. Without ADMIN_TOKEN the endpoints stay open (handy for local dev).
    """
    if not ADMIN_TOKEN:
        return
    supplied = token or request.headers.get("x-admin-token") or ""
    if supplied != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="admin token required")


def price_for(model: str) -> tuple[float, float]:
    p = MODEL_PRICES.get(model)
    if not p:
        return DEFAULT_PRICE_IN, DEFAULT_PRICE_OUT
    return float(p.get("in", DEFAULT_PRICE_IN)), float(p.get("out", DEFAULT_PRICE_OUT))


def cost_of(model: str, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> float:
    price_in, price_out = price_for(model)
    return ((prompt_tokens or 0) * price_in + (completion_tokens or 0) * price_out) / 1_000_000


def record(
    *,
    client_key: str,
    model: str,
    path: str,
    status: int,
    latency_ms: float,
    streamed: bool = False,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    row = {
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        "client_key": client_key,
        "model": model or "unknown",
        "path": path,
        "status": status,
        "streamed": 1 if streamed else 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "cost_usd": cost_of(model, prompt_tokens, completion_tokens),
        "error": (error or "")[:256] or None,
    }
    with engine.begin() as conn:
        conn.execute(usage_events.insert().values(**row))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


async def upstream_headers(authorization: Optional[str]) -> dict[str, str]:
    """Prefer the caller's own key (BYOK); fall back to the service key."""
    bearer = UPSTREAM_API_KEY
    if authorization and not GATEWAY_API_KEYS:
        bearer = authorization.removeprefix("Bearer ").strip()
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


@app.api_route("/v1/{path:path}", methods=["POST", "GET"])
async def proxy(path: str, request: Request, authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    label = key_label(authorization)
    body = await request.body()
    payload: dict[str, Any] = {}
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="request body is not valid JSON")
    model = str(payload.get("model", "unknown"))
    streamed = bool(payload.get("stream"))
    started = time.perf_counter()
    url = f"{UPSTREAM_BASE_URL}/{path}"
    headers = await upstream_headers(authorization)

    if streamed:
        # Streaming has no usage block to read; meter the request only.
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        req = client.build_request("POST", url, content=body, headers=headers, params=request.query_params)
        upstream = await client.send(req, stream=True)

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
                record(
                    client_key=label,
                    model=model,
                    path=path,
                    status=upstream.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    streamed=True,
                )

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            upstream = await client.request(
                method=request.method, url=url, content=body, headers=headers, params=request.query_params
            )
    except httpx.HTTPError as exc:
        record(
            client_key=label,
            model=model,
            path=path,
            status=502,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=str(exc),
        )
        return JSONResponse({"error": {"message": f"upstream error: {exc}"}}, status_code=502)

    latency_ms = (time.perf_counter() - started) * 1000
    usage: dict[str, Any] = {}
    if upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            usage = (upstream.json() or {}).get("usage") or {}
        except json.JSONDecodeError:
            usage = {}

    record(
        client_key=label,
        model=model,
        path=path,
        status=upstream.status_code,
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        error=None if upstream.status_code < 400 else upstream.text[:200],
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request, token: Optional[str] = None) -> str:
    require_admin(request, token)
    lines: list[str] = []
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                usage_events.c.model,
                usage_events.c.status,
                func.count().label("n"),
                func.coalesce(func.sum(usage_events.c.prompt_tokens), 0),
                func.coalesce(func.sum(usage_events.c.completion_tokens), 0),
                func.coalesce(func.sum(usage_events.c.cost_usd), 0.0),
                func.coalesce(func.sum(usage_events.c.latency_ms), 0.0),
            ).group_by(usage_events.c.model, usage_events.c.status)
        ).all()

    lines.append("# HELP gateway_requests_total Requests proxied, by model and status")
    lines.append("# TYPE gateway_requests_total counter")
    for model, status, n, _p, _c, _cost, _l in rows:
        lines.append(f'gateway_requests_total{{model="{model}",status="{status}"}} {n}')

    lines.append("# HELP gateway_prompt_tokens_total Prompt tokens seen upstream")
    lines.append("# TYPE gateway_prompt_tokens_total counter")
    lines.append("# HELP gateway_completion_tokens_total Completion tokens seen upstream")
    lines.append("# TYPE gateway_completion_tokens_total counter")
    lines.append("# HELP gateway_cost_usd_total Estimated upstream cost in USD")
    lines.append("# TYPE gateway_cost_usd_total counter")
    lines.append("# HELP gateway_latency_ms_sum Total upstream latency in milliseconds")
    lines.append("# TYPE gateway_latency_ms_sum counter")

    agg: dict[str, dict[str, float]] = {}
    for model, _status, _n, p, c, cost, latency in rows:
        a = agg.setdefault(model, {"p": 0.0, "c": 0.0, "cost": 0.0, "lat": 0.0})
        a["p"] += float(p)
        a["c"] += float(c)
        a["cost"] += float(cost)
        a["lat"] += float(latency)
    for model, a in agg.items():
        lines.append(f'gateway_prompt_tokens_total{{model="{model}"}} {a["p"]:.0f}')
        lines.append(f'gateway_completion_tokens_total{{model="{model}"}} {a["c"]:.0f}')
        lines.append(f'gateway_cost_usd_total{{model="{model}"}} {a["cost"]:.8f}')
        lines.append(f'gateway_latency_ms_sum{{model="{model}"}} {a["lat"]:.1f}')
    return "\n".join(lines) + "\n"


@app.get("/usage")
def usage(request: Request, limit: int = 50, token: Optional[str] = None) -> dict[str, Any]:
    require_admin(request, token)
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                usage_events.c.model,
                func.count().label("requests"),
                func.coalesce(func.sum(usage_events.c.prompt_tokens), 0),
                func.coalesce(func.sum(usage_events.c.completion_tokens), 0),
                func.coalesce(func.sum(usage_events.c.cost_usd), 0.0),
            )
            .group_by(usage_events.c.model)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        recent = conn.execute(
            select(
                usage_events.c.created_at,
                usage_events.c.client_key,
                usage_events.c.model,
                usage_events.c.status,
                usage_events.c.total_tokens,
                usage_events.c.latency_ms,
                usage_events.c.cost_usd,
            )
            .order_by(usage_events.c.id.desc())
            .limit(limit)
        ).all()

    return {
        "by_model": [
            {
                "model": r[0],
                "requests": r[1],
                "prompt_tokens": r[2],
                "completion_tokens": r[3],
                "cost_usd": round(r[4], 8),
            }
            for r in rows
        ],
        "recent": [
            {
                "created_at": r[0].isoformat(),
                "client_key": r[1],
                "model": r[2],
                "status": r[3],
                "total_tokens": r[4],
                "latency_ms": round(r[5], 1),
                "cost_usd": round(r[6], 8),
            }
            for r in recent
        ],
    }
