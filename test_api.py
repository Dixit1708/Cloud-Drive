"""Automated end-to-end test of the API.  Run:  python test_api.py"""
import io, os, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp(); os.environ["DEMO_PAYMENTS"] = "1"          # isolated throw-away DB/storage
from app import app

c = app.test_client()
def reg(u):
    r = c.post("/api/auth/register", json={"username": u, "email": u + "@x.com", "password": "secret1"})
    assert r.status_code == 201, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}

a, b = reg("alice"), reg("bob")
assert c.get("/api/files").status_code == 401                                   # auth enforced
assert c.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
fid = c.post("/api/folders", json={"name": "Work"}, headers=a).get_json()["id"]
up = lambda content, h, parent=None: c.post("/api/files/upload", headers=h, data={"file": (io.BytesIO(content), "a.txt"), **({"parent_id": str(parent)} if parent else {})})
r = up(b"hello v1", a, fid); assert r.status_code == 201 and r.get_json()["version"] == 1
file_id = r.get_json()["id"]
r = up(b"hello v2!", a, fid); assert r.get_json()["version"] == 2 and r.get_json()["id"] == file_id  # versioning
assert c.get(f"/api/files/{file_id}/download", headers=a).data == b"hello v2!"
assert c.get(f"/api/files/{file_id}/download?version=1", headers=a).data == b"hello v1"
assert c.get(f"/api/files/{file_id}/download", headers=b).status_code == 404    # not shared yet
assert up(b"x", b, fid).status_code == 403
assert c.post(f"/api/files/{fid}/share", json={"username": "bob", "role": "view"}, headers=a).status_code == 200
assert c.get(f"/api/files/{file_id}/download", headers=b).data == b"hello v2!"  # inherited from folder
assert up(b"x", b, fid).status_code == 403                                      # view-only can't upload
c.post(f"/api/files/{fid}/share", json={"username": "bob", "role": "edit"}, headers=a)
assert up(b"bob's edit", b, fid).get_json()["version"] == 3                      # edit can upload
assert len(c.get("/api/shared", headers=b).get_json()["items"]) == 1
assert c.get(f"/api/files/{file_id}/versions", headers=a).get_json()["current"] == 3
assert c.post(f"/api/files/{file_id}/restore/1", headers=a).status_code == 200
assert c.get(f"/api/files/{file_id}/download", headers=a).data == b"hello v1"
assert c.patch(f"/api/files/{file_id}", json={"name": "b.txt"}, headers=a).status_code == 200
assert len(c.get("/api/files?q=b.t", headers=a).get_json()["items"]) == 1        # search
assert c.delete(f"/api/files/{fid}", headers=b).status_code == 403               # only owner deletes
assert c.delete(f"/api/files/{fid}", headers=a).status_code == 200               # cascade delete
assert c.get("/api/files", headers=a).get_json()["items"] == []
assert c.get("/api/me", headers=a).get_json()["used"] == 0
print("ALL TESTS PASSED ✅")

# ---------------- billing / plans ----------------
import time
p = c.get("/api/plans").get_json(); assert p["provider"] == "demo" and len(p["plans"]) == 4
me = c.get("/api/me", headers=a).get_json(); assert me["plan"] == "free" and me["quota"] == 1024 * 1024 * 1024
co = c.post("/api/billing/checkout", json={"plan": "pro", "period": "monthly"}, headers=a).get_json()
assert co["mode"] == "demo" and co["amount"] == 299
card = {"payment_id": co["payment_id"], "name": "Alice", "expiry": "12/40", "cvc": "123"}
assert c.post("/api/billing/demo-pay", json={**card, "card_number": "1234 5678 9012 3456"}, headers=a).status_code == 400   # bad Luhn
assert c.post("/api/billing/demo-pay", json={**card, "card_number": "4000 0000 0000 0002"}, headers=a).status_code == 402   # declined
co = c.post("/api/billing/checkout", json={"plan": "pro", "period": "yearly"}, headers=a).get_json(); assert co["amount"] == 2990
assert c.post("/api/billing/demo-pay", json={**card, "payment_id": co["payment_id"], "card_number": "4242 4242 4242 4242"}, headers=b).status_code == 404  # not bob's
r = c.post("/api/billing/demo-pay", json={**card, "payment_id": co["payment_id"], "card_number": "4242 4242 4242 4242"}, headers=a); assert r.status_code == 200
me = c.get("/api/me", headers=a).get_json(); assert me["plan"] == "pro" and me["quota"] == 100 * 1024 ** 3
assert me["plan_expires"] > time.time() + 360 * 86400
h = c.get("/api/billing/history", headers=a).get_json()["payments"]; assert [x["status"] for x in h] == ["paid", "failed"]
inv = c.get(f"/api/billing/invoice/{h[0]['id']}", headers=a); assert inv.status_code == 200 and b"Invoice" in inv.data
assert c.get(f"/api/billing/invoice/{h[0]['id']}", headers=b).status_code == 404
c.post("/api/billing/cancel", headers=a); assert c.get("/api/me", headers=a).get_json()["plan"] == "free"
print("BILLING TESTS PASSED 💳")
