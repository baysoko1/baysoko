# users/urls.py
from django.urls import path, include
from django.contrib.auth import views as auth_views
from .views import register, ProfileDetailView, ProfileUpdateView, google_callback, facebook_callback, CustomLoginView, CustomLogoutView
from .views import oauth_diagnostics, google_login, facebook_login, google_connect, google_native_signin, password_reset_send_code, password_reset_verify_code, password_reset_set_password, verification_required
from . import views
from allauth.socialaccount.views import SignupView
from django.urls import reverse_lazy

urlpatterns = [
    path('register/', register, name='register'),
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', CustomLogoutView.as_view(), name='logout'),
    
    # Social Authentication URLs
    path('accounts/google/login/', google_login, name='google_login'),
    path('accounts/google/connect/', google_connect, name='google_connect'),
    path('accounts/google/callback/', google_callback, name='google_callback'),
    path('accounts/google/native-signin/', google_native_signin, name='google_native_signin'),
    path('accounts/facebook/login/', facebook_login, name='facebook_login'),
    path('accounts/facebook/callback/', facebook_callback, name='facebook_callback'),
    
    # Password reset URLs
    
    
    path('password-reset/done/', 
         auth_views.PasswordResetDoneView.as_view(
             template_name='users/password_reset_done.html'
         ), 
         name='password_reset_done'),
    
    # Debug endpoint for SMTP testing (staff only)
    path('debug-email-send/', views.debug_send_email, name='debug_email_send'),
    
    # Password Change URLs (for logged-in users)
    path('password-change/', 
         auth_views.PasswordChangeView.as_view(
             template_name='users/password_change_form.html',
             success_url=reverse_lazy('password_change_done')
         ), 
         name='password_change'),
    
    path('password-change/done/', 
         auth_views.PasswordChangeDoneView.as_view(
             template_name='users/password_change_done.html'
         ), 
         name='password_change_done'),

    path('profile/<int:pk>/', ProfileDetailView.as_view(), name='profile'),
    path('profile/<int:pk>/edit/', ProfileUpdateView.as_view(), name='profile-edit'),
    path('oauth-diagnostics/', oauth_diagnostics, name='oauth-diagnostics'),
    path('ajax/password-change/', views.ajax_password_change, name='ajax_password_change'),
    path('verify-email/', views.verify_email, name='verify_email'),
    path('resend-code/', views.resend_code, name='resend_code'),
    path('change-verification-email/', views.change_verification_email, name='change_verification_email'),
    path('verify-phone/', views.verify_phone, name='verify_phone'),
    path('resend-phone-code/', views.resend_phone_code, name='resend_phone_code'),
    path('verify/', views.verification_required, name='verification_required'),
    path('password-reset-modal/', views.password_reset_modal, name='password_reset_modal'),
    path('password-reset-ajax/send-code/', password_reset_send_code, name='password_reset_send_code'),
    path('password-reset-ajax/verify-code/', password_reset_verify_code, name='password_reset_verify_code'),
    path('password-reset-ajax/set-password/', password_reset_set_password, name='password_reset_set_password'),
    path('change-password-ajax/', views.change_password_ajax, name='change_password_ajax'),
    path('delete-account-ajax/', views.delete_account_ajax, name='delete_account_ajax'),
    # WebSocket login completion (sets session cookie via HTTP)
    path('ws-login-complete/', views.ws_login_complete, name='ws_login_complete'),
    path('clear-welcome-toast/', views.clear_welcome_toast, name='clear_welcome_toast'),
    path('settings/toggle-contact-info/', views.toggle_show_contact_info, name='toggle_contact_info'),
    path('settings/get/', views.get_user_settings, name='get_user_settings'),
    path('settings/update-notifications/', views.update_notification_settings, name='update_notifications'),
    path('capture-location/', views.capture_location, name='capture_location'),
]
