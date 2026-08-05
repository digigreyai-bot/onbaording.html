# Digigrey Client Onboarding (Admin + Client)

FastAPI app: Digigrey creates invites from `/admin`, clients submit at `/o/{token}`, submissions + uploads appear on the admin dashboard.

## Local run

```bash
cd onboarding
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
set ADMIN_PASSWORD=changeme
set SESSION_SECRET=random-long-string
set APP_BASE_URL=http://127.0.0.1:8000
uvicorn app:app --reload --port 8000
```

Open http://127.0.0.1:8000/admin

## Railway

1. Root directory / start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
2. Env vars:
   - `ADMIN_PASSWORD` — required
   - `SESSION_SECRET` — required (long random)
   - `APP_BASE_URL` — your Railway public URL (no trailing slash)
   - `DATA_DIR=/data`
3. Attach a **Volume** mounted at `/data` so SQLite + uploads persist across deploys.

## Flow

1. Login at `/admin`
2. Create invite: label, products, paste VTour/Avatar **connect** URL when needed
3. Copy client form link `/o/{token}` → WhatsApp
4. Client fills form + uploads → status becomes `submitted`
5. Admin opens invite detail → download files / view payload
