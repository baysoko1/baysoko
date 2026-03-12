from django.urls import path
from . import views

app_name = 'affiliates'

urlpatterns = [
    path('dashboard/', views.affiliate_dashboard, name='dashboard'),
    path('commissions/', views.affiliate_commissions, name='commissions'),
    path('admin/', views.affiliate_admin_dashboard, name='admin_dashboard'),
    path('terms/', views.affiliate_terms, name='terms'),
]
