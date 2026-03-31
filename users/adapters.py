# users/adapters.py - Fixed version
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.http import Http404
import os
from django.conf import settings

class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def _base_site_url(self):
        site_url = (getattr(settings, 'SITE_URL', '') or os.environ.get('SITE_URL', '')).strip().rstrip('/')
        if site_url:
            return site_url
        return 'http://localhost:8000' if getattr(settings, 'DEBUG', False) else 'https://baysoko.up.railway.app'

    def get_app(self, request, provider, client_id=None):
        """
        Override to handle social apps gracefully
        """
        try:
            # First, try the default behavior
            return super().get_app(request, provider, client_id)
        except SocialApp.DoesNotExist:
            # If no app exists, try to create one on the fly
            return self.create_social_app_from_env(provider)
        except SocialApp.MultipleObjectsReturned:
            # If multiple apps found, get the current site and return the first one
            site = Site.objects.get_current()
            apps = SocialApp.objects.filter(
                provider=provider, 
                sites=site
            )
            if client_id:
                apps = apps.filter(client_id=client_id)
            
            if apps.exists():
                return apps.first()
            raise Http404(f"No social app found for {provider}")

    def create_social_app_from_env(self, provider):
        """Create a social app from environment variables"""
        site = Site.objects.get_current()
        
        if provider == 'google':
            client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
            secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')
            name = 'Google'
        elif provider == 'facebook':
            client_id = os.environ.get('FACEBOOK_OAUTH_CLIENT_ID')
            secret = os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET')
            name = 'Facebook'
        else:
            raise Http404(f"Provider {provider} not supported")
        
        if not client_id or not secret:
            raise Http404(f"No OAuth credentials found for {provider}")
        
        # Create the social app
        app, created = SocialApp.objects.get_or_create(
            provider=provider,
            defaults={
                'name': name,
                'client_id': client_id,
                'secret': secret,
            }
        )
        app.sites.add(site)
        
        return app

    def save_user(self, request, sociallogin, form=None):
        """
        Saves a newly signed up social login.
        """
        user = super().save_user(request, sociallogin, form)
        
        # Extract data from social account
        extra_data = sociallogin.account.extra_data
        
        # Update user fields with social data
        if extra_data:
            # For Google
            if 'given_name' in extra_data and not user.first_name:
                user.first_name = extra_data.get('given_name', '')
            if 'family_name' in extra_data and not user.last_name:
                user.last_name = extra_data.get('family_name', '')
            if 'name' in extra_data and not user.first_name:
                # Try to split full name
                name_parts = extra_data.get('name', '').split(' ', 1)
                if len(name_parts) > 0 and not user.first_name:
                    user.first_name = name_parts[0]
                if len(name_parts) > 1 and not user.last_name:
                    user.last_name = name_parts[1]
            if 'email' in extra_data and not user.email:
                user.email = extra_data.get('email', '')
            if not user.location and request:
                pending_location = (request.session.get('pending_location') or '').strip()
                if pending_location:
                    user.location = pending_location
        
        user.save()
        return user
    
    def get_redirect_uri(self, request, provider):
        """
        Get the correct redirect URI for social authentication
        """
        return f"{self._base_site_url()}/accounts/{provider}/callback/"
