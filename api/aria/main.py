"""
ARIA - Main FastAPI Application

Phase: 1
Purpose: FastAPI application entry point

Related Spec Sections:
- Section 5: API Specification
- Section 7: Project Structure
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aria.config import settings
from aria.db.mongodb import connect_db, close_db
from aria.api.routes import health, conversations, agents


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events."""
    # Startup
    await connect_db()
    yield
    # Shutdown
    await close_db()


app = FastAPI(
    title="ARIA",
    description="Autonomous Reasoning & Intelligence Architecture",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS middleware for web UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(conversations.router, prefix="/api/v1", tags=["conversations"])
app.include_router(agents.router, prefix="/api/v1", tags=["agents"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "ARIA",
        "version": "0.2.0",
        "docs": "/docs",
    }
