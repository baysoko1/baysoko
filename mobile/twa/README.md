# Baysoko Trusted Web Activity

This folder documents the Trusted Web Activity path for Baysoko.

## Why TWA

- Play Store-friendly web app packaging
- Better alignment with PWA installability
- Lower maintenance than a full native rewrite

## Server-side prerequisites

Already available in the Django app:

- `manifest.json`
- `service-worker.js`
- `/.well-known/assetlinks.json`

## Required Railway env vars

```env
SITE_URL=https://baysoko.up.railway.app
ANDROID_APP_PACKAGE=com.baysoko.marketplace
ANDROID_APP_SHA256=YOUR_RELEASE_CERT_SHA256
```

## Expected asset links endpoint

```text
https://baysoko.up.railway.app/.well-known/assetlinks.json
```

## Suggested next TWA toolchain

- Bubblewrap
- Android Studio
- Play App Signing / release keystore

## Bubblewrap high-level flow

1. Install Bubblewrap
2. Initialize a TWA project pointing to `https://baysoko.up.railway.app`
3. Use the same package id configured in `ANDROID_APP_PACKAGE`
4. Generate signing certificate fingerprint
5. Set `ANDROID_APP_SHA256` on Railway
6. Regenerate / verify `assetlinks.json`
7. Build AAB/APK for Play distribution
