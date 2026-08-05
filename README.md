# Digigrey Client Onboarding (Admin + Client)

FastAPI app: Digigrey creates invites from `/admin`, clients submit at `/o/{token}`.

## Railway

Uses Dockerfile (python:3.12-slim). Set env:

- ADMIN_PASSWORD
- SESSION_SECRET
- APP_BASE_URL (public URL, no trailing slash)
- DATA_DIR=/data

Mount a volume at /data. Open /admin after deploy.
