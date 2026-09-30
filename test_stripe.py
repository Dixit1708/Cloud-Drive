"""Tests the Stripe flow with a FAKE stripe module (no network). Run: python test_stripe.py"""
import os, sys, types, json, tempfile
os.environ.update(DATA_DIR=tempfile.mkdtemp(), STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_x", BASE_URL="https://drive.test")
store = {}
class S(dict): __getattr__ = dict.get
def create(**kw):
    sid = f"cs_{len(store)+1}"; store[sid] = S(id=sid, url="https://checkout.stripe.test/" + sid, metadata=kw["metadata"], payment_status="unpaid", kw=kw); return store[sid]
class Webhook:
    @staticmethod
    def construct_event(payload, sig, secret):
        if sig != "good": raise ValueError("bad signature")
        return json.loads(payload)
fake = types.ModuleType("stripe"); fake.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(create=create, retrieve=lambda sid: store[sid])); fake.Webhook = Webhook
sys.modules["stripe"] = fake
from app import app
c = app.test_client()
def reg(u): return {"Authorization": "Bearer " + c.post("/api/auth/register", json={"username": u, "email": u + "@x.com", "password": "secret1"}).get_json()["token"]}
a, b = reg("alice"), reg("bob")
assert c.get("/api/plans").get_json()["provider"] == "stripe"
co = c.post("/api/billing/checkout", json={"plan": "plus", "period": "monthly"}, headers=a).get_json()
assert co["mode"] == "stripe" and co["url"].startswith("https://checkout.stripe.test/")
sid = co["url"].rsplit("/", 1)[1]
assert store[sid].kw["success_url"] == "https://drive.test/?paid=1&sid={CHECKOUT_SESSION_ID}"
assert store[sid].kw["line_items"][0]["price_data"]["unit_amount"] == 99 and store[sid].kw["line_items"][0]["price_data"]["currency"] == "usd"
assert c.post("/api/billing/demo-pay", json={}, headers=a).status_code == 403          # demo disabled
assert c.post("/api/billing/verify", json={"session_id": sid}, headers=a).status_code == 402   # not paid yet
assert c.post("/api/billing/verify", json={"session_id": sid}, headers=b).status_code == 404   # other user
store[sid]["payment_status"] = "paid"
assert c.post("/api/billing/verify", json={"session_id": sid}, headers=a).status_code == 200
assert c.get("/api/me", headers=a).get_json()["plan"] == "plus"
assert c.post("/api/billing/verify", json={"session_id": sid}, headers=a).status_code == 200   # idempotent
# webhook path
co2 = c.post("/api/billing/checkout", json={"plan": "pro", "period": "yearly"}, headers=b).get_json(); sid2 = co2["url"].rsplit("/", 1)[1]
ev = {"type": "checkout.session.completed", "data": {"object": {"metadata": store[sid2]["metadata"]}}}
assert c.post("/api/billing/webhook", data=json.dumps(ev), headers={"Stripe-Signature": "bad"}).status_code == 400
assert c.post("/api/billing/webhook", data=json.dumps(ev), headers={"Stripe-Signature": "good"}).status_code == 200
assert c.get("/api/me", headers=b).get_json()["plan"] == "pro"
print("STRIPE FLOW TESTS PASSED 💳")
