# Baysoko Android APK

This directory contains the Android wrapper workspace for Baysoko using Capacitor.

## Why this approach

- Keeps the existing Django app as the single source of truth
- Ships an installable Android APK quickly
- Preserves login, marketplace, delivery, notifications UI, and PWA behavior
- Lets us incrementally add native Android capabilities later

## What is included now

- Capacitor Android wrapper configuration
- Live remote-hosted Baysoko app target
- Android-ready package/app id defaults
- TWA preparation support through `/.well-known/assetlinks.json`
- A path for Android Studio APK generation now
- A path for Trusted Web Activity / Play Store hardening next

## Prerequisites

- Node.js 20+
- Android Studio
- Android SDK / Gradle
- Live HTTPS deployment of Baysoko

## Current remote app target

- `https://baysoko.up.railway.app/?source=android_app`

## Setup

From the `mobile` directory:

```bash
npm install
npx cap add android
npx cap sync android
```

Then open Android Studio:

```bash
npx cap open android
```

Or use the prepared helper flow:

```bash
npm install
npm run android:init
npm run android:prepare
npm run android:doctor
npm run android:open
```

## Android polish plan

Use the generated Android project to add the following polish items:

1. Splash screen:
   - already configured in `capacitor.config.ts`
2. Back button behavior:
   - automated patch script now adds WebView-history back navigation before app exit
3. File uploads / camera:
   - patch script adds the common Android permissions needed for camera/media upload flows
4. Status bar / app chrome:
   - already configured in `capacitor.config.ts`
5. Offline behavior:
   - the existing Baysoko service worker remains the first offline layer
 6. Soft keyboard behavior:
   - patch script enables `adjustResize` on the main activity

## Current native helper scripts

- `npm run android:doctor`
- `npm run android:prepare`
- `npm run android:release:help`
- `npm run twa:help`

## Trusted Web Activity path

If you want a more Play Store-native web app package, use a TWA next.

Requirements:

- Production HTTPS domain
- Working PWA manifest
- Service worker
- Android Digital Asset Links

This repo now exposes:

- `https://baysoko.up.railway.app/.well-known/assetlinks.json`

To activate it, set:

```env
ANDROID_APP_PACKAGE=com.baysoko.marketplace
ANDROID_APP_SHA256=YOUR_RELEASE_CERT_SHA256
```

The SHA256 can be a comma-separated list if you have multiple signing certs.

## Build APK

In Android Studio:

1. Open the generated Android project
2. Let Gradle sync
3. Use `Build > Build Bundle(s) / APK(s) > Build APK(s)`

Debug APK output is usually under:

- `android/app/build/outputs/apk/debug/`

For signed release output:

1. `Build > Generate Signed Bundle / APK`
2. choose `APK` or `Android App Bundle`
3. create or select your keystore
4. note the SHA256 release fingerprint for TWA / App Links verification

## Recommended env and production assumptions

- `SITE_URL` should stay `https://baysoko.up.railway.app`
- HTTPS must remain enabled
- Google OAuth callback remains:
  - `https://baysoko.up.railway.app/accounts/google/callback/`

## Notes

- This wrapper currently uses the live deployed web app inside an Android shell.
- Push notifications and deeper Android integrations can be added later through Capacitor plugins.
- If you want Play Store-grade app-links / full-screen web integration, we can later move this to a Trusted Web Activity or a fuller native shell.
