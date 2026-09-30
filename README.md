# Skynex FoodBridge (Innothon'26 prototype)

Time-aware food redistribution: **List -> Match -> Notify -> Deliver -> Verify -> Report**.

| Layer | Tech |
|---|---|
| Frontend | React PWA (Vite), Leaflet + OpenStreetMap |
| Backend | FastAPI (Python), WebSockets |
| Data | MongoDB with 2dsphere geo index |
| Real-time | Redis (claim lock + pub/sub) |
| Routing | OSRM (public demo server by default, self-host for real use) |
| Messaging | WhatsApp Cloud API (optional, hook in `push()`), add SMS/FCM there too |
| Verification | OpenCV face check (demo-grade) + OTP fallback |

## Run

```bash
docker compose up -d                     # MongoDB + Redis

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # then export the vars, or use a dotenv loader
uvicorn main:app --reload                # http://localhost:8000/docs

cd ../frontend
npm install
cp .env.example .env
npm run dev                              # http://localhost:5173
```

## Try the full flow (use 3 browser profiles or private windows)

1. Register an **NGO** (capacity 50, tap the map) and a **volunteer** near the same spot.
2. Register a **donor** and post food (allergen confirmation is mandatory).
3. NGO sees a countdown offer. Decline it, or wait, to watch auto-fallback (set `OFFER_WINDOW_S=30` for a quick demo).
4. NGO accepts and gets a 6-digit code. Volunteer takes the delivery, collects, shares location, sends the receiving photo, then confirms with the code.
5. Impact totals update on every screen.

## Honest limits (fix before a real pilot)

- **No real authentication.** The app trusts the `user_id` it is given. Add phone OTP login + JWT, and verify NGOs manually ("verified NGOs" in the deck).
- **Face matching is demo-grade** (Haar detect + pixel correlation). Swap in OpenCV `FaceRecognizerSF` or on-device face-api.js so biometrics stay on the phone, as the deck claims. Keep it strictly opt-in (DPDP Act 2023).
- Delivery proof photos are stored on local disk and never served publicly. Move to private object storage.
- Inbound WhatsApp/SMS replies (accepting by replying "YES") need a webhook endpoint, not included here.
- Impact kg/CO2e constants in `main.py` are placeholders. Cite a real factor before quoting numbers.
- Food safety and donor liability rules (FSSAI) are not encoded beyond the safe-until timer.
