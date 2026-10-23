"""Stocked engine: Ring motion at a community fridge -> wait for the door to close -> shelf snapshot
-> fill analysis -> public status + volunteer alerts."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .ring import RingClient, RingEvent
from .vision import DEFAULT_SHELF_BOX, Analysis, analyze

log = logging.getLogger("stocked")
DEMO_IMAGES = Path(__file__).resolve().parent.parent / "demo_images"
RANK = {"empty": 0, "low": 1, "half": 2, "full": 3}


@dataclass
class Fridge:
    id: str
    name: str
    address: str
    device_id: str  # Ring camera facing the fridge
    shelf_box: tuple[float, float, float, float] = DEFAULT_SHELF_BOX
    level: str = "unknown"
    percent: int | None = None
    food_types: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    method: str = ""
    note: str = ""
    updated_at: float | None = None
    empty_since: float | None = None
    empty_alerted: bool = False
    thumb: str = ""  # small base64 JPEG of the shelf crop only (no people)
    history: list[dict] = field(default_factory=list)  # {t, percent, level, source}
    sim_image: str = "half"  # simulator: which demo picture the "camera" returns


@dataclass
class Alert:
    id: str
    t: float
    fridge_id: str
    audience: str  # "volunteers" | "public"
    kind: str  # empty | restocked | issue | stale
    text: str
    resolved: bool = False


class Network:
    """A group of community fridges run by one mutual-aid group."""

    def __init__(self, data_dir: str | os.PathLike[str] | None = None, ring: RingClient | None = None,
                 debounce_s: float | None = None, clock: Callable[[], float] = time.time):
        self.dir = Path(data_dir or os.getenv("STOCKED_DATA", Path.home() / ".stocked"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ring = ring or RingClient()
        self.debounce_s = debounce_s if debounce_s is not None else float(os.getenv("STOCKED_DEBOUNCE_S", "20"))
        self.empty_alert_min = float(os.getenv("STOCKED_EMPTY_ALERT_MIN", "30"))
        self.stale_hours = float(os.getenv("STOCKED_STALE_HOURS", "6"))
        self.volunteer_url = os.getenv("STOCKED_VOLUNTEER_NOTIFY_URL", "")  # e.g. https://ntfy.sh/<topic>
        self.public_url = os.getenv("STOCKED_PUBLIC_NOTIFY_URL", "")  # "fridge restocked" subscribers
        self.network_name = os.getenv("STOCKED_NETWORK_NAME", "Neighborhood Fridge Network")
        self.clock = clock
        self.fridges: list[Fridge] = []
        self.alerts: list[Alert] = []
        self.events: list[dict] = []
        self._pending: dict[str, asyncio.Task] = {}
        self._seen: set[str] = set()
        self._listeners: list[Callable[[dict], None]] = []
        self._load()

    # -- persistence -------------------------------------------------------------------------
    def _path(self) -> Path:
        return self.dir / "network.json"

    def _load(self) -> None:
        if self._path().exists():
            d = json.loads(self._path().read_text(encoding="utf-8"))
            self.fridges = [Fridge(**{**f, "shelf_box": tuple(f["shelf_box"])}) for f in d.get("fridges", [])]
            self.alerts = [Alert(**a) for a in d.get("alerts", [])]
        if not self.fridges:
            self.seed()

    def save(self) -> None:
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"fridges": [asdict(f) for f in self.fridges],
                                   "alerts": [asdict(a) for a in self.alerts[:200]]}), encoding="utf-8")
        tmp.replace(self._path())

    def seed(self) -> None:
        """Demo network (fictional fridges). Replace with your own via STOCKED_FRIDGES (JSON)."""
        raw = os.getenv("STOCKED_FRIDGES")
        specs = json.loads(raw) if raw else [
            {"name": "Maple Street Fridge", "address": "Outside the laundromat, Maple St", "device_id": "sim-maple"},
            {"name": "Library Corner Fridge", "address": "Side entrance of the public library", "device_id": "sim-library"},
            {"name": "Riverside Fridge", "address": "Community garden gate, Riverside Ave", "device_id": "sim-riverside"},
        ]
        self.fridges = [Fridge(id=uuid.uuid5(uuid.NAMESPACE_URL, s["device_id"]).hex[:8], name=s["name"],
                               address=s["address"], device_id=s["device_id"],
                               shelf_box=tuple(s.get("shelf_box", DEFAULT_SHELF_BOX))) for s in specs]
        self.save()

    def reset(self) -> None:
        for t in self._pending.values():
            t.cancel()
        self._pending.clear()
        self.alerts, self.events = [], []
        self._seen.clear()
        self.fridges = []
        self.seed()
        self._emit("state")

    # -- pub/sub -------------------------------------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def _emit(self, kind: str, **data: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn({"kind": kind, **data})
            except Exception:
                pass

    def by_device(self, device_id: str) -> Fridge | None:
        return next((f for f in self.fridges if f.device_id == device_id), None)

    def by_id(self, fridge_id: str) -> Fridge | None:
        return next((f for f in self.fridges if f.id == fridge_id), None)

    # -- Ring events -----------------------------------------------------------------------------
    async def handle_event(self, ev: RingEvent) -> dict:
        if ev.request_id in self._seen:
            return {"duplicate": True}
        self._seen.add(ev.request_id)
        fridge = self.by_device(ev.device_id)
        if fridge is None:
            return {"ignored": "camera is not assigned to a fridge"}
        self.events.insert(0, {"t": self.clock(), "fridge": fridge.name, "type": ev.type, "sub_type": ev.sub_type})
        self.events = self.events[:30]
        # Someone is at the fridge. Wait until activity stops (door closed), then look at the shelves once.
        if fridge.id in self._pending:
            self._pending[fridge.id].cancel()
        self._pending[fridge.id] = asyncio.create_task(self._check_after_quiet(fridge))
        self._emit("event")
        return {"fridge": fridge.id, "check_in_s": self.debounce_s}

    async def _check_after_quiet(self, fridge: Fridge) -> None:
        try:
            await asyncio.sleep(self.debounce_s)
            await self.check(fridge, source="ring")
        except asyncio.CancelledError:
            pass
        finally:
            self._pending.pop(fridge.id, None)

    async def snapshot(self, fridge: Fridge) -> bytes | None:
        if fridge.device_id.startswith("sim-"):
            p = DEMO_IMAGES / f"{fridge.sim_image}.jpg"
            if not p.exists():
                p = self.dir / f"upload_{fridge.id}.jpg"
            return p.read_bytes() if p.exists() else None
        return await self.ring.snapshot(fridge.device_id)

    async def check(self, fridge: Fridge, source: str = "ring", image: bytes | None = None) -> Analysis | None:
        image = image or await self.snapshot(fridge)
        if not image:
            log.info("no snapshot available for %s", fridge.name)
            return None
        result, crop = await asyncio.to_thread(analyze, image, fridge.shelf_box)
        self.apply(fridge, result, crop, source)
        return result

    # -- status + alerts -----------------------------------------------------------------------------
    def apply(self, fridge: Fridge, a: Analysis, crop: Any, source: str) -> None:
        now = self.clock()
        prev_level = fridge.level
        fridge.level, fridge.percent, fridge.food_types = a.level, a.percent, a.food_types
        fridge.method, fridge.note, fridge.updated_at = a.method, a.note, now
        new_issues = [i for i in a.issues if i not in fridge.issues]
        fridge.issues = a.issues
        fridge.history.append({"t": now, "percent": a.percent, "level": a.level, "source": source})
        fridge.history = fridge.history[-200:]
        if crop is not None:
            thumb = crop.copy()
            thumb.thumbnail((240, 240))
            buf = io.BytesIO()
            thumb.save(buf, format="JPEG", quality=70)
            fridge.thumb = base64.b64encode(buf.getvalue()).decode()

        if a.level == "empty":
            fridge.empty_since = fridge.empty_since or now
        else:
            if prev_level in ("empty", "low") and RANK[a.level] >= RANK["half"]:
                foods = f" ({', '.join(a.food_types)})" if a.food_types else ""
                self._alert(fridge, "public", "restocked", f"{fridge.name} was just restocked: about {a.percent}% full{foods}.")
                for al in self.alerts:  # an empty alert for this fridge is now resolved
                    if al.fridge_id == fridge.id and al.kind == "empty":
                        al.resolved = True
            fridge.empty_since, fridge.empty_alerted = None, False
        for issue in new_issues:
            self._alert(fridge, "volunteers", "issue",
                        f"{fridge.name}: {issue.replace('_', ' ')} spotted on the shelves. Can someone stop by?")
        self.evaluate_timers()
        self.save()
        self._emit("state")

    def evaluate_timers(self) -> None:
        now = self.clock()
        for f in self.fridges:
            if f.empty_since and not f.empty_alerted and now - f.empty_since >= self.empty_alert_min * 60:
                mins = int((now - f.empty_since) // 60)
                self._alert(f, "volunteers", "empty", f"{f.name} has been empty for {mins} min. Can anyone restock it?")
                f.empty_alerted = True

    def is_stale(self, f: Fridge) -> bool:
        return f.updated_at is None or self.clock() - f.updated_at > self.stale_hours * 3600

    def _alert(self, fridge: Fridge, audience: str, kind: str, text: str) -> None:
        al = Alert(id=uuid.uuid4().hex[:8], t=self.clock(), fridge_id=fridge.id, audience=audience, kind=kind, text=text)
        self.alerts.insert(0, al)
        self._emit("alert", alert=asdict(al))
        url = self.volunteer_url if audience == "volunteers" else self.public_url
        if url:
            asyncio.get_event_loop().create_task(self._push(url, al))

    async def _push(self, url: str, al: Alert) -> None:
        try:
            async with httpx.AsyncClient(timeout=8) as http:
                if "ntfy" in url:
                    await http.post(url, content=al.text.encode(), headers={
                        "Title": "Fridge restocked" if al.kind == "restocked" else "Fridge needs a volunteer",
                        "Tags": "green_apple" if al.kind == "restocked" else "warning"})
                else:
                    await http.post(url, json=asdict(al))
        except httpx.HTTPError as exc:
            log.warning("notify failed: %s", exc)

    # -- volunteer actions -------------------------------------------------------------------------------
    def resolve(self, alert_id: str) -> None:
        for al in self.alerts:
            if al.id == alert_id:
                al.resolved = True
        self.save()
        self._emit("state")

    def clear_issues(self, fridge: Fridge) -> None:
        fridge.issues = []
        for al in self.alerts:
            if al.fridge_id == fridge.id and al.kind == "issue":
                al.resolved = True
        self.save()
        self._emit("state")

    async def run_timers(self, every_s: float = 30) -> None:
        while True:
            try:
                self.evaluate_timers()
                self.save()
            except Exception:
                log.exception("timer round failed")
            await asyncio.sleep(every_s)

    # -- views ----------------------------------------------------------------------------------------------
    def view(self) -> dict:
        now = self.clock()

        def fv(f: Fridge) -> dict:
            d = asdict(f)
            d["stale"] = self.is_stale(f)
            d["updated_min_ago"] = None if f.updated_at is None else int((now - f.updated_at) // 60)
            d["empty_for_min"] = None if not f.empty_since else int((now - f.empty_since) // 60)
            d["history"] = f.history[-48:]
            return d

        return {"network": self.network_name, "now": now, "fridges": [fv(f) for f in self.fridges],
                "alerts": [asdict(a) for a in self.alerts[:40]], "events": self.events[:15],
                "ring_configured": self.ring.configured, "debounce_s": self.debounce_s,
                "demo_images": sorted(p.stem for p in DEMO_IMAGES.glob("*.jpg"))}
