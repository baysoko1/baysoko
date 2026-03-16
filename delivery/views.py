from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, TemplateView
from django.utils.decorators import method_decorator
from django.urls import reverse_lazy
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q, Count, Sum, Avg, F
from django.utils import timezone
from datetime import datetime, timedelta
import json
import csv
from decimal import Decimal
import logging
from django.db.models import Count, Sum, Avg, F, DurationField
from django.db.models.functions import TruncDate
from django.utils.timezone import make_aware
from django.contrib import messages
from django.contrib.auth.views import LoginView as AuthLoginView, LogoutView
from django.contrib.auth import login as auth_login
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

from .models import (
    DeliveryRequest, DeliveryPerson, DeliveryService, DeliveryZone,
    DeliveryStatusHistory, DeliveryProof, DeliveryRoute, DeliveryRating,
    DeliveryNotification, DeliveryPackageType, DeliveryTimeSlot,
    DeliveryPricingRule, DeliveryAnalytics, DeliveryConfirmation,
    DeliveryOTP, DeliveryAuditLog
)
from .forms import (
    DeliveryRequestForm, DeliveryPersonForm, DeliveryServiceForm,
    DeliveryZoneForm, DeliveryProofForm, DeliveryRouteForm,
    DeliveryRatingForm, DeliveryTimeSlotForm, DeliveryPricingRuleForm,
    DeliveryUserCreationForm, DeliveryProfileForm
)
from .utils import calculate_delivery_fee, optimize_route, send_delivery_notification
from .decorators import delivery_person_required, admin_required, seller_or_delivery_or_admin_required
from storefront.models import Store
from listings.models import Order, OrderItem


def _get_store_name_for_delivery(delivery):
    """Get store name for a delivery request via its order"""
    try:
        order = Order.objects.get(id=delivery.order_id)
        order_item = order.order_items.first()
        if order_item and order_item.listing and order_item.listing.store:
            return order_item.listing.store.name
        # Fallback to seller name if no store
        if order_item and order_item.listing and order_item.listing.seller:
            return order_item.listing.seller.get_full_name() or order_item.listing.seller.username
    except (Order.DoesNotExist, OrderItem.DoesNotExist, AttributeError):
        pass
    return "Unknown Store"


def _get_deliveries_for_user(user, request=None):
    """Module-level helper to return deliveries scoped to the given user.

    If `request` is provided, will respect a `store`/`store_id` GET filter
    but only if the store belongs to the user.
    """
    # Admin/superusers see all
    if user.is_staff or user.is_superuser:
        return DeliveryRequest.objects.all()

    # Delivery persons see their own assignments
    if hasattr(user, 'delivery_person'):
        return DeliveryRequest.objects.filter(delivery_person=user.delivery_person)

    # Sellers see deliveries for their stores only (require store ownership)
    try:
        stores = Store.objects.filter(owner=user)
        if not stores.exists():
            return DeliveryRequest.objects.none()
        store_ids = [s.id for s in stores]

        # Build store lookup Q
        store_lookup = []
        for store_id in store_ids:
            store_lookup.append(Q(metadata__store_id=store_id))
            store_lookup.append(Q(metadata__store=str(store_id)))
        from functools import reduce
        from operator import or_
        store_q = reduce(or_, store_lookup) if store_lookup else None

        # Deliveries tied to orders that contain listings sold by this user
        try:
            from listings.models import Order
            sold_order_ids = Order.objects.filter(
                order_items__listing__seller=user
            ).values_list('id', flat=True).distinct()
            sold_order_ids = [str(i) for i in sold_order_ids]
        except Exception:
            sold_order_ids = []

        seller_q = Q()
        if sold_order_ids:
            seller_q = Q(order_id__in=sold_order_ids) | Q(metadata__seller_id=user.id) | Q(metadata__seller=str(user.id))

        # Respect explicit store filter if provided and belongs to the user
        if request is not None:
            store_filter = request.GET.get('store') or request.GET.get('store_id') or request.GET.get('storeId')
            if store_filter and str(store_filter).isdigit():
                sf = int(store_filter)
                if sf in store_ids:
                    return DeliveryRequest.objects.filter(
                        Q(metadata__store_id=sf) | Q(metadata__store=str(sf))
                    )
                else:
                    return DeliveryRequest.objects.none()

        # Combine
        if store_q and seller_q:
            combined_q = store_q | seller_q
            return DeliveryRequest.objects.filter(combined_q)
        if store_q:
            return DeliveryRequest.objects.filter(store_q)
        if seller_q:
            return DeliveryRequest.objects.filter(seller_q)
    except Exception:
        return DeliveryRequest.objects.none()

    return DeliveryRequest.objects.none()


# ============================================================================
# AJAX/API VIEWS FOR DYNAMIC LOADING AND POLLING
# ============================================================================

@require_GET
@login_required(login_url='delivery:login')
def quick_stats(request):
    """Return quick stats for sidebar (AJAX)"""
    user = request.user
    base_qs = _get_deliveries_for_user(user, request)

    today = timezone.now().date()

    data = {
        'today_deliveries': base_qs.filter(created_at__date=today).count(),
        'active_deliveries': base_qs.filter(
            status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']
        ).count(),
        'pending_count': base_qs.filter(status__in=['pending', 'accepted', 'assigned']).count(),
        'success_rate': 0
    }

    # Calculate success rate if we have deliveries
    total_completed = base_qs.filter(status='delivered').count()
    total_deliveries = base_qs.count()
    if total_deliveries > 0:
        data['success_rate'] = round((total_completed / total_deliveries) * 100, 1)

    return JsonResponse(data)


@require_GET
@login_required(login_url='delivery:login')
def notification_count(request):
    """Return unread notification count (AJAX)"""
    count = DeliveryNotification.objects.filter(
        user=request.user,
        is_read=False
    ).count()

    # Also return recent notifications for dropdown
    notifications = DeliveryNotification.objects.filter(
        user=request.user
    ).order_by('-created_at')[:5]

    notification_list = []
    for notification in notifications:
        notification_list.append({
            'id': notification.id,
            'title': notification.title,
            'message': notification.message,
            'type': notification.notification_type,
            'icon': notification.get_icon() if hasattr(notification, 'get_icon') else 'bell',
            'time_ago': notification.get_time_ago() if hasattr(notification, 'get_time_ago') else '',
            'is_read': notification.is_read,
            'url': notification.get_absolute_url() if hasattr(notification, 'get_absolute_url') else '#'
        })

    return JsonResponse({
        'count': count,
        'notifications': notification_list
    })


def become_driver(request):
    """Public driver registration.

    - If the user is authenticated: allow them to register as a DeliveryPerson.
    - If anonymous: allow creating a new `User` and `DeliveryPerson` together. If the supplied
      username/email already exists, inform the user and attach the DeliveryPerson to that
      existing account.
    """
    from django.contrib.auth import get_user_model, login
    User = get_user_model()

    # If already a delivery person and authenticated, redirect to dashboard
    if request.user.is_authenticated and hasattr(request.user, 'delivery_person'):
        return redirect('delivery:driver_dashboard')

    if request.method == 'POST':
        # Handle authenticated user posting only delivery person details
        if request.user.is_authenticated:
            form = DeliveryPersonForm(request.POST, request.FILES)
            if form.is_valid():
                dp = form.save(commit=False)
                dp.user = request.user
                if not dp.employee_id:
                    import uuid
                    dp.employee_id = f"D-{str(uuid.uuid4())[:8].upper()}"
                dp.is_verified = False
                dp.save()
                try:
                    from .utils import send_driver_registration_notification
                    send_driver_registration_notification(dp)
                except Exception:
                    pass
                try:
                    request.session['delivery_auth'] = True
                except Exception:
                    pass
                return redirect('delivery:driver_dashboard')
            return render(request, 'delivery/driver_register.html', {'form': form, 'is_authenticated': True})
        else:
            # Anonymous: try to create a new user + delivery person
            user_form = DeliveryUserCreationForm(request.POST)
            dp_form = DeliveryPersonForm(request.POST, request.FILES)
            existing_user = None

            # Check for existing user by username or email (if provided)
            username = request.POST.get('username') or request.POST.get('email')
            email = request.POST.get('email')
            try:
                if username:
                    existing_user = User.objects.filter(username=username).first()
                if not existing_user and email:
                    existing_user = User.objects.filter(email=email).first()
            except Exception:
                existing_user = None

            if existing_user:
                # Inform user that account exists but proceed to create DeliveryPerson
                # Attach DeliveryPerson to existing_user
                if dp_form.is_valid():
                    dp = dp_form.save(commit=False)
                    dp.user = existing_user
                    if not dp.employee_id:
                        import uuid
                        dp.employee_id = f"D-{str(uuid.uuid4())[:8].upper()}"
                    dp.is_verified = False
                    dp.save()
                    try:
                        from .utils import send_driver_registration_notification
                        send_driver_registration_notification(dp)
                    except Exception:
                        pass
                    # Inform and ask user to sign in
                    messages.info(request, 'An existing Baysoko account was found and your driver profile was attached. Please sign in to continue.')
                    login_url = reverse_lazy('delivery:login')
                    next_url = reverse_lazy('delivery:driver_dashboard')
                    return redirect(f"{login_url}?next={next_url}")
                else:
                    # Render forms with errors
                    return render(request, 'delivery/driver_register.html', {'user_form': user_form, 'form': dp_form})

            # No existing user: create new user and delivery person
            if user_form.is_valid() and dp_form.is_valid():
                new_user = user_form.save()
                dp = dp_form.save(commit=False)
                dp.user = new_user
                if not dp.employee_id:
                    import uuid
                    dp.employee_id = f"D-{str(uuid.uuid4())[:8].upper()}"
                dp.is_verified = False
                dp.save()
                try:
                    from .utils import send_driver_registration_notification
                    send_driver_registration_notification(dp)
                except Exception:
                    pass
                # Auto-login the new user
                login(request, new_user)
                try:
                    request.session['delivery_auth'] = True
                except Exception:
                    pass
                return redirect('delivery:driver_dashboard')
            else:
                return render(request, 'delivery/driver_register.html', {'user_form': user_form, 'form': dp_form})

    else:
        # GET: show appropriate forms
        if request.user.is_authenticated:
            import uuid
            initial = {'employee_id': f"D-{str(uuid.uuid4())[:8].upper()}"}
            form = DeliveryPersonForm(initial=initial)
            return render(request, 'delivery/driver_register.html', {'form': form, 'is_authenticated': True})
        else:
            user_form = DeliveryUserCreationForm()
            import uuid
            initial = {'employee_id': f"D-{str(uuid.uuid4())[:8].upper()}"}
            form = DeliveryPersonForm(initial=initial)
            return render(request, 'delivery/driver_register.html', {'user_form': user_form, 'form': form, 'is_authenticated': False})


def delivery_home(request):
    """Landing page for the delivery app (unauthenticated)."""
    try:
        if request.user.is_authenticated and request.session.get('delivery_auth'):
            return redirect('delivery:dashboard')
    except Exception:
        pass
    delivery_authenticated = False
    try:
        delivery_authenticated = bool(request.session.get('delivery_auth'))
    except Exception:
        delivery_authenticated = False
    return render(request, 'delivery/home.html', {
        'delivery_authenticated': delivery_authenticated,
    })


@require_GET
@login_required
@delivery_person_required
def driver_active_deliveries(request):
    """Return active deliveries for driver (AJAX)"""
    driver = request.user.delivery_person
    active_deliveries = DeliveryRequest.objects.filter(
        delivery_person=driver,
        status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']
    ).order_by('priority', 'estimated_delivery_time')

    deliveries_data = []
    for delivery in active_deliveries:
        deliveries_data.append({
            'id': delivery.id,
            'tracking_number': delivery.tracking_number,
            'status': delivery.status,
            'status_display': delivery.get_status_display(),
            'recipient_name': delivery.recipient_name,
            'recipient_address': delivery.recipient_address,
            'delivery_fee': delivery.delivery_fee,
            'estimated_delivery_time': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
            'priority': delivery.priority,
            'store_name': _get_store_name_for_delivery(delivery),
            'has_updates': delivery.status_updates.filter(is_read=False).exists()
        })

    return JsonResponse({
        'has_updates': any(d['has_updates'] for d in deliveries_data),
        'assignments': deliveries_data
    })


@require_GET
@login_required
@delivery_person_required
def driver_updates(request):
    """Check for driver updates (AJAX for polling)"""
    driver = request.user.delivery_person

    # Check for new assignments
    new_assignments = DeliveryRequest.objects.filter(
        delivery_person=driver,
        status='assigned',
        created_at__gte=timezone.now() - timedelta(minutes=5)
    ).exists()

    # Check for status updates
    status_updates = DeliveryRequest.objects.filter(
        delivery_person=driver,
        status_updates__is_read=False
    ).exists()

    return JsonResponse({
        'has_updates': new_assignments or status_updates,
        'new_assignments': new_assignments,
        'status_updates': status_updates
    })


@require_GET
@login_required
def dashboard_stats(request):
    """Return dashboard statistics (AJAX)"""
    user = request.user
    base_qs = _get_deliveries_for_user(user, request)

    # Get today's date
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # Calculate current stats
    total_deliveries = base_qs.count()
    pending_deliveries = base_qs.filter(status__in=['pending', 'accepted', 'assigned']).count()
    in_transit_deliveries = base_qs.filter(status__in=['picked_up', 'in_transit', 'out_for_delivery']).count()
    completed_deliveries = base_qs.filter(status='delivered').count()

    # Calculate trends
    total_yesterday = base_qs.filter(created_at__date=yesterday).count()
    total_week_ago = base_qs.filter(created_at__date=week_ago).count()

    trend_total = calculate_trend(total_deliveries, total_yesterday)
    trend_pending = calculate_trend(
        pending_deliveries,
        base_qs.filter(status__in=['pending', 'accepted', 'assigned'], created_at__date=yesterday).count()
    )
    trend_transit = calculate_trend(
        in_transit_deliveries,
        base_qs.filter(status__in=['picked_up', 'in_transit', 'out_for_delivery'], created_at__date=yesterday).count()
    )
    trend_completed = calculate_trend(
        completed_deliveries,
        base_qs.filter(status='delivered', created_at__date=yesterday).count()
    )

    # For delivery persons, add earnings data
    earnings_today = 0
    if hasattr(user, 'delivery_person'):
        earnings_today = base_qs.filter(
            delivery_person=user.delivery_person,
            status='delivered',
            actual_delivery_time__date=today
        ).aggregate(total=Sum('delivery_fee'))['total'] or 0
        earnings_today = float(earnings_today * Decimal('0.7'))  # 70% commission

    return JsonResponse({
        'total_deliveries': total_deliveries,
        'pending_deliveries': pending_deliveries,
        'in_transit_deliveries': in_transit_deliveries,
        'completed_deliveries': completed_deliveries,
        'today_deliveries': base_qs.filter(created_at__date=today).count(),
        'earnings_today': earnings_today,
        'trend_total': trend_total,
        'trend_pending': trend_pending,
        'trend_transit': trend_transit,
        'trend_completed': trend_completed
    })


@require_GET
@login_required
def recent_deliveries(request):
    """Return recent deliveries for dashboard (AJAX)"""
    user = request.user
    base_qs = _get_deliveries_for_user(user, request)

    # Apply status filter if provided
    status = request.GET.get('status')
    if status:
        base_qs = base_qs.filter(status=status)

    # Apply limit
    limit = min(int(request.GET.get('limit', 10)), 50)

    deliveries = base_qs.select_related(
        'delivery_person', 'delivery_service'
    ).order_by('-created_at')[:limit]

    deliveries_data = []
    for delivery in deliveries:
        deliveries_data.append({
            'id': delivery.id,
            'tracking_number': delivery.tracking_number,
            'status': delivery.status,
            'status_display': delivery.get_status_display(),
            'recipient_name': delivery.recipient_name,
            'recipient_address': delivery.recipient_address,
            'delivery_fee': delivery.delivery_fee,
            'created_at': delivery.created_at.isoformat(),
            'estimated_delivery': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
            'priority': delivery.priority,
            'delivery_person': delivery.delivery_person.user.get_full_name() if delivery.delivery_person else None,
            'store_name': _get_store_name_for_delivery(delivery)
        })

    return JsonResponse({
        'deliveries': deliveries_data,
        'count': len(deliveries_data)
    })


@require_GET
@login_required
def chart_data(request):
    """Return chart data for dashboard (AJAX)"""
    user = request.user
    base_qs = _get_deliveries_for_user(user, request)

    # Status distribution
    status_distribution = []
    status_counts = base_qs.values('status').annotate(count=Count('id'))
    for item in status_counts:
        status_distribution.append({
            'status': item['status'].replace('_', ' ').title(),
            'count': item['count']
        })

    # Weekly activity
    weekly_activity = []
    today = timezone.now().date()
    for i in range(6, -1, -1):  # Last 7 days including today
        date = today - timedelta(days=i)
        count = base_qs.filter(created_at__date=date).count()
        weekly_activity.append({
            'date': date.isoformat(),
            'day': date.strftime('%a'),
            'count': count
        })

    # Monthly revenue if seller/admin
    monthly_revenue = []
    if user.is_staff or user.is_superuser or Store.objects.filter(owner=user).exists():
        for i in range(5, -1, -1):  # Last 6 months
            month_start = today.replace(day=1) - timedelta(days=30*i)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            revenue = base_qs.filter(
                created_at__date__range=[month_start, month_end],
                payment_status='paid'
            ).aggregate(total=Sum('total_amount'))['total'] or 0
            monthly_revenue.append({
                'month': month_start.strftime('%b'),
                'revenue': float(revenue)
            })

    return JsonResponse({
        'status_distribution': status_distribution,
        'weekly_activity': weekly_activity,
        'monthly_revenue': monthly_revenue
    })


@require_GET
@login_required
@delivery_person_required
def driver_assignments(request):
    """Return driver assignments (AJAX)"""
    driver = request.user.delivery_person

    # Get today's assignments
    today = timezone.now().date()
    assignments = DeliveryRequest.objects.filter(
        delivery_person=driver,
        status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']
    ).order_by('priority', 'estimated_delivery_time')

    assignments_data = []
    for assignment in assignments:
        assignments_data.append({
            'id': assignment.id,
            'tracking_number': assignment.tracking_number,
            'status': assignment.status,
            'status_display': assignment.get_status_display(),
            'recipient_name': assignment.recipient_name,
            'recipient_address': assignment.recipient_address,
            'estimated_delivery_time': assignment.estimated_delivery_time.strftime('%I:%M %p') if assignment.estimated_delivery_time else 'N/A',
            'priority': assignment.priority,
            'delivery_fee': assignment.delivery_fee,
            'store_name': _get_store_name_for_delivery(assignment),
            'pickup_name': assignment.pickup_name,
            'pickup_address': assignment.pickup_address,
            'phone': assignment.recipient_phone
        })

    # Driver stats
    driver_stats = {
        'total_completed': driver.completed_deliveries,
        'rating': driver.rating,
        'completed_today': assignments.filter(status='delivered', actual_delivery_time__date=today).count(),
        'earnings_today': float((assignments.filter(status='delivered', actual_delivery_time__date=today).aggregate(total=Sum('delivery_fee'))['total'] or 0) * Decimal('0.7')),
        'active_assignments': assignments.filter(status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']).count()
    }

    return JsonResponse({
        'assignments': assignments_data,
        'driver_stats': driver_stats
    })


def calculate_trend(current, previous):
    """Calculate percentage trend between current and previous values"""
    if previous == 0:
        return 100 if current > 0 else 0
    return round(((current - previous) / previous) * 100, 1)


# ============================================================================
# MAIN VIEWS
# ============================================================================

class DashboardView(LoginRequiredMixin, TemplateView):
    """Delivery system dashboard"""
    template_name = 'delivery/dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        # Allow all authenticated users (buyers/sellers/drivers) full dashboard access.
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get statistics based on user type
        today = timezone.now().date()
        start_of_month = today.replace(day=1)

        # Determine base queryset based on user role
        base_qs = _get_deliveries_for_user(user, request=self.request)

        # Store the base queryset for use in other calculations
        context['base_deliveries'] = base_qs

        context['total_deliveries'] = base_qs.count()
        context['pending_deliveries'] = base_qs.filter(
            status__in=['pending', 'accepted', 'assigned']
        ).count()
        context['in_transit_deliveries'] = base_qs.filter(
            status__in=['picked_up', 'in_transit', 'out_for_delivery']
        ).count()
        context['completed_deliveries'] = base_qs.filter(
            status='delivered'
        ).count()
        context['today_deliveries'] = base_qs.filter(
            created_at__date=today
        ).count()

        # Revenue statistics (only for relevant deliveries)
        context['monthly_revenue'] = base_qs.filter(
            created_at__gte=start_of_month,
            payment_status='paid'
        ).aggregate(total=Sum('total_amount'))['total'] or 0

        # Recent deliveries with store filter
        status_filter = self.request.GET.get('status')
        store_filter = self.request.GET.get('store_id')

        recent_qs = base_qs.select_related(
            'delivery_person', 'delivery_service'
        ).order_by('-created_at')[:10]

        # Apply status filter if provided
        if status_filter:
            recent_qs = base_qs.filter(status=status_filter).select_related(
                'delivery_person', 'delivery_service'
            ).order_by('-created_at')[:10]

        context['recent_deliveries'] = recent_qs
        context['filter_status'] = status_filter or ''
        context['status_choices'] = DeliveryRequest.STATUS_CHOICES

        # Get user's stores for filter dropdown
        try:
            from storefront.models import Store
            stores = Store.objects.filter(owner=user)
            context['stores'] = stores
            context['selected_store_id'] = store_filter if store_filter else None
        except Exception:
            context['stores'] = []
            context['selected_store_id'] = None

        # Nearest drivers panel (seller-only)
        context['nearest_drivers'] = []
        try:
            if not (user.is_staff or user.is_superuser) and not hasattr(user, 'delivery_person'):
                store_with_coords = Store.objects.filter(
                    owner=user,
                    location_latitude__isnull=False,
                    location_longitude__isnull=False
                ).first()
                if store_with_coords:
                    base_lat = float(store_with_coords.location_latitude)
                    base_lng = float(store_with_coords.location_longitude)
                    drivers = DeliveryPerson.objects.filter(
                        is_available=True,
                        current_latitude__isnull=False,
                        current_longitude__isnull=False
                    )
                    def _haversine(lat1, lng1, lat2, lng2):
                        from math import radians, sin, cos, sqrt, atan2
                        R = 6371
                        dlat = radians(lat2 - lat1)
                        dlon = radians(lng2 - lng1)
                        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
                        c = 2 * atan2(sqrt(a), sqrt(1 - a))
                        return R * c
                    ranked = []
                    for d in drivers:
                        dist = _haversine(base_lat, base_lng, float(d.current_latitude), float(d.current_longitude))
                        ranked.append({
                            'name': d.user.get_full_name() or d.user.username,
                            'vehicle': d.get_vehicle_type_display(),
                            'rating': d.rating,
                            'distance_km': round(dist, 1),
                        })
                    ranked.sort(key=lambda x: x['distance_km'])
                    context['nearest_drivers'] = ranked[:6]
                    context['nearest_drivers_store'] = store_with_coords
        except Exception:
            context['nearest_drivers'] = []

        # Delivery person statistics
        if hasattr(user, 'delivery_person'):
            context['is_delivery_person'] = True
            delivery_person = user.delivery_person
            context['my_assignments'] = DeliveryRequest.objects.filter(
                delivery_person=delivery_person,
                status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']
            ).order_by('-priority', 'estimated_delivery_time')[:5]

            # Calculate driver stats
            earnings_today = base_qs.filter(
                delivery_person=delivery_person,
                status='delivered',
                actual_delivery_time__date=today
            ).aggregate(total=Sum('delivery_fee'))['total'] or 0

            context['driver_stats'] = {
                'total_completed': delivery_person.completed_deliveries,
                'rating': delivery_person.rating,
                'completed_today': base_qs.filter(
                    delivery_person=delivery_person,
                    status='delivered',
                    actual_delivery_time__date=today
                ).count(),
                'earnings_today': float(earnings_today * Decimal('0.7')),
                'active_assignments': base_qs.filter(
                    delivery_person=delivery_person,
                    status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery']
                ).count()
            }

        # Notifications
        context['unread_notifications'] = DeliveryNotification.objects.filter(
            user=self.request.user,
            is_read=False
        ).order_by('-created_at')[:5]

        # Chart data
        context['status_distribution'] = get_status_distribution(base_qs)
        context['weekly_activity'] = get_weekly_activity(base_qs)

        # Add user role context for templates
        context['is_admin'] = user.is_staff or user.is_superuser
        context['is_seller'] = Store.objects.filter(owner=user).exists()

        return context


class DeliveryLoginView(AuthLoginView):
    """Login view for delivery app that redirects to delivery dashboard by default."""
    template_name = 'delivery/login.html'
    redirect_authenticated_user = False

    def dispatch(self, request, *args, **kwargs):
        try:
            if request.method == 'GET':
                # Mark intent without forcing a logout (prevents session interruption during concurrent requests)
                request.session['delivery_login_intent'] = True
        except Exception:
            pass
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        try:
            self.request.session['delivery_auth'] = True
        except Exception:
            pass
        try:
            if not getattr(self.request.user, 'email_verified', False):
                self.request.session['post_verify_redirect'] = reverse_lazy('delivery:dashboard')
                self.request.session['delivery_login_intent'] = True
                return redirect('verification_required')
        except Exception:
            pass
        return response

    def get_success_url(self):
        # Respect explicit next parameter when present
        next_url = self.request.POST.get('next') or self.request.GET.get('next')
        if next_url:
            return next_url
        return reverse_lazy('delivery:dashboard')


def delivery_register(request):
    """Simple user registration for the delivery app (standalone users).

    Creates a Django `User`, logs them in and sends them to the delivery dashboard.
    This allows users who are not part of the main Baysoko app to manage standalone deliveries.
    """
    if request.method == 'GET':
        # Mark intent without forcing a logout (prevents session interruption during concurrent requests)
        try:
            request.session['delivery_login_intent'] = True
        except Exception:
            pass
    if request.method == 'POST':
        form = DeliveryUserCreationForm(request.POST)
        if form.is_valid():
            email = (form.cleaned_data.get('email') or '').strip().lower()
            existing_user = None
            if email:
                try:
                    User = get_user_model()
                    existing_user = User.objects.filter(email__iexact=email).order_by('date_joined', 'id').first()
                except Exception:
                    existing_user = None
            if existing_user:
                messages.info(request, 'An account with this email already exists. Please sign in to continue.')
                login_url = reverse_lazy('delivery:login')
                next_url = reverse_lazy('delivery:dashboard')
                return redirect(f"{login_url}?next={next_url}")

            user = form.save()
            try:
                auth_login(request, user)
                request.session['delivery_auth'] = True
                request.session.pop('delivery_login_intent', None)
            except Exception:
                pass
            messages.success(request, 'Account created. Please verify your email to continue.')
            try:
                request.session['post_verify_redirect'] = reverse_lazy('delivery:dashboard')
                request.session['delivery_login_intent'] = True
            except Exception:
                pass
            return redirect('verification_required')
    else:
        form = DeliveryUserCreationForm()

    return render(request, 'delivery/register.html', {'form': form})


class DeliveryLogoutView(LogoutView):
    """Logout delivery users and clear delivery session flag."""
    next_page = 'delivery:home'

    def dispatch(self, request, *args, **kwargs):
        try:
            if 'delivery_auth' in request.session:
                del request.session['delivery_auth']
            if 'delivery_login_intent' in request.session:
                del request.session['delivery_login_intent']
        except Exception:
            pass
        return super().dispatch(request, *args, **kwargs)


@login_required
def delivery_profile_complete(request):
    """Complete delivery app profile for non-driver users."""
    user = request.user
    if hasattr(user, 'delivery_person'):
        return redirect('delivery:dashboard')

    profile = getattr(user, 'delivery_profile', None)
    if request.method == 'POST':
        form = DeliveryProfileForm(request.POST, instance=profile, user=user)
        if form.is_valid():
            delivery_profile = form.save(commit=False)
            delivery_profile.user = user
            delivery_profile.save()
            try:
                updated_fields = []
                if delivery_profile.phone_number and not getattr(user, 'phone_number', None):
                    user.phone_number = delivery_profile.phone_number
                    updated_fields.append('phone_number')
                if not getattr(user, 'location', None):
                    loc = delivery_profile.city or delivery_profile.address
                    if loc:
                        user.location = loc
                        updated_fields.append('location')
                if updated_fields:
                    user.save(update_fields=updated_fields)
            except Exception:
                pass
            messages.success(request, 'Delivery profile updated.')
            return redirect('delivery:dashboard')
    else:
        initial = {}
        try:
            if not profile:
                if getattr(user, 'phone_number', None):
                    initial['phone_number'] = user.phone_number
                if getattr(user, 'location', None):
                    initial['city'] = user.location
        except Exception:
            pass
        form = DeliveryProfileForm(instance=profile, initial=initial, user=user)

    # Pre-fill missing fields from social account data when available
    try:
        if not profile:
            from allauth.socialaccount.models import SocialAccount
            sa = SocialAccount.objects.filter(user=user).order_by('-last_login', '-date_joined', '-id').first()
            if sa and isinstance(sa.extra_data, dict):
                extra = sa.extra_data
                if not getattr(user, 'first_name', '') and extra.get('given_name'):
                    user.first_name = extra.get('given_name')
                if not getattr(user, 'last_name', '') and extra.get('family_name'):
                    user.last_name = extra.get('family_name')
                if not getattr(user, 'email', '') and extra.get('email'):
                    user.email = extra.get('email')
                try:
                    user.save(update_fields=['first_name', 'last_name', 'email'])
                except Exception:
                    pass
    except Exception:
        pass

    return render(request, 'delivery/profile_complete.html', {'form': form})

def get_filtered_deliveries(user, request=None):
    """Module helper that delegates to _get_deliveries_for_user for external callers."""
    return _get_deliveries_for_user(user, request=request)


def get_status_distribution(base_qs):
    """Get delivery status distribution for chart"""
    data = base_qs.values('status').annotate(
        count=Count('id')
    ).order_by('status')
    return list(data)


def get_weekly_activity(base_qs):
    """Get weekly delivery activity"""
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)

    activity = []
    for i in range(7):
        date = week_ago + timedelta(days=i)
        count = base_qs.filter(created_at__date=date).count()
        activity.append({
            'date': date.strftime('%Y-%m-%d'),
            'day': date.strftime('%a'),
            'count': count
        })

    return activity


class DeliveryListView(LoginRequiredMixin, ListView):
    """List all deliveries"""
    model = DeliveryRequest
    template_name = 'delivery/delivery_list.html'
    context_object_name = 'deliveries'
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        queryset = _get_deliveries_for_user(user, request=self.request)

        # Apply additional filters
        queryset = queryset.select_related(
            'delivery_person', 'delivery_service', 'delivery_zone'
        ).order_by('-created_at')

        # Filter by status
        status = self.request.GET.get('status')
        if status:
            queryset = queryset.filter(status=status)

        # Filter by delivery person
        if self.request.GET.get('my_deliveries') and hasattr(self.request.user, 'delivery_person'):
            queryset = queryset.filter(delivery_person=self.request.user.delivery_person)

        # Filter by date range
        date_from = self.request.GET.get('date_from')
        date_to = self.request.GET.get('date_to')
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        # Search
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(
                Q(tracking_number__icontains=search) |
                Q(order_id__icontains=search) |
                Q(recipient_name__icontains=search) |
                Q(recipient_phone__icontains=search) |
                Q(pickup_name__icontains=search)
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = DeliveryRequest.STATUS_CHOICES
        context['filter_status'] = self.request.GET.get('status', '')
        context['date_from'] = self.request.GET.get('date_from', '')
        context['date_to'] = self.request.GET.get('date_to', '')
        context['search'] = self.request.GET.get('search', '')
        context['my_deliveries'] = self.request.GET.get('my_deliveries', '')

        # Add store filter for sellers
        user = self.request.user
        if not (user.is_staff or user.is_superuser) and not hasattr(user, 'delivery_person'):
            try:
                from storefront.models import Store
                stores = Store.objects.filter(owner=user)
                context['stores'] = stores
                context['store_filter'] = self.request.GET.get('store', '')
            except Exception:
                context['stores'] = []
                context['store_filter'] = ''

        return context


class DeliveryDetailView(LoginRequiredMixin, DetailView):
    """View delivery details"""
    model = DeliveryRequest
    template_name = 'delivery/delivery_detail.html'
    context_object_name = 'delivery'

    def dispatch(self, request, *args, **kwargs):
        # Ensure only allowed users can view this delivery
        user = request.user
        if not user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        # Admins and delivery persons allowed
        if user.is_staff or user.is_superuser or hasattr(user, 'delivery_person'):
            return super().dispatch(request, *args, **kwargs)

        # Otherwise, check store ownership or sold items
        try:
            delivery = self.get_object()
            # If delivery has store metadata, only that store owner can view
            if isinstance(delivery.metadata, dict):
                store_id = delivery.metadata.get('store_id')
                if store_id:
                    try:
                        from storefront.models import Store
                        if Store.objects.filter(id=int(store_id), owner=user).exists():
                            return super().dispatch(request, *args, **kwargs)
                    except Exception:
                        pass

            # Allow sellers who have items in the linked order
            if delivery.order_id:
                try:
                    from listings.models import Order
                    oid = None
                    try:
                        oid = int(delivery.order_id)
                    except Exception:
                        parts = str(delivery.order_id).split('_')
                        try:
                            oid = int(parts[-1])
                        except Exception:
                            oid = None
                    if oid:
                        order = Order.objects.filter(id=oid).first()
                        if order and order.order_items.filter(listing__seller=user).exists():
                            return super().dispatch(request, *args, **kwargs)
                except Exception:
                    pass
        except Exception:
            pass

        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('You do not have permission to view this delivery.')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get status history with safe user info
        status_history = []
        for history in self.object.status_history.all().order_by('-created_at'):
            status_history.append({
                'id': history.id,
                'old_status': history.old_status,
                'old_status_display': history.get_old_status_display(),
                'new_status': history.new_status,
                'new_status_display': history.get_new_status_display(),
                'notes': history.notes,
                'created_at': history.created_at,
                'changed_by': history.changed_by,
                'changed_by_display': history.get_changed_by_display(),
            })

        context['status_history'] = status_history
        context['proofs'] = self.object.proofs.all()
        context['can_update_status'] = self.can_update_status()
        context['status_choices'] = DeliveryRequest.STATUS_CHOICES

        # Add user's stores context for permission checking
        user = self.request.user
        if not (user.is_staff or user.is_superuser) and not hasattr(user, 'delivery_person'):
            try:
                from storefront.models import Store
                stores = Store.objects.filter(owner=user)
                context['user_stores'] = stores
            except Exception:
                context['user_stores'] = []
        # If the current user is the assigned driver, expose active OTP info for UI
        try:
            if hasattr(user, 'delivery_person') and self.object.delivery_person and self.object.delivery_person == user.delivery_person:
                from django.utils import timezone as _tz
                active_otp = DeliveryOTP.objects.filter(
                    delivery_request=self.object,
                    used=False,
                    expires_at__gte=_tz.now(),
                    created_by=user.delivery_person
                ).order_by('-created_at').first()
                if active_otp:
                    context['active_otp'] = True
                    context['otp_expires_at'] = active_otp.expires_at
                    context['verify_otp_url'] = reverse_lazy('delivery:verify_delivery_otp', kwargs={'pk': self.object.pk})
                else:
                    context['active_otp'] = False
        except Exception:
            context['active_otp'] = False

        # Attach linked order details (if any) so templates can show items and attributes
        context['order'] = None
        try:
            if self.object.order_id:
                from listings.models import Order
                oid = None
                try:
                    oid = int(self.object.order_id)
                except Exception:
                    parts = str(self.object.order_id).split('_')
                    try:
                        oid = int(parts[-1])
                    except Exception:
                        oid = None

                if oid:
                    order = Order.objects.filter(id=oid).select_related('buyer').prefetch_related('order_items__listing').first()
                    if order:
                        items = []
                        for item in order.order_items.all():
                            items.append({
                                'id': item.id,
                                'listing_id': getattr(item.listing, 'id', None),
                                'title': getattr(getattr(item, 'listing', None), 'title', getattr(item, 'title', None) if hasattr(item, 'title') else ''),
                                'quantity': getattr(item, 'quantity', None),
                                'unit_price': float(getattr(item, 'unit_price', 0)) if getattr(item, 'unit_price', None) is not None else None,
                                'total_price': float(getattr(item, 'total_price', getattr(item, 'quantity', 0) * getattr(item, 'unit_price', 0))) if getattr(item, 'quantity', None) is not None else None,
                                'attributes': getattr(item, 'attributes', None)
                            })

                        context['order'] = {
                            'id': order.id,
                            'buyer': getattr(order, 'buyer', None),
                            'total_amount': float(getattr(order, 'total_amount', 0)) if getattr(order, 'total_amount', None) is not None else None,
                            'payment_status': getattr(order, 'payment_status', None),
                            'items': items,
                            'metadata': getattr(order, 'metadata', None)
                        }
        except Exception:
            context['order'] = None

        return context

    def can_update_status(self):
        """Check if user can update delivery status"""
        user = self.request.user
        delivery = self.get_object()

        if user.is_superuser or user.is_staff:
            return True

        if hasattr(user, 'delivery_person'):
            return delivery.delivery_person == user.delivery_person

        # Check if user owns the store that this delivery belongs to
        try:
            from storefront.models import Store
            stores = Store.objects.filter(owner=user)
            # If the delivery has store_id metadata and the user owns that store
            if stores.exists() and isinstance(delivery.metadata, dict):
                store_id = delivery.metadata.get('store_id')
                if store_id:
                    try:
                        store_id_int = int(store_id)
                        if stores.filter(id=store_id_int).exists():
                            return True
                    except ValueError:
                        if stores.filter(id=str(store_id)).exists():
                            return True

            # Additionally allow sellers who sold items in the linked order to update
            try:
                if delivery.order_id:
                    from listings.models import Order
                    # Match numeric order ids
                    try:
                        oid = int(delivery.order_id)
                    except Exception:
                        # Try to extract trailing number if integration uses prefixes
                        parts = str(delivery.order_id).split('_')
                        oid = None
                        try:
                            oid = int(parts[-1])
                        except Exception:
                            oid = None

                    if oid:
                        order = Order.objects.filter(id=oid).first()
                        if order and order.order_items.filter(listing__seller=user).exists():
                            return True
            except Exception:
                pass
        except Exception:
            pass

        return False


@login_required
@require_POST
def confirm_delivery(request):
    """Endpoint for buyer to confirm receipt of a delivery.

    Expects POST body with either `tracking_number` or `delivery_id`.
    """
    data = request.POST
    tracking = data.get('tracking_number') or data.get('tracking')
    delivery_id = data.get('delivery_id')

    dr = None
    if tracking:
        dr = DeliveryRequest.objects.filter(tracking_number=tracking).first()
    elif delivery_id:
        dr = DeliveryRequest.objects.filter(id=delivery_id).first()

    if not dr:
        return JsonResponse({'success': False, 'error': 'Delivery not found'}, status=404)

    # Ensure the requesting user is the intended recipient or the order's user
    allowed = False
    try:
        # If metadata contains user_id use it
        if isinstance(dr.metadata, dict) and dr.metadata.get('user_id'):
            if int(dr.metadata.get('user_id')) == request.user.id:
                allowed = True
        # fallback: try to map to Order
        if not allowed and dr.order_id:
            try:
                oid = int(str(dr.order_id).split('_')[-1])
                from listings.models import Order
                order = Order.objects.filter(id=oid).first()
                if order and getattr(order, 'user', None) and order.user.id == request.user.id:
                    allowed = True
            except Exception:
                pass
    except Exception:
        allowed = False

    if not allowed:
        return JsonResponse({'success': False, 'error': 'Not authorized'}, status=403)

    # Create confirmation (idempotent) and process release
    try:
        from .models import DeliveryConfirmation
        confirmation, created = DeliveryConfirmation.objects.get_or_create(
            delivery_request=dr,
            confirmed_by=request.user
        )
        # Process release (best-effort)
        confirmation.process_release()
        return JsonResponse({'success': True, 'created': created})
    except Exception as e:
        logger.exception('Error confirming delivery')
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@method_decorator(seller_or_delivery_or_admin_required, name='dispatch')
class CreateDeliveryView(LoginRequiredMixin, CreateView):
    """Create a new delivery request"""
    model = DeliveryRequest
    form_class = DeliveryRequestForm
    template_name = 'delivery/create_delivery.html'
    success_url = reverse_lazy('delivery:dashboard')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get user's pending orders that don't have deliveries yet
        user = self.request.user

        try:
            from listings.models import Order
            from django.db.models import Q
            from storefront.models import Store

            # Get user's stores
            user_stores = Store.objects.filter(owner=user)

            # For sellers: get their store orders
            if user_stores.exists():
                store_ids = user_stores.values_list('id', flat=True)

                # Get orders from user's stores
                pending_orders = Order.objects.filter(
                    Q(order_items__listing__store__id__in=store_ids) |
                    Q(order_items__listing__seller=user)
                ).distinct()
            else:
                # For other users, get their own orders
                pending_orders = Order.objects.filter(user=user)

            # Filter orders that don't have a delivery request yet
            orders_with_delivery = DeliveryRequest.objects.filter(
                order_id__isnull=False
            ).values_list('order_id', flat=True)

            # Convert to string for comparison (since order_id is CharField)
            orders_with_delivery = [str(id) for id in orders_with_delivery]

            # Exclude orders that already have deliveries
            pending_orders = pending_orders.exclude(
                Q(id__in=orders_with_delivery) |
                Q(tracking_number__isnull=False)
            ).order_by('-created_at')[:50]

            context['pending_orders'] = pending_orders

            # Get user's default store for pre-filling pickup info
            try:
                from storefront.models import Store
                default_store = Store.objects.filter(owner=user).first()
                context['default_store'] = default_store
            except Exception:
                context['default_store'] = None

        except Exception as e:
            logger.error(f"Error fetching pending orders: {e}")
            context['pending_orders'] = []
            context['default_store'] = None

        # Get delivery services and zones for dropdowns
        context['delivery_services'] = DeliveryService.objects.filter(is_active=True)
        context['delivery_zones'] = DeliveryZone.objects.filter(is_active=True)

        # Nearby drivers (for seller assistance in picking closest driver)
        try:
            drivers_qs = DeliveryPerson.objects.filter(
                is_available=True,
                current_latitude__isnull=False,
                current_longitude__isnull=False
            )
            context['nearby_drivers'] = [
                {
                    'id': d.id,
                    'name': d.user.get_full_name() or d.user.username,
                    'lat': float(d.current_latitude),
                    'lng': float(d.current_longitude),
                    'rating': float(d.rating or 0),
                    'vehicle': d.get_vehicle_type_display(),
                    'status': d.current_status,
                } for d in drivers_qs
            ]
        except Exception:
            context['nearby_drivers'] = []

        # Add API URLs for AJAX calls
        context['get_user_orders_url'] = reverse_lazy('delivery:get_user_orders')
        context['get_order_details_url'] = reverse_lazy('delivery:order_details')
        context['calculate_fee_url'] = reverse_lazy('delivery:calculate_fee_api')

        return context

    def get_initial(self):
        initial = super().get_initial()
        user = self.request.user

        # Set default pickup information from user's store
        try:
            from storefront.models import Store
            default_store = Store.objects.filter(owner=user).first()
            if default_store:
                initial.update({
                    'pickup_name': default_store.name,
                    'pickup_address': default_store.location or '',
                    'pickup_phone': default_store.payout_phone or getattr(user, 'phone_number', ''),
                    'pickup_latitude': default_store.location_latitude,
                    'pickup_longitude': default_store.location_longitude,
                    'pickup_email': user.email,
                })
        except Exception:
            pass

        # Set default delivery service
        default_service = DeliveryService.objects.filter(is_active=True).first()
        if default_service:
            initial['delivery_service'] = default_service

        # Set default estimated delivery time (tomorrow)
        initial['estimated_delivery_time'] = timezone.now() + timedelta(days=1)

        return initial

    def form_valid(self, form):
        # Set tracking number
        import uuid
        timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
        unique_id = str(uuid.uuid4())[:8].upper()
        form.instance.tracking_number = f"DLV{timestamp}{unique_id}"

        # Calculate delivery fee
        try:
            distance_km = float(self.request.POST.get('distance_km') or 0) or None
        except Exception:
            distance_km = None
        delivery_fee = calculate_delivery_fee(
            weight=form.cleaned_data['package_weight'],
            distance=distance_km,
            service_type=form.cleaned_data.get('delivery_service'),
            zone=form.cleaned_data.get('delivery_zone'),
            pickup_address=form.cleaned_data.get('pickup_address'),
            recipient_address=form.cleaned_data.get('recipient_address')
        )
        form.instance.delivery_fee = delivery_fee
        form.instance.total_amount = delivery_fee

        # Set metadata
        form.instance.metadata = {
            'created_by': self.request.user.username,
            'created_via': 'manual_form',
            'user_id': self.request.user.id,
        }
        if distance_km:
            form.instance.metadata['distance_km'] = distance_km

        # Attach store_id if available
        if self.request.POST.get('store_id'):
            form.instance.metadata['store_id'] = int(self.request.POST.get('store_id'))
        else:
            try:
                from storefront.models import Store
                user_stores = Store.objects.filter(owner=self.request.user)
                if user_stores.count() == 1:
                    form.instance.metadata['store_id'] = user_stores.first().id
            except Exception:
                pass

        # External delivery payment requirement (non-marketplace)
        if not form.instance.order_id and not getattr(self.request, 'is_seller', False):
            form.instance.payment_status = 'pending'
            form.instance.metadata['external_delivery'] = True
            form.instance.metadata['requires_payment'] = True

        # Save the form
        response = super().form_valid(form)

        # Update order tracking number if order_id was provided
        if form.instance.order_id:
            try:
                from listings.models import Order
                order = Order.objects.filter(id=form.instance.order_id).first()
                if order and not order.tracking_number:
                    order.tracking_number = form.instance.tracking_number
                    order.save(update_fields=['tracking_number'])
            except Exception as e:
                logger.error(f"Error updating order tracking: {e}")

        # Send notification
        try:
            send_delivery_notification(
                delivery=self.object,
                notification_type='delivery_created',
                recipient=self.request.user
            )
        except Exception as e:
            logger.error(f"Error sending notification: {e}")

        if isinstance(self.object.metadata, dict) and self.object.metadata.get('external_delivery') and self.object.payment_status != 'paid':
            messages.warning(self.request, f"Delivery created. Payment of KSh {self.object.total_amount} is required before dispatch.")

        return response


@method_decorator(seller_or_delivery_or_admin_required, name='dispatch')
class UpdateDeliveryStatusView(LoginRequiredMixin, UpdateView):
    """Update delivery status"""
    model = DeliveryRequest
    fields = ['status']
    template_name = 'delivery/update_status.html'

    def get_success_url(self):
        return reverse_lazy('delivery:delivery_detail', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        # Ensure self.object is populated
        self.object = self.get_object()
        old_status = self.object.status
        new_status = form.cleaned_data['status']

        if isinstance(self.object.metadata, dict) and self.object.metadata.get('external_delivery'):
            if self.object.payment_status != 'paid' and new_status not in ['pending', 'accepted', 'cancelled']:
                messages.error(self.request, "Payment required before dispatching this delivery.")
                return redirect(self.get_success_url())

        # Validate status transition
        valid_transitions = self.get_valid_transitions(old_status)
        if new_status not in valid_transitions:
            form.add_error('status', f'Invalid status transition from {old_status} to {new_status}')
            return self.form_invalid(form)

        # If attempting to mark delivered, enforce driver-only or staff
        notes = self.request.POST.get('notes', '')
        if new_status == 'delivered':
            user = self.request.user
            # Staff may mark delivered
            if user.is_staff or user.is_superuser:
                self.object.update_status(new_status, notes, changed_by_user=user)
            else:
                # Only assigned delivery person may mark delivered — require OTP flow
                if not hasattr(user, 'delivery_person') or not self.object.delivery_person or self.object.delivery_person != user.delivery_person:
                    form.add_error('status', 'Only the assigned delivery person may mark this delivery as delivered')
                    return self.form_invalid(form)

                # Generate OTP and send to recipient; driver must verify OTP to complete delivery
                from django.utils import timezone
                import random
                from django.contrib.auth.hashers import make_password

                # Rate-limit OTP generation per driver+delivery: max 3 per 5 minutes
                recent_limit = timezone.now() - timedelta(minutes=5)
                recent_count = DeliveryOTP.objects.filter(
                    delivery_request=self.object,
                    created_by=user.delivery_person,
                    created_at__gte=recent_limit
                ).count()
                if recent_count >= 3:
                    form.add_error(None, 'Too many verification code requests. Try again later.')
                    return self.form_invalid(form)

                code = f"{random.randint(100000, 999999)}"
                expires_at = timezone.now() + timedelta(minutes=5)
                hashed = make_password(code)
                ip = self.request.META.get('REMOTE_ADDR') or self.request.META.get('HTTP_X_FORWARDED_FOR')
                otp = DeliveryOTP.objects.create(
                    delivery_request=self.object,
                    hashed_code=hashed,
                    created_by=user.delivery_person,
                    expires_at=expires_at,
                    request_ip=ip
                )

                # Audit log
                try:
                    from .models import DeliveryAuditLog
                    DeliveryAuditLog.objects.create(
                        delivery_request=self.object,
                        user=user,
                        event_type='otp_created',
                        message=f'OTP created by {user.username}',
                        meta={'otp_id': otp.id, 'expires_at': str(expires_at)}
                    )
                except Exception:
                    pass

                # Send email to recipient using Brevo helper when possible
                try:
                    recipient = None
                    if self.object.recipient_email:
                        recipient = self.object.recipient_email
                    else:
                        if isinstance(self.object.metadata, dict) and self.object.metadata.get('user_email'):
                            recipient = self.object.metadata.get('user_email')
                        else:
                            try:
                                oid = int(str(self.object.order_id).split('_')[-1])
                                order = Order.objects.filter(id=oid).first()
                                if order and getattr(order, 'email', None):
                                    recipient = order.email
                                elif order and getattr(order, 'user', None) and getattr(order.user, 'email', None):
                                    recipient = order.user.email
                            except Exception:
                                recipient = None

                    if recipient:
                        try:
                            from baysoko.utils.email_helpers import send_email_brevo
                            subject = f"Your delivery verification code: {self.object.tracking_number}"
                            plain = f"Your one-time verification code is {code}. It expires in 5 minutes."
                            html = f"<p>Your one-time verification code is <strong>{code}</strong>. It expires in 5 minutes.</p>"
                            send_email_brevo(subject, plain, html, [recipient])
                        except Exception:
                            try:
                                from django.core.mail import send_mail as _send
                                _send(subject, f"Your code is {code}", None, [recipient])
                            except Exception:
                                pass
                except Exception:
                    pass

                messages.info(self.request, 'A verification code has been sent to the recipient. Please verify to complete delivery.')
                return redirect(self.get_success_url())
        else:
            self.object.update_status(new_status, notes, changed_by_user=self.request.user)

        # Try to update ecommerce order and enqueue external sync
        try:
            from . import integration as integration_module
            try:
                integration_module.update_order_from_delivery(self.object)
            except Exception:
                pass
        except Exception:
            pass

        try:
            from . import tasks as delivery_tasks
            try:
                delivery_tasks.sync_with_external_system.delay(self.object.id)
            except Exception:
                delivery_tasks.sync_with_external_system(self.object.id)
        except Exception:
            pass

        return redirect(self.get_success_url())

    def get_valid_transitions(self, current_status):
        """Define valid status transitions"""
        transitions = {
            'pending': ['accepted', 'cancelled'],
            'accepted': ['assigned', 'cancelled'],
            'assigned': ['picked_up', 'cancelled'],
            'picked_up': ['in_transit', 'cancelled'],
            'in_transit': ['out_for_delivery', 'delivered', 'failed'],
            'out_for_delivery': ['delivered', 'failed'],
            'delivered': [],
            'failed': ['returned', 'accepted'],
            'cancelled': [],
            'returned': ['accepted', 'cancelled'],
        }
        return transitions.get(current_status, [])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context['delivery'] = self.get_object()
        except Exception:
            context['delivery'] = getattr(self, 'object', None)
        context['status_choices'] = DeliveryRequest.STATUS_CHOICES
        # Provide the set of valid transitions for the current delivery status
        try:
            current = None
            if context.get('delivery'):
                current = context['delivery'].status
            elif getattr(self, 'object', None):
                current = self.object.status
            context['valid_transitions'] = self.get_valid_transitions(current) if current is not None else []
        except Exception:
            context['valid_transitions'] = []
        return context


@method_decorator(delivery_person_required, name='dispatch')
class DriverDashboardView(LoginRequiredMixin, TemplateView):
    """Dashboard for delivery drivers"""
    template_name = 'delivery/driver_dashboard.html'

    def calculate_weekly_earnings(self, driver):
        """Calculate weekly earnings for driver"""
        week_ago = timezone.now() - timedelta(days=7)

        earnings = DeliveryRequest.objects.filter(
            delivery_person=driver,
            status='delivered',
            actual_delivery_time__gte=week_ago
        ).aggregate(
            total=Sum('delivery_fee')
        )['total'] or 0

        # Assume driver gets 70% of delivery fee
        return float(earnings * Decimal('0.7'))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        driver = self.request.user.delivery_person

        # Get today's assignments
        today = timezone.now().date()
        context['today_assignments'] = DeliveryRequest.objects.filter(
            delivery_person=driver,
            status__in=['assigned', 'picked_up', 'in_transit', 'out_for_delivery'],
            created_at__date=today
        ).order_by('priority', 'estimated_delivery_time')

        # Get pending pickups
        context['pending_pickups'] = DeliveryRequest.objects.filter(
            delivery_person=driver,
            status='assigned'
        ).order_by('estimated_delivery_time')

        # Get recent deliveries
        context['recent_deliveries'] = DeliveryRequest.objects.filter(
            delivery_person=driver,
            status='delivered'
        ).order_by('-actual_delivery_time')[:10]

        # Driver statistics
        weekly_earnings = self.calculate_weekly_earnings(driver)
        context['driver_stats'] = {
            'total': driver.total_deliveries,
            'completed': driver.completed_deliveries,
            'success_rate': (driver.completed_deliveries / driver.total_deliveries * 100) if driver.total_deliveries > 0 else 0,
            'rating': driver.rating,
            'weekly_earnings': weekly_earnings,
        }

        # Update driver status if needed
        if self.request.GET.get('status'):
            new_status = self.request.GET.get('status')
            if new_status in dict(DeliveryPerson.STATUS_CHOICES):
                driver.current_status = new_status
                driver.is_available = (new_status == 'available')
                driver.save()

        return context


@login_required
@delivery_person_required
@require_POST
def update_driver_location(request):
    """Update driver's current location"""
    try:
        data = json.loads(request.body)
        lat = data.get('latitude')
        lng = data.get('longitude')
        status = data.get('status')

        driver = request.user.delivery_person

        if lat and lng:
            driver.current_latitude = lat
            driver.current_longitude = lng
            driver.location_updated_at = timezone.now()

        if status:
            driver.current_status = status
            driver.is_available = (status == 'available')

        driver.save()

        return JsonResponse({
            'success': True,
            'message': 'Location updated successfully'
        })
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({
            'success': False,
            'error': 'Invalid request'
        }, status=400)


@login_required
@delivery_person_required
@require_POST
def update_driver_status(request):
    """Update driver's status"""
    try:
        data = json.loads(request.body)
        status = data.get('status')

        if status not in dict(DeliveryPerson.STATUS_CHOICES):
            return JsonResponse({
                'success': False,
                'error': 'Invalid status'
            }, status=400)

        driver = request.user.delivery_person
        driver.current_status = status
        driver.is_available = (status == 'available')
        driver.save()

        return JsonResponse({
            'success': True,
            'message': f'Status updated to {status}'
        })
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({
            'success': False,
            'error': 'Invalid request'
        }, status=400)


@login_required
def track_delivery(request, tracking_number):
    """Public tracking page"""
    delivery = get_object_or_404(DeliveryRequest, tracking_number=tracking_number)

    # Get status history
    status_history = delivery.status_history.all().order_by('-created_at')

    # Calculate estimated delivery time
    estimated_time = None
    if delivery.estimated_delivery_time:
        estimated_time = delivery.estimated_delivery_time

    # Determine if the current user (if authenticated) is allowed to confirm delivery
    can_confirm = False
    if request.user.is_authenticated:
        # If metadata has user_id and matches
        try:
            if isinstance(delivery.metadata, dict) and delivery.metadata.get('user_id'):
                if int(delivery.metadata.get('user_id')) == request.user.id:
                    can_confirm = True
        except Exception:
            pass

        # Fallback: map delivery to an Order and check ownership
        if not can_confirm and delivery.order_id:
            try:
                oid = int(str(delivery.order_id).split('_')[-1])
                order = Order.objects.filter(id=oid).first()
                if order and getattr(order, 'user', None) and order.user == request.user:
                    can_confirm = True
            except Exception:
                pass

    # Pull order items (role-based visibility)
    order = None
    visible_items = []
    total_qty = 0
    try:
        if delivery.order_id:
            oid = int(str(delivery.order_id).split('_')[-1])
            order = Order.objects.filter(id=oid).first()
    except Exception:
        order = None

    if order and request.user.is_authenticated:
        items_qs = order.order_items.select_related('listing', 'listing__seller')
        if request.user.is_staff or request.user.is_superuser:
            visible_items = list(items_qs)
        elif getattr(order, 'user', None) == request.user:
            visible_items = list(items_qs)
        else:
            # seller view: only their items
            visible_items = list(items_qs.filter(listing__seller=request.user))
        try:
            total_qty = sum(int(i.quantity or 0) for i in visible_items)
        except Exception:
            total_qty = 0

    # Store markers for map (only stores with valid coordinates)
    store_markers = []
    try:
        from storefront.models import Store
        stores_qs = Store.objects.filter(
            is_active=True,
            location_latitude__isnull=False,
            location_longitude__isnull=False
        ).exclude(location_latitude='', location_longitude='')[:200]
        for s in stores_qs:
            try:
                store_markers.append({
                    'name': s.name,
                    'location': s.location,
                    'slug': s.slug,
                    'lat': float(s.location_latitude),
                    'lng': float(s.location_longitude),
                })
            except Exception:
                continue
    except Exception:
        store_markers = []

    context = {
        'delivery': delivery,
        'status_history': status_history,
        'estimated_time': estimated_time,
        'show_details': True,  # Public can see basic info
        'can_confirm': can_confirm,
        'order': order,
        'order_items': visible_items,
        'order_items_total_qty': total_qty,
        'store_markers_json': json.dumps(store_markers),
    }

    return render(request, 'delivery/tracking.html', context)


@login_required
def submit_proof(request, pk):
    """Handle proof of delivery submissions (files, signatures, codes)."""
    delivery = get_object_or_404(DeliveryRequest, pk=pk)

    # Permission: only staff, delivery_person assigned, or store owner may submit proof
    user = request.user
    allowed = False
    if user.is_staff or user.is_superuser:
        allowed = True
    if hasattr(user, 'delivery_person') and delivery.delivery_person and delivery.delivery_person == user.delivery_person:
        allowed = True
    try:
        from storefront.models import Store
        stores = Store.objects.filter(owner=user)
        if stores.exists() and isinstance(delivery.metadata, dict):
            sid = delivery.metadata.get('store_id')
            if sid and any(str(s.id) == str(sid) or s.id == int(sid) for s in stores):
                allowed = True
    except Exception:
        pass

    if not allowed:
        messages.error(request, 'You do not have permission to submit proof for this delivery.')
        return redirect('delivery:delivery_detail', pk=delivery.pk)

    if request.method == 'POST':
        proof_type = request.POST.get('proof_type')
        notes = request.POST.get('notes', '')
        verification_code = request.POST.get('verification_code')
        recipient_name = request.POST.get('recipient_name')
        recipient_id_type = request.POST.get('recipient_id_type')
        recipient_id_number = request.POST.get('recipient_id_number')

        file = request.FILES.get('file')
        signature_data = request.POST.get('signature_data')

        proof = DeliveryProof.objects.create(
            delivery_request=delivery,
            proof_type=proof_type or 'photo',
            file=file if file else None,
            signature_data=signature_data or None,
            verification_code=verification_code or None,
            recipient_name=recipient_name or None,
            recipient_id_type=recipient_id_type or None,
            recipient_id_number=recipient_id_number or None,
            notes=notes or ''
        )

        # Mark delivery metadata/proof_of_delivery for reference
        try:
            meta = delivery.metadata or {}
            meta['proof'] = meta.get('proof', [])
            meta['proof'].append({'id': proof.id, 'type': proof.proof_type})
            delivery.metadata = meta
            delivery.save(update_fields=['metadata'])
        except Exception:
            pass

        messages.success(request, 'Proof of delivery submitted successfully.')
        return redirect('delivery:delivery_detail', pk=delivery.pk)

    # If not POST, redirect back
    return redirect('delivery:delivery_detail', pk=delivery.pk)


@login_required
@seller_or_delivery_or_admin_required
def delivery_reports(request):
    """Generate delivery reports"""
    report_type = request.GET.get('type', 'daily')

    if report_type == 'daily':
        return generate_daily_report(request)
    elif report_type == 'weekly':
        return generate_weekly_report(request)
    elif report_type == 'monthly':
        return generate_monthly_report(request)
    elif report_type == 'driver':
        return generate_driver_report(request)
    elif report_type == 'zone':
        return generate_zone_report(request)

    return render(request, 'delivery/reports/daily_report.html')


def generate_daily_report(request):
    """Generate daily delivery report"""
    date = request.GET.get('date', timezone.now().date())

    if isinstance(date, str):
        date = datetime.strptime(date, '%Y-%m-%d').date()

    base_qs = _get_deliveries_for_user(request.user, request=request)
    deliveries = base_qs.filter(
        created_at__date=date
    ).select_related('delivery_person', 'delivery_service')

    # Summary statistics
    summary = {
        'total': deliveries.count(),
        'delivered': deliveries.filter(status='delivered').count(),
        'pending': deliveries.filter(status__in=['pending', 'accepted', 'assigned']).count(),
        'in_transit': deliveries.filter(status__in=['picked_up', 'in_transit', 'out_for_delivery']).count(),
        'failed': deliveries.filter(status='failed').count(),
        'revenue': deliveries.filter(payment_status='paid').aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
    }

    context = {
        'report_type': 'daily',
        'date': date,
        'deliveries': deliveries,
        'summary': summary,
    }

    if request.GET.get('format') == 'csv':
        return export_to_csv(deliveries, f'daily_report_{date}.csv')

    return render(request, 'delivery/reports/daily_report.html', context)


def generate_weekly_report(request):
    """Generate weekly delivery report"""
    from datetime import timedelta
    today = timezone.now().date()
    start_of_week = today - timedelta(days=today.weekday())

    base_qs = _get_deliveries_for_user(request.user, request=request)
    deliveries = base_qs.filter(
        created_at__date__gte=start_of_week
    ).select_related('delivery_person', 'delivery_service')

    summary = {
        'total': deliveries.count(),
        'delivered': deliveries.filter(status='delivered').count(),
        'pending': deliveries.filter(status__in=['pending', 'accepted', 'assigned']).count(),
        'in_transit': deliveries.filter(status__in=['picked_up', 'in_transit', 'out_for_delivery']).count(),
        'failed': deliveries.filter(status='failed').count(),
        'revenue': deliveries.filter(payment_status='paid').aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
    }

    context = {
        'report_type': 'weekly',
        'start_date': start_of_week,
        'end_date': today,
        'deliveries': deliveries,
        'summary': summary,
    }

    if request.GET.get('format') == 'csv':
        return export_to_csv(deliveries, f'weekly_report_{start_of_week}_to_{today}.csv')

    return render(request, 'delivery/reports/weekly_report.html', context)


def generate_monthly_report(request):
    """Generate monthly delivery report"""
    today = timezone.now().date()
    start_of_month = today.replace(day=1)

    base_qs = _get_deliveries_for_user(request.user, request=request)
    deliveries = base_qs.filter(
        created_at__date__gte=start_of_month
    ).select_related('delivery_person', 'delivery_service')

    summary = {
        'total': deliveries.count(),
        'delivered': deliveries.filter(status='delivered').count(),
        'pending': deliveries.filter(status__in=['pending', 'accepted', 'assigned']).count(),
        'in_transit': deliveries.filter(status__in=['picked_up', 'in_transit', 'out_for_delivery']).count(),
        'failed': deliveries.filter(status='failed').count(),
        'revenue': deliveries.filter(payment_status='paid').aggregate(
            total=Sum('total_amount')
        )['total'] or 0,
    }

    context = {
        'report_type': 'monthly',
        'start_date': start_of_month,
        'end_date': today,
        'deliveries': deliveries,
        'summary': summary,
    }

    if request.GET.get('format') == 'csv':
        return export_to_csv(deliveries, f'monthly_report_{start_of_month}_to_{today}.csv')

    return render(request, 'delivery/reports/monthly_report.html', context)


def generate_driver_report(request):
    """Generate driver performance report"""
    base_qs = _get_deliveries_for_user(request.user, request=request)
    # Drivers relevant to the user's deliveries
    drivers = DeliveryPerson.objects.filter(assignments__in=base_qs).distinct()

    if request.GET.get('driver_id'):
        drivers = drivers.filter(id=request.GET.get('driver_id'))

    driver_stats = []
    for driver in drivers:
        assignments = driver.assignments.filter(id__in=base_qs.values_list('id', flat=True))
        completed = assignments.filter(status='delivered')

        stats = {
            'driver': driver,
            'total_assignments': assignments.count(),
            'completed': completed.count(),
            'completion_rate': (completed.count() / assignments.count() * 100) if assignments.count() > 0 else 0,
            'avg_rating': completed.aggregate(avg=Avg('rating__rating'))['avg'] or 0,
            'total_revenue': completed.aggregate(total=Sum('delivery_fee'))['total'] or 0,
        }
        driver_stats.append(stats)

    context = {
        'report_type': 'driver',
        'driver_stats': driver_stats,
        'drivers': drivers,
    }

    return render(request, 'delivery/reports/driver_report.html', context)


def generate_zone_report(request):
    """Generate delivery zone performance report"""
    zones = DeliveryZone.objects.filter(is_active=True)

    base_qs = _get_deliveries_for_user(request.user, request=request)
    zone_stats = []
    for zone in zones:
        deliveries = base_qs.filter(delivery_zone=zone)

        stats = {
            'zone': zone,
            'total_deliveries': deliveries.count(),
            'completed': deliveries.filter(status='delivered').count(),
            'pending': deliveries.filter(status__in=['pending', 'accepted', 'assigned']).count(),
            'total_revenue': deliveries.aggregate(total=Sum('delivery_fee'))['total'] or 0,
            'avg_delivery_fee': deliveries.aggregate(avg=Avg('delivery_fee'))['avg'] or 0,
        }
        zone_stats.append(stats)

    context = {
        'report_type': 'zone',
        'zone_stats': zone_stats,
    }

    return render(request, 'delivery/reports/zone_report.html', context)


@require_POST
@login_required
def bulk_update_status(request):
    """Bulk update delivery status"""
    try:
        data = json.loads(request.body)
        delivery_ids = data.get('delivery_ids', [])
        new_status = data.get('status')
        notes = data.get('notes', '')

        if not delivery_ids or not new_status:
            return JsonResponse({'error': 'Missing required fields'}, status=400)

        # Get deliveries
        deliveries = DeliveryRequest.objects.filter(id__in=delivery_ids)

        # Update each delivery
        updated = 0
        for delivery in deliveries:
            old_status = delivery.status
            if new_status != old_status:
                delivery.update_status(new_status, notes, changed_by_user=request.user)
                updated += 1

        return JsonResponse({
            'success': True,
            'message': f'Updated {updated} deliveries',
            'updated': updated
        })

    except (json.JSONDecodeError, ValueError) as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
def delivery_analytics(request):
    """Delivery analytics dashboard"""
    user = request.user

    # Time period
    period = request.GET.get('period', 'week')

    if period == 'week':
        start_date = timezone.now() - timedelta(days=7)
    elif period == 'month':
        start_date = timezone.now() - timedelta(days=30)
    elif period == 'quarter':
        start_date = timezone.now() - timedelta(days=90)
    else:
        start_date = timezone.now() - timedelta(days=7)

    # Get deliveries for the period (scoped to user)
    base_deliveries = _get_deliveries_for_user(user, request=request)
    deliveries = base_deliveries.filter(created_at__gte=start_date)

    # Calculate metrics
    total_deliveries = deliveries.count()
    completed = deliveries.filter(status='delivered').count()
    success_rate = (completed / total_deliveries * 100) if total_deliveries > 0 else 0

    # Average delivery time in hours
    completed_deliveries = deliveries.filter(
        status='delivered',
        actual_delivery_time__isnull=False,
        pickup_time__isnull=False
    )

    avg_time_hours = None
    if completed_deliveries.exists():
        total_hours = 0
        count = 0
        for delivery in completed_deliveries:
            if delivery.actual_delivery_time and delivery.pickup_time:
                duration = delivery.actual_delivery_time - delivery.pickup_time
                total_hours += duration.total_seconds() / 3600
                count += 1

        if count > 0:
            avg_time_hours = total_hours / count

    # Revenue
    revenue = deliveries.filter(
        payment_status='paid'
    ).aggregate(
        total=Sum('total_amount')
    )['total'] or 0

    # Top drivers (only for admin/staff)
    top_drivers = None
    if user.is_staff or user.is_superuser:
        top_drivers = DeliveryPerson.objects.annotate(
            delivery_count=Count('assignments'),
            avg_rating=Avg('assignments__rating__rating')
        ).order_by('-delivery_count')[:5]

    # Zone performance - calculate properly for all users
    try:
        from storefront.models import Store
        stores = Store.objects.filter(owner=user)

        if user.is_staff or user.is_superuser:
            # Admin sees all zones
            zone_performance = DeliveryZone.objects.annotate(
                delivery_count=Count('deliveryrequest'),
                avg_fee=Avg('deliveryrequest__delivery_fee'),
                total_revenue=Sum('deliveryrequest__delivery_fee')
            ).order_by('-delivery_count')
        elif stores.exists():
            # Seller sees zones for their stores
            store_ids = [store.id for store in stores]

            # Get zone IDs that have deliveries from seller's stores
            zone_ids = base_deliveries.filter(
                delivery_zone__isnull=False
            ).values_list('delivery_zone_id', flat=True).distinct()

            # Get zones with performance data
            zone_performance = DeliveryZone.objects.filter(
                id__in=zone_ids
            ).annotate(
                delivery_count=Count('deliveryrequest', filter=Q(
                    deliveryrequest__id__in=base_deliveries.values_list('id', flat=True)
                )),
                avg_fee=Avg('deliveryrequest__delivery_fee', filter=Q(
                    deliveryrequest__id__in=base_deliveries.values_list('id', flat=True)
                )),
                total_revenue=Sum('deliveryrequest__delivery_fee', filter=Q(
                    deliveryrequest__id__in=base_deliveries.values_list('id', flat=True)
                ))
            ).order_by('-delivery_count')
        else:
            zone_performance = DeliveryZone.objects.none()
    except Exception:
        zone_performance = DeliveryZone.objects.none()

    # Status distribution for chart
    status_distribution = deliveries.values('status').annotate(
        count=Count('id')
    ).order_by('status')

    # Weekly activity for chart
    weekly_activity = []
    for i in range(7):
        date = (timezone.now() - timedelta(days=i)).date()
        count = deliveries.filter(created_at__date=date).count()
        weekly_activity.append({
            'date': date.strftime('%Y-%m-%d'),
            'day': date.strftime('%a'),
            'count': count
        })
    weekly_activity.reverse()  # Show oldest to newest

    context = {
        'period': period,
        'total_deliveries': total_deliveries,
        'completed_deliveries': completed,
        'success_rate': round(success_rate, 2),
        'average_delivery_time_hours': round(avg_time_hours, 2) if avg_time_hours else None,
        'revenue': revenue,
        'top_drivers': top_drivers,
        'zone_performance': zone_performance,
        'status_distribution': list(status_distribution),
        'weekly_activity': weekly_activity,
        'start_date': start_date,
        'end_date': timezone.now(),
        'is_admin': user.is_staff or user.is_superuser,
        'is_delivery_person': hasattr(user, 'delivery_person'),
    }

    return render(request, 'delivery/reports/analytics.html', context)


@require_POST
@login_required
@delivery_person_required
def verify_delivery_otp(request, pk):
    """Verify OTP submitted by driver and mark delivery as delivered."""
    try:
        code = request.POST.get('code')
        if not code:
            return JsonResponse({'success': False, 'error': 'Missing code'}, status=400)

        delivery = get_object_or_404(DeliveryRequest, pk=pk)

        # Ensure the requester is the assigned driver
        user = request.user
        if not hasattr(user, 'delivery_person') or not delivery.delivery_person or delivery.delivery_person != user.delivery_person:
            return JsonResponse({'success': False, 'error': 'Not authorized for this delivery'}, status=403)

        from django.utils import timezone
        otp = DeliveryOTP.objects.filter(delivery_request=delivery, used=False, expires_at__gte=timezone.now()).order_by('-created_at').first()
        if not otp:
            return JsonResponse({'success': False, 'error': 'No valid code found or it has expired'}, status=400)
        # Increment attempts and enforce small attempt limit
        otp.attempts = otp.attempts + 1
        otp.save(update_fields=['attempts'])

        if otp.attempts > 5:
            # Audit
            try:
                DeliveryAuditLog.objects.create(
                    delivery_request=delivery,
                    user=user,
                    event_type='otp_locked',
                    message='OTP locked after too many attempts',
                    meta={'otp_id': otp.id}
                )
            except Exception:
                pass
            return JsonResponse({'success': False, 'error': 'Too many attempts. Request a new code.'}, status=403)

        if not otp.is_valid(code):
            try:
                DeliveryAuditLog.objects.create(
                    delivery_request=delivery,
                    user=user,
                    event_type='otp_failed',
                    message='OTP validation failed',
                    meta={'otp_id': otp.id, 'attempts': otp.attempts}
                )
            except Exception:
                pass
            return JsonResponse({'success': False, 'error': 'Invalid or expired code'}, status=400)

        # Mark OTP used and update status
        otp.mark_used()
        try:
            DeliveryAuditLog.objects.create(
                delivery_request=delivery,
                user=user,
                event_type='otp_verified',
                message='OTP verified and delivery marked delivered',
                meta={'otp_id': otp.id}
            )
        except Exception:
            pass

        delivery.update_status('delivered', notes=f'Verified via OTP by {user.username}', changed_by_user=user)

        return JsonResponse({'success': True, 'message': 'Delivery marked as delivered'})
    except Exception as e:
        logger.exception(f"Error verifying OTP: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def export_to_csv(queryset, filename):
    """Export queryset to CSV"""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)

    # Write headers
    writer.writerow([
        'Tracking Number', 'Order ID', 'Status', 'Recipient Name',
        'Recipient Phone', 'Delivery Fee', 'Payment Status',
        'Created At', 'Delivered At', 'Driver'
    ])

    # Write data
    for item in queryset:
        writer.writerow([
            item.tracking_number,
            item.order_id,
            item.get_status_display(),
            item.recipient_name,
            item.recipient_phone,
            item.delivery_fee,
            item.get_payment_status_display(),
            item.created_at.strftime('%Y-%m-%d %H:%M'),
            item.actual_delivery_time.strftime('%Y-%m-%d %H:%M') if item.actual_delivery_time else '',
            item.delivery_person.user.get_full_name() if item.delivery_person else ''
        ])

    return response


@csrf_exempt
@require_POST
def glovo_webhook(request):
    """Receive Glovo webhook events and process them.

    Validates `X-Glovo-Token` header against settings.GLOVO_WEBHOOK_TOKEN.
    """
    try:
        # Respect site setting to disable Glovo webhooks entirely
        from django.conf import settings as _dj_settings
        if not getattr(_dj_settings, 'GLOVO_ENABLED', False):
            return HttpResponse(status=404)

        provided = request.headers.get('X-Glovo-Token') or request.META.get('HTTP_X_GLOVO_TOKEN')
        expected = getattr(__import__('django.conf').conf.settings, 'GLOVO_WEBHOOK_TOKEN', None)
        if expected and provided != expected:
            return HttpResponse(status=401)

        data = json.loads(request.body)
        event = data.get('event') or data.get('type')
        logger.info('Received Glovo webhook event: %s', event)

        # Handle order created
        if event == 'ORDER_CREATED' or (isinstance(data.get('order'), dict) and data.get('event') == 'ORDER_CREATED'):
            order = data.get('order') or {}
            order_id = order.get('order_id')
            # Minimal handling: create a DeliveryRequest record pointing to order_id
            try:
                dr = DeliveryRequest.objects.create(
                    order_id=str(order_id) if order_id else '',
                    pickup_name=order.get('store_name', 'Glovo Store'),
                    pickup_address=order.get('store_address', ''),
                    recipient_name=order.get('recipient', {}).get('name', '') if isinstance(order.get('recipient'), dict) else '',
                    recipient_address=order.get('recipient', {}).get('address', '') if isinstance(order.get('recipient'), dict) else '',
                    recipient_phone=order.get('recipient', {}).get('phone', '') if isinstance(order.get('recipient'), dict) else '',
                    recipient_email=order.get('recipient', {}).get('email', '') if isinstance(order.get('recipient'), dict) else '',
                    package_description=order.get('items', []),
                    package_weight=order.get('weight', 0) or 0,
                    delivery_fee=order.get('delivery_fee', 0) or 0,
                    total_amount=order.get('total_value', 0) or 0,
                    pickup_time=None
                )
                logger.info('Created DeliveryRequest for Glovo order %s -> %s', order_id, dr.id)
            except Exception:
                logger.exception('Failed creating DeliveryRequest for Glovo order')

        # Handle status updates
        if event == 'ORDER_STATUS_UPDATED' or (isinstance(data.get('order'), dict) and data.get('event') == 'ORDER_STATUS_UPDATED'):
            order = data.get('order') or {}
            order_id = order.get('order_id')
            status = order.get('status')
            # Map Glovo status to our delivery statuses if possible
            try:
                if order_id:
                    dr = DeliveryRequest.objects.filter(order_id=str(order_id)).first()
                    if dr and status:
                        # Basic mapping
                        mapping = {
                            'ACCEPTED': 'accepted',
                            'READY_FOR_PICKUP': 'picked_up',
                            'PICKED_UP': 'in_transit',
                            'DELIVERED': 'delivered',
                            'CANCELLED': 'cancelled'
                        }
                        new_status = mapping.get(status, None)
                        if new_status:
                            dr.update_status(new_status, notes=f'Glovo webhook: {status}')
            except Exception:
                logger.exception('Error processing Glovo status update')

        return HttpResponse(status=200)
    except Exception as e:
        logger.exception('Error in glovo_webhook: %s', e)
        return HttpResponse(status=500)


@require_GET
@login_required
@seller_or_delivery_or_admin_required
def get_order_details(request, order_id):
    """Get order details for pre-filling delivery form"""
    try:
        from listings.models import Order, CartItem
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # Get the order
        order = Order.objects.filter(id=order_id).first()
        if not order:
            return JsonResponse({'error': 'Order not found'}, status=404)

        # Check permission
        user = request.user
        if not (user.is_staff or user.is_superuser):
            if order.user_id == user.id:
                pass
            else:
            # Check if user owns a store that sold items in this order
                try:
                    from storefront.models import Store
                    stores = Store.objects.filter(owner=user)
                    if not stores.exists():
                        return JsonResponse({'error': 'Access denied'}, status=403)

                    # Check if order contains items from user's stores
                    order_has_user_items = False
                    for item in order.order_items.all():
                        if hasattr(item.listing, 'store') and item.listing.store in stores:
                            order_has_user_items = True
                            break
                        elif hasattr(item, 'product') and hasattr(item.product, 'store') and item.product.store in stores:
                            order_has_user_items = True
                            break

                    if not order_has_user_items:
                        return JsonResponse({'error': 'Access denied'}, status=403)
                except Exception as e:
                    logger.error(f"Permission check error: {e}")
                    return JsonResponse({'error': 'Access denied'}, status=403)

        # Calculate package weight (seller-scoped if not admin)
        package_weight = 0.0
        package_items = []
        seller_store_ids = set()
        if not (user.is_staff or user.is_superuser):
            try:
                seller_store_ids = set(Store.objects.filter(owner=user).values_list('id', flat=True))
            except Exception:
                seller_store_ids = set()

        try:
            # Try order_items first
            for item in order.order_items.all():
                if seller_store_ids:
                    try:
                        if hasattr(item.listing, 'store') and item.listing.store_id not in seller_store_ids and item.listing.seller_id != user.id:
                            continue
                    except Exception:
                        pass
                try:
                    weight = getattr(item.listing, 'weight', 1.0)
                    if weight:
                        item_weight = float(weight) * (item.quantity or 1)
                        package_weight += item_weight
                        package_items.append({
                            'name': item.listing.title,
                            'quantity': item.quantity,
                            'weight': item_weight
                        })
                except Exception:
                    package_weight += 1.0 * (item.quantity or 1)
        except Exception:
            # Fallback to cart items
            try:
                cart_items = CartItem.objects.filter(order=order)
                for item in cart_items:
                    try:
                        if seller_store_ids:
                            try:
                                if hasattr(item.product, 'store') and item.product.store_id not in seller_store_ids:
                                    continue
                            except Exception:
                                pass
                        weight = getattr(item.product, 'weight', 1.0)
                        item_weight = float(weight) * item.quantity
                        package_weight += item_weight
                        package_items.append({
                            'name': item.product.name,
                            'quantity': item.quantity,
                            'weight': item_weight
                        })
                    except Exception:
                        package_weight += 1.0 * item.quantity
            except Exception:
                package_weight = 1.0

        # Prepare response data
        pickup_name = ''
        pickup_address = ''
        pickup_lat = None
        pickup_lng = None
        try:
            first_item = order.order_items.select_related('listing__store').first()
            if first_item and first_item.listing and first_item.listing.store:
                store = first_item.listing.store
                pickup_name = store.name
                pickup_address = store.location or ''
                pickup_lat = float(store.location_latitude) if store.location_latitude else None
                pickup_lng = float(store.location_longitude) if store.location_longitude else None
        except Exception:
            pass

        data = {
            'success': True,
            'order': {
                'id': order.id,
                'order_number': getattr(order, 'order_number', str(order.id)),
                'created_at': order.created_at.isoformat() if order.created_at else None,
                'total_amount': float(order.total_price) if order.total_price else 0.0,
                'currency': 'KES',
            },
            'customer': {
                'name': f"{order.first_name} {order.last_name}".strip() or
                       (order.user.get_full_name() if order.user else ''),
                'email': order.email or (order.user.email if order.user else ''),
                'phone': order.phone_number or '',
                'shipping_address': order.shipping_address or '',
                'shipping_latitude': float(order.shipping_latitude) if order.shipping_latitude else None,
                'shipping_longitude': float(order.shipping_longitude) if order.shipping_longitude else None,
                'shipping_place_id': order.shipping_place_id or '',
                'city': getattr(order, 'city', ''),
                'state': getattr(order, 'state', ''),
                'zip_code': getattr(order, 'zip_code', ''),
            },
            'package': {
                'weight': round(package_weight, 2),
                'items': package_items,
                'item_count': len(package_items),
                'total_value': float(order.total_price) if order.total_price else 0.0,
            },
            'pickup': {
                'name': pickup_name or getattr(order, 'store_name', ''),
                'address': pickup_address or getattr(order, 'store_address', ''),
                'pickup_latitude': pickup_lat,
                'pickup_longitude': pickup_lng,
            }
        }

        return JsonResponse(data)

    except Exception as e:
        logger.error(f"Error fetching order details: {e}")
        return JsonResponse({'error': str(e)}, status=500)


@require_POST
@login_required
def calculate_delivery_fee_api(request):
    """Calculate delivery fee based on parameters"""
    try:
        import json
        data = json.loads(request.body)
        weight = Decimal(data.get('weight', '0'))
        service_id = data.get('service_id')
        zone_id = data.get('zone_id')
        distance = data.get('distance')
        pickup_address = data.get('pickup_address')
        recipient_address = data.get('recipient_address')

        service = None
        zone = None

        if service_id:
            service = get_object_or_404(DeliveryService, id=service_id)
        if zone_id:
            zone = get_object_or_404(DeliveryZone, id=zone_id)

        fee = calculate_delivery_fee(
            weight=weight,
            service_type=service,
            zone=zone,
            distance=distance,
            pickup_address=pickup_address,
            recipient_address=recipient_address
        )

        return JsonResponse({
            'delivery_fee': str(fee),
            'currency': 'KES',
            'calculation': {
                'weight': str(weight),
                'service': service.name if service else 'Standard',
                'zone': zone.name if zone else 'Default',
                'distance': distance
            }
        })
    except Exception as e:
        logger.error(f"Error calculating delivery fee: {e}")
        return JsonResponse(
            {'error': str(e)},
            status=400
        )


@require_GET
@login_required
@seller_or_delivery_or_admin_required
def get_user_orders(request):
    """Get user's orders for dropdown"""
    try:
        from listings.models import Order
        from django.db.models import Q

        user = request.user
        orders = []

        # Get pending orders without deliveries
        if user.is_staff or user.is_superuser:
            # Admins can see all orders
            all_orders = Order.objects.all()
        else:
            # Sellers see orders from their stores
            try:
                from storefront.models import Store
                stores = Store.objects.filter(owner=user)
                if stores.exists():
                    store_ids = stores.values_list('id', flat=True)
                    # Orders containing items from user's stores
                    all_orders = Order.objects.filter(
                        Q(order_items__listing__store__id__in=store_ids) |
                        Q(order_items__listing__seller=user)
                    ).distinct()
                else:
                    # Regular users see their own orders
                    all_orders = Order.objects.filter(user=user)
            except Exception:
                all_orders = Order.objects.filter(user=user)

        # Get orders that don't have deliveries yet
        orders_with_delivery = DeliveryRequest.objects.filter(
            order_id__isnull=False
        ).values_list('order_id', flat=True)

        orders_with_delivery = [str(id) for id in orders_with_delivery]

        # Get recent orders (last 100)
        pending_orders = all_orders.exclude(
            Q(id__in=orders_with_delivery) |
            Q(tracking_number__isnull=False)
        ).order_by('-created_at')[:100]

        for order in pending_orders:
            seller_items_qs = None
            if not (user.is_staff or user.is_superuser):
                try:
                    from storefront.models import Store
                    stores = Store.objects.filter(owner=user)
                    if stores.exists():
                        seller_items_qs = order.order_items.filter(
                            Q(listing__store__in=stores) |
                            Q(listing__seller=user)
                        )
                except Exception:
                    seller_items_qs = None

            if seller_items_qs is not None:
                item_count = seller_items_qs.count()
                seller_total = sum((i.price or 0) * (i.quantity or 0) for i in seller_items_qs)
                total_amount = float(seller_total)
            else:
                item_count = order.order_items.count() if hasattr(order, 'order_items') else 0
                total_amount = float(order.total_price) if order.total_price else 0.0

            orders.append({
                'id': order.id,
                'order_number': getattr(order, 'order_number', f"#{order.id}"),
                'customer_name': f"{order.first_name} {order.last_name}".strip() or
                               (order.user.get_full_name() if order.user else 'Anonymous'),
                'customer_email': order.email or (order.user.email if order.user else ''),
                'created_at': order.created_at.strftime('%Y-%m-%d %H:%M'),
                'total_amount': total_amount,
                'item_count': item_count,
                'status': getattr(order, 'status', 'pending'),
                'shipping_address': order.shipping_address or '',
            })

        return JsonResponse({'orders': orders})

    except Exception as e:
        logger.error(f"Error fetching user orders: {e}")
        return JsonResponse({'orders': [], 'error': str(e)})
