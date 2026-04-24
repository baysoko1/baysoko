# OAuth Setup Guide — Baysoko

This document records the exact redirect URIs that must be registered in each
OAuth provider's developer console. Each provider has its **own** callback
path — they are not interchangeable.

---

## Redirect URIs

| Provider | Console | Authorized Redirect URI |
|---|---|---|
| **Google** | [console.cloud.google.com](https://console.cloud.google.com) | `https://baysoko.up.railway.app/accounts/google/callback/` |
| **Facebook** | [developers.facebook.com](https://developers.facebook.com) | `https://baysoko.up.railway.app/accounts/facebook/callback/` |

> ⚠️ **Common mistake:** Do NOT register the Facebook callback URL
> (`/accounts/facebook/callback/`) in the Google OAuth Console. Google will
> reject the request with a `redirect_uri_mismatch` error because the URI it
> sends during the OAuth flow (`/accounts/google/callback/`) will not match
> what is registered.

---

## Google OAuth Console — step-by-step

1. Go to **APIs & Services → Credentials** in the [Google Cloud Console](https://console.cloud.google.com).
2. Open the OAuth 2.0 Client ID used by Baysoko.
3. Under **Authorized redirect URIs**, ensure the following URI is present
   (and only this URI for the Google provider):

   ```
   https://baysoko.up.railway.app/accounts/google/callback/
   ```

4. Save. Changes propagate within a few minutes.

---

## Facebook Developer Console — step-by-step

1. Go to your app in the [Facebook Developer Console](https://developers.facebook.com).
2. Navigate to **Facebook Login → Settings**.
3. Under **Valid OAuth Redirect URIs**, ensure the following URI is present:

   ```
   https://baysoko.up.railway.app/accounts/facebook/callback/
   ```

4. Save changes.

---

## Verifying the configuration

Run the management command to confirm the app's expected callback URLs match
what is registered in each console:

```bash
python manage.py verify_oauth
```

To re-apply the database-side OAuth app configuration (client IDs, secrets,
and site association):

```bash
python manage.py configure_oauth
# or
python manage.py setup_social_apps
```

---

## Environment variables required

| Variable | Description |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Client ID from Google Cloud Console |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Client secret from Google Cloud Console |
| `FACEBOOK_OAUTH_CLIENT_ID` | App ID from Facebook Developer Console |
| `FACEBOOK_OAUTH_CLIENT_SECRET` | App secret from Facebook Developer Console |
| `SITE_URL` | Public base URL of the deployment (e.g. `https://baysoko.up.railway.app`) |
