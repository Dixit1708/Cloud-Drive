"""Cloud-Based Online File Storage System (Google Drive clone)
Backend: Flask + SQLite, JWT auth, pluggable storage (local disk or AWS S3)."""
import os, time, hmac, hashlib, base64, json, sqlite3, uuid, io
from functools import wraps
from flask import Flask, request, jsonify, g, send_file, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "drive.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

FREE_MB = int(os.environ.get("USER_QUOTA_MB", "1024"))          # storage of the Free plan
CURRENCY = os.environ.get("CURRENCY", "USD").upper()
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PROVIDER = "stripe" if STRIPE_KEY else ("demo" if os.environ.get("DEMO_PAYMENTS") == "1" else "none")  # demo is opt-in (local testing only)
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
# prices are in minor units (paise/cents); yearly = 10 x monthly (2 months free)
PLANS = {
    "free":  dict(name="Free",  storage_mb=FREE_MB,       monthly=0,     features=["Basic storage", "File sharing", "Version history"]),
    "plus":  dict(name="Plus",  storage_mb=10 * 1024,     monthly=99,    features=["10 GB storage", "Share with view/edit access", "Version history"]),
    "pro":   dict(name="Pro",   storage_mb=100 * 1024,    monthly=299,  features=["100 GB storage", "Everything in Plus", "Priority support"]),
    "ultra": dict(name="Ultra", storage_mb=1024 * 1024,   monthly=999,  features=["1 TB storage", "Everything in Pro", "Best for teams"]),
}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").lower()
TOKEN_TTL = 60 * 60 * 12  # 12 hours

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ---------------------------------------------------------------- secret / JWT
def _load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"].encode()
    p = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(uuid.uuid4().hex + uuid.uuid4().hex)
    return open(p).read().encode()

SECRET = _load_secret()

def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def make_token(uid):
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"sub": uid, "exp": int(time.time()) + TOKEN_TTL}).encode())
    sig = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"

def read_token(tok):
    try:
        head, body, sig = tok.split(".")
        good = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(body))
        return data["sub"] if data["exp"] > time.time() else None
    except Exception:
        return None


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  email TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',
  plan_expires INTEGER);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  is_folder INTEGER NOT NULL DEFAULT 0,
  current_version INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS file_metadata(          -- one row per file version
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  storage_key TEXT NOT NULL,
  size INTEGER NOT NULL,
  mime TEXT,
  uploaded_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS permissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('view','edit')),
  UNIQUE(file_id, user_id));
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan TEXT NOT NULL, period TEXT NOT NULL,
  amount INTEGER NOT NULL, currency TEXT NOT NULL,
  status TEXT NOT NULL,                       -- pending | paid | failed
  provider TEXT NOT NULL, provider_ref TEXT,
  card_last4 TEXT, created_at INTEGER NOT NULL, paid_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS idx_meta_file ON file_metadata(file_id);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

with sqlite3.connect(DB_PATH) as _c:
    _c.executescript(SCHEMA)
    _cols = [r[1] for r in _c.execute("PRAGMA table_info(users)")]
    if "plan" not in _cols:                       # upgrade older databases in place
        _c.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        _c.execute("ALTER TABLE users ADD COLUMN plan_expires INTEGER")


# ---------------------------------------------------------------- storage backends
class LocalStorage:
    def save(self, key, stream):
        with open(os.path.join(UPLOAD_DIR, key), "wb") as out:
            while chunk := stream.read(1 << 20):
                out.write(chunk)
    def open(self, key): return open(os.path.join(UPLOAD_DIR, key), "rb")
    def delete(self, key):
        try: os.remove(os.path.join(UPLOAD_DIR, key))
        except FileNotFoundError: pass

class S3Storage:
    def __init__(self):
        import boto3  # pip install boto3
        self.bucket = os.environ["S3_BUCKET"]
        self.s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"),
                               endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None)  # set for Cloudflare R2 / MinIO etc.
    def save(self, key, stream): self.s3.upload_fileobj(stream, self.bucket, key)
    def open(self, key):
        return io.BytesIO(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
    def delete(self, key): self.s3.delete_object(Bucket=self.bucket, Key=key)

storage = S3Storage() if STORAGE_BACKEND == "s3" else LocalStorage()


# ---------------------------------------------------------------- auth helpers
def auth_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        h = request.headers.get("Authorization", "")
        uid = read_token(h[7:]) if h.startswith("Bearer ") else None
        user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
        if not user:
            return jsonify(error="Unauthorized"), 401
        g.user = user
        return fn(*a, **kw)
    return wrapper

def err(msg, code=400): return jsonify(error=msg), code

def get_file(fid): return db().execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()

def role_for(f, uid):
    """'owner' | 'edit' | 'view' | None -- permissions inherit down from shared folders."""
    if f["owner_id"] == uid: return "owner"
    best, cur = None, f
    while cur is not None:
        p = db().execute("SELECT role FROM permissions WHERE file_id=? AND user_id=?", (cur["id"], uid)).fetchone()
        if p:
            if p["role"] == "edit": return "edit"
            best = "view"
        cur = get_file(cur["parent_id"]) if cur["parent_id"] else None
    return best

def can_write(role): return role in ("owner", "edit")

def active_plan(u):
    """Plan in force right now; a lapsed paid plan falls back to Free (files are kept, uploads blocked if over quota)."""
    return u["plan"] if u["plan"] in PLANS and u["plan"] != "free" and (u["plan_expires"] or 0) > time.time() else "free"

def quota_for(uid):
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return PLANS[active_plan(u)]["storage_mb"] * 1024 * 1024

def used_bytes(owner_id):
    return db().execute("""SELECT COALESCE(SUM(m.size),0) FROM file_metadata m
        JOIN files f ON f.id=m.file_id WHERE f.owner_id=?""", (owner_id,)).fetchone()[0]

SELECT_ITEM = """SELECT f.*, m.size, m.mime, u.username AS owner_name FROM files f
  LEFT JOIN file_metadata m ON m.file_id=f.id AND m.version=f.current_version
  JOIN users u ON u.id=f.owner_id """

def item(r):
    return dict(id=r["id"], name=r["name"], is_folder=bool(r["is_folder"]), parent_id=r["parent_id"],
                size=r["size"], mime=r["mime"], version=r["current_version"], owner=r["owner_name"],
                owner_id=r["owner_id"], updated_at=r["updated_at"])


# ---------------------------------------------------------------- pages
@app.route("/")
def index(): return send_from_directory("static", "index.html")


# ---------------------------------------------------------------- auth API
@app.post("/api/auth/register")
def register():
    d = request.get_json(silent=True) or {}
    u, e, p = (d.get("username") or "").strip(), (d.get("email") or "").strip(), d.get("password") or ""
    if len(u) < 3 or not u.replace("_", "").replace(".", "").isalnum(): return err("Username: 3+ letters/digits/_/.")
    if "@" not in e: return err("Enter a valid email")
    if len(p) < 6: return err("Password must be at least 6 characters")
    try:
        cur = db().execute("INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)",
                           (u, e, generate_password_hash(p), int(time.time())))
        db().commit()
    except sqlite3.IntegrityError:
        return err("Username already taken", 409)
    return jsonify(token=make_token(cur.lastrowid), username=u), 201

_attempts = {}
def _ip(): return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")

@app.post("/api/auth/login")
def login():
    now, ip = time.time(), _ip()
    recent = [t for t in _attempts.get(ip, []) if now - t < 60]
    if len(recent) >= 10: return err("Too many attempts. Try again in a minute.", 429)
    d = request.get_json(silent=True) or {}
    r = db().execute("SELECT * FROM users WHERE username=?", ((d.get("username") or "").strip(),)).fetchone()
    if not r or not check_password_hash(r["password_hash"], d.get("password") or ""):
        _attempts[ip] = recent + [now]
        return err("Invalid username or password", 401)
    return jsonify(token=make_token(r["id"]), username=r["username"])

@app.get("/api/me")
@auth_required
def me():
    return jsonify(id=g.user["id"], username=g.user["username"], email=g.user["email"],
                   used=used_bytes(g.user["id"]), quota=quota_for(g.user["id"]), plan=active_plan(g.user), plan_expires=g.user["plan_expires"],
                   files=db().execute("SELECT COUNT(*) FROM files WHERE owner_id=? AND is_folder=0", (g.user["id"],)).fetchone()[0],
                   backend=STORAGE_BACKEND)


# ---------------------------------------------------------------- files API
@app.get("/api/files")
@auth_required
def list_files():
    uid, parent, q = g.user["id"], request.args.get("parent", type=int), (request.args.get("q") or "").strip()
    crumbs = []
    if q:
        rows = db().execute(SELECT_ITEM + "WHERE f.owner_id=? AND f.name LIKE ? ORDER BY f.is_folder DESC, f.name",
                            (uid, f"%{q}%")).fetchall()
    elif parent is None:
        rows = db().execute(SELECT_ITEM + "WHERE f.owner_id=? AND f.parent_id IS NULL ORDER BY f.is_folder DESC, f.name COLLATE NOCASE",
                            (uid,)).fetchall()
    else:
        pf = get_file(parent)
        role = role_for(pf, uid) if pf and pf["is_folder"] else None
        if not role: return err("Folder not found", 404)
        rows = db().execute(SELECT_ITEM + "WHERE f.parent_id=? ORDER BY f.is_folder DESC, f.name COLLATE NOCASE", (parent,)).fetchall()
        cur = pf
        while cur:
            crumbs.insert(0, dict(id=cur["id"], name=cur["name"]))
            if cur["owner_id"] != uid and db().execute("SELECT 1 FROM permissions WHERE file_id=? AND user_id=?", (cur["id"], uid)).fetchone():
                break
            cur = get_file(cur["parent_id"]) if cur["parent_id"] else None
        return jsonify(items=[item(r) for r in rows], breadcrumb=crumbs, role=role)
    return jsonify(items=[item(r) for r in rows], breadcrumb=crumbs, role="owner")

@app.get("/api/shared")
@auth_required
def shared_with_me():
    rows = db().execute(SELECT_ITEM + "JOIN permissions p ON p.file_id=f.id WHERE p.user_id=? ORDER BY f.name COLLATE NOCASE",
                        (g.user["id"],)).fetchall()
    return jsonify(items=[item(r) for r in rows], breadcrumb=[], role="view")

@app.post("/api/folders")
@auth_required
def make_folder():
    d = request.get_json(silent=True) or {}
    name, parent = (d.get("name") or "").strip(), d.get("parent_id")
    if not name or "/" in name or "\\" in name: return err("Invalid folder name")
    owner = g.user["id"]
    if parent:
        pf = get_file(parent)
        if not pf or not pf["is_folder"] or not can_write(role_for(pf, owner)): return err("No permission", 403)
        owner = pf["owner_id"]
    if db().execute("SELECT 1 FROM files WHERE parent_id IS ? AND owner_id=? AND name=?", (parent, owner, name)).fetchone():
        return err("An item with this name already exists", 409)
    now = int(time.time())
    cur = db().execute("INSERT INTO files(owner_id,parent_id,name,is_folder,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                       (owner, parent, name, now, now))
    db().commit()
    return jsonify(id=cur.lastrowid), 201

@app.post("/api/files/upload")
@auth_required
def upload():
    fs, parent = request.files.get("file"), request.form.get("parent_id", type=int)
    if not fs or not fs.filename: return err("No file provided")
    name = os.path.basename(fs.filename.replace("\\", "/")).strip()
    if not name: return err("Invalid file name")
    owner = g.user["id"]
    if parent:
        pf = get_file(parent)
        if not pf or not pf["is_folder"] or not can_write(role_for(pf, owner)): return err("No permission", 403)
        owner = pf["owner_id"]
    fs.stream.seek(0, 2); size = fs.stream.tell(); fs.stream.seek(0)
    if used_bytes(owner) + size > quota_for(owner): return err("Storage quota exceeded", 413)

    now = int(time.time())
    ex = db().execute("SELECT * FROM files WHERE parent_id IS ? AND owner_id=? AND name=? AND is_folder=0",
                      (parent, owner, name)).fetchone()
    if ex:                                   # same name -> new version (version control)
        fid, ver = ex["id"], ex["current_version"] + 1
    else:
        fid = db().execute("INSERT INTO files(owner_id,parent_id,name,is_folder,current_version,created_at,updated_at) VALUES(?,?,?,0,0,?,?)",
                           (owner, parent, name, now, now)).lastrowid
        ver = 1
    key = f"{owner}/{uuid.uuid4().hex}" if STORAGE_BACKEND == "s3" else uuid.uuid4().hex
    storage.save(key, fs.stream)
    db().execute("INSERT INTO file_metadata(file_id,version,storage_key,size,mime,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?)",
                 (fid, ver, key, size, fs.mimetype or "application/octet-stream", g.user["id"], now))
    db().execute("UPDATE files SET current_version=?, updated_at=? WHERE id=?", (ver, now, fid))
    db().commit()
    return jsonify(id=fid, version=ver, name=name), 201

@app.get("/api/files/<int:fid>/download")
@auth_required
def download(fid):
    f = get_file(fid)
    if not f or f["is_folder"] or not role_for(f, g.user["id"]): return err("File not found", 404)
    ver = request.args.get("version", f["current_version"], type=int)
    m = db().execute("SELECT * FROM file_metadata WHERE file_id=? AND version=?", (fid, ver)).fetchone()
    if not m: return err("Version not found", 404)
    return send_file(storage.open(m["storage_key"]), mimetype=m["mime"], as_attachment=not request.args.get("inline"), download_name=f["name"])

@app.get("/api/files/<int:fid>/versions")
@auth_required
def versions(fid):
    f = get_file(fid)
    if not f or f["is_folder"] or not role_for(f, g.user["id"]): return err("File not found", 404)
    rows = db().execute("""SELECT m.version,m.size,m.created_at,u.username FROM file_metadata m
        JOIN users u ON u.id=m.uploaded_by WHERE m.file_id=? ORDER BY m.version DESC""", (fid,)).fetchall()
    return jsonify(current=f["current_version"], versions=[dict(r) for r in rows])

@app.post("/api/files/<int:fid>/restore/<int:ver>")
@auth_required
def restore(fid, ver):
    f = get_file(fid)
    if not f or f["is_folder"] or not can_write(role_for(f, g.user["id"])): return err("No permission", 403)
    if not db().execute("SELECT 1 FROM file_metadata WHERE file_id=? AND version=?", (fid, ver)).fetchone():
        return err("Version not found", 404)
    db().execute("UPDATE files SET current_version=?, updated_at=? WHERE id=?", (ver, int(time.time()), fid))
    db().commit()
    return jsonify(ok=True)

@app.patch("/api/files/<int:fid>")
@auth_required
def rename(fid):
    f = get_file(fid)
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not f or not can_write(role_for(f, g.user["id"])): return err("No permission", 403)
    if not name or "/" in name or "\\" in name: return err("Invalid name")
    if db().execute("SELECT 1 FROM files WHERE parent_id IS ? AND owner_id=? AND name=? AND id<>?",
                    (f["parent_id"], f["owner_id"], name, fid)).fetchone():
        return err("An item with this name already exists", 409)
    db().execute("UPDATE files SET name=?, updated_at=? WHERE id=?", (name, int(time.time()), fid))
    db().commit()
    return jsonify(ok=True)

@app.delete("/api/files/<int:fid>")
@auth_required
def delete(fid):
    f = get_file(fid)
    if not f or f["owner_id"] != g.user["id"]: return err("Only the owner can delete", 403)
    keys = [r[0] for r in db().execute("""WITH RECURSIVE t(id) AS (SELECT ? UNION ALL SELECT f.id FROM files f JOIN t ON f.parent_id=t.id)
        SELECT storage_key FROM file_metadata WHERE file_id IN (SELECT id FROM t)""", (fid,)).fetchall()]
    db().execute("DELETE FROM files WHERE id=?", (fid,))     # cascades to versions/permissions/children
    db().commit()
    for k in keys: storage.delete(k)
    return jsonify(ok=True)


# ---------------------------------------------------------------- sharing API
@app.get("/api/files/<int:fid>/permissions")
@auth_required
def get_perms(fid):
    f = get_file(fid)
    if not f or f["owner_id"] != g.user["id"]: return err("Only the owner can manage sharing", 403)
    rows = db().execute("SELECT u.username, p.role FROM permissions p JOIN users u ON u.id=p.user_id WHERE p.file_id=?", (fid,)).fetchall()
    return jsonify(shared_with=[dict(r) for r in rows])

@app.post("/api/files/<int:fid>/share")
@auth_required
def share(fid):
    f, d = get_file(fid), request.get_json(silent=True) or {}
    if not f or f["owner_id"] != g.user["id"]: return err("Only the owner can share", 403)
    target = db().execute("SELECT id FROM users WHERE username=?", ((d.get("username") or "").strip(),)).fetchone()
    role = d.get("role", "view")
    if not target: return err("User not found", 404)
    if target["id"] == g.user["id"]: return err("You already own this")
    if role not in ("view", "edit"): return err("Role must be view or edit")
    db().execute("""INSERT INTO permissions(file_id,user_id,role) VALUES(?,?,?)
        ON CONFLICT(file_id,user_id) DO UPDATE SET role=excluded.role""", (fid, target["id"], role))
    db().commit()
    return jsonify(ok=True)

@app.delete("/api/files/<int:fid>/share/<username>")
@auth_required
def unshare(fid, username):
    f = get_file(fid)
    if not f or f["owner_id"] != g.user["id"]: return err("Only the owner can manage sharing", 403)
    db().execute("DELETE FROM permissions WHERE file_id=? AND user_id=(SELECT id FROM users WHERE username=?)", (fid, username))
    db().commit()
    return jsonify(ok=True)



# ---------------------------------------------------------------- billing API
def price_for(plan, period): return PLANS[plan]["monthly"] * (10 if period == "yearly" else 1)

def activate_plan(pay):
    """Mark a payment paid and grant/extend the plan (renewing the same plan extends from its current expiry)."""
    u = db().execute("SELECT * FROM users WHERE id=?", (pay["user_id"],)).fetchone()
    now = int(time.time())
    start = u["plan_expires"] if u["plan"] == pay["plan"] and (u["plan_expires"] or 0) > now else now
    end = start + (365 if pay["period"] == "yearly" else 30) * 86400
    db().execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?", (pay["plan"], end, pay["user_id"]))
    db().execute("UPDATE payments SET status='paid', paid_at=? WHERE id=?", (now, pay["id"]))
    db().commit()

def luhn_ok(num):
    digits = [int(d) for d in num][::-1]
    return sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits)) % 10 == 0

@app.get("/api/plans")
def plans():
    return jsonify(currency=CURRENCY, symbol=SYMBOLS.get(CURRENCY, CURRENCY + " "), provider=PROVIDER,
                   plans=[dict(id=k, name=v["name"], storage_mb=v["storage_mb"], monthly=v["monthly"],
                               yearly=price_for(k, "yearly") if k != "free" else 0, features=v["features"]) for k, v in PLANS.items()])

@app.post("/api/billing/checkout")
@auth_required
def checkout():
    d = request.get_json(silent=True) or {}
    plan, period = d.get("plan"), d.get("period", "monthly")
    if plan not in PLANS or plan == "free" or period not in ("monthly", "yearly"): return err("Invalid plan")
    if PROVIDER == "none": return err("Payments are not configured on this server yet", 503)
    amount = price_for(plan, period)
    pid = db().execute("INSERT INTO payments(user_id,plan,period,amount,currency,status,provider,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                       (g.user["id"], plan, period, amount, CURRENCY, PROVIDER, int(time.time()))).lastrowid
    db().commit()
    if PROVIDER == "stripe":
        import stripe
        stripe.api_key = STRIPE_KEY
        sess = stripe.checkout.Session.create(
            mode="payment", client_reference_id=str(g.user["id"]), metadata={"payment_id": str(pid)},
            line_items=[{"quantity": 1, "price_data": {"currency": CURRENCY.lower(), "unit_amount": amount,
                         "product_data": {"name": f"CloudDrive {PLANS[plan]['name']} ({period})"}}}],
            success_url=BASE_URL + "/?paid=1&sid={CHECKOUT_SESSION_ID}", cancel_url=BASE_URL + "/?paid=0")
        db().execute("UPDATE payments SET provider_ref=? WHERE id=?", (sess.id, pid)); db().commit()
        return jsonify(mode="stripe", url=sess.url)
    return jsonify(mode="demo", payment_id=pid, amount=amount)

@app.post("/api/billing/demo-pay")
@auth_required
def demo_pay():
    """Simulated card payment (only when no Stripe key is set). Card details are validated but NEVER stored."""
    if PROVIDER != "demo": return err("Demo payments are disabled", 403)
    d = request.get_json(silent=True) or {}
    pay = db().execute("SELECT * FROM payments WHERE id=? AND user_id=? AND status='pending'", (d.get("payment_id"), g.user["id"])).fetchone()
    if not pay: return err("Payment not found", 404)
    num = "".join(ch for ch in str(d.get("card_number", "")) if ch.isdigit())
    exp, cvc = str(d.get("expiry", "")), str(d.get("cvc", ""))
    if not (13 <= len(num) <= 19 and luhn_ok(num)): return err("Invalid card number")
    try:
        mm, yy = exp.split("/"); mm, yy = int(mm), 2000 + int(yy)
        assert 1 <= mm <= 12 and (yy, mm) >= (time.gmtime().tm_year, time.gmtime().tm_mon)
    except Exception:
        return err("Invalid or expired card date (use MM/YY)")
    if not (cvc.isdigit() and 3 <= len(cvc) <= 4): return err("Invalid CVC")
    if not (d.get("name") or "").strip(): return err("Enter the name on the card")
    if num == "4000000000000002":
        db().execute("UPDATE payments SET status='failed', card_last4=? WHERE id=?", (num[-4:], pay["id"])); db().commit()
        return err("Your card was declined (test card)", 402)
    db().execute("UPDATE payments SET card_last4=?, provider_ref=? WHERE id=?", (num[-4:], "demo_" + uuid.uuid4().hex[:12], pay["id"]))
    activate_plan(pay)
    return jsonify(ok=True, plan=pay["plan"])

@app.post("/api/billing/verify")
@auth_required
def verify_payment():
    """Called when the user returns from Stripe: confirms the session with Stripe and activates the plan."""
    if PROVIDER != "stripe": return err("Not enabled", 404)
    import stripe
    stripe.api_key = STRIPE_KEY
    sid = (request.get_json(silent=True) or {}).get("session_id", "")
    pay = db().execute("SELECT * FROM payments WHERE provider_ref=? AND user_id=?", (sid, g.user["id"])).fetchone()
    if not pay: return err("Payment not found", 404)
    if pay["status"] == "paid": return jsonify(ok=True, plan=pay["plan"])
    try:
        sess = stripe.checkout.Session.retrieve(sid)
    except Exception:
        return err("Could not verify payment with Stripe", 502)
    if sess["payment_status"] != "paid": return err("Payment not completed yet", 402)
    activate_plan(pay)
    return jsonify(ok=True, plan=pay["plan"])

@app.post("/api/billing/webhook")
def stripe_webhook():
    if PROVIDER != "stripe": return err("Not enabled", 404)
    import stripe
    try:
        ev = stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""), os.environ["STRIPE_WEBHOOK_SECRET"])
    except Exception:
        return err("Invalid signature", 400)
    if ev["type"] == "checkout.session.completed":
        pid = ev["data"]["object"]["metadata"].get("payment_id")
        pay = db().execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
        if pay: activate_plan(pay)
    return jsonify(received=True)

@app.post("/api/billing/cancel")
@auth_required
def cancel_plan():
    db().execute("UPDATE users SET plan='free', plan_expires=NULL WHERE id=?", (g.user["id"],)); db().commit()
    return jsonify(ok=True)

@app.get("/api/billing/history")
@auth_required
def pay_history():
    rows = db().execute("SELECT id,plan,period,amount,currency,status,card_last4,created_at FROM payments WHERE user_id=? AND status<>'pending' ORDER BY id DESC", (g.user["id"],)).fetchall()
    return jsonify(payments=[dict(r) for r in rows])

@app.get("/api/billing/invoice/<int:pid>")
@auth_required
def invoice(pid):
    from html import escape
    p = db().execute("SELECT * FROM payments WHERE id=? AND user_id=? AND status='paid'", (pid, g.user["id"])).fetchone()
    if not p: return err("Invoice not found", 404)
    sym = SYMBOLS.get(p["currency"], p["currency"] + " ")
    when = time.strftime("%d %b %Y", time.gmtime(p["paid_at"] or p["created_at"]))
    html = f"""<!doctype html><meta charset=utf-8><title>Invoice #{p['id']}</title>
<body style="font-family:system-ui;max-width:640px;margin:40px auto;color:#111"><h1>☁ CloudDrive</h1><h2>Invoice #{p['id']:05d}</h2>
<p>Billed to: <b>{escape(g.user['username'])}</b> ({escape(g.user['email'])})<br>Date: {when}<br>Payment: {escape(p['provider'])}{' · card ending ' + escape(p['card_last4']) if p['card_last4'] else ''}</p>
<table width=100% cellpadding=10 style="border-collapse:collapse;border:1px solid #ddd"><tr style="background:#f3f4f6"><th align=left>Item</th><th align=right>Amount</th></tr>
<tr><td>CloudDrive {escape(PLANS[p['plan']]['name'])} – {escape(p['period'])} ({PLANS[p['plan']]['storage_mb'] // 1024} GB)</td><td align=right>{sym}{p['amount'] / 100:,.2f}</td></tr>
<tr><td align=right><b>Total paid</b></td><td align=right><b>{sym}{p['amount'] / 100:,.2f}</b></td></tr></table>
<p style="color:#666;font-size:13px">{'Demo invoice – no real money was charged.' if p['provider'] == 'demo' else 'Thank you for your purchase.'}</p><script>window.print()</script></body>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.errorhandler(413)
def too_large(_): return err(f"File too large (max {MAX_UPLOAD_MB} MB)", 413)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG", "0") == "1")
