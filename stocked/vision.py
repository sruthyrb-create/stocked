"""How full is the fridge? Two analyzers, both run on the shelf crop only.

Privacy first: before any analysis the Ring snapshot is cropped to the fridge interior
(``shelf_box``, set per fridge), so people standing beside the fridge never reach the model or
the heuristic. The full snapshot is discarded; only the result (and optionally a small shelf
thumbnail) is kept.

* ``bedrock``  - Amazon Bedrock (Converse API, image input; Nova Lite by default) returns the fill
                 level, food types and issues (spill, door left open, spoiled food, non-food items).
* ``heuristic`` - no AWS needed: splits the shelf crop into a grid and counts cells that contain
                 colourful or dark objects against the bright, empty fridge interior. It estimates
                 fill only (no food types or issues) and is labelled as such everywhere.
"""

from __future__ import annotations

import io
import json
import logging
import os
from dataclasses import asdict, dataclass, field

from PIL import Image

log = logging.getLogger(__name__)
MODEL_ID = os.getenv("BEDROCK_VISION_MODEL_ID", "us.amazon.nova-lite-v1:0")
DEFAULT_SHELF_BOX = (0.331, 0.128, 0.669, 0.944)  # x0, y0, x1, y1 as fractions of the image
LEVELS = ["empty", "low", "half", "full"]
ISSUES = {"spill", "door_open", "spoiled_food", "non_food_items", "needs_cleaning"}


@dataclass
class Analysis:
    level: str  # empty | low | half | full
    percent: int  # 0-100 estimate
    food_types: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    method: str = "heuristic"
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def level_for(percent: float) -> str:
    if percent < 5:
        return "empty"
    if percent < 30:
        return "low"
    if percent < 78:
        return "half"
    return "full"


def crop_shelves(jpeg: bytes, box: tuple[float, float, float, float] = DEFAULT_SHELF_BOX) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    return img.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))


def heuristic(crop: Image.Image, grid: int = 16, calibration: float | None = None) -> Analysis:
    """Share of grid cells holding colourful/dark objects, scaled by the coverage a packed fridge reaches."""
    calibration = calibration or float(os.getenv("STOCKED_FULL_COVERAGE", "0.45"))
    cell = 8
    small = crop.resize((grid * cell, grid * cell))
    px = small.load()
    occupied = 0
    for gy in range(grid):
        for gx in range(grid):
            hits = 0
            for y in range(gy * cell, gy * cell + cell):
                for x in range(gx * cell, gx * cell + cell):
                    r, g, b = px[x, y]
                    if max(r, g, b) - min(r, g, b) > 45 or max(r, g, b) < 110:
                        hits += 1
            if hits / (cell * cell) > 0.35:
                occupied += 1
    pct = min(100, round(occupied / (grid * grid) / calibration * 100))
    return Analysis(level=level_for(pct), percent=pct, method="heuristic")


PROMPT = """This image is the inside of a public community fridge (free food shared by neighbors), cropped to the shelves.
Reply with ONLY this JSON:
{"percent": <0-100 how full the shelves are>, "food_types": [up to 5 of "produce","dairy","bread","prepared meals","drinks","canned/dry goods","snacks","other"],
 "issues": [any of "spill","door_open","spoiled_food","non_food_items","needs_cleaning"], "note": "<one short factual sentence>"}
Describe only food and the fridge. Never describe or mention people."""


def bedrock(crop: Image.Image) -> Analysis | None:
    try:
        import boto3

        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
        resp = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"image": {"format": "jpeg", "source": {"bytes": buf.getvalue()}}},
                                                   {"text": PROMPT}]}],
            inferenceConfig={"maxTokens": 200, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as exc:
        log.warning("Bedrock unavailable, using heuristic: %s", exc)
        return None
    pct = max(0, min(100, int(data.get("percent", 0))))
    return Analysis(level=level_for(pct), percent=pct,
                    food_types=[str(t) for t in data.get("food_types", [])][:5],
                    issues=[i for i in data.get("issues", []) if i in ISSUES],
                    method="bedrock", note=str(data.get("note", ""))[:200])


def analyze(jpeg: bytes, box: tuple[float, float, float, float] = DEFAULT_SHELF_BOX) -> tuple[Analysis, Image.Image]:
    crop = crop_shelves(jpeg, box)
    result = bedrock(crop) if os.getenv("STOCKED_BEDROCK") == "1" else None
    return (result or heuristic(crop)), crop
