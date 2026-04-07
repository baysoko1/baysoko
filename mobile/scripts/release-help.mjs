console.log(`
Baysoko Android release checklist

1. npm install
2. npx cap add android
3. npm run android:prepare
4. npm run android:open
5. In Android Studio:
   - let Gradle sync
   - set app icon / splash assets
   - Build > Generate Signed Bundle / APK
6. Export SHA256 signing fingerprint
7. Set Railway env:
   ANDROID_APP_PACKAGE=com.baysoko.marketplace
   ANDROID_APP_SHA256=<release sha256>
8. Verify:
   https://baysoko.up.railway.app/.well-known/assetlinks.json
`);
