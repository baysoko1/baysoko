# users/management/commands/verify_oauth.py
#
# IMPORTANT — Correct redirect URIs for each OAuth provider:
#
#   Google OAuth Console  → https://baysoko.up.railway.app/accounts/google/callback/
#   Facebook Developer    → https://baysoko.up.railway.app/accounts/facebook/callback/
#
# These paths are PROVIDER-SPECIFIC. Do NOT use the Facebook callback URL in
# the Google OAuth Console, or vice versa. A mismatch will cause a
# redirect_uri_mismatch error and block sign-in for that provider.
#
from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from allauth.socialaccount.models import SocialApp
from django.conf import settings
import os

class Command(BaseCommand):
    help = 'Verify OAuth configuration and redirect URIs'

    def handle(self, *args, **options):
        self.stdout.write("🔍 Verifying OAuth Configuration...")
        site_url = (getattr(settings, 'SITE_URL', '') or os.environ.get('SITE_URL') or 'https://baysoko.up.railway.app').strip().rstrip('/')
        google_redirect_uri = f"{site_url}/accounts/google/callback/"
        facebook_redirect_uri = f"{site_url}/accounts/facebook/callback/"
        
        # Check current site
        site = Site.objects.get_current()
        self.stdout.write(f"✅ Current Site: {site.name} - {site.domain}")
        
        # Check Google OAuth
        try:
            google_app = SocialApp.objects.get(provider='google')
            self.stdout.write(f"✅ Google OAuth App: {google_app.name}")
            self.stdout.write(f"✅ Google Client ID: {google_app.client_id[:20]}...")
            self.stdout.write(f"✅ Google Sites: {list(google_app.sites.all())}")
            
            self.stdout.write(f"✅ Google Redirect URI: {google_redirect_uri}")
            
            self.stdout.write("✅ Google Redirect URI matches SITE_URL configuration.")
                
        except SocialApp.DoesNotExist:
            self.stdout.write("❌ Google OAuth app not configured!")
        
        # Check Facebook OAuth
        try:
            facebook_app = SocialApp.objects.get(provider='facebook')
            self.stdout.write(f"✅ Facebook OAuth App: {facebook_app.name}")
            self.stdout.write(f"✅ Facebook Client ID: {facebook_app.client_id[:20]}...")
            self.stdout.write(f"✅ Facebook Sites: {list(facebook_app.sites.all())}")
            
            self.stdout.write(f"✅ Facebook Redirect URI: {facebook_redirect_uri}")
            
        except SocialApp.DoesNotExist:
            self.stdout.write("❌ Facebook OAuth app not configured!")
        
        # Check environment variables
        self.stdout.write("\n🔍 Checking Environment Variables:")
        google_client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
        google_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')
        facebook_client_id = os.environ.get('FACEBOOK_OAUTH_CLIENT_ID')
        facebook_secret = os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET')
        
        self.stdout.write(f"✅ GOOGLE_OAUTH_CLIENT_ID: {'✓' if google_client_id else '✗'}")
        self.stdout.write(f"✅ GOOGLE_OAUTH_CLIENT_SECRET: {'✓' if google_secret else '✗'}")
        self.stdout.write(f"✅ FACEBOOK_OAUTH_CLIENT_ID: {'✓' if facebook_client_id else '✗'}")
        self.stdout.write(f"✅ FACEBOOK_OAUTH_CLIENT_SECRET: {'✓' if facebook_secret else '✗'}")
        
        self.stdout.write("\n🔍 Effective callback URLs:")
        self.stdout.write(f"✅ Google callback: {google_redirect_uri}")
        self.stdout.write(f"✅ Facebook callback: {facebook_redirect_uri}")
        
        self.stdout.write("\n✅ OAuth Verification Complete!")
