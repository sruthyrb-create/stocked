"""Stocked web app: public fridge board, volunteer console, Ring webhook receiver, simulator.

    python -m stocked.app      ->  http://localhost:8095
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .core import DEMO_IMAGES, Network
from .ring import build_webhook, parse_webhook, sign, verify_signature

log = logging.getLogger("stocked.app")
STATIC = Path(__file__).parent / "static"
HMAC_KEY = os.getenv("RING_HMAC_KEY", "stocked-local-demo-key")  # real key: Ring Developer Portal

net = Network()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(net.run_timers(float(os.getenv("STOCKED_TIMER_S", "30"))))
    yield
    task.cancel()


app = FastAPI(title="Stocked", lifespan=lifespan)


@app.post("/webhooks/ring")
async def ring_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    body = await request.body()  # verify the raw bytes
    if not verify_signature(body, request.headers.get("X-Signature"), HMAC_KEY):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        ev = parse_webhook(body)
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail="malformed payload")
    background.add_task(net.handle_event, ev)  # answer Ring within 5 s
    return JSONResponse({"ok": True})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state() -> dict:
    return net.view()


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
    unsubscribe = net.subscribe(lambda m: queue.put_nowait(m) if not queue.full() else None)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps({'kind': msg['kind']})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/fridges/{fridge_id}/check")
async def check_now(fridge_id: str) -> dict:
    f = net.by_id(fridge_id) or _404()
    result = await net.check(f, source="volunteer check")
    return {"result": result.to_dict() if result else None}


@app.post("/api/fridges/{fridge_id}/photo")
async def volunteer_photo(fridge_id: str, file: UploadFile = File(...)) -> dict:
    """A volunteer (or the demo) uploads a photo; it goes through the same crop + analysis as a Ring snapshot."""
    f = net.by_id(fridge_id) or _404()
    data = await file.read()
    if len(data) > 12_000_000:
        raise HTTPException(status_code=413, detail="photo too large")
    result = await net.check(f, source="volunteer photo", image=data)
    return {"result": result.to_dict() if result else None}


@app.post("/api/fridges/{fridge_id}/cleaned")
async def cleaned(fridge_id: str) -> dict:
    net.clear_issues(net.by_id(fridge_id) or _404())
    return {"ok": True}


@app.post("/api/alerts/{alert_id}/resolve")
async def resolve(alert_id: str) -> dict:
    net.resolve(alert_id)
    return {"ok": True}


@app.get("/api/ring/devices")
async def ring_devices() -> dict:
    if not net.ring.configured:
        return {"configured": False, "devices": []}
    try:
        return {"configured": True, "devices": await net.ring.devices()}
    except Exception as exc:
        return {"configured": True, "devices": [], "error": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------------------- simulator


class SimShelf(BaseModel):
    image: str  # demo image name (full/half/low/empty/spill)


@app.post("/api/sim/{fridge_id}/shelf")
async def sim_shelf(fridge_id: str, body: SimShelf) -> dict:
    """Choose what the simulated camera will see the next time it takes a snapshot."""
    f = net.by_id(fridge_id) or _404()
    if not (DEMO_IMAGES / f"{body.image}.jpg").exists():
        raise HTTPException(status_code=400, detail="unknown demo image")
    f.sim_image = body.image
    net.save()
    return {"ok": True}


@app.post("/api/sim/{fridge_id}/visit")
async def sim_visit(fridge_id: str, request: Request) -> dict:
    """Someone opens the fridge: send a signed Ring motion webhook to our own /webhooks/ring."""
    f = net.by_id(fridge_id) or _404()
    payload = json.dumps(build_webhook("motion_detected", f.device_id, "human")).encode()
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(str(request.base_url).rstrip("/") + "/webhooks/ring", content=payload,
                            headers={"Content-Type": "application/json", "X-Signature": sign(payload, HMAC_KEY)})
    return {"status": r.status_code, "check_in_s": net.debounce_s}


@app.post("/api/sim/advance")
async def sim_advance(minutes: float = 30) -> dict:
    """Demo helper: pretend time passed (ages the 'empty since' timers)."""
    for f in net.fridges:
        if f.empty_since:
            f.empty_since -= minutes * 60
        if f.updated_at:
            f.updated_at -= minutes * 60
    net.evaluate_timers()
    net.save()
    net._emit("state")
    return {"ok": True}


@app.post("/api/sim/reset")
async def sim_reset() -> dict:
    net.reset()
    return {"ok": True}


@app.get("/demo/{name}.jpg")
async def demo_image(name: str) -> FileResponse:
    p = DEMO_IMAGES / f"{name}.jpg"
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p)


def _404():
    raise HTTPException(status_code=404, detail="no such fridge")


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8095")))


if __name__ == "__main__":
    main()
