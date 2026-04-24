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
      '<uses-permission android:name="android.permission.READ_MEDIA_IMAGES" />',
      '<uses-permission android:name="android.permission.READ_MEDIA_VIDEO" />',
      '<uses-permission android:name="android.permission.READ_MEDIA_VISUAL_USER_SELECTED" />'
    ];
    for (const permission of permissions) {
      if (!out.includes(permission)) {
        out = out.replace('<application', `${permission}\n\n    <application`);
      }
    }
    if (!out.includes('android:usesCleartextTraffic="false"')) {
      out = out.replace('<application', '<application android:usesCleartextTraffic="false"');
    }
    if (!out.includes('android:hardwareAccelerated="true"')) {
      out = out.replace('<application', '<application android:hardwareAccelerated="true"');
    }
    if (!out.includes('android:largeHeap="true"')) {
      out = out.replace('<application', '<application android:largeHeap="true"');
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
    if ((src.includes('ActivityManager.TaskDescription')
      && src.includes('BaysokoAndroidApp/1.0')
      && src.includes('Press back again to exit Baysoko'))
      || src.includes('SwipeRefreshLayout')) {
      return src;
    }
    return `package com.baysoko.marketplace;\n\nimport com.getcapacitor.BridgeActivity;\nimport android.app.ActivityManager;\nimport android.os.Bundle;\nimport android.view.View;\nimport android.view.ViewGroup;\nimport android.graphics.Color;\nimport android.os.SystemClock;\nimport android.webkit.CookieManager;\nimport android.webkit.WebSettings;\nimport android.webkit.WebView;\nimport android.widget.Toast;\nimport android.content.res.Configuration;\nimport androidx.activity.OnBackPressedCallback;\nimport androidx.activity.EdgeToEdge;\nimport androidx.core.graphics.Insets;\nimport androidx.core.splashscreen.SplashScreen;\nimport androidx.core.view.ViewCompat;\nimport androidx.core.view.WindowCompat;\nimport androidx.core.view.WindowInsetsCompat;\nimport androidx.core.view.WindowInsetsControllerCompat;\n\npublic class MainActivity extends BridgeActivity {\n    private long lastBackPressedAt = 0L;\n\n    @Override\n    protected void onCreate(Bundle savedInstanceState) {\n        SplashScreen.installSplashScreen(this);\n        EdgeToEdge.enable(this);\n        super.onCreate(savedInstanceState);\n\n        try {\n            updateSystemBarsTheme();\n        } catch (Exception ignored) {}\n        setTitle(getString(R.string.app_name));\n        try {\n            setTaskDescription(new ActivityManager.TaskDescription(getString(R.string.app_name), null, Color.parseColor(\"#FF6B35\")));\n        } catch (Exception ignored) {}\n\n        WebView webView = (WebView) bridge.getWebView();\n        if (webView != null) {\n            WebSettings settings = webView.getSettings();\n            settings.setDomStorageEnabled(true);\n            settings.setDatabaseEnabled(true);\n            settings.setJavaScriptCanOpenWindowsAutomatically(true);\n            settings.setMediaPlaybackRequiresUserGesture(false);\n            settings.setGeolocationEnabled(true);\n            settings.setSupportZoom(false);\n            settings.setSupportMultipleWindows(false);\n            settings.setBuiltInZoomControls(false);\n            settings.setDisplayZoomControls(false);\n            settings.setLoadWithOverviewMode(true);\n            settings.setUseWideViewPort(true);\n            settings.setAllowFileAccess(false);\n            settings.setAllowFileAccessFromFileURLs(false);\n            settings.setAllowUniversalAccessFromFileURLs(false);\n            settings.setAllowContentAccess(true);\n            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);\n            settings.setCacheMode(WebSettings.LOAD_DEFAULT);\n            String defaultAgent = settings.getUserAgentString();\n            String customAgent = defaultAgent.replaceAll(\";\\\\s*wv\", \"\")\n                                           .replaceAll(\"Version\\\\/\\\\d+\\\\.\\\\d+\\\\s?\", \"\")\n                                           + \" BaysokoAndroidApp/1.0\";\n            settings.setUserAgentString(customAgent);\n\n            CookieManager cookieManager = CookieManager.getInstance();\n            cookieManager.setAcceptCookie(true);\n            cookieManager.setAcceptThirdPartyCookies(webView, true);\n            try {\n                cookieManager.flush();\n            } catch (Exception ignored) {}\n\n            webView.setBackgroundColor(Color.parseColor(\"#0F1115\"));\n            webView.setOverScrollMode(View.OVER_SCROLL_NEVER);\n            webView.setVerticalScrollBarEnabled(false);\n            webView.setHorizontalScrollBarEnabled(false);\n\n            ViewCompat.setOnApplyWindowInsetsListener(webView, (v, insets) -> {\n                Insets systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());\n                try {\n                    ViewGroup.LayoutParams layoutParams = v.getLayoutParams();\n                    if (layoutParams instanceof ViewGroup.MarginLayoutParams params) {\n                        params.topMargin = systemBars.top;\n                        params.bottomMargin = systemBars.bottom;\n                        params.leftMargin = systemBars.left;\n                        params.rightMargin = systemBars.right;\n                        v.setLayoutParams(params);\n                    } else {\n                        v.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom);\n                    }\n                } catch (Exception ignored) {\n                    v.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom);\n                }\n                return WindowInsetsCompat.CONSUMED;\n            });\n        }\n\n        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {\n            @Override\n            public void handleOnBackPressed() {\n                if (bridge != null && bridge.getWebView() != null && bridge.getWebView().canGoBack()) {\n                    bridge.getWebView().goBack();\n                } else {\n                    long now = SystemClock.elapsedRealtime();\n                    if (now - lastBackPressedAt < 1800L) {\n                        setEnabled(false);\n                        getOnBackPressedDispatcher().onBackPressed();\n                        return;\n                    }\n                    lastBackPressedAt = now;\n                    Toast.makeText(MainActivity.this, \"Press back again to exit Baysoko\", Toast.LENGTH_SHORT).show();\n                }\n            }\n        });\n    }\n\n    private void updateSystemBarsTheme() {\n        int nightModeFlags = getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK;\n        boolean isDarkMode = nightModeFlags == Configuration.UI_MODE_NIGHT_YES;\n        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());\n        if (controller == null) return;\n        if (isDarkMode) {\n            getWindow().setStatusBarColor(Color.parseColor(\"#0f0f10\"));\n            getWindow().setNavigationBarColor(Color.parseColor(\"#0f0f10\"));\n            controller.setAppearanceLightStatusBars(false);\n            controller.setAppearanceLightNavigationBars(false);\n        } else {\n            getWindow().setStatusBarColor(Color.parseColor(\"#ff6b35\"));\n            getWindow().setNavigationBarColor(Color.parseColor(\"#111827\"));\n            controller.setAppearanceLightStatusBars(false);\n            controller.setAppearanceLightNavigationBars(false);\n        }\n    }\n\n    @Override\n    public void onConfigurationChanged(Configuration newConfig) {\n        super.onConfigurationChanged(newConfig);\n        updateSystemBarsTheme();\n    }\n}\n`;
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
    if (out.includes('@drawable/splash')) {
      out = out.replace('@drawable/splash', '@drawable/ic_baysoko_brand_white');
    }
    if (out.includes('@mipmap/ic_launcher_foreground')) {
      out = out.replace('@mipmap/ic_launcher_foreground', '@drawable/ic_baysoko_brand_white');
    }
    if (out.includes('windowSplashScreenBackground">#ff6b35')) {
      out = out.replace('windowSplashScreenBackground">#ff6b35', 'windowSplashScreenBackground">@color/splashBackground');
    }
    if (out.includes('windowSplashScreenBackground">@color/colorSurfaceDark')) {
      out = out.replace('windowSplashScreenBackground">@color/colorSurfaceDark', 'windowSplashScreenBackground">@color/splashBackground');
    }
    if (out.includes('windowSplashScreenAnimatedIcon">@drawable/ic_baysoko_brand</item>')) {
      out = out.replace('windowSplashScreenAnimatedIcon">@drawable/ic_baysoko_brand</item>', 'windowSplashScreenAnimatedIcon">@drawable/ic_baysoko_brand_white</item>');
    }
    if (out.includes('windowSplashScreenAnimationDuration">1000')) {
      out = out.replace('windowSplashScreenAnimationDuration">1000', 'windowSplashScreenAnimationDuration">700');
    }
    if (out.includes('postSplashScreenTheme">@style/AppTheme</item>')) {
      out = out.replace('postSplashScreenTheme">@style/AppTheme</item>', 'postSplashScreenTheme">@style/AppTheme.NoActionBar</item>');
    }
    return out;
  });
}

const launcherBgFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'values', 'ic_launcher_background.xml')));
for (const file of launcherBgFiles) {
  patchFile(file, (src) => src.replace('#FFFFFF', '#00000000').replace('#FF6B35', '#00000000'));
}

const launcherFiles = walk(androidDir, (p) => p.endsWith('ic_launcher.xml') || p.endsWith('ic_launcher_round.xml'));
for (const file of launcherFiles) {
  patchFile(file, (src) => {
    let out = src
      .replace('@mipmap/ic_launcher_foreground', '@drawable/ic_baysoko_brand');
    if (!out.includes('<monochrome')) {
      out = out.replace('</adaptive-icon>', '    <monochrome android:drawable="@drawable/ic_baysoko_brand"/>\n</adaptive-icon>');
    }
    return out;
  });
}

const stringFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'values', 'strings.xml')));
for (const file of stringFiles) {
  patchFile(file, (src) => src
    .replace('<string name="app_name">Baysoko</string>', '<string name="app_name">Baysoko Marketplace</string>')
    .replace('<string name="title_activity_main">Baysoko</string>', '<string name="title_activity_main">Baysoko Marketplace</string>')
  );
}

const brandDrawableFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'drawable', 'ic_baysoko_brand.xml')));
if (!brandDrawableFiles.length) {
  const brandDrawablePath = path.join(androidDir, 'app', 'src', 'main', 'res', 'drawable', 'ic_baysoko_brand.xml');
  fs.mkdirSync(path.dirname(brandDrawablePath), { recursive: true });
  fs.writeFileSync(
    brandDrawablePath,
    `<?xml version="1.0" encoding="utf-8"?>\n<vector xmlns:android="http://schemas.android.com/apk/res/android"\n    android:width="108dp"\n    android:height="108dp"\n    android:viewportWidth="108"\n    android:viewportHeight="108">\n    <path\n        android:fillColor="#00000000"\n        android:pathData="M54,12 L84,34 L54,96 L24,34 Z"\n        android:strokeColor="#FF6B35"\n        android:strokeLineCap="round"\n        android:strokeLineJoin="round"\n        android:strokeWidth="7" />\n    <path\n        android:fillColor="#00000000"\n        android:pathData="M39,34 L69,34"\n        android:strokeColor="#FF6B35"\n        android:strokeLineCap="round"\n        android:strokeLineJoin="round"\n        android:strokeWidth="7" />\n    <path\n        android:fillColor="#00000000"\n        android:pathData="M54,12 L39,34 L54,96 L69,34 Z"\n        android:strokeColor="#FF6B35"\n        android:strokeLineCap="round"\n        android:strokeLineJoin="round"\n        android:strokeWidth="4.5" />\n</vector>\n`,
    'utf8'
  );
  console.log(`patched: ${path.relative(root, brandDrawablePath)}`);
}

const whiteBrandDrawableFiles = walk(androidDir, (p) => p.endsWith(path.join('res', 'drawable', 'ic_baysoko_brand_white.xml')));
if (!whiteBrandDrawableFiles.length) {
  const whiteBrandDrawablePath = path.join(androidDir, 'app', 'src', 'main', 'res', 'drawable', 'ic_baysoko_brand_white.xml');
  fs.mkdirSync(path.dirname(whiteBrandDrawablePath), { recursive: true });
  fs.writeFileSync(
    whiteBrandDrawablePath,
    `<?xml version="1.0" encoding="utf-8"?>\n<vector xmlns:android="http://schemas.android.com/apk/res/android"\n    android:width="108dp"\n    android:height="108dp"\n    android:viewportWidth="108"\n    android:viewportHeight="108">\n    <path\n        android:fillColor="#00000000"\n        android:pathData="M54,12 L84,34 L54,96 L24,34 Z"\n        android:strokeColor="#FFFFFFFF"\n        android:strokeLineCap="round"\n        android:strokeLineJoin="round"\n        android:strokeWidth="7" />\n</vector>\n`,
    'utf8'
  );
  console.log(`patched: ${path.relative(root, whiteBrandDrawablePath)}`);
}

console.log('Android patching complete.');

