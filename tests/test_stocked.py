import asyncio
import json
import os
import tempfile

os.environ["STOCKED_DATA"] = tempfile.mkdtemp()
os.environ["RING_HMAC_KEY"] = "test-key"
os.environ["STOCKED_DEBOUNCE_S"] = "0"
os.environ.pop("RING_ACCESS_TOKEN", None)
os.environ.pop("STOCKED_BEDROCK", None)

from fastapi.testclient import TestClient
from PIL import Image

from stocked import app as appmod
from stocked import vision
from stocked.core import DEMO_IMAGES, Network
from stocked.ring import RingClient, build_webhook, parse_webhook, sign


def img(name):
    return (DEMO_IMAGES / f"{name}.jpg").read_bytes()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make_net():
    clock = Clock()
    net = Network(data_dir=tempfile.mkdtemp(), ring=RingClient(access_token=None), debounce_s=0, clock=clock)
    return net, clock


def test_heuristic_orders_fill_levels():
    levels = {n: vision.analyze(img(n))[0] for n in ["full", "half", "low", "empty"]}
    assert [levels[n].level for n in ["full", "half", "low", "empty"]] == ["full", "half", "low", "empty"]
    assert levels["full"].percent > levels["half"].percent > levels["low"].percent > levels["empty"].percent == 0
    assert levels["full"].method == "heuristic" and levels["full"].issues == []


def test_crop_excludes_people_beside_the_fridge():
    # the 'full' and 'low' demo images contain a person left of the fridge (x < 170 of 960)
    crop = vision.crop_shelves(img("full"))
    full_w = Image.open(DEMO_IMAGES / "full.jpg").width
    assert crop.width < full_w * 0.4
    assert vision.DEFAULT_SHELF_BOX[0] * full_w > 170


def test_motion_event_checks_shelves_after_quiet_and_alerts_flow():
    async def run():
        net, clock = make_net()
        f = net.fridges[0]
        f.sim_image = "empty"
        ev = parse_webhook(json.dumps(build_webhook("motion_detected", f.device_id, "human")).encode())
        await net.handle_event(ev)
        await asyncio.sleep(0.2)
        assert f.level == "empty" and f.empty_since is not None
        assert not any(a.kind == "empty" for a in net.alerts)  # not yet: empty only just now
        clock.t += 31 * 60
        net.evaluate_timers()
        assert [a.kind for a in net.alerts] == ["empty"] and net.alerts[0].audience == "volunteers"
        net.evaluate_timers()
        assert len(net.alerts) == 1  # one alert per empty episode
        # restocked -> public alert, empty alert resolved
        await net.check(f, image=img("full"), source="ring")
        kinds = [a.kind for a in net.alerts]
        assert kinds[0] == "restocked" and net.alerts[0].audience == "public"
        assert all(a.resolved for a in net.alerts if a.kind == "empty")
        assert f.empty_since is None and f.thumb
        assert (await net.handle_event(ev)) == {"duplicate": True}

    asyncio.run(run())


def test_unknown_camera_is_ignored():
    async def run():
        net, _ = make_net()
        ev = parse_webhook(json.dumps(build_webhook("motion_detected", "someone-elses-cam", "human")).encode())
        assert "ignored" in await net.handle_event(ev)

    asyncio.run(run())


def test_issue_from_bedrock_alerts_volunteers_once(monitor_issue=vision.Analysis(level="half", percent=55,
                                                    food_types=["produce"], issues=["spill"], method="bedrock")):
    net, _ = make_net()
    f = net.fridges[0]
    net.apply(f, monitor_issue, None, "ring")
    net.apply(f, monitor_issue, None, "ring")
    issues = [a for a in net.alerts if a.kind == "issue"]
    assert len(issues) == 1 and "spill" in issues[0].text
    net.clear_issues(f)
    assert f.issues == [] and issues[0].resolved


def test_stale_reading_is_flagged():
    net, clock = make_net()
    f = net.fridges[0]
    net.apply(f, vision.heuristic(vision.crop_shelves(img("full"))), None, "ring")
    assert not net.is_stale(f)
    clock.t += 7 * 3600
    assert net.is_stale(f)


def test_bedrock_result_is_parsed_and_sanitized(monkeypatch):
    class Fake:
        def converse(self, **kw):
            text = 'Here: {"percent": 140, "food_types": ["produce","bread"], "issues": ["spill","a person"], "note": "Bottom shelf has a puddle."}'
            return {"output": {"message": {"content": [{"text": text}]}}}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Fake())
    monkeypatch.setenv("STOCKED_BEDROCK", "1")
    a, _ = vision.analyze(img("spill"))
    assert a.method == "bedrock" and a.percent == 100 and a.level == "full"
    assert a.issues == ["spill"]  # unknown issue strings are dropped


def test_webhook_signature_required():
    with TestClient(appmod.app) as client:
        body = json.dumps(build_webhook("motion_detected", "sim-maple", "human")).encode()
        assert client.post("/webhooks/ring", content=body, headers={"X-Signature": "sha256=bad"}).status_code == 401
        assert client.post("/webhooks/ring", content=body, headers={"X-Signature": sign(body, "test-key")}).status_code == 200


def test_volunteer_photo_upload_runs_same_pipeline():
    with TestClient(appmod.app) as client:
        fid = client.get("/api/state").json()["fridges"][1]["id"]
        r = client.post(f"/api/fridges/{fid}/photo", files={"file": ("p.jpg", img("low"), "image/jpeg")})
        assert r.json()["result"]["level"] == "low"
