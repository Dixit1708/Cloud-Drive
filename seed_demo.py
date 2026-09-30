"""Optional: fills the app with demo users and synthetic sample-file metadata.
Run once:  python seed_demo.py     (then log in as  demo / demo123  or  friend / friend123)"""
import io, random
from app import app

random.seed(7)
c = app.test_client()
def reg(u, p):
    r = c.post("/api/auth/register", json={"username": u, "email": f"{u}@example.com", "password": p})
    if r.status_code != 201: r = c.post("/api/auth/login", json={"username": u, "password": p})
    return {"Authorization": "Bearer " + r.get_json()["token"]}

demo, friend = reg("demo", "demo123"), reg("friend", "friend123")
folders = {}
for name in ["Documents", "Images", "Projects"]:
    folders[name] = c.post("/api/folders", json={"name": name}, headers=demo).get_json().get("id")

samples = [("Documents", "report.txt"), ("Documents", "notes.md"), ("Images", "data.csv"), ("Projects", "todo.txt")]
for folder, fname in samples:                       # synthetic "Sample File Metadata" dataset
    body = "\n".join(f"row {i},{random.randint(1, 999)}" for i in range(50)).encode()
    c.post("/api/files/upload", headers=demo, data={"file": (io.BytesIO(body), fname), "parent_id": str(folders[folder])})
c.post("/api/files/upload", headers=demo, data={"file": (io.BytesIO(b"version two"), "report.txt"), "parent_id": str(folders["Documents"])})
c.post(f"/api/files/{folders['Projects']}/share", json={"username": "friend", "role": "edit"}, headers=demo)
print("Seeded. Login: demo/demo123 (owner) and friend/friend123 (has 'Projects' shared with edit access).")
