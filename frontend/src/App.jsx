import { useCallback, useEffect, useRef, useState } from "react";
import { MapContainer, TileLayer, Marker, useMapEvents } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { api, BASE, WS } from "./api";
import "./styles.css";

const ALLERGENS = ["peanuts", "tree nuts", "milk", "egg", "gluten", "soy", "fish", "shellfish", "sesame"];
const FALLBACK_CENTER = [20.59, 78.96];
const STATUS = {
  matching: "Finding a recipient",
  offered: "Waiting for a reply",
  claimed: "Claimed. Finding a volunteer",
  assigned: "Volunteer heading to pickup",
  in_transit: "On the way to the recipient",
  delivered: "Delivered. Handover check pending",
  completed: "Handed over",
  expired: "Expired",
  unmatched: "No recipient found",
};

const pin = (color) =>
  L.divIcon({ className: "", html: `<span class="pin" style="background:${color}"></span>`, iconSize: [16, 16], iconAnchor: [8, 8] });
const ll = (p) => [p.coordinates[1], p.coordinates[0]];
const getPos = () =>
  new Promise((ok, no) =>
    navigator.geolocation ? navigator.geolocation.getCurrentPosition(ok, () => no(new Error("Allow location access to continue.")), { enableHighAccuracy: true }) : no(new Error("This device has no location support."))
  );

/* ------------------------------------------------------------ hooks */
function useSession() {
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem("fb_user")); } catch { return null; }
  });
  const save = (u) => {
    if (u) localStorage.setItem("fb_user", JSON.stringify(u)); else localStorage.removeItem("fb_user");
    setUser(u);
  };
  return [user, save];
}

function useLive(channel, onMsg) {
  const cb = useRef(onMsg);
  cb.current = onMsg;
  useEffect(() => {
    if (!channel) return;
    let ws, stop = false, timer;
    const open = () => {
      ws = new WebSocket(`${WS}/ws/${channel}`);
      ws.onmessage = (e) => cb.current(JSON.parse(e.data));
      ws.onopen = () => { ws.ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000); };
      ws.onclose = () => { clearInterval(ws.ping); if (!stop) timer = setTimeout(open, 2000); };
    };
    open();
    return () => { stop = true; clearTimeout(timer); ws && ws.close(); };
  }, [channel]);
}

function useInbox(uid) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");
  const load = useCallback(() => api.inbox(uid).then((x) => { setItems(x); setError(""); }).catch((e) => setError(e.message)), [uid]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  useLive(`user:${uid}`, load);
  return { items, load, error };
}

/* ------------------------------------------------------------ shared bits */
function Countdown({ until, from, label = "until unsafe" }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const end = new Date(until).getTime();
  const total = Math.max(1, end - new Date(from).getTime());
  const left = Math.max(0, end - now);
  const mins = Math.floor(left / 60000);
  const secs = Math.floor(left / 1000) % 60;
  const tone = left < 30 * 60000 ? "hot" : left < 90 * 60000 ? "warm" : "ok";
  const text = mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}:${String(secs).padStart(2, "0")}`;
  return (
    <div className={`cd ${tone}`} role="timer">
      <div className="cd-bar"><i style={{ width: `${(left / total) * 100}%` }} /></div>
      <b>{text}</b><span>{label}</span>
    </div>
  );
}

function ListingCard({ l, children }) {
  return (
    <article className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3>{l.title}</h3>
        <span className="status">{STATUS[l.status] || l.status}</span>
      </div>
      <div className="muted">{l.servings} servings{l.donor ? ` from ${l.donor.name}` : ""}</div>
      {l.photo && <img src={`${BASE}/photos/${l.photo}`} alt="" style={{ width: "100%", borderRadius: 10, margin: ".6rem 0", maxHeight: 180, objectFit: "cover" }} />}
      <div className="chips" style={{ margin: ".5rem 0" }}>
        {l.allergens.length ? l.allergens.map((a) => <span key={a} className="tag">contains {a}</span>) : <span className="tag safe">no listed allergens</span>}
      </div>
      {!["completed", "expired"].includes(l.status) && <Countdown until={l.safe_until} from={l.created_at} />}
      {children}
    </article>
  );
}

function TrackMap({ l }) {
  const [track, setTrack] = useState(l.track);
  useLive(`listing:${l.id}`, (m) => m.type === "track" && setTrack(m.track));
  if (!l.donor) return null;
  const centre = track ? ll(track.location) : ll(l.donor.location);
  return (
    <>
      <div className="muted" style={{ marginTop: ".5rem" }}>
        {track ? `Volunteer is about ${track.eta_min} min away` : "Waiting for the volunteer's first location"}
      </div>
      <MapContainer className="map" center={centre} zoom={13} key={l.id}>
        <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" attribution="&copy; OpenStreetMap contributors" />
        <Marker position={ll(l.donor.location)} icon={pin("#ffb000")} />
        {l.recipient && <Marker position={ll(l.recipient.location)} icon={pin("#1f7a5c")} />}
        {track && <Marker position={ll(track.location)} icon={pin("#10241f")} />}
      </MapContainer>
    </>
  );
}

function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async (fn) => {
    setBusy(true); setError("");
    try { return await fn(); } catch (e) { setError(e.message); } finally { setBusy(false); }
  };
  return { busy, error, run };
}

/* ------------------------------------------------------------ onboarding */
function LocationPicker({ value, onChange }) {
  useMapEvents({ click: (e) => onChange([e.latlng.lat, e.latlng.lng]) });
  return value ? <Marker position={value} icon={pin("#1f7a5c")} /> : null;
}

function Onboard({ onDone }) {
  const [role, setRole] = useState("donor");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [capacity, setCapacity] = useState(50);
  const [need, setNeed] = useState(3);
  const [avoid, setAvoid] = useState([]);
  const [loc, setLoc] = useState(null);
  const [centre, setCentre] = useState(FALLBACK_CENTER);
  const { busy, error, run } = useAction();

  useEffect(() => {
    navigator.geolocation?.getCurrentPosition((p) => { const c = [p.coords.latitude, p.coords.longitude]; setCentre(c); setLoc(c); });
  }, []);

  const toggle = (a) => setAvoid((x) => (x.includes(a) ? x.filter((y) => y !== a) : [...x, a]));
  const submit = () => run(async () => {
    if (!loc) throw new Error("Tap the map to set your location.");
    const u = await api.register({
      name, role, phone: phone || null, lat: loc[0], lng: loc[1],
      capacity_servings: role === "ngo" ? +capacity : 0, need_level: +need, avoid_allergens: role === "ngo" ? avoid : [],
    });
    onDone(u);
  });

  return (
    <div className="shell">
      <h1>FoodBridge</h1>
      <p className="muted">Surplus food, matched to the nearest need and delivered before it spoils.</p>
      <div className="roles" role="group" aria-label="I am a">
        {[["donor", "I have food"], ["ngo", "We receive food"], ["volunteer", "I can deliver"]].map(([r, t]) => (
          <button key={r} aria-pressed={role === r} onClick={() => setRole(r)}>{t}</button>
        ))}
      </div>
      <div className="panel" style={{ marginTop: ".9rem" }}>
        <label htmlFor="n">{role === "ngo" ? "Organisation name" : "Name"}</label>
        <input id="n" type="text" value={name} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="p">WhatsApp number (optional, with country code)</label>
        <input id="p" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
        {role === "ngo" && (
          <>
            <label htmlFor="c">Servings you can take at once</label>
            <input id="c" type="number" min="1" value={capacity} onChange={(e) => setCapacity(e.target.value)} />
            <label htmlFor="nd">How urgent is your need? (1 low, 5 high)</label>
            <input id="nd" type="number" min="1" max="5" value={need} onChange={(e) => setNeed(e.target.value)} />
            <label>Never offer us food containing</label>
            <div className="chips">{ALLERGENS.map((a) => <button key={a} className="chip" aria-pressed={avoid.includes(a)} onClick={() => toggle(a)}>{a}</button>)}</div>
          </>
        )}
        <label>Your location (tap the map)</label>
        <MapContainer className="map" center={centre} zoom={loc ? 13 : 5} key={centre.join()}>
          <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" attribution="&copy; OpenStreetMap contributors" />
          <LocationPicker value={loc} onChange={setLoc} />
        </MapContainer>
        {error && <p className="err">{error}</p>}
        <button className="btn go" style={{ marginTop: ".8rem" }} disabled={busy || !name.trim()} onClick={submit}>Continue</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ donor */
function isoLocalPlus(hours) {
  const d = new Date(Date.now() + hours * 3600000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

function DonorView({ user }) {
  const { items, load, error } = useInbox(user.id);
  const [title, setTitle] = useState("");
  const [servings, setServings] = useState(20);
  const [allergens, setAllergens] = useState([]);
  const [declared, setDeclared] = useState(false);
  const [safeUntil, setSafeUntil] = useState(isoLocalPlus(3));
  const [photo, setPhoto] = useState(null);
  const { busy, error: formErr, run } = useAction();

  const toggle = (a) => setAllergens((x) => (x.includes(a) ? x.filter((y) => y !== a) : [...x, a]));
  const readPhoto = (f) => {
    if (!f) return setPhoto(null);
    const rd = new FileReader();
    rd.onload = () => setPhoto(rd.result);
    rd.readAsDataURL(f);
  };
  const post = () => run(async () => {
    await api.createListing({
      donor_id: user.id, title, servings: +servings, allergens, allergens_declared: declared,
      safe_until: new Date(safeUntil).toISOString(), photo_data_url: photo,
    });
    setTitle(""); setAllergens([]); setDeclared(false); setPhoto(null);
    load();
  });

  return (
    <>
      <h2>Post surplus food</h2>
      <div className="panel">
        <label htmlFor="t">What is it?</label>
        <input id="t" type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Veg biryani, sealed trays" />
        <div className="row">
          <div className="grow">
            <label htmlFor="s">How many people it feeds</label>
            <input id="s" type="number" min="1" value={servings} onChange={(e) => setServings(e.target.value)} />
          </div>
          <div className="grow">
            <label htmlFor="u">Safe to eat until</label>
            <input id="u" type="datetime-local" value={safeUntil} onChange={(e) => setSafeUntil(e.target.value)} />
          </div>
        </div>
        <label>Contains</label>
        <div className="chips">{ALLERGENS.map((a) => <button key={a} className="chip" aria-pressed={allergens.includes(a)} onClick={() => toggle(a)}>{a}</button>)}</div>
        <label htmlFor="ph">Photo</label>
        <input id="ph" type="file" accept="image/*" capture="environment" onChange={(e) => readPhoto(e.target.files[0])} />
        <label style={{ display: "flex", gap: ".5rem", alignItems: "center" }}>
          <input type="checkbox" checked={declared} onChange={(e) => setDeclared(e.target.checked)} />
          I have checked the allergens above (or none apply)
        </label>
        {formErr && <p className="err">{formErr}</p>}
        <button className="btn go" disabled={busy || !declared || title.trim().length < 2} onClick={post}>Post food</button>
      </div>

      <h2>Your listings</h2>
      {error && <p className="err">{error}</p>}
      {!items.length && <p className="muted">Nothing posted yet. Post your first surplus above.</p>}
      {items.map((l) => (
        <ListingCard key={l.id} l={l}>
          {l.status === "unmatched" && <button className="btn" onClick={() => api.rematch(l.id, user.id).then(load)}>Try matching again</button>}
          {["assigned", "in_transit"].includes(l.status) && <TrackMap l={l} />}
        </ListingCard>
      ))}
    </>
  );
}

/* ------------------------------------------------------------ NGO */
function NgoView({ user }) {
  const { items, load, error } = useInbox(user.id);
  const { busy, error: actErr, run } = useAction();
  const offers = items.filter((l) => l.status === "offered");
  const mine = items.filter((l) => l.status !== "offered");

  return (
    <>
      <h2>Offers for you</h2>
      {error && <p className="err">{error}</p>}
      {actErr && <p className="err">{actErr}</p>}
      {!offers.length && <p className="muted">No open offers. You will be alerted here and on WhatsApp.</p>}
      {offers.map((l) => (
        <ListingCard key={l.id} l={l}>
          <Countdown until={l.offer_expires_at} from={l.offer_started_at} label="to reply before it goes to the next NGO" />
          <div className="row">
            <button className="btn go" disabled={busy} onClick={() => run(async () => { await api.accept(l.id, user.id); load(); })}>Accept</button>
            <button className="btn ghost" disabled={busy} onClick={() => run(async () => { await api.decline(l.id, user.id); load(); })}>Decline</button>
          </div>
        </ListingCard>
      ))}

      <h2>Accepted</h2>
      {mine.map((l) => (
        <ListingCard key={l.id} l={l}>
          {l.otp && !["completed", "expired"].includes(l.status) && (
            <>
              <div className="muted">Give this code to the volunteer at handover</div>
              <div className="otp">{l.otp}</div>
            </>
          )}
          {["assigned", "in_transit"].includes(l.status) && <TrackMap l={l} />}
        </ListingCard>
      ))}
      <FaceOptIn user={user} />
    </>
  );
}

function FaceOptIn({ user }) {
  const [state, setState] = useState("");
  const { busy, error, run } = useAction();
  return (
    <>
      <h2>Face check at handover (optional)</h2>
      <div className="panel">
        <p className="muted" style={{ marginTop: 0 }}>Your code always works. Add a photo only if you want the volunteer to confirm you by face. You can remove it any time.</p>
        <input type="file" accept="image/*" capture="user" aria-label="Face photo"
          onChange={(e) => e.target.files[0] && run(async () => { await api.enrollFace(user.id, e.target.files[0]); setState("Face check is on."); })} />
        {state && <p className="status">{state}</p>}
        {error && <p className="err">{error}</p>}
        <button className="linkbtn" disabled={busy} onClick={() => fetch(`${BASE}/users/${user.id}/face`, { method: "DELETE" }).then(() => setState("Face data removed."))}>Remove my face data</button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------ volunteer */
function useShareLocation(l, uid) {
  const last = useRef(0);
  const active = ["assigned", "in_transit"].includes(l.status);
  useEffect(() => {
    if (!active || !navigator.geolocation) return;
    const id = navigator.geolocation.watchPosition((p) => {
      if (Date.now() - last.current < 8000) return;
      last.current = Date.now();
      api.track(l.id, uid, p.coords.latitude, p.coords.longitude).catch(() => {});
    }, () => {}, { enableHighAccuracy: true });
    return () => navigator.geolocation.clearWatch(id);
  }, [active, l.id, uid]);
}

function Run({ l, uid, reload }) {
  useShareLocation(l, uid);
  const { busy, error, run } = useAction();
  const [photo, setPhoto] = useState(null);
  const [otp, setOtp] = useState("");
  const [face, setFace] = useState(null);

  const sendProof = () => run(async () => {
    const pos = await getPos();
    const f = new FormData();
    f.append("user_id", uid); f.append("lat", pos.coords.latitude); f.append("lng", pos.coords.longitude); f.append("photo", photo);
    await api.deliver(l.id, f);
    reload();
  });
  const verify = () => run(async () => {
    const f = new FormData();
    f.append("user_id", uid);
    if (otp) f.append("otp", otp);
    if (face) f.append("face", face);
    await api.verify(l.id, f);
    reload();
  });

  return (
    <>
      {error && <p className="err">{error}</p>}
      {l.status === "claimed" && <button className="btn go" disabled={busy} onClick={() => run(async () => { await api.assign(l.id, uid); reload(); })}>Take this delivery</button>}
      {l.status === "assigned" && (
        <>
          <p className="muted">Pick up from {l.donor?.name}. Your location is shared while you travel.</p>
          <button className="btn go" disabled={busy} onClick={() => run(async () => { await api.pickup(l.id, uid); reload(); })}>I have collected it</button>
          <TrackMap l={l} />
        </>
      )}
      {l.status === "in_transit" && (
        <>
          <p className="muted">Deliver to {l.recipient?.name}. Take the receiving photo at the door.</p>
          <input type="file" accept="image/*" capture="environment" aria-label="Receiving photo" onChange={(e) => setPhoto(e.target.files[0])} />
          <button className="btn go" style={{ marginTop: ".5rem" }} disabled={busy || !photo} onClick={sendProof}>Send delivery photo</button>
          <TrackMap l={l} />
        </>
      )}
      {l.status === "delivered" && (
        <>
          <p className="muted">Confirm the recipient with their 6-digit code, or a face check if they opted in.</p>
          <label htmlFor={`o${l.id}`}>Recipient's code</label>
          <input id={`o${l.id}`} type="text" inputMode="numeric" maxLength={6} value={otp} onChange={(e) => setOtp(e.target.value)} />
          <label htmlFor={`f${l.id}`}>Or face photo</label>
          <input id={`f${l.id}`} type="file" accept="image/*" capture="user" onChange={(e) => setFace(e.target.files[0])} />
          <button className="btn go" style={{ marginTop: ".6rem" }} disabled={busy || (!otp && !face)} onClick={verify}>Confirm handover</button>
        </>
      )}
    </>
  );
}

function VolunteerView({ user }) {
  const { items, load, error } = useInbox(user.id);
  const open = items.filter((l) => l.status === "claimed");
  const mine = items.filter((l) => l.status !== "claimed");
  return (
    <>
      <h2>Pickups near you</h2>
      {error && <p className="err">{error}</p>}
      {!open.length && <p className="muted">No pickups waiting. New ones appear here and on WhatsApp.</p>}
      {open.map((l) => <ListingCard key={l.id} l={l}><Run l={l} uid={user.id} reload={load} /></ListingCard>)}
      <h2>Your deliveries</h2>
      {mine.map((l) => <ListingCard key={l.id} l={l}><Run l={l} uid={user.id} reload={load} /></ListingCard>)}
    </>
  );
}

/* ------------------------------------------------------------ impact + root */
function Impact() {
  const [d, setD] = useState(null);
  useEffect(() => { api.impact().then(setD).catch(() => {}); }, []);
  if (!d) return null;
  return (
    <div className="stats">
      <div><b>{d.meals_saved}</b>meals delivered</div>
      <div><b>{d.meals_lost_to_expiry}</b>meals expired</div>
      <div><b>{d.est_food_kg} kg</b>food rescued (est.)</div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useSession();
  if (!user) return <Onboard onDone={setUser} />;
  const View = { donor: DonorView, ngo: NgoView, volunteer: VolunteerView }[user.role];
  return (
    <div className="shell">
      <div className="top">
        <div><h1>FoodBridge</h1><small>{user.name}, {user.role === "ngo" ? "receiving" : user.role}</small></div>
        <button className="linkbtn" onClick={() => setUser(null)}>Switch account</button>
      </div>
      <Impact />
      <View user={user} />
    </div>
  );
}
