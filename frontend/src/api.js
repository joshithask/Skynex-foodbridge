export const BASE = import.meta.env.VITE_API || "http://localhost:8000";
export const WS = BASE.replace(/^http/, "ws");

async function call(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(BASE + path, {
    headers: isForm ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    throw new Error(typeof e.detail === "string" ? e.detail : "Something went wrong. Try again.");
  }
  return res.json();
}
const post = (path, body) => call(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  register: (b) => post("/users", b),
  enrollFace: (id, file) => {
    const f = new FormData();
    f.append("photo", file);
    return call(`/users/${id}/face`, { method: "POST", body: f });
  },
  inbox: (id) => call(`/users/${id}/inbox`),
  createListing: (b) => post("/listings", b),
  rematch: (id, user_id) => post(`/listings/${id}/rematch`, { user_id }),
  accept: (id, user_id) => post(`/listings/${id}/accept`, { user_id }),
  decline: (id, user_id) => post(`/listings/${id}/decline`, { user_id }),
  assign: (id, user_id) => post(`/listings/${id}/assign`, { user_id }),
  pickup: (id, user_id) => post(`/listings/${id}/pickup`, { user_id }),
  track: (id, user_id, lat, lng) => post(`/listings/${id}/location`, { user_id, lat, lng }),
  deliver: (id, form) => call(`/listings/${id}/deliver`, { method: "POST", body: form }),
  verify: (id, form) => call(`/listings/${id}/verify`, { method: "POST", body: form }),
  impact: () => call("/impact"),
};
