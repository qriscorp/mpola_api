from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from middleware.security import SecurityHeadersMiddleware, RateLimitMiddleware
from routers import (
    auth_router, users_router, loans_router,
    wallet_router, notifications_router, admin_router,
)
from database import ENGINE
from database.tables import Base

app = FastAPI(
    title="LendFlow API",
    description="Peer-to-peer lending platform — fintech-grade security",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── Security Middleware (order matters: outermost first) ───

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

# ─── CORS — strict allowlist by default; supports ALLOWED_ORIGINS=* ───

DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:3000",       # Next.js dev
    "http://localhost:3001",       # Next.js alt
    "http://localhost:8081",       # Expo dev
    "http://localhost:19006",      # Expo web
    "https://welend.qriscorp.com", # Production web
    "https://admin.welend.qriscorp.com",  # Production admin
    "https://api.welend.qriscorp.com",    # API docs/tools
    "https://lendflow.app",       # Production web
    "https://admin.lendflow.app", # Production admin
]




raw_allowed_origins = os.getenv("ALLOWED_ORIGINS", "")
if raw_allowed_origins.strip():
    parsed_allowed_origins = [
        origin.strip()
        for origin in raw_allowed_origins.split(",")
        if origin.strip()
    ]
else:
    parsed_allowed_origins = DEFAULT_ALLOWED_ORIGINS

allow_all_origins = len(parsed_allowed_origins) == 1 and parsed_allowed_origins[0] == "*"

if allow_all_origins:
    cors_allow_origins = ["*"]
    # Browsers do not allow credentialed requests with wildcard origins.
    cors_allow_credentials = False
    cors_allow_methods = ["*"]
    cors_allow_headers = ["*"]
else:
    cors_allow_origins = parsed_allowed_origins
    cors_allow_credentials = True
    cors_allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    cors_allow_headers = ["Authorization", "Content-Type", "X-Request-ID"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=cors_allow_methods,
    allow_headers=cors_allow_headers,
    expose_headers=["X-Request-ID"],
)

# ─── Routers ────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(loans_router)
app.include_router(wallet_router)
app.include_router(notifications_router)
app.include_router(admin_router)

# ─── Static files ───────────────────────────────────────────
os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ─── Health check ───────────────────────────────────────────

@app.get("/", tags=["Health"])
async def health():
    return {"status": "ok", "service": "LendFlow API", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok"}


# ─── Create tables on startup (dev — use alembic in prod) ──

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=ENGINE)



# git add .
# git commit -m "message"
# git push

# ./.venv/Scripts/Activate