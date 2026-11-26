"""The deployed service: uvicorn dlra.serve:create_app --factory

ponytail: until diagnose.py and the web pages exist this serves the supplier API over the demo
cases with scripted diagnoses. The database is rebuilt on every boot (all data is synthetic).
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from dlra import api, mailer, scheduler, service
from dlra.db import connect
from dlra.policy import load_policy
from scripts import demo_world as world
from scripts.replay_demo import replay


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "demo.db")
    if Path(db_path).suffix != ".db":
        raise ValueError("DB_PATH must end in .db: it is deleted and rebuilt at boot")
    Path(db_path).unlink(missing_ok=True)
    deps = service.Deps(policy=load_policy(), propose=world.scripted_diagnosis)
    conn = connect(db_path)
    try:
        world.seed(conn)
        replay(conn, deps, say=lambda *_: None)  # its mailer is a list: boot never sends
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        jobs = scheduler.start(db_path, deps, mailer.send)
        yield
        jobs.shutdown(wait=False)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(api.build_router(db_path, deps))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "data": "synthetic", "diagnoses": "scripted"}

    return app
