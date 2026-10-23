# Stocked: Devpost submission text

## Tracks

- **Ring** (primary): Ring Partner API webhooks + snapshots.
- **AWS Builder** (mini challenge): Amazon Bedrock vision on the Ring snapshot.

## Elevator pitch

Live "is there food in it?" status for community fridges, from the Ring camera already watching them.

## Project description

**Inspiration.** Community fridges feed neighbors with no staff and no inventory. When SNAP benefits were disrupted in November 2025, fridges [emptied faster than volunteers could refill them](https://pix11.com/news/local-news/community-fridges-see-surge-in-use-as-snap-benefits-face-cuts/amp/). People walk over not knowing whether there is anything inside, and volunteers learn a fridge is bare only when someone posts about it. A [WPI team](https://digital.wpi.edu/concern/student_works/td96k588h) found that tracking fridge use is hard without spoiling the take-what-you-need model.

**What it does.** When a Ring camera sees someone use the fridge, Stocked waits for them to leave, takes one snapshot, crops it to the shelves, and estimates how full the fridge is. A public board shows every fridge in the network (well stocked / about half / running low / empty, with when it was last checked). Volunteers get an alert when a fridge has been empty for 30 minutes or has a spill; neighbors who follow a fridge get "just restocked."

**Privacy.** The snapshot is cropped to the fridge interior before analysis, so people are never analyzed or stored. Only the fill estimate and a small shelf thumbnail are kept.

## How we built it

Python, FastAPI, Server-Sent Events, Pillow. Ring webhooks are verified with HMAC-SHA256 on the raw body, deduplicated by `request_id`, and answered immediately; the shelf check runs after a debounce. A simulator sends the exact signed webhook a Ring camera would, so the whole pipeline can be judged without hardware. 9 automated tests.

## AWS Builder: services and how

- **Amazon Bedrock Runtime, Converse API with image content** (`stocked/vision.py`, `us.amazon.nova-lite-v1:0`): returns fill percent, food types and issues as JSON from the shelf crop only. The prompt forbids describing people, and unknown issue labels are discarded.
- **Container** (`Dockerfile`) for AWS App Runner or ECS, which give the public HTTPS endpoint Ring webhooks need.

## Product feedback

**Ring Partner API.** *Used for* webhooks, device discovery, snapshots, Event History and OAuth. *Worked well:* webhook HMAC signing is simple and clearly documented. *Needs work:* the snapshot request body isn't in the main API reference (we found `latest_in_range` / `start_timestamp` in a community thread); motion sub-type values aren't enumerated; the sample payload uses `data.subType` while other material uses `attributes.sub_type`. *Onboarding:* the Developer Playground's 30-minute tokens and lack of webhook delivery mean event-driven apps need their own simulator. *Would build again:* yes. Ring cameras already watch many shared spaces; a "scene state" use case like this one fits the platform well.

**Amazon Bedrock.** *Used for* image understanding with Nova Lite. *Worked well:* one Converse API shape for text and images. *Needs work:* model access must be enabled per region before the first call, which is easy to miss.

## Friction log

| Task | Steps | Expected | Actual | Severity | Workaround | Suggestion |
|---|---|---|---|---|---|---|
| Get a snapshot after a motion event | Read the Image Snapshots entry in the API reference | Request body schema and response | Only "pass a `components` array"; no body fields or redirect behavior | High | Found `type: latest_in_range` + `start_timestamp` in a forum thread, the 303 in a GitHub issue | Full request/response example including the 303 and `403 TIME_RANGE_NOT_AUTHORIZED` |
| Tell "a person used the fridge" from other motion | Searched for motion sub-types | List of `sub_type` values | Only `human` as an example | Medium | Treat any motion as a trigger, debounce | Enumerate sub-types |
| Parse the webhook | Followed the sample payload | One field name | `data.subType` vs `attributes.sub_type` | Medium | Accept both | Use one spelling |
| Test webhooks end to end | Used the Developer Playground | Test events reach my URL | Playground doesn't deliver webhooks | Medium | Wrote a signed-webhook simulator | "Send test webhook" button in the portal |
| Snapshot of the moment after motion ends | Looked for a "motion ended" event | An end event or clip-ready event | Not documented | Medium | Debounce: wait N seconds after the last motion | Document a motion-ended / clip-ready event |
| Read the docs with AI tools | Fetched the single-page reference | Whole reference | ~120k characters, truncated before Media and Event History | Low | Read sections from other sources | Per-section pages |

## Feature requests

- **Webhook test sender** in the Developer Portal: critical.
- **Motion-ended / clip-ready event**, so apps can grab the frame after a visit without guessing: important.
- **Snapshot of a region of interest** (server-side crop), so privacy-preserving apps never receive the full frame: nice to have.

## Demo video script (under 3 minutes)

1. **0:00–0:20, the problem.** Community fridge photo/headline. "Is there food in it? Nobody knows until they walk over."
2. **0:20–0:45, the board.** Three fridges, each with a live fill gauge and when it was last checked.
3. **0:45–1:25, a visit.** Simulator: set Library Corner to *empty*, click *Someone used it*. The signed Ring webhook arrives, the countdown runs, the card turns red: Empty. Point at the thumbnail: shelves only, the passer-by in the frame was cropped out.
4. **1:25–1:50, volunteers.** *Skip ahead 30 min* → "Library Corner Fridge has been empty for 35 min. Can anyone restock it?" Show the ntfy push on a phone if configured.
5. **1:50–2:15, restock.** Set it to *full*, *Someone used it* → "just restocked" alert to followers; the empty alert resolves itself.
6. **2:15–2:45, under the hood.** Spill image with Bedrock on (or describe it): food types + "spill" issue → volunteer alert. Terminal: `pytest` 9 passed; a bad signature gets 401.
7. **2:45–3:00, close.** "Stocked: the camera already there, now telling the neighborhood when there's food."
