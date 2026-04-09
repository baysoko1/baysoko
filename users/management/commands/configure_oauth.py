# users/management/commands/configure_oauth.py
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
    help = 'Configure OAuth settings for production'

    def handle(self, *args, **options):
        self.stdout.write("⚙️  Configuring OAuth settings...")

        site_url = (getattr(settings, 'SITE_URL', '') or os.environ.get('SITE_URL') or 'https://baysoko.up.railway.app').strip().rstrip('/')
        site_domain = site_url.replace('https://', '').replace('http://', '').strip('/')
        
        # 1. Configure the Site
        site = Site.objects.get_current()
        site.domain = site_domain
        site.name = 'Baysoko Marketplace'
        site.save()
        
        self.stdout.write(f"✅ Site configured: {site.domain}")
        
        # 2. Configure Google OAuth
        google_config = self.configure_provider('google', 'Google')
        
        # 3. Configure Facebook OAuth
        facebook_config = self.configure_provider('facebook', 'Facebook')
        
        self.stdout.write("\n" + "="*50)
        self.stdout.write("✅ OAuth Configuration Complete!")
        self.stdout.write("="*50)
        
        # Each provider has its own callback path — they must be registered
        # in the correct console. Google uses /accounts/google/callback/ and
        # Facebook uses /accounts/facebook/callback/.
        self.stdout.write("\n📋 Redirect URIs to configure in provider dashboards:")
        self.stdout.write("-" * 50)
        self.stdout.write("Google OAuth Console  (console.cloud.google.com):")
        self.stdout.write(f"  ✅ Authorized redirect URI: {site_url}/accounts/google/callback/")
        self.stdout.write("")
        self.stdout.write("Facebook Developer Console  (developers.facebook.com):")
        self.stdout.write(f"  ✅ Valid OAuth Redirect URI: {site_url}/accounts/facebook/callback/")
        
        self.stdout.write("\n💡 Tip: Make sure these URIs are EXACTLY as shown above.")
        self.stdout.write("⚠️  Do NOT use the Facebook callback URL in the Google Console, or vice versa.")
        
    def configure_provider(self, provider, display_name):
        """Configure a single OAuth provider"""
        self.stdout.write(f"\n🔧 Configuring {display_name} OAuth...")
        
        client_id = os.environ.get(f'{provider.upper()}_OAUTH_CLIENT_ID')
        secret = os.environ.get(f'{provider.upper()}_OAUTH_CLIENT_SECRET')
        
        if not client_id or not secret:
            self.stdout.write(f"❌ {display_name} credentials not found in environment")
            self.stdout.write(f"   Set {provider.upper()}_OAUTH_CLIENT_ID and {provider.upper()}_OAUTH_CLIENT_SECRET")
            return False
        
        try:
            site = Site.objects.get_current()
            app, created = SocialApp.objects.update_or_create(
                provider=provider,
                defaults={
                    'name': display_name,
                    'client_id': client_id,
                    'secret': secret,
                }
            )
            app.sites.set([site])
            
            self.stdout.write(f"✅ {display_name} OAuth {'created' if created else 'updated'}")
            return True
        except Exception as e:
            self.stdout.write(f"❌ Error configuring {display_name}: {str(e)}")
            return False
