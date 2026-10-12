# pokemon-card-scanner

Scan a whole pack of Pokémon cards from one photo, log it, price it, fill your
Pokédex and battle it — on the web or as a phone app.

```
app/        FastAPI backend (scanning, auth, pulls, collection, dex, battles)
frontend/   React web app (Vite) — served by the backend in production
mobile/     React Native app (Expo) — iOS + Android
shared/     TypeScript shared by both front ends: API client, wire types,
            copy/formatting, review rules, theme tokens, pixel icons
matcher/ runpod_worker/ training/   ML services and training tooling
```

Both front ends talk to the same backend endpoints through the one client in
`shared/src/client.ts`, and share the "Pixel Pop" retro theme
(`shared/src/theme.ts`, mirrored as CSS variables in `frontend/src/index.css`).

## Backend

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # DATABASE_URL, AUTH_SECRET, POKEWALLET_API_KEY, …
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Local dev over plain http needs `COOKIE_SECURE=false`. The pack scan reads code
cards with Tesseract (`apt install tesseract-ocr`; Railway installs it via
`railpack.json`).

## Web app

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api → localhost:8000
npm run build      # → frontend/dist, which the backend serves at /
```

`frontend/e2e/scan.spec.ts` is a Playwright run of sign-up → scan → review →
save against a real backend (`npm run e2e`).

## Mobile app

See [`mobile/README.md`](mobile/README.md). Short version:

```bash
cd mobile
npm ci
npx expo start     # scan the QR code with Expo Go on your phone
```

In development the app finds the API on port 8000 of the machine running
`expo start`, so start the backend with `--host 0.0.0.0`.
