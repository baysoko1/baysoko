# users/management/commands/setup_social_apps.py - Updated version
from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from allauth.socialaccount.models import SocialApp
from django.conf import settings
import os

class Command(BaseCommand):
    help = 'Setup social apps for OAuth authentication'

    def handle(self, *args, **options):
        self.stdout.write("🚀 Setting up OAuth for production...")
        site_url = (getattr(settings, 'SITE_URL', '') or os.environ.get('SITE_URL') or 'https://baysoko.up.railway.app').strip().rstrip('/')
        site_domain = site_url.replace('https://', '').replace('http://', '').strip('/')
        
        # Get or create the site
        site, created = Site.objects.get_or_create(
            id=1,
            defaults={
                'domain': site_domain,
                'name': 'Baysoko Marketplace'
            }
        )
        
        if created:
            self.stdout.write(f"✅ Created new site: {site.name}")
        else:
            site.domain = site_domain
            site.name = 'Baysoko Marketplace'
            site.save()
            self.stdout.write(f"✅ Updated site: {site.name}")
        
        # Set up Google OAuth
        google_client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
        google_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')
        
        if google_client_id and google_secret:
            google_app, created = SocialApp.objects.get_or_create(
                provider='google',
                defaults={
                    'name': 'Google',
                    'client_id': google_client_id,
                    'secret': google_secret,
                }
            )
            
            if not created:
                google_app.client_id = google_client_id
                google_app.secret = google_secret
                google_app.save()
            
            google_app.sites.add(site)
            google_app.sites.add(site)
            self.stdout.write(f"✅ Google OAuth app {'created' if created else 'updated'}")
        else:
            self.stdout.write("❌ Google OAuth credentials not found in environment")
        
        # Set up Facebook OAuth
        facebook_client_id = os.environ.get('FACEBOOK_OAUTH_CLIENT_ID')
        facebook_secret = os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET')
        
        if facebook_client_id and facebook_secret:
            facebook_app, created = SocialApp.objects.get_or_create(
                provider='facebook',
                defaults={
                    'name': 'Facebook',
                    'client_id': facebook_client_id,
                    'secret': facebook_secret,
                }
            )
            
            if not created:
                facebook_app.client_id = facebook_client_id
                facebook_app.secret = facebook_secret
                facebook_app.save()
            
            facebook_app.sites.add(site)
            self.stdout.write(f"✅ Facebook OAuth app {'created' if created else 'updated'}")
        else:
            self.stdout.write("❌ Facebook OAuth credentials not found in environment")
        
        self.stdout.write("\n✅ OAuth setup complete!")
        self.stdout.write(f"🌐 Site Domain: {site.domain}")
        self.stdout.write(f"🔗 Google Callback: {site_url}/accounts/google/callback/")
        self.stdout.write(f"🔗 Facebook Callback: {site_url}/accounts/facebook/callback/")
        
        # Display the actual redirect URIs that will be used
        self.stdout.write("\n📋 Google OAuth Configuration in Google Console:")
        self.stdout.write(f"Authorized redirect URI: {site_url}/accounts/google/callback/")
        self.stdout.write("\n📋 Facebook OAuth Configuration in Facebook Developer:")
        self.stdout.write(f"Valid OAuth Redirect URIs: {site_url}/accounts/facebook/callback/")
