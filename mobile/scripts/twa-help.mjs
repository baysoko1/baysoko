console.log(`
Baysoko Trusted Web Activity flow

1. Keep SITE_URL=https://baysoko.up.railway.app
2. Set Android env on Railway:
   ANDROID_APP_PACKAGE=com.baysoko.marketplace
   ANDROID_APP_SHA256=<release sha256>
3. Verify:
   https://baysoko.up.railway.app/.well-known/assetlinks.json
4. Use Bubblewrap with:
   mobile/twa/bubblewrap-config.example.json
5. Generate Android App Bundle / APK for Play release
`);
