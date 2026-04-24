import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.baysoko.marketplace',
  appName: 'Baysoko',
  webDir: 'www',
  server: {
    url: 'https://baysoko.up.railway.app/?source=android_app&shell=capacitor',
    cleartext: false,
    allowNavigation: [
      'baysoko.up.railway.app',
      '*.up.railway.app',
      'accounts.google.com',
      '*.google.com',
      '*.google.co.*',
      '*.googleusercontent.com',
      '*.gstatic.com',
      '*.apis.google.com',
      '*.firebaseapp.com',
      '*.googleapis.com',
      '*.firebase.com'
    ]
  },
  android: {
    allowMixedContent: false,
    captureInput: true,
    webContentsDebuggingEnabled: false
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 900,
      backgroundColor: '#FF6B35',
      showSpinner: false
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#ff6b35',
      overlaysWebView: false
    },
    LocalNotifications: {
      smallIcon: 'ic_stat_baysoko',
      iconColor: '#FF6B35'
    }
  }
};

export default config;
