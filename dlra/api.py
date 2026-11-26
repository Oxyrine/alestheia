"""The supplier-side JSON API. The MCP server uses it too: one write path.

Every route needs `Authorization: Bearer $OPS_TOKEN`. With no token configured, nothing is served.
The web app includes `build_router(...)`; `create_app` serves it on its own.
"""
import hmac
import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from dlra import loader, reconcile, service
from dlra.db import connect
from dlra.rules import CONDUCT_RULES, GATE_RULES, HARD_RULES, LIMIT_RULES, RULE_TABLE

JSON_COLUMNS = ("evidence_ids", "payload", "gates", "basis", "data")


def _rows(cursor) -> list[dict]:
    return [
        {k: json.loads(v) if k in JSON_COLUMNS and isinstance(v, str) else v for k, v in dict(row).items()}
        for row in cursor
    ]


def rule_table_json() -> dict:
    return {
        "states": {
            str(state): {
                "label": row.label,
                "spec": row.spec,
                "allowed": [{"id": r.id, "action": r.action, "text": r.text} for r in row.allowed.values()],
                "forbidden": [{"id": r.id, "action": r.action, "text": r.text} for r in row.forbidden.values()],
            }
            for state, row in RULE_TABLE.items()
        },
        "hard": HARD_RULES, "gates": GATE_RULES, "conduct": CONDUCT_RULES, "limits": LIMIT_RULES,
    }


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject", "changes"]
    note: str = Field("", max_length=1000)
    options: dict = {}


def build_router(
    db_path: str, deps: service.Deps, token: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(ZoneInfo("Asia/Kolkata")),
) -> APIRouter:
    token = token if token is not None else os.environ.get("OPS_TOKEN", "")

    def authorised(authorization: str = Header("")) -> None:
        if not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "unauthorised")

    async def db():  # async so the connection is made on the handlers' thread
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    router = APIRouter(prefix="/api", dependencies=[Depends(authorised)])

    # Handlers are `async` on purpose: each request's connection then stays on one thread.
    @router.get("/cards")
    async def cards(status: str = "open", conn=Depends(db)):
        return _rows(conn.execute("SELECT * FROM cards WHERE status = ? ORDER BY rowid", (status,)))

    @router.get("/cards/{card_id}")
    async def card(card_id: str, conn=Depends(db)):
        found = _rows(conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)))
        if not found:
            raise HTTPException(404, "no such card")
        claims = {e.id: e for e in loader.case_evidence(conn, found[0]["case_id"])}
        found[0]["evidence"] = [
            claims[i].model_dump(mode="json") for i in found[0]["evidence_ids"] if i in claims
        ]
        return found[0]

    @router.post("/cards/{card_id}/decision")
    async def decision(card_id: str, body: DecisionBody, conn=Depends(db)):
        now = clock()
        try:
            service.decide_card(conn, card_id, body.decision, deps, now, body.note, options=body.options)
        except KeyError:
            raise HTTPException(404, "no such card") from None
        except service.DecisionError as error:
            raise HTTPException(409, str(error)) from None
        runs = service.dispatch(conn, deps, now)
        return {"card_id": card_id, "reran": [case_id for case_id, _ in runs]}

    @router.get("/cases")
    async def cases(state: str | None = None, conn=Depends(db)):
        return _rows(conn.execute(
            "SELECT c.id, c.state, c.diagnosis_confidence, c.under_review, i.number, cu.canonical_name AS buyer, "
            "(SELECT COUNT(*) FROM cards k WHERE k.case_id = c.id AND k.status = 'open') AS open_cards "
            "FROM cases c JOIN invoices i ON i.id = c.invoice_id JOIN customers cu ON cu.id = i.customer_id "
            "WHERE ?1 IS NULL OR c.state = ?1 ORDER BY c.id", (state,),
        ))

    @router.get("/cases/{case_id}")
    async def case(case_id: str, conn=Depends(db)):
        try:
            bundle = loader.load_bundle(conn, case_id, clock())
        except KeyError:
            raise HTTPException(404, "no such case") from None
        invoice = bundle.invoice
        proposed_tds = bundle.facts.reported_tds_paise or 0
        view = reconcile.settlement(
            invoice.gross_paise - invoice.credit_note_paise, bundle.facts.confirmed_paid.get(invoice.id, 0),
            proposed_tds, bundle.facts.tds_evidenced_paise,
        )
        row = _rows(conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)))[0]
        return {
            "case": row,
            "invoice": invoice.model_dump(mode="json"),
            "diagnosis": json.loads(row["diagnosis"]) if row["diagnosis"] else None,
            "settlement": view.__dict__,
            "promise": bundle.promise.model_dump(mode="json") if bundle.promise else None,
            "contacts": [c.model_dump(mode="json") for c in bundle.contacts],
            "evidence": [e.model_dump(mode="json") for e in bundle.evidence],
            "decisions": _rows(conn.execute("SELECT * FROM decisions WHERE case_id = ? ORDER BY rowid", (case_id,))),
            "messages": _rows(conn.execute("SELECT * FROM messages WHERE case_id = ? ORDER BY rowid", (case_id,))),
        }

    @router.get("/decisions/{decision_id}")
    async def explain(decision_id: str, conn=Depends(db)):
        found = _rows(conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)))
        if not found:
            raise HTTPException(404, "no such decision")
        claims = {e.id: e for e in loader.case_evidence(conn, found[0]["case_id"])}
        found[0]["evidence"] = [
            claims[i].model_dump(mode="json") for i in found[0]["evidence_ids"] if i in claims
        ]
        return found[0]

    @router.get("/rules")
    async def rules():
        return rule_table_json()

    return router


def create_app(db_path: str, deps: service.Deps, **kwargs) -> FastAPI:
    app = FastAPI(title="Diagnosis-Locked Receivables Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_router(db_path, deps, **kwargs))
    return app
