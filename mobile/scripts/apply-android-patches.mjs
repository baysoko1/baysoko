import fs from 'fs';
import path from 'path';

const root = process.cwd();
const androidDir = path.join(root, 'android');

function exists(p) {
  return fs.existsSync(p);
}

function walk(dir, matcher) {
  const out = [];
  if (!exists(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, matcher));
    else if (matcher(full)) out.push(full);
  }
  return out;
}

function patchFile(file, transform) {
  const original = fs.readFileSync(file, 'utf8');
  const updated = transform(original);
  if (updated !== original) {
    fs.writeFileSync(file, updated, 'utf8');
    console.log(`patched: ${path.relative(root, file)}`);
  }
}

if (!exists(androidDir)) {
  console.log('android/ not found yet. Run `npx cap add android` first.');
  process.exit(0);
}

const manifestFiles = walk(androidDir, (p) => p.endsWith('AndroidManifest.xml'));
for (const manifestFile of manifestFiles) {
  patchFile(manifestFile, (src) => {
    let out = src;
    const permissions = [
      '<uses-permission android:name="android.permission.INTERNET" />',
      '<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />',
      '<uses-permission android:name="android.permission.CAMERA" />',
      '<uses-permission android:name="android.permission.READ_MEDIA_IMAGES" />'
    ];
    for (const permission of permissions) {
      if (!out.includes(permission)) {
        out = out.replace('<application', `${permission}\n\n    <application`);
      }
    }
    if (!out.includes('android:usesCleartextTraffic="false"')) {
      out = out.replace('<application', '<application android:usesCleartextTraffic="false"');
    }
    if (!out.includes('android:windowSoftInputMode="adjustResize"')) {
      out = out.replace(/<activity([^>]+MainActivity[^>]*)>/, '<activity$1 android:windowSoftInputMode="adjustResize">');
    }
    if (!out.includes('android:autoVerify="true"')) {
      out = out.replace(
        '</intent-filter>\n\n        </activity>',
        `</intent-filter>\n\n            <intent-filter android:autoVerify="true">\n                <action android:name="android.intent.action.VIEW" />\n                <category android:name="android.intent.category.DEFAULT" />\n                <category android:name="android.intent.category.BROWSABLE" />\n                <data android:scheme="https" android:host="baysoko.up.railway.app" />\n            </intent-filter>\n\n        </activity>`
      );
    }
    return out;
  });
}

const kotlinActivities = walk(androidDir, (p) => p.endsWith('MainActivity.kt'));
for (const file of kotlinActivities) {
  patchFile(file, (src) => {
    let out = src;
    if (!out.includes('import androidx.activity.OnBackPressedCallback')) {
      out = out.replace(
        'import com.getcapacitor.BridgeActivity',
        'import com.getcapacitor.BridgeActivity\nimport androidx.activity.OnBackPressedCallback'
      );
    }
    if (!out.includes('onBackPressedDispatcher.addCallback')) {
      out = out.replace(
        /class MainActivity : BridgeActivity\(\) \{\s*/m,
        `class MainActivity : BridgeActivity() {\n    override fun onStart() {\n        super.onStart()\n        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {\n            override fun handleOnBackPressed() {\n                val webView = bridge?.webView\n                if (webView != null && webView.canGoBack()) {\n                    webView.goBack()\n                } else {\n                    isEnabled = false\n                    onBackPressedDispatcher.onBackPressed()\n                }\n            }\n        })\n    }\n\n`
      );
    }
    return out;
  });
}

const javaActivities = walk(androidDir, (p) => p.endsWith('MainActivity.java'));
for (const file of javaActivities) {
  patchFile(file, (src) => {
    if (src.includes('BaysokoAndroidApp/1.0') && src.includes('Press back again to exit Baysoko')) {
      return src;
    }
    return `package com.baysoko.marketplace;\n\nimport com.getcapacitor.BridgeActivity;\nimport android.os.Bundle;\nimport android.view.View;\nimport android.graphics.Color;\nimport android.os.SystemClock;\nimport android.webkit.CookieManager;\nimport android.webkit.WebSettings;\nimport android.widget.Toast;\nimport androidx.activity.OnBackPressedCallback;\nimport androidx.activity.EdgeToEdge;\nimport androidx.core.graphics.Insets;\nimport androidx.core.splashscreen.SplashScreen;\nimport androidx.core.view.ViewCompat;\nimport androidx.core.view.WindowCompat;\nimport androidx.core.view.WindowInsetsCompat;\n\npublic class MainActivity extends BridgeActivity {\n    private long lastBackPressedAt = 0L;\n\n    @Override\n    protected void onCreate(Bundle savedInstanceState) {\n        SplashScreen.installSplashScreen(this);\n        EdgeToEdge.enable(this);\n        super.onCreate(savedInstanceState);\n\n        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);\n        getWindow().setStatusBarColor(Color.parseColor(\"#FF6B35\"));\n        getWindow().setNavigationBarColor(Color.parseColor(\"#111827\"));\n\n        View webView = bridge.getWebView();\n        if (webView != null) {\n            WebSettings settings = bridge.getWebView().getSettings();\n            settings.setDomStorageEnabled(true);\n            settings.setDatabaseEnabled(true);\n            settings.setJavaScriptCanOpenWindowsAutomatically(true);\n            settings.setMediaPlaybackRequiresUserGesture(false);\n            settings.setSupportZoom(false);\n            settings.setBuiltInZoomControls(false);\n            settings.setDisplayZoomControls(false);\n            settings.setLoadWithOverviewMode(true);\n            settings.setUseWideViewPort(true);\n            settings.setAllowFileAccess(false);\n            settings.setAllowContentAccess(true);\n            settings.setUserAgentString(settings.getUserAgentString() + \" BaysokoAndroidApp/1.0\");\n\n            CookieManager cookieManager = CookieManager.getInstance();\n            cookieManager.setAcceptCookie(true);\n            cookieManager.setAcceptThirdPartyCookies(bridge.getWebView(), true);\n\n            webView.setOverScrollMode(View.OVER_SCROLL_NEVER);\n            webView.setVerticalScrollBarEnabled(false);\n            webView.setHorizontalScrollBarEnabled(false);\n\n            ViewCompat.setOnApplyWindowInsetsListener(webView, (v, insets) -> {\n                Insets systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());\n                v.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom);\n                return WindowInsetsCompat.CONSUMED;\n            });\n        }\n\n        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {\n            @Override\n            public void handleOnBackPressed() {\n                if (bridge != null && bridge.getWebView() != null && bridge.getWebView().canGoBack()) {\n                    bridge.getWebView().goBack();\n                } else {\n                    long now = SystemClock.elapsedRealtime();\n                    if (now - lastBackPressedAt < 1800L) {\n                        setEnabled(false);\n                        getOnBackPressedDispatcher().onBackPressed();\n                        return;\n                    }\n                    lastBackPressedAt = now;\n                    Toast.makeText(MainActivity.this, \"Press back again to exit Baysoko\", Toast.LENGTH_SHORT).show();\n                }\n            }\n        });\n    }\n}\n`;
  });
}

const colorsFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'values', 'colors.xml')));
for (const file of colorsFiles) {
  patchFile(file, (src) => {
    let out = src;
    if (!out.includes('<color name="colorSurfaceDark">#111827</color>')) {
      out = out.replace('</resources>', '    <color name="colorSurfaceDark">#111827</color>\n    <color name="colorStatusBar">#ff6b35</color>\n</resources>');
    }
    return out;
  });
}

const styleFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'values', 'styles.xml')));
for (const file of styleFiles) {
  patchFile(file, (src) => {
    let out = src;
    if (!out.includes('android:navigationBarColor')) {
      out = out.replace(
        '<item name="colorAccent">@color/colorAccent</item>',
        '<item name="colorAccent">@color/colorAccent</item>\n        <item name="android:statusBarColor">@color/colorStatusBar</item>\n        <item name="android:navigationBarColor">@color/colorSurfaceDark</item>\n        <item name="android:windowLightStatusBar">false</item>\n        <item name="android:windowLightNavigationBar">false</item>'
      );
    }
    if (!out.includes('<item name="android:navigationBarColor">@color/colorSurfaceDark</item>')) {
      out = out.replace(
        '<item name="android:background">@null</item>',
        '<item name="android:background">@null</item>\n        <item name="android:statusBarColor">@color/colorStatusBar</item>\n        <item name="android:navigationBarColor">@color/colorSurfaceDark</item>'
      );
    }
    return out;
  });
}

console.log('Android patching complete.');
