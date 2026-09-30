# ☁️ CloudDrive – Cloud-Based Online File Storage System (Google Drive Clone)

A full-stack file storage app: user accounts, file upload/download, folders, sharing with permissions,
version control, JWT authentication and pluggable cloud storage (local disk by default, **AWS S3** optional).

## Tech stack
| Layer | Technology |
|---|---|
| Front-end | HTML, CSS, JavaScript (single-page app, no build step) |
| Back-end | Python **Flask** REST API |
| Database | **SQLite** (zero setup; schema is portable to MySQL/PostgreSQL) |
| Auth | **JWT** (HS256) + salted password hashing |
| Storage | Local disk (`data/uploads`) or **AWS S3** (`STORAGE_BACKEND=s3`) |
| IDE | VS Code |

## Features
- Register / login with JWT, protected API routes
- Upload (button or drag & drop, multiple files), download, rename, delete
- Folders and nested folder navigation with breadcrumbs
- **Sharing** with other users – *view* or *edit* role, inherited by sub-folders
- **Version control** – uploading a file with the same name creates a new version; view history, download or restore any version
- Modern responsive UI: list/grid views, dark mode, sorting, drag & drop with upload progress, in-browser preview (images, PDF, video, audio, text)
- Search, storage plans (Free/Plus/Pro/Ultra) with checkout, invoices and usage bar
- Multi-user tested (automated test included)

## Run it in VS Code (5 steps)
1. Install **Python 3.9+** (https://python.org) and **VS Code**.
2. Unzip the project and open the folder in VS Code (`File → Open Folder`).
3. Open the terminal (`Ctrl + ~`) and create a virtual environment:
   ```bash
   python -m venv venv
   # Windows:
   venv\Scripts\activate
   # macOS / Linux:
   source venv/bin/activate
   ```
4. Install dependencies:
   ```bash
   pip install Flask
   ```
   (`boto3` is only needed for S3 – see below.)
5. Start the server and open **http://localhost:5000**
   ```bash
   python app.py
   ```

Optional demo data: `python seed_demo.py` → log in as `demo / demo123` or `friend / friend123`.
Run tests: `python test_api.py`.

## Project structure
```
cloud-drive/
├── app.py            # Flask API: auth, files, folders, sharing, versions, storage backends
├── static/
│   ├── index.html    # UI
│   ├── style.css
│   └── app.js        # front-end logic (fetch + JWT)
├── seed_demo.py      # demo users + synthetic sample file metadata
├── test_api.py       # end-to-end API tests
├── requirements.txt
└── data/             # created at first run: drive.db + uploads/
```

## Database schema
- **users**(id, username, email, password_hash, created_at)
- **files**(id, owner_id, parent_id, name, is_folder, current_version, created_at, updated_at)
- **file_metadata**(id, file_id, version, storage_key, size, mime, uploaded_by, created_at) – one row per version
- **permissions**(id, file_id, user_id, role `view|edit`)

## REST API (all except auth need `Authorization: Bearer <token>`)
| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register`, `/api/auth/login` | Create account / get JWT |
| GET | `/api/me` | Profile + storage usage |
| GET | `/api/files?parent=<id>&q=<search>` | List folder contents / search |
| GET | `/api/shared` | Items shared with me |
| POST | `/api/folders` | Create folder `{name, parent_id}` |
| POST | `/api/files/upload` | Multipart upload `file`, `parent_id` (new version if name exists) |
| GET | `/api/files/<id>/download?version=N` | Download |
| GET | `/api/files/<id>/versions` | Version history |
| POST | `/api/files/<id>/restore/<ver>` | Make an old version current |
| PATCH | `/api/files/<id>` | Rename `{name}` |
| DELETE | `/api/files/<id>` | Delete file/folder (owner only) |
| GET/POST | `/api/files/<id>/permissions`, `/share` | List / grant access `{username, role}` |
| DELETE | `/api/files/<id>/share/<username>` | Revoke access |

## Using AWS S3 instead of local disk
```bash
pip install boto3
aws configure                      # or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
# Windows PowerShell:  $env:STORAGE_BACKEND="s3"; $env:S3_BUCKET="my-bucket"; $env:AWS_REGION="ap-south-1"
# macOS/Linux:         export STORAGE_BACKEND=s3 S3_BUCKET=my-bucket AWS_REGION=ap-south-1
python app.py
```
Azure Blob / GCP Storage: add a class with `save / open / delete` next to `LocalStorage` in `app.py`.

## Configuration (environment variables)
`SECRET_KEY` (JWT secret; auto-generated if unset), `USER_QUOTA_MB` (1024, Free-plan size), `CURRENCY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BASE_URL`, `MAX_UPLOAD_MB` (200), `S3_ENDPOINT_URL`,
`PORT` (5000), `STORAGE_BACKEND` (`local`|`s3`), `DATA_DIR`.

## 💳 Storage plans & payments (like Google One)
| Plan | Storage | Monthly | Yearly |
|---|---|---|---|
| Free | 1 GB | $0 | – |
| Plus | 10 GB | $0.99 | $9.90 |
| Pro | 100 GB | $2.99 | $29.90 |
| Ultra | 1 TB | $9.99 | $99.90 |

Edit prices/sizes in the `PLANS` dictionary in `app.py`. Prices are in US dollars (minor units = cents). Currency is set with `CURRENCY` (USD by default; INR, EUR, GBP also supported).

- **Upgrade:** click *Get more storage* (or *Billing*) → choose plan and Monthly/Yearly → checkout → storage limit rises instantly.
- Renewing the same plan **extends** the expiry; when a plan expires the user falls back to Free (files are kept, but uploads are blocked while over quota).
- Payment history, printable **invoices**, and *Cancel plan*.
- Tables added: `payments`; `users` gets `plan` and `plan_expires` (old databases are migrated automatically).

**Demo mode (opt-in, local testing only):** set `DEMO_PAYMENTS=1` to get a simulated checkout with no real money. It is OFF by default; with no Stripe key and no demo flag the server refuses to take payments, so nobody can upgrade for free.
Test card `4242 4242 4242 4242`, any future expiry (e.g. `12/30`), any CVC. `4000 0000 0000 0002` simulates a declined card.
Card details are validated but never stored.

**Real payments with Stripe:**
1. Create a Stripe account, `pip install stripe`.
2. Set environment variables: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BASE_URL=https://your-site.com`.
3. In the Stripe dashboard add a webhook to `https://your-site.com/api/billing/webhook` for the event `checkout.session.completed`.
4. Use Stripe *test keys* first (`sk_test_…`, card 4242…). Users pay on Stripe's hosted page, so no card data touches your server.

Real-money notes: Stripe integration is included but was not exercised against live Stripe here, so test it with test keys first. Payments are one-time per period (users renew manually), not auto-recurring subscriptions. Charging real customers also requires a verified business account and tax/GST compliance, so use demo mode for a college submission.

## 🚀 Go live (public website + real payments)

Checklist – do these in order:

1. **Stripe** (https://dashboard.stripe.com): create an account, then copy your *Secret key* (`sk_test_…` first, `sk_live_…` when ready).
2. **Host the app** on a server with a **persistent disk** (the database and uploaded files live in `DATA_DIR`):
   - **Render** (easiest): push this folder to GitHub → *New → Blueprint* → pick the repo. `render.yaml` creates the web service + disk. Needs a paid instance because free instances have no persistent disk.
   - **Any VPS / Railway / Fly.io** with Docker: `docker build -t clouddrive .` then
     `docker run -d -p 80:8000 -v clouddrive-data:/data -e SECRET_KEY=... -e STRIPE_SECRET_KEY=... -e STRIPE_WEBHOOK_SECRET=... -e BASE_URL=https://yourdomain.com clouddrive`
3. **Environment variables** to set on the host:
   | Variable | Value |
   |---|---|
   | `SECRET_KEY` | long random string (required – signs login tokens) |
   | `STRIPE_SECRET_KEY` | `sk_test_…` or `sk_live_…` |
   | `STRIPE_WEBHOOK_SECRET` | `whsec_…` from step 4 |
   | `BASE_URL` | your public URL, e.g. `https://clouddrive.onrender.com` (no trailing slash) |
   | `DATA_DIR` | path of the persistent disk (e.g. `/var/data`) |
4. **Webhook:** Stripe dashboard → *Developers → Webhooks → Add endpoint* → URL `https://YOUR-SITE/api/billing/webhook`, event `checkout.session.completed`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`. (Even without the webhook, the app confirms the payment with Stripe when the user returns to the site.)
5. **Test in Stripe test mode** (card `4242 4242 4242 4242`), confirm the plan upgrades, then switch to live keys.
6. HTTPS is provided automatically by Render / most hosts. Keep `FLASK_DEBUG=0`.

For big storage, add `STORAGE_BACKEND=s3` with an S3 or Cloudflare R2 bucket (see below) so files are not limited by the server disk.

### Other hosting notes
- **PythonAnywhere:** works for the app itself (guide below), but its free plan restricts outgoing internet access, which can block payment calls – check their current policy before using it for real payments.
- Storage size = your host's disk (or your bucket). `USER_QUOTA_MB` is only the Free-plan size.

## Detailed hosting guides

> Localhost only works on your own PC. To get a public link you must host it on a server. Two important facts:
> 1. **Files and the database must live on a persistent disk.** Free "serverless" hosts wipe the disk on restart, so your users' files would vanish.
> 2. **Storage size = what your host gives you.** The app's per-user limit is `USER_QUOTA_MB` (default 1024 = 1 GB), but the host's disk is the real ceiling.

### Option A – PythonAnywhere (easiest, free plan available, keeps files)
1. Create an account at https://www.pythonanywhere.com (your app will be at `https://YOURNAME.pythonanywhere.com`).
2. **Files** tab → upload `cloud-drive.zip`. Open a **Bash console** and run:
   ```bash
   unzip cloud-drive.zip
   mkvirtualenv drive --python=python3.10
   pip install Flask
   ```
3. **Web** tab → *Add a new web app* → *Manual configuration* → pick the same Python version (3.10).
4. On the Web tab set **Source code** = `/home/YOURNAME/cloud-drive` and **Virtualenv** = `/home/YOURNAME/.virtualenvs/drive`.
5. Click the **WSGI configuration file** link, delete everything and paste:
   ```python
   import sys, os
   sys.path.insert(0, '/home/YOURNAME/cloud-drive')
   os.environ['SECRET_KEY'] = 'put-a-long-random-string-here'
   os.environ['USER_QUOTA_MB'] = '100'      # keep within your plan's disk size
   from app import app as application
   ```
6. (Optional, faster) Static files: URL `/static/` → `/home/YOURNAME/cloud-drive/static`.
7. Press **Reload**, then open your URL. Done ✅. The free plan has a small disk (about 512 MB); paid plans give more.

### Option B – Render (more space, needs a paid instance for persistent disk)
1. Push the project to a GitHub repo.
2. On https://render.com → *New → Blueprint* → select the repo. `render.yaml` already sets up gunicorn, a 10 GB disk and a random `SECRET_KEY`.
3. Open the generated `https://….onrender.com` URL. (Render's free plan has no persistent disk – files would be lost.)

### Option C – Unlimited-style storage with S3 / Cloudflare R2
Keep the app on any host and store the files in a bucket:
```
STORAGE_BACKEND=s3
S3_BUCKET=your-bucket
AWS_ACCESS_KEY_ID=...   AWS_SECRET_ACCESS_KEY=...   AWS_REGION=ap-south-1
S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com   # only for Cloudflare R2 (has a free tier)
```
Set `USER_QUOTA_MB` as high as you like. The SQLite database (`data/drive.db`) still needs a persistent disk – or migrate to PostgreSQL for a fully stateless server.

**Before going public:** set a strong `SECRET_KEY`, keep `FLASK_DEBUG=0`, and use HTTPS (both hosts above give it automatically).

## Security notes
Passwords are hashed (PBKDF2 via Werkzeug); JWTs expire after 12 h; every file access is permission-checked
server-side; uploads are stored under random keys, never user-supplied paths; front-end escapes all names.
