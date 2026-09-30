"""
Skynex FoodBridge API  --  time-aware food redistribution (Innothon'26 prototype)

Flow:  List -> Match -> Notify -> Deliver -> Verify -> Report
Stack: FastAPI + MongoDB (2dsphere) + Redis (claim lock + pub/sub) + WebSockets + OpenCV
Run:   uvicorn main:app --reload
"""
import asyncio
import base64
import json
import math
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import cv2
import httpx
import numpy as np
import redis.asyncio as aioredis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, field_validator

# ----------------------------------------------------------------- config
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org")
OFFER_WINDOW_S = int(os.getenv("OFFER_WINDOW_S", "600"))   # time a recipient gets to respond
MAX_RADIUS_KM = float(os.getenv("MAX_RADIUS_KM", "15"))
MIN_SAFE_MINUTES = 20                                       # refuse listings that are already almost unsafe
DELIVERY_GEO_TOLERANCE_M = 300                              # proof photo must be taken near the recipient
FACE_THRESHOLD = 0.6                                        # demo-grade, see face_vec()
KG_PER_SERVING = 0.4                                        # ASSUMPTION for impact report
CO2E_PER_KG_FOOD = 2.5                                      # ASSUMPTION; replace with a cited UNEP/FAO figure
WA_TOKEN, WA_PHONE_ID = os.getenv("WA_TOKEN"), os.getenv("WA_PHONE_ID")

UPLOADS = Path(os.getenv("UPLOAD_DIR", "uploads"))
(UPLOADS / "food").mkdir(parents=True, exist_ok=True)       # served publicly (food photos only)
(UPLOADS / "proof").mkdir(parents=True, exist_ok=True)      # NOT served (contains people)

db = AsyncIOMotorClient(MONGO_URL, tz_aware=True)["foodbridge"]
r = aioredis.from_url(REDIS_URL, decode_responses=True)
FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


# ----------------------------------------------------------------- helpers
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def point(lng: float, lat: float) -> dict:
    return {"type": "Point", "coordinates": [lng, lat]}


def haversine_km(a: dict, b: dict) -> float:
    (lng1, lat1), (lng2, lat2) = a["coordinates"], b["coordinates"]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def norm_allergens(items: list[str]) -> list[str]:
    return sorted({a.strip().lower() for a in items if a.strip()})


def save_data_url(url: str, folder: str) -> str:
    try:
        _, b64 = url.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(422, "Photo is not a valid image data URL")
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Photo is larger than 2 MB")
    name = f"{new_id()}.jpg"
    (UPLOADS / folder / name).write_bytes(raw)
    return name


def face_vec(img_bytes: bytes) -> Optional[np.ndarray]:
    """Detect the largest face and return a normalised 64x64 template.

    DEMO-GRADE ONLY. For production use an embedding model (OpenCV FaceRecognizerSF /
    SFace, or face-api.js on the phone so biometrics never leave the device) and keep
    the OTP/QR fallback.
    """
    img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    faces = FACE_CASCADE.detectMultiScale(img, 1.1, 5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    crop = cv2.equalizeHist(cv2.resize(img[y:y + h, x:x + w], (64, 64)))
    v = crop.astype(np.float32).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n else None


# ----------------------------------------------------------------- realtime
sockets: dict[str, set[WebSocket]] = {}


async def publish(channel: str, data: dict):
    await r.publish("events", json.dumps({"channel": channel, "data": data}, default=str))


async def forwarder():
    """Redis pub/sub -> local websockets (lets you run several API workers)."""
    ps = r.pubsub()
    await ps.subscribe("events")
    async for m in ps.listen():
        if m["type"] != "message":
            continue
        try:
            ev = json.loads(m["data"])
        except ValueError:
            continue
        for ws in list(sockets.get(ev["channel"], ())):
            try:
                await ws.send_json(ev["data"])
            except Exception:
                sockets[ev["channel"]].discard(ws)


async def send_whatsapp(phone: str, text: str):
    # Free-form text only works inside WhatsApp's 24h window; outside it use an approved template.
    if not (WA_TOKEN and WA_PHONE_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {WA_TOKEN}"},
                json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}},
            )
    except httpx.HTTPError:
        pass


async def push(user_id: str, message: str):
    """In-app (websocket) + WhatsApp. Add SMS / FCM here."""
    await publish(f"user:{user_id}", {"type": "refresh", "message": message})
    u = await db.users.find_one({"_id": user_id}, {"phone": 1})
    if u and u.get("phone"):
        asyncio.create_task(send_whatsapp(u["phone"], message))


async def set_status(lid: str, status: str, event: str, **extra):
    now = utcnow()
    await db.listings.update_one(
        {"_id": lid},
        {"$set": {"status": status}, "$push": {"timeline": {"event": event, "at": now, **extra}}},
    )
    await publish(f"listing:{lid}", {"type": "status", "status": status})


# ----------------------------------------------------------------- models
class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: Literal["donor", "ngo", "volunteer"]
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    phone: Optional[str] = None
    capacity_servings: int = Field(0, ge=0)          # NGO: servings it can take per offer
    need_level: int = Field(1, ge=1, le=5)           # NGO: 5 = most in need
    avoid_allergens: list[str] = []                  # NGO: never offer me these
    language: str = "en"


class ListingIn(BaseModel):
    donor_id: str
    title: str = Field(min_length=2, max_length=80)
    servings: int = Field(ge=1, le=5000)
    allergens: list[str] = []
    allergens_declared: bool                         # mandatory declaration
    safe_until: datetime
    photo_data_url: Optional[str] = None

    @field_validator("safe_until")
    @classmethod
    def _aware(cls, v):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class ActorIn(BaseModel):
    user_id: str


class TrackIn(BaseModel):
    user_id: str
    lat: float
    lng: float


# ----------------------------------------------------------------- matching
async def rank_recipients(l: dict) -> list[dict]:
    """Geospatial search, then rank by distance, need, capacity and allergen fit."""
    ranked = []
    cursor = db.users.find({
        "role": "ngo",
        "location": {"$near": {"$geometry": l["location"], "$maxDistance": MAX_RADIUS_KM * 1000}},
    }).limit(50)
    async for u in cursor:
        if u.get("capacity_servings", 0) < l["servings"]:
            continue
        if set(u.get("avoid_allergens", [])) & set(l["allergens"]):
            continue
        d = haversine_km(l["location"], u["location"])
        ranked.append({"user_id": u["_id"], "distance_km": round(d, 2), "score": d - 0.5 * u.get("need_level", 1)})
    ranked.sort(key=lambda x: x["score"])
    return ranked


async def start_matching(lid: str):
    l = await db.listings.find_one({"_id": lid})
    ranked = await rank_recipients(l)
    await db.listings.update_one({"_id": lid}, {"$set": {"queue": ranked, "queue_index": -1, "status": "matching"}})
    await advance_offer(lid)


async def advance_offer(lid: str):
    """Offer to the next recipient in the queue (auto-fallback when nobody responds)."""
    l = await db.listings.find_one({"_id": lid})
    if not l or l["status"] not in ("matching", "offered"):
        return
    now = utcnow()
    left = (l["safe_until"] - now).total_seconds()
    nxt = l["queue_index"] + 1
    if left <= 0:
        return await set_status(lid, "expired", "expired")
    if nxt >= len(l["queue"]):
        await set_status(lid, "unmatched", "no_recipient")
        return await push(l["donor_id"], f"No recipient found for '{l['title']}' yet. You can retry matching.")
    window = min(OFFER_WINDOW_S, left * 0.5)         # never burn more than half the remaining time
    cand = l["queue"][nxt]
    res = await db.listings.update_one(
        {"_id": lid, "queue_index": l["queue_index"], "status": l["status"]},
        {"$set": {"status": "offered", "queue_index": nxt, "offer_started_at": now,
                  "offer_expires_at": now + timedelta(seconds=window)},
         "$push": {"timeline": {"event": "offered", "to": cand["user_id"], "at": now}}},
    )
    if res.modified_count:
        await publish(f"listing:{lid}", {"type": "status", "status": "offered"})
        await push(cand["user_id"],
                   f"Food available: {l['title']} ({l['servings']} servings, {cand['distance_km']} km). "
                   f"Reply within {int(window // 60)} min.")


async def expiry_worker():
    while True:
        try:
            now = utcnow()
            async for l in db.listings.find({"status": "offered", "offer_expires_at": {"$lte": now}}, {"_id": 1}):
                await advance_offer(l["_id"])
            async for l in db.listings.find(
                {"status": {"$in": ["matching", "offered", "claimed", "assigned", "unmatched"]},
                 "safe_until": {"$lte": now}}, {"_id": 1, "donor_id": 1, "title": 1}):
                await set_status(l["_id"], "expired", "expired")
                await push(l["donor_id"], f"'{l['title']}' passed its safe-until time.")
        except Exception as e:  # keep the worker alive
            print("expiry_worker:", e)
        await asyncio.sleep(2)


# ----------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.users.create_index([("location", "2dsphere")])
    await db.listings.create_index([("location", "2dsphere")])
    await db.listings.create_index([("status", 1), ("safe_until", 1)])
    tasks = [asyncio.create_task(forwarder()), asyncio.create_task(expiry_worker())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Skynex FoodBridge", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"], allow_headers=["*"],
)
app.mount("/photos", StaticFiles(directory=UPLOADS / "food"), name="photos")


async def get_user(uid: str, role: Optional[str] = None) -> dict:
    u = await db.users.find_one({"_id": uid})
    if not u:
        raise HTTPException(404, "User not found")
    if role and u["role"] != role:
        raise HTTPException(403, f"Only a {role} can do this")
    return u


async def get_listing(lid: str) -> dict:
    l = await db.listings.find_one({"_id": lid})
    if not l:
        raise HTTPException(404, "Listing not found")
    return l


def current_recipient(l: dict) -> Optional[str]:
    i = l.get("queue_index", -1)
    return l["queue"][i]["user_id"] if 0 <= i < len(l["queue"]) else None


async def enrich(l: dict, viewer_id: str) -> dict:
    out = dict(l)
    out["id"] = out.pop("_id")
    out["queue_len"] = len(out.pop("queue", []))
    out.pop("timeline", None)
    if out.get("proof"):
        out["proof"] = {k: v for k, v in out["proof"].items() if k != "file"}
    if viewer_id != l.get("recipient_id"):
        out.pop("otp", None)                          # only the recipient sees the handover code
    donor = await db.users.find_one({"_id": l["donor_id"]}, {"name": 1, "location": 1})
    out["donor"] = {"name": donor["name"], "location": donor["location"]} if donor else None
    if l.get("recipient_id"):
        rc = await db.users.find_one({"_id": l["recipient_id"]}, {"name": 1, "location": 1})
        out["recipient"] = {"name": rc["name"], "location": rc["location"]} if rc else None
    return out


# ----------------------------------------------------------------- users
@app.post("/users", status_code=201)
async def create_user(b: UserIn):
    doc = {"_id": new_id(), "name": b.name, "role": b.role, "phone": b.phone,
           "location": point(b.lng, b.lat), "capacity_servings": b.capacity_servings,
           "need_level": b.need_level, "avoid_allergens": norm_allergens(b.avoid_allergens),
           "language": b.language, "face_optin": False, "face_template": None, "created_at": utcnow()}
    await db.users.insert_one(doc)
    doc.pop("face_template")
    doc["id"] = doc.pop("_id")
    return doc


@app.post("/users/{uid}/face")
async def enroll_face(uid: str, photo: UploadFile = File(...)):
    """Opt-in enrolment. Wire deletion-on-request to your DPDP Act 2023 consent handling."""
    await get_user(uid)
    v = face_vec(await photo.read())
    if v is None:
        raise HTTPException(422, "No clear face found. Try better light and face the camera.")
    await db.users.update_one({"_id": uid}, {"$set": {"face_template": v.tolist(), "face_optin": True}})
    return {"face_optin": True}


@app.delete("/users/{uid}/face")
async def delete_face(uid: str):
    await db.users.update_one({"_id": uid}, {"$set": {"face_template": None, "face_optin": False}})
    return {"face_optin": False}


@app.get("/users/{uid}/inbox")
async def inbox(uid: str):
    u = await get_user(uid)
    items: dict[str, dict] = {}
    if u["role"] == "donor":
        async for l in db.listings.find({"donor_id": uid}).sort("created_at", -1).limit(50):
            items[l["_id"]] = l
    elif u["role"] == "ngo":
        async for l in db.listings.find({"status": "offered", "queue.user_id": uid}):
            if current_recipient(l) == uid:
                items[l["_id"]] = l
        async for l in db.listings.find({"recipient_id": uid}).sort("created_at", -1).limit(30):
            items[l["_id"]] = l
    else:
        async for l in db.listings.find({"status": "claimed", "location": {
                "$near": {"$geometry": u["location"], "$maxDistance": MAX_RADIUS_KM * 1000}}}):
            items[l["_id"]] = l
        async for l in db.listings.find({"volunteer_id": uid}).sort("created_at", -1).limit(30):
            items[l["_id"]] = l
    out = [await enrich(l, uid) for l in items.values()]
    out.sort(key=lambda x: x["safe_until"])            # most urgent first
    return out


# ----------------------------------------------------------------- listings
@app.post("/listings", status_code=201)
async def create_listing(b: ListingIn):
    donor = await get_user(b.donor_id, "donor")
    if not b.allergens_declared:
        raise HTTPException(422, "Declare allergens (or confirm none) before posting")
    if b.safe_until < utcnow() + timedelta(minutes=MIN_SAFE_MINUTES):
        raise HTTPException(422, f"Safe-until must be at least {MIN_SAFE_MINUTES} minutes from now")
    now = utcnow()
    doc = {"_id": new_id(), "donor_id": b.donor_id, "title": b.title, "servings": b.servings,
           "allergens": norm_allergens(b.allergens), "safe_until": b.safe_until,
           "photo": save_data_url(b.photo_data_url, "food") if b.photo_data_url else None,
           "location": donor["location"], "status": "matching", "queue": [], "queue_index": -1,
           "recipient_id": None, "volunteer_id": None, "otp": None, "track": None, "proof": None,
           "created_at": now, "timeline": [{"event": "listed", "at": now}]}
    await db.listings.insert_one(doc)
    await start_matching(doc["_id"])
    return await enrich(await get_listing(doc["_id"]), b.donor_id)


@app.post("/listings/{lid}/rematch")
async def rematch(lid: str, b: ActorIn):
    l = await get_listing(lid)
    if l["donor_id"] != b.user_id or l["status"] != "unmatched":
        raise HTTPException(409, "Only the donor can retry, and only when nobody was found")
    await start_matching(lid)
    return {"ok": True}


@app.post("/listings/{lid}/accept")
async def accept(lid: str, b: ActorIn):
    await get_user(b.user_id, "ngo")
    if not await r.set(f"lock:{lid}", b.user_id, nx=True, ex=120):   # first to accept wins
        raise HTTPException(409, "Someone else just claimed this")
    l = await get_listing(lid)
    if l["status"] != "offered" or current_recipient(l) != b.user_id:
        await r.delete(f"lock:{lid}")
        raise HTTPException(409, "This offer is no longer available to you")
    otp = f"{secrets.randbelow(10**6):06d}"
    res = await db.listings.update_one(
        {"_id": lid, "status": "offered", "queue_index": l["queue_index"]},
        {"$set": {"status": "claimed", "recipient_id": b.user_id, "otp": otp}})
    if not res.modified_count:
        await r.delete(f"lock:{lid}")
        raise HTTPException(409, "Offer expired")
    await set_status(lid, "claimed", "claimed", by=b.user_id)
    await push(l["donor_id"], f"'{l['title']}' was claimed. Finding a volunteer.")
    async for v in db.users.find({"role": "volunteer", "location": {
            "$near": {"$geometry": l["location"], "$maxDistance": MAX_RADIUS_KM * 1000}}}).limit(20):
        await push(v["_id"], f"Pickup needed: {l['title']} ({l['servings']} servings). Open the app to take it.")
    return {"ok": True, "otp": otp}


@app.post("/listings/{lid}/decline")
async def decline(lid: str, b: ActorIn):
    l = await get_listing(lid)
    if l["status"] != "offered" or current_recipient(l) != b.user_id:
        raise HTTPException(409, "Nothing to decline")
    await db.listings.update_one({"_id": lid}, {"$push": {"timeline": {"event": "declined", "by": b.user_id, "at": utcnow()}}})
    await advance_offer(lid)
    return {"ok": True}


@app.post("/listings/{lid}/assign")
async def assign(lid: str, b: ActorIn):
    await get_user(b.user_id, "volunteer")
    res = await db.listings.update_one({"_id": lid, "status": "claimed"},
                                       {"$set": {"status": "assigned", "volunteer_id": b.user_id}})
    if not res.modified_count:
        raise HTTPException(409, "Already taken")
    l = await get_listing(lid)
    await set_status(lid, "assigned", "volunteer_assigned", by=b.user_id)
    await push(l["donor_id"], f"A volunteer is on the way to pick up '{l['title']}'.")
    await push(l["recipient_id"], f"A volunteer will bring '{l['title']}'. Keep your code ready: {l['otp']}")
    return {"ok": True}


@app.post("/listings/{lid}/pickup")
async def pickup(lid: str, b: ActorIn):
    res = await db.listings.update_one({"_id": lid, "status": "assigned", "volunteer_id": b.user_id},
                                       {"$set": {"status": "in_transit"}})
    if not res.modified_count:
        raise HTTPException(409, "Not your pickup, or already collected")
    await set_status(lid, "in_transit", "picked_up")
    return {"ok": True}


async def eta_minutes(frm: dict, to: dict) -> float:
    (a, b), (c, d) = frm["coordinates"], to["coordinates"]
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            j = (await cl.get(f"{OSRM_URL}/route/v1/driving/{a},{b};{c},{d}?overview=false")).json()
        return round(j["routes"][0]["duration"] / 60, 1)
    except Exception:
        return round(haversine_km(frm, to) / 20 * 60, 1)   # fallback: 20 km/h city average


@app.post("/listings/{lid}/location")
async def track(lid: str, b: TrackIn):
    l = await get_listing(lid)
    if l.get("volunteer_id") != b.user_id or l["status"] not in ("assigned", "in_transit"):
        raise HTTPException(409, "Not tracking this run")
    here = point(b.lng, b.lat)
    if l["status"] == "assigned":
        target = l["location"]
    else:
        target = (await db.users.find_one({"_id": l["recipient_id"]}))["location"]
    t = {"location": here, "eta_min": await eta_minutes(here, target), "at": utcnow()}
    await db.listings.update_one({"_id": lid}, {"$set": {"track": t}})
    await publish(f"listing:{lid}", {"type": "track", "track": t})
    return t


@app.post("/listings/{lid}/deliver")
async def deliver(lid: str, user_id: str = Form(...), lat: float = Form(...), lng: float = Form(...),
                  photo: UploadFile = File(...)):
    l = await get_listing(lid)
    if l.get("volunteer_id") != user_id or l["status"] != "in_transit":
        raise HTTPException(409, "Not your delivery, or not in transit")
    raw = await photo.read()
    if len(raw) > 5_000_000:
        raise HTTPException(413, "Photo too large")
    name = f"{new_id()}.jpg"
    (UPLOADS / "proof" / name).write_bytes(raw)
    rc = await db.users.find_one({"_id": l["recipient_id"]})
    dist_m = round(haversine_km(point(lng, lat), rc["location"]) * 1000)
    proof = {"file": name, "location": point(lng, lat), "at": utcnow(),
             "distance_to_recipient_m": dist_m, "geo_ok": dist_m <= DELIVERY_GEO_TOLERANCE_M}
    await db.listings.update_one({"_id": lid}, {"$set": {"proof": proof}})
    await set_status(lid, "delivered", "proof_photo", geo_ok=proof["geo_ok"])
    return {"ok": True, "geo_ok": proof["geo_ok"], "distance_m": dist_m}


@app.post("/listings/{lid}/verify")
async def verify(lid: str, user_id: str = Form(...), otp: Optional[str] = Form(None),
                 face: Optional[UploadFile] = File(None)):
    l = await get_listing(lid)
    if l.get("volunteer_id") != user_id or l["status"] != "delivered":
        raise HTTPException(409, "Nothing to verify")
    method = None
    if otp and secrets.compare_digest(otp.strip(), l["otp"]):
        method = "otp"
    elif face is not None:
        rc = await db.users.find_one({"_id": l["recipient_id"]})
        if not rc.get("face_optin") or not rc.get("face_template"):
            raise HTTPException(422, "Recipient has not opted in to face verification. Use the code.")
        v = face_vec(await face.read())
        if v is not None and float(np.dot(v, np.array(rc["face_template"], dtype=np.float32))) >= FACE_THRESHOLD:
            method = "face"
    if not method:
        raise HTTPException(400, "Could not verify. Ask for the 6-digit code.")
    await db.listings.update_one({"_id": lid}, {"$set": {"verified_by": method}})
    await set_status(lid, "completed", "verified", method=method)
    await push(l["donor_id"], f"'{l['title']}' reached {l['servings']} people. Thank you.")
    return {"ok": True, "method": method}


# ----------------------------------------------------------------- report
@app.get("/impact")
async def impact():
    done = await db.listings.aggregate([
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "meals": {"$sum": "$servings"}, "deliveries": {"$sum": 1}}}]).to_list(1)
    lost = await db.listings.aggregate([
        {"$match": {"status": {"$in": ["expired", "unmatched"]}}},
        {"$group": {"_id": None, "meals": {"$sum": "$servings"}}}]).to_list(1)
    meals = done[0]["meals"] if done else 0
    kg = meals * KG_PER_SERVING
    return {"meals_saved": meals, "deliveries": done[0]["deliveries"] if done else 0,
            "meals_lost_to_expiry": lost[0]["meals"] if lost else 0,
            "est_food_kg": round(kg, 1), "est_co2e_kg": round(kg * CO2E_PER_KG_FOOD, 1),
            "note": "kg and CO2e use configurable assumptions; cite your own factors before publishing"}


@app.get("/impact/audit")
async def audit(limit: int = 100):
    """Audit trail for CSR reporting (proof photos are never exposed here)."""
    rows = []
    async for l in db.listings.find({"status": "completed"}).sort("created_at", -1).limit(limit):
        rows.append({"id": l["_id"], "title": l["title"], "servings": l["servings"],
                     "verified_by": l.get("verified_by"), "geo_ok": (l.get("proof") or {}).get("geo_ok"),
                     "timeline": l["timeline"]})
    return rows


# ----------------------------------------------------------------- websocket
@app.websocket("/ws/{channel:path}")
async def ws_endpoint(ws: WebSocket, channel: str):
    if not channel.startswith(("user:", "listing:")):
        return await ws.close(code=1008)
    await ws.accept()
    sockets.setdefault(channel, set()).add(ws)
    try:
        while True:
            await ws.receive_text()            # keep-alive pings from the client
    except WebSocketDisconnect:
        sockets[channel].discard(ws)
