# Pack Scanner — mobile

The React Native (Expo) app for Pack Scanner. Same backend, same API client and
same retro look as the web app — the shared pieces live in `../shared`.

## What's in it

| Tab | |
| --- | --- |
| **Scan** | Pick a mode: **One photo** (staircase + code card), **Live** (one card at a time), **Binder page** (into your collection) |
| **Pulls** | Every pack you've logged, with prices |
| **Dex** | Species you've seen |
| **Cards** | Your collection — search, adjust quantities, remove |
| **Battle** | Pack battles against randoms, a bot or a friend |

Live scan on the phone is tap-to-catch: Expo can't analyse camera frames the
way the web app's auto-fire does, so you tap **CATCH** for each card. Everything
else about the session (duplicate prompts, the code card, background
identification, recovering an expired session) matches the web flow.

The analyst/admin dashboard stays web-only.

## Run it

```bash
npm ci
npx expo start
```

Open it in **Expo Go** (every native module the app uses ships in Expo Go, so no
custom dev build is needed), or press `i` / `a` for a simulator.

### Pointing it at a backend

- **Development:** leave `EXPO_PUBLIC_API_BASE` unset. The app calls port 8000
  on the machine serving the Expo bundle — run the backend from the repo root
  with `uvicorn app.main:app --host 0.0.0.0 --port 8000` and
  `COOKIE_SECURE=false` (plain http). Phone and computer must share a network.
- **Anything else:** set `EXPO_PUBLIC_API_BASE` (see `.env.example`), e.g. your
  Railway URL. It's inlined at bundle time, so restart with `npx expo start -c`
  after changing it.

Login uses the backend's normal session cookie; the OS keeps it, so trainers
stay logged in between launches.

## Checks

```bash
npm run typecheck
npm run lint
npm run export:check   # bundles iOS + Android JS with Metro (no native toolchain)
```

## Building for the stores

Builds run on [EAS](https://docs.expo.dev/eas/) — no Xcode or Android Studio
needed:

```bash
npx eas-cli@latest login
# the backend URL the build should talk to, per EAS environment
npx eas-cli@latest env:create --name EXPO_PUBLIC_API_BASE --value https://your-backend.example.com --environment preview
npx eas-cli@latest env:create --name EXPO_PUBLIC_API_BASE --value https://your-backend.example.com --environment production
npx eas-cli@latest build --profile preview --platform android   # installable APK
npx eas-cli@latest build --profile production --platform all
```

Bundle ids (`com.packscanner.app`) are in `app.json` — change them to ones you
own before submitting.

## Layout

```
src/app/            routes (Expo Router): tabs, login, trainer card, scan flows
src/features/       capture, review, live session engine, scan bits
src/ui/             Pixel Pop kit (buttons, panels, badges, HP bar, …)
src/lib/            API config, auth, image prep
../shared/src/      API client + types + copy + theme shared with the web app
```
