"""Local HTTP server for the traces KiCad extension.

A dependency-light stand-in for the old hosted backend: same `/jobs/*` request
and `{job_id}` → poll → `{status, result}` contract the plugin already speaks,
but computed locally with an in-memory job store (no Redis, no auth, no
billing). The MCP server is also mounted at `/mcp` for agents that prefer HTTP.
"""

import asyncio
import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import library, netlist
from .mcp_server import mcp
from .suppliers import sourcing

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("traces")

# job_id -> {"status": "in_progress"|"complete"|"failed", "result": dict|None, "error": str|None}
_JOBS: dict[str, dict] = {}


def _enqueue(coro) -> str:
    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {"status": "in_progress", "result": None, "error": None}

    async def _run():
        try:
            _JOBS[job_id]["result"] = await coro
            _JOBS[job_id]["status"] = "complete"
        except Exception as exc:  # noqa: BLE001 - surfaced to the client via job status
            logger.exception("job %s failed", job_id)
            _JOBS[job_id]["status"] = "failed"
            _JOBS[job_id]["error"] = str(exc)

    asyncio.create_task(_run())
    return job_id


# The streamable-http MCP app carries its own session-manager lifespan, which
# FastAPI must run — so hand its lifespan to the parent app and mount it.
mcp_http = mcp.http_app(path="/", transport="streamable-http")
app = FastAPI(
    title="traces (local)",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=mcp_http.lifespan,
)
app.mount("/mcp", mcp_http)


@app.get("/health")
async def health():
    from .config import active_model
    return {"status": "online", "model": active_model()}


@app.get("/model")
async def model():
    """The model that will answer requests right now (cloud vs local + name).
    The KiCad extension shows this so you always know what's running."""
    from .config import active_model
    return active_model()


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        return {"job_id": job_id, "status": "not_found", "result": None}
    return {"job_id": job_id, "status": job["status"], "result": job["result"], "error": job["error"]}


# --- Request models ---------------------------------------------------------

class SearchRequest(BaseModel):
    description: str
    footprint: str = ""
    max_price: float | None = None
    min_qty: int | None = None
    count: int = Field(1, ge=1, le=5)


class JlcpcbSourceRequest(BaseModel):
    mpn: str = ""
    lcsc: str = ""
    footprint: str = ""


class SupplierSourceRequest(BaseModel):
    description: str | None = None
    mpn: str | None = None
    footprint: str = ""

    @property
    def search_terms(self) -> str:
        return (self.description or self.mpn or "").strip()


class PartNumberRequest(BaseModel):
    part_number: str


class DatasheetFetchRequest(BaseModel):
    lcsc: str


class NetlistCheckRequest(BaseModel):
    xml: str


# --- JLCPCB -----------------------------------------------------------------

@app.post("/jobs/jlcpcb/search")
async def jlcpcb_search(req: SearchRequest):
    return {"job_id": _enqueue(sourcing.jlcpcb_search(
        req.description, req.footprint, req.max_price, req.min_qty, req.count))}


@app.post("/jobs/jlcpcb/source")
async def jlcpcb_source(req: JlcpcbSourceRequest):
    identifier = (req.lcsc or req.mpn).strip()
    if not identifier:
        raise HTTPException(422, "mpn or lcsc is required")
    id_type = "LCSC" if req.lcsc.strip() else "MPN"
    return {"job_id": _enqueue(sourcing.jlcpcb_source(identifier, req.footprint, id_type))}


# --- Digi-Key ---------------------------------------------------------------

@app.post("/jobs/digikey/search")
async def digikey_search(req: SearchRequest):
    return {"job_id": _enqueue(sourcing.digikey_search(
        req.description, req.footprint, req.max_price, req.min_qty, req.count))}


@app.post("/jobs/digikey/source")
async def digikey_source(req: SupplierSourceRequest):
    if not req.search_terms:
        raise HTTPException(422, "description or mpn is required")
    return {"job_id": _enqueue(sourcing.digikey_source(req.search_terms, req.footprint))}


@app.post("/jobs/digikey/datasheet")
async def digikey_datasheet(req: PartNumberRequest):
    return {"job_id": _enqueue(sourcing.digikey_datasheet(req.part_number.strip()))}


# --- Mouser -----------------------------------------------------------------

@app.post("/jobs/mouser/search")
async def mouser_search(req: SearchRequest):
    return {"job_id": _enqueue(sourcing.mouser_search(
        req.description, req.footprint, req.max_price, req.min_qty, req.count))}


@app.post("/jobs/mouser/source")
async def mouser_source(req: SupplierSourceRequest):
    if not req.search_terms:
        raise HTTPException(422, "description or mpn is required")
    return {"job_id": _enqueue(sourcing.mouser_source(req.search_terms, req.footprint))}


@app.post("/jobs/mouser/datasheet")
async def mouser_datasheet(req: PartNumberRequest):
    return {"job_id": _enqueue(sourcing.mouser_datasheet(req.part_number.strip()))}


# --- Datasheet & netlist ----------------------------------------------------

@app.post("/jobs/datasheet/fetch")
async def datasheet_fetch(req: DatasheetFetchRequest):
    return {"job_id": _enqueue(sourcing.datasheet_fetch(req.lcsc.strip()))}


def _netlist_route(check: str):
    async def handler(req: NetlistCheckRequest):
        return {"job_id": _enqueue(run_in_threadpool(netlist.CHECKS[check], req.xml))}
    return handler


for _check in netlist.CHECKS:
    app.add_api_route(f"/jobs/netlist/{_check}", _netlist_route(_check), methods=["POST"])


# --- Library (KiCad symbol/footprint via easyeda2kicad) ---------------------

@app.get("/library/search")
async def library_search(mpn: str):
    return await run_in_threadpool(library.search_lcsc, mpn)


@app.get("/library/symbol/{lcsc_id}")
async def library_symbol(lcsc_id: str):
    try:
        return await run_in_threadpool(library.get_symbol, lcsc_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


def main() -> None:
    """HTTP entry point — `traces-serve`."""
    import uvicorn

    from .config import HOST, PORT

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
