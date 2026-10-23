# Stocked

**Live "is there food in it?" status for community fridges, from the Ring camera already watching them.**

Community fridges are free, public fridges on the sidewalk: neighbors leave food, anyone takes what they need. They have no staff and no inventory system. Two people constantly guess:

- **Someone who needs food** walks over without knowing if the fridge is full or bare.
- **Volunteers** find out it has been empty for hours only when someone happens to post a photo.

Many fridges already have a camera pointed at them for safety. Stocked turns that camera into a fill gauge: after someone uses the fridge, it takes one snapshot, crops it to the shelves (never the people), estimates how full it is, and updates a public board. Volunteers are told when a fridge has been empty for 30 minutes or has a spill; followers are told when it is restocked.

## Why it matters

- After the November 2025 SNAP disruption, fridges in New York and elsewhere were [emptying faster than volunteers could refill them](https://pix11.com/news/local-news/community-fridges-see-surge-in-use-as-snap-benefits-face-cuts/amp/), and a fridge in New Mexico was described as [emptying and refilling all day](https://sourcenm.com/2025/11/04/as-new-mexicans-face-food-uncertainty-a-local-community-fridge-empties-and-refills/).
- A WPI student team working with a fridge network found that [measuring fridge use is hard without breaking the self-service model](https://digital.wpi.edu/concern/student_works/td96k588h): they fell back on surveys and a door sensor. Stocked needs no new hardware and asks nothing of the people using the fridge.

## How it works

```
Ring motion (human) ──► /webhooks/ring  (HMAC verified, deduped, answered < 5 s)
                          │
                          ▼ wait until activity stops (debounce, default 20 s)
                  Ring snapshot of the fridge
                          │
                          ▼ crop to the shelf box  ── people outside the box are never analyzed
            Amazon Bedrock (Nova Lite)   or   no-AI pixel heuristic
            fill %, food types, issues        fill % only
                          │
                          ▼
    public board  ·  "restocked" alert to followers  ·  "empty 30 min" / "spill" alert to volunteers
```

| Ring Partner API | Where | Why |
|---|---|---|
| Webhooks v1.1 (`motion_detected`, sub-type `human`) with `X-Signature` HMAC-SHA256 | `stocked/ring.py`, `stocked/app.py` | Know when someone used the fridge |
| `POST /v1/devices/{id}/media/image/download` (`latest_in_range`) → 303 to a pre-signed URL | `RingClient.snapshot` | One picture of the shelves after the visit (bearer token is not forwarded on the redirect) |
| `GET /v1/devices` | `/api/ring/devices` | Find the device id to assign to each fridge |
| `GET /v1/history/devices/{id}/events` | `RingClient.history` | Catch up after downtime |
| OAuth refresh at `oauth.ring.com` | `RingClient._ensure_token` | Long-running server |

**AWS:** Amazon Bedrock Runtime Converse API with image input (`stocked/vision.py`, model `us.amazon.nova-lite-v1:0`) reads the shelf crop and returns JSON: fill percent, food types, and issues from a fixed list (spill, door open, spoiled food, non-food items, needs cleaning). The prompt forbids describing people, and only the shelf crop is sent. Without AWS credentials, Stocked uses a pixel heuristic and labels it "Estimated without AI (fill level only)".

## Privacy by design

- The snapshot is cropped to the fridge interior (`shelf_box`, per fridge) **before** any analysis. The full frame is discarded.
- Stored: fill estimate, food types, issues, and a 240 px thumbnail of the shelves only. No faces, no counts of people, no visit log tied to anyone.
- Motion events are used only as "the fridge was just used, look now."

## Run it

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m stocked.app                                  # http://localhost:8095
pytest                                                 # 9 tests
```

No Ring camera or AWS account is needed for the demo. In the **Simulator** panel choose what a fridge's shelves look like (full / half / low / empty / spill), then click **Someone used it**. That sends the same signed webhook Ring would send; after the debounce the shelves are checked and the board updates live (Server-Sent Events). **Skip ahead 30 min** triggers the "empty too long" volunteer alert. You can also **Upload a photo** of a real fridge.

Tip for recording: `STOCKED_DEBOUNCE_S=3 python -m stocked.app` shortens the wait.

### With a real Ring camera

1. Copy `.env.example` to `.env`; add your Ring Partner API tokens and the webhook HMAC key from the Ring Developer Portal.
2. Set `STOCKED_FRIDGES` to your fridges with their Ring `device_id` (see `/api/ring/devices`) and, if needed, a `shelf_box`.
3. Deploy (the `Dockerfile` works on AWS App Runner or ECS) and register `https://<host>/webhooks/ring` as the webhook URL.
4. Optional: `STOCKED_BEDROCK=1` with AWS credentials and Nova Lite access enabled in your region.
5. Optional: ntfy.sh topic URLs in `STOCKED_VOLUNTEER_NOTIFY_URL` / `STOCKED_PUBLIC_NOTIFY_URL` for phone pushes.

## Project layout

```
stocked/ring.py      Ring Partner API: webhook verify/parse, REST client (devices, history, snapshot, OAuth)
stocked/vision.py    shelf crop, Bedrock analysis, no-AI heuristic
stocked/core.py      fridges, debounce, status, alerts, timers, persistence
stocked/app.py       FastAPI: webhook, board + console API, SSE, simulator
stocked/static/      the web page (public board, volunteer console, simulator)
demo_images/         illustrative drawings used by the simulator and tests
scripts/make_demo_images.py
tests/
```

## Limitations

- Demo images are drawings, not photos. The heuristic is calibrated on them; real fridges need the `shelf_box` set and, ideally, Bedrock.
- A glass-door fridge is assumed (most community fridges are). With a solid door the camera sees inside only while the door is open, so the debounce should be shortened to catch that moment.
- The live Ring API path is implemented to the published docs but was tested with the simulator, not a physical camera.

## License

MIT
