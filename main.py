"""
LendFlow API — FastAPI Application
───────────────────────────────────
Peer-to-peer lending platform backend.
Security: Rate limiting, security headers, JWT auth, RBAC, audit logging.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

# ─── CORS — restrict to known origins (not wildcard *) ──────

ALLOWED_ORIGINS = [
    "http://localhost:3000",       # Next.js dev
    "http://localhost:3001",       # Next.js alt
    "http://localhost:8081",       # Expo dev
    "http://localhost:19006",      # Expo web
    "https://lendflow.app",       # Production web
    "https://admin.lendflow.app", # Production admin
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
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

import os
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