# storefront/subscription_service.py (updated with strict trial enforcement)
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction
from django.core.cache import cache
from django.contrib import messages
from django.shortcuts import redirect
import logging
from .models import Store, Subscription, MpesaPayment
from .mpesa import MpesaGateway
from .models_trial import UserTrial
from django.db import models
from django.conf import settings

logger = logging.getLogger(__name__)

class SubscriptionService:
    """Centralized subscription management service with strict trial enforcement"""
    TRIAL_LIMIT_PER_USER = 1  # Only 1 trial per user
    
    PLAN_DETAILS = {
        'free': {
            'name': 'Free',
            'price': 0,
            'period': 'month',
            'features': [
                'Seller dashboard only',
                'Up to 5 listings',
                'Single storefront',
            ],
            'max_products': 5,
            'max_stores': 1,
        },
        'basic': {
            'name': 'Basic',
            'price': 999,
            'period': 'month',
            'features': [
                'Priority listing',
                'Basic analytics',
                'Store customization',
                'Verified badge',
                    'Up to 50 products',
                    'Up to 3 storefronts',
                'Email support',
            ],
            'max_products': 50,
                'max_stores': 3,
        },
        'premium': {
            'name': 'Premium',
            'price': 1999,
            'period': 'month',
            'features': [
                'Everything in Basic',
                'Advanced analytics',
                'Bulk product upload',
                'Inventory management',
                'Product bundles',
                'Up to 200 products',
                'Up to 10 storefronts',
                'Priority support',
            ],
            'max_products': 200,
            'max_stores': 10,
        },
        'enterprise': {
            'name': 'Enterprise',
            'price': 4999,
            'period': 'month',
            'features': [
                'Everything in Premium',
                'Custom integrations',
                'API access',
                'Unlimited products',
                'Unlimited storefronts',
                'Custom domain',
                'Dedicated support',
                'White-label options',
            ],
            'max_products': None,  # Unlimited
            'max_stores': None,    # Unlimited
        }
    }
    
    @classmethod
    def get_user_eligibility(cls, user, store=None):
        """Check user's trial and subscription eligibility with strict enforcement"""
        from .models_trial import UserTrial
        
        # Check if user has EVER had ANY trial (UserTrial records OR Subscription with trial_ends_at)
        user_trial_exists = UserTrial.objects.filter(user=user).exists()
        subscription_trial_exists = Subscription.objects.filter(
            store__owner=user,
            trial_ends_at__isnull=False
        ).exists()
        ever_had_trial = user_trial_exists or subscription_trial_exists
        
        # Check if user has ACTIVE trial (currently in trial period)
        active_trial = Subscription.objects.filter(
            store__owner=user,
            status='trialing',
            trial_ends_at__gt=timezone.now()
        ).exists()
        
        # Check if user has EXPIRED trial (trial ended in past)
        expired_trial = Subscription.objects.filter(
            store__owner=user,
            status='trialing',
            trial_ends_at__lt=timezone.now()
        ).exists()
        
        # Check if user has ACTIVE subscription
        active_subscription = None
        if store:
            active_subscription = Subscription.objects.filter(
                store=store,
                status='active'
            ).order_by('-created_at').first()
        else:
            # Check across all user stores
            active_subscription = Subscription.objects.filter(
                store__owner=user,
                status='active'
            ).order_by('-created_at').first()
        
        # User can ONLY start trial if they have NEVER had ANY trial
        can_start_trial = not ever_had_trial
        
        # User can subscribe if they don't have an active subscription across their stores
        # An active subscription on any store covers all stores (enterprise/basic/premium semantics)
        user_active = Subscription.objects.filter(store__owner=user, status='active').order_by('-created_at').first()
        can_subscribe = not user_active
        
        # Get trial usage count (1 if any trial exists, 0 otherwise)
        trial_count = 1 if ever_had_trial else 0
        
        return {
            'ever_had_trial': ever_had_trial,
            'active_trial': active_trial,
            'expired_trial': expired_trial,
            'has_expired_trial': expired_trial,  # Alias for template compatibility
            'can_start_trial': can_start_trial,
            'can_subscribe': can_subscribe,
            'active_subscription': active_subscription,
            'trial_count': trial_count,
            'trial_limit': 1,  # Only 1 trial per user
        }

    @classmethod
    def get_user_active_subscription(cls, user):
        """Return the most recent active subscription across all stores owned by the user."""
        return Subscription.objects.filter(store__owner=user, status='active').order_by('-created_at').first()
    
    @classmethod
    def subscribe_immediately(cls, store, plan, phone_number):
        """Subscribe immediately without trial"""
        # Validate phone number length
        normalized_phone = cls.normalize_phone_number(phone_number)
        
        # Ensure phone number doesn't exceed database field length
        max_length = 15  # Based on your database schema
        if len(normalized_phone) > max_length:
            # Truncate or handle the error
            return False, f"Phone number is too long. Maximum {max_length} characters allowed."
        
        with transaction.atomic():
            # Check if user already has an active subscription (owner-level precedence)
            owner_active = Subscription.objects.filter(
                store__owner=store.owner,
                status='active'
            ).exists()

            if owner_active:
                return False, "You already have an active subscription covering your stores. Wait until it expires before subscribing again."
            
            # Create active subscription
            subscription = Subscription.objects.create(
                store=store,
                plan=plan,
                status='active',
                amount=cls.PLAN_DETAILS[plan]['price'],
                started_at=timezone.now(),
                current_period_end=timezone.now() + timedelta(days=30),
                mpesa_phone=normalized_phone,  # Use normalized phone
                metadata={
                    'subscribed_at': timezone.now().isoformat(),
                    'skipped_trial': True,
                    'bypassed_trial': True,
                    'original_phone': phone_number,  # Store original for reference
                }
            )
            
            # Enable premium features
            store.is_premium = True
            store.save()
            
            return True, subscription

    @classmethod
    def normalize_phone_number(cls, phone_number):
        """Normalize phone number to ensure it fits in database field"""
        if not phone_number:
            return phone_number
        
        # Remove any non-digit characters except +
        import re
        phone = str(phone_number).strip()
        
        # Remove any whitespace, dashes, etc.
        phone = re.sub(r'[^\d\+]', '', phone)
        
        # If it starts with 0, replace with +254
        if phone.startswith('0'):
            phone = '+254' + phone[1:]
        # If it starts with 254 without +, add +
        elif phone.startswith('254') and not phone.startswith('+254'):
            phone = '+' + phone
        # If it's just digits and length is 9 (Kenyan number without prefix)
        elif phone.isdigit() and len(phone) == 9:
            phone = '+254' + phone
        # If it's digits and length is 10 (with leading 0)
        elif phone.isdigit() and len(phone) == 10 and phone.startswith('0'):
            phone = '+254' + phone[1:]
        
        # Ensure maximum length of 15 characters for database field
        max_length = 15
        if len(phone) > max_length:
            # Try to truncate intelligently
            if phone.startswith('+254') and len(phone) > max_length:
                # Keep +254 and truncate the rest
                phone = '+254' + phone[4:max_length]
            else:
                phone = phone[:max_length]
        
        return phone

    @classmethod
    def start_trial(cls, store, plan, phone_number, user):
        """Start a 7-day free trial with strict validation"""
        # First, check eligibility
        eligibility = cls.get_user_eligibility(user)
        
        if not eligibility['can_start_trial']:
            if eligibility['trial_count'] >= 1:
                return False, "You have already used your one free trial. Each user is limited to one trial period."
            return False, "You are not eligible for a free trial."
        
        # Additional safety check: verify user hasn't had any trial
        user_trials = Subscription.objects.filter(
            store__owner=user,
            trial_ends_at__isnull=False
        ).count()
        
        if user_trials >= 1:
            return False, "Trial limit reached. You have already used your free trial."
        
        # Normalize phone number
        normalized_phone = cls.normalize_phone_number(phone_number)
        
        with transaction.atomic():
            # Create trial subscription
            subscription = Subscription.objects.create(
                store=store,
                plan=plan,
                status='trialing',
                amount=cls.PLAN_DETAILS[plan]['price'],
                trial_ends_at=timezone.now() + timedelta(days=7),
                started_at=timezone.now(),
                mpesa_phone=normalized_phone,  # Use normalized phone
                metadata={
                    'trial_started': timezone.now().isoformat(),
                    'via_trial': True,
                    'user_id': user.id,
                    'is_first_trial': True,
                    'trial_number': 1,
                    'original_phone': phone_number,
                }
            )
            
            # Enable premium features for trial
            store.is_premium = True
            store.save()
            
            # Log trial start for audit
            logger.info(f"Trial started for user {user.id} on store {store.id}. Trial count: {user_trials + 1}")
            
            return True, subscription

    
    @classmethod
    def enforce_trial_expiry(cls):
        """Strict enforcement of trial expiry - disables premium features immediately"""
        expired_trials = Subscription.objects.filter(
            status='trialing',
            trial_ends_at__lt=timezone.now(),
            store__is_premium=True
        ).select_related('store')
        
        for subscription in expired_trials:
            with transaction.atomic():
                # Mark trial as expired and sync store via centralized setter
                subscription.metadata = subscription.metadata or {}
                subscription.metadata.update({
                    'trial_expired_at': timezone.now().isoformat(),
                    'auto_downgraded': True,
                })
                subscription.save()
                subscription.set_status('canceled')
                logger.info(f"Trial expired and premium features disabled for store: {subscription.store.name}")
    
    @classmethod
    def enforce_subscription_expiry(cls):
        """Strict enforcement of subscription expiry"""
        expired_subs = Subscription.objects.filter(
            status='active',
            current_period_end__lt=timezone.now()
        ).select_related('store')
        
        for subscription in expired_subs:
            with transaction.atomic():
                subscription.metadata = subscription.metadata or {}
                subscription.metadata.update({
                    'subscription_expired_at': timezone.now().isoformat(),
                    'payment_required': True,
                })
                subscription.save()
                subscription.set_status('past_due')
                logger.info(f"Subscription expired for store: {subscription.store.name}")
    
    @classmethod
    def can_user_access_premium(cls, user, store):
        """Check if user can access premium features with strict validation"""
        # Prefer owner-level active subscription (covers all stores)
        owner_active = cls.get_user_active_subscription(user)
        if owner_active and owner_active.is_active():
            return True

        # Fallback to store-level subscription
        subscription = Subscription.objects.filter(store=store).order_by('-created_at').first()
        if subscription and subscription.is_active():
            return True
        return False
    
    @classmethod
    def validate_subscription_access(cls, user, store, feature_name):
        """Validate subscription access for specific features"""
        # Get current subscription
        subscription = Subscription.objects.filter(
            store=store
        ).order_by('-created_at').first()
        
        if not subscription:
            return False, "No subscription found"
        
        # Use is_active() first
        if subscription.is_active():
            if subscription.status == 'trialing' and subscription.trial_ends_at and timezone.now() < subscription.trial_ends_at:
                return True, "Access granted during trial"
            return True, "Access granted"

        if subscription.status in ['past_due', 'canceled']:
            return False, "Subscription is not active. Please renew to access premium features."

        # Trial expired or unknown state
        if subscription.status == 'trialing' and subscription.trial_ends_at and timezone.now() >= subscription.trial_ends_at:
            return False, "Trial period has ended. Please subscribe to continue."

        return False, "Access denied"
    
    @classmethod
    def get_subscription_summary(cls, user):
        """Get comprehensive subscription summary for user"""
        user_stores = Store.objects.filter(owner=user)
        
        summary = {
            'total_stores': user_stores.count(),
            'premium_stores': 0,
            'trial_stores': 0,
            'active_subscriptions': 0,
            'expired_trials': 0,
            'total_revenue_potential': 0,
            'trial_usage': {},
        }
        
        for store in user_stores:
            subscription = Subscription.objects.filter(
                store=store
            ).order_by('-created_at').first()
            
            if subscription:
                # Use subscription.is_active() to determine active subscriptions (includes valid trials)
                try:
                    is_active = subscription.is_active()
                except Exception:
                    is_active = subscription.status == 'active'

                if is_active:
                    summary['active_subscriptions'] += 1
                    summary['premium_stores'] += 1
                    summary['total_revenue_potential'] += subscription.amount
                elif subscription.status == 'trialing':
                    if subscription.trial_ends_at and timezone.now() < subscription.trial_ends_at:
                        summary['trial_stores'] += 1
                    else:
                        summary['expired_trials'] += 1
                
                # Track trial usage
                if subscription.trial_ends_at:
                    store_key = f"{store.name} ({store.slug})"
                    summary['trial_usage'][store_key] = {
                        'started': subscription.created_at,
                        'ended': subscription.trial_ends_at,
                        'status': subscription.status,
                    }
        
        return summary

    @classmethod
    def change_plan(cls, store, new_plan):
        """Change subscription plan with immediate effect"""
        subscription = Subscription.objects.filter(
            store=store,
            status__in=['active', 'trialing']
        ).order_by('-created_at').first()
        
        if not subscription:
            return False, "No active subscription found to change plan."
        
        with transaction.atomic():
            # Update plan details
            subscription.plan = new_plan
            subscription.amount = cls.PLAN_DETAILS[new_plan]['price']
            subscription.metadata.update({
                'plan_changed_at': timezone.now().isoformat(),
                'new_plan': new_plan,
            })
            subscription.save()
            
            logger.info(f"Subscription plan changed to {new_plan} for store: {store.name}")
            
            return True, subscription
            
    @classmethod
    def get_user_trial_status(cls, user):
        """Get detailed trial status for user"""
        from .models import Subscription
        
        # Get trial summary
        trial_summary = UserTrial.get_user_trial_summary(user)
        
        # Get all user subscriptions with trials
        trial_subscriptions = Subscription.objects.filter(
            store__owner=user,
            trial_ends_at__isnull=False
        ).order_by('-created_at')
        
        # Check if any trial is currently active
        active_trial = None
        for sub in trial_subscriptions:
            if sub.status == 'trialing' and sub.trial_ends_at and sub.trial_ends_at > timezone.now():
                active_trial = sub
                break
        
        # Strict one-trial-per-user policy
        # If user has ANY trial records (UserTrial or Subscription with trial_ends_at), they have used their trial
        has_used_trial = trial_summary['total_trials'] > 0 or trial_subscriptions.exists()
        trial_count = 1 if has_used_trial else 0
        remaining_trials = 0 if has_used_trial else 1
        can_start_trial = not has_used_trial
        
        # Calculate days until next eligible trial (if any)
        next_trial_eligible = None
        if has_used_trial:
            # User has already used their trial, no next trial
            next_trial_eligible = None
        
        return {
            'summary': {
                'total_trials': trial_count,
                'remaining_trials': remaining_trials,
                'has_exceeded_limit': has_used_trial,
                'trial_limit': cls.TRIAL_LIMIT_PER_USER,
            },
            'active_trial': active_trial,
            'trial_subscriptions': list(trial_subscriptions.values(
                'id', 'plan', 'status', 'trial_ends_at', 'created_at', 'store__name'
            )),
            'next_trial_eligible': next_trial_eligible,
            'can_start_trial': can_start_trial,
            'trial_limit': cls.TRIAL_LIMIT_PER_USER,
            'trial_count': trial_count,
        }
    
    @classmethod
    def validate_trial_eligibility(cls, user):
        """Validate if user can start a trial"""
        trial_status = cls.get_user_trial_status(user)
        
        # Check if user has exceeded trial limit
        if trial_status['summary']['has_exceeded_limit']:
            return False, {
                'code': 'TRIAL_LIMIT_EXCEEDED',
                'message': f'You have already used your {cls.TRIAL_LIMIT_PER_USER} free trial(s).',
                'details': {
                    'trial_count': trial_status['trial_count'],
                    'trial_limit': cls.TRIAL_LIMIT_PER_USER,
                    'remaining': 0,
                }
            }
        
        # Check if user has an active trial
        if trial_status['active_trial']:
            return False, {
                'code': 'ACTIVE_TRIAL_EXISTS',
                'message': 'You already have an active trial.',
                'details': {
                    'trial_end_date': trial_status['active_trial'].trial_ends_at,
                    'store': trial_status['active_trial'].store.name,
                }
            }
        
        # Check if user has remaining trials
        if trial_status['summary']['remaining_trials'] <= 0:
            return False, {
                'code': 'NO_TRIALS_REMAINING',
                'message': 'No trials remaining.',
                'details': {
                    'trial_count': trial_status['trial_count'],
                    'trial_limit': cls.TRIAL_LIMIT_PER_USER,
                }
            }
        
        return True, {
            'code': 'ELIGIBLE',
            'message': 'User is eligible for trial.',
            'details': {
                'remaining_trials': trial_status['summary']['remaining_trials'],
                'trial_number': trial_status['trial_count'] + 1,
            }
        }
    
    
    @classmethod
    def start_trial_with_tracking(cls, store, plan, phone_number, user):
        """Start a trial with comprehensive tracking"""
        # Validate trial eligibility
        eligible, eligibility_data = cls.validate_trial_eligibility(user)
        
        if not eligible:
            return False, eligibility_data['message']
        
        # Normalize phone number
        normalized_phone = cls.normalize_phone_number(phone_number)
        
        with transaction.atomic():
            # Create subscription with trial
            subscription = Subscription.objects.create(
                store=store,
                plan=plan,
                status='trialing',
                amount=cls.PLAN_DETAILS[plan]['price'],
                trial_ends_at=timezone.now() + timedelta(days=7),
                started_at=timezone.now(),
                mpesa_phone=normalized_phone,  # Use normalized phone
                trial_number=eligibility_data['details']['trial_number'],
                metadata={
                    'trial_started': timezone.now().isoformat(),
                    'via_trial': True,
                    'user_id': user.id,
                    'trial_number': eligibility_data['details']['trial_number'],
                    'trial_limit': cls.TRIAL_LIMIT_PER_USER,
                    'remaining_trials_before': eligibility_data['details']['remaining_trials'],
                    'original_phone': phone_number,
                }
            )
            
            # Enable premium features
            store.is_premium = True
            store.save()
            
            # Record trial in UserTrial model
            trial_record = UserTrial.record_trial_start(
                user=user,
                store=store,
                subscription=subscription
            )
            
            # Log trial start
            logger.info(
                f"Trial #{trial_record.trial_number} started for user {user.id} "
                f"on store {store.id}. Remaining trials: {eligibility_data['details']['remaining_trials'] - 1}"
            )
            # Attempt to send trial-start email and create an in-app notification immediately.
            try:
                from baysoko.utils.email_helpers import render_and_send
                from notifications.utils import notify_system_message

                owner_email = getattr(store.owner, 'email', None)
                recipients = [e for e in [owner_email] if e]
                email_ctx = {
                    'subscription': subscription,
                    'store': store,
                    'user': store.owner,
                    'trial_record': trial_record,
                    'site_url': getattr(settings, 'SITE_URL', ''),
                }
                if recipients:
                    try:
                        render_and_send('emails/subscription_trial_started.html', 'emails/subscription_trial_started.txt', email_ctx,
                                        f'Your trial for {store.name} has started', recipients)
                    except Exception:
                        logger.exception('Failed to send trial start email via centralized sender')

                # Create in-app notification if owner exists
                try:
                    if getattr(store, 'owner', None):
                        notify_system_message(store.owner, 'Trial Started', f'Your trial for {store.name} has started.')
                except Exception:
                    logger.exception('Failed to create in-app notification for trial start')
            except Exception:
                logger.exception('Error while attempting immediate trial-start notifications')
            
            return True, {
                'subscription': subscription,
                'trial_record': trial_record,
                'trial_number': trial_record.trial_number,
                'remaining_trials': eligibility_data['details']['remaining_trials'] - 1,
            }

    @classmethod
    def end_trial_with_tracking(cls, subscription, reason='ended'):
        """End a trial with comprehensive tracking"""
        with transaction.atomic():
            # Update subscription metadata and cancel via centralized setter
            subscription.metadata = subscription.metadata or {}
            subscription.metadata.update({
                'trial_ended_at': timezone.now().isoformat(),
                'trial_end_reason': reason,
                'auto_downgraded': True,
            })
            subscription.save()
            subscription.set_status('canceled')
            
            # Record trial end
            trial_record = UserTrial.record_trial_end(subscription, reason)
            
            # Log trial end
            logger.info(
                f"Trial #{subscription.trial_number} ended for user {subscription.store.owner.id} "
                f"on store {subscription.store.id}. Reason: {reason}"
            )
            
            return True, {
                'subscription': subscription,
                'trial_record': trial_record,
            }
    
    @classmethod
    def convert_trial_to_paid(cls, subscription, phone_number):
        """Convert trial to paid subscription - requires successful payment"""
        # Validate phone number
        if not phone_number:
            return False, "Phone number is required for payment processing."

        # Process payment first - DO NOT activate until payment succeeds
        payment_success, payment_result = cls.process_payment(
            subscription=subscription,
            phone_number=phone_number
        )

        if not payment_success:
            return False, f"Payment failed: {payment_result}. Trial not converted."

        # Payment initiated successfully - subscription will be activated by webhook on payment success
        # Normalize and store phone number for future reference (fits DB max length)
        normalized = cls.normalize_phone_number(phone_number)
        subscription.mpesa_phone = normalized
        subscription.save()

        return True, "Payment initiated successfully. Trial will be converted upon payment confirmation."
    
    @classmethod
    def get_trial_usage_analytics(cls, user=None):
        """Get trial usage analytics for admin or user"""
        from django.db.models import Count, Avg, Max, Min
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        
        if user:
            # User-specific analytics
            user_trials = UserTrial.objects.filter(user=user)
            
            analytics = {
                'user': {
                    'email': user.email,
                    'id': user.id,
                    'date_joined': user.date_joined,
                },
                'trial_summary': UserTrial.get_user_trial_summary(user),
                'conversion_rate': 0,
                'average_trial_days': 0,
                'preferred_plan': None,
            }
            
            if user_trials.exists():
                # Calculate conversion rate
                total_trials = user_trials.count()
                converted_trials = user_trials.filter(status='converted').count()
                analytics['conversion_rate'] = (converted_trials / total_trials) * 100 if total_trials > 0 else 0
                
                # Calculate average trial days
                ended_trials = user_trials.exclude(ended_at__isnull=True)
                if ended_trials.exists():
                    avg_days = ended_trials.aggregate(Avg('days_used'))['days_used__avg']
                    analytics['average_trial_days'] = round(avg_days, 1)
                
                # Find preferred plan
                from .models import Subscription
                subscriptions = Subscription.objects.filter(
                    store__owner=user,
                    trial_ends_at__isnull=False
                )
                if subscriptions.exists():
                    plan_counts = subscriptions.values('plan').annotate(count=Count('id')).order_by('-count')
                    if plan_counts:
                        analytics['preferred_plan'] = plan_counts[0]['plan']
            
            return analytics
        
        else:
            # Admin/global analytics
            total_users = User.objects.count()
            users_with_trials = User.objects.filter(trials__isnull=False).distinct().count()
            
            trial_stats = UserTrial.objects.aggregate(
                total_trials=Count('id'),
                active_trials=Count('id', filter=models.Q(status='active')),
                converted_trials=Count('id', filter=models.Q(status='converted')),
                avg_days_used=Avg('days_used'),
                max_trials_per_user=Max('trial_number'),
            )
            
            # Trial conversion rate
            conversion_rate = (trial_stats['converted_trials'] / trial_stats['total_trials'] * 100) if trial_stats['total_trials'] > 0 else 0
            
            # Users exceeding trial limit
            from django.db.models import Count
            users_exceeding_limit = User.objects.annotate(
                trial_count=Count('trials')
            ).filter(
                trial_count__gt=cls.TRIAL_LIMIT_PER_USER
            ).count()
            
            return {
                'total_users': total_users,
                'users_with_trials': users_with_trials,
                'users_without_trials': total_users - users_with_trials,
                'trial_stats': trial_stats,
                'conversion_rate': round(conversion_rate, 2),
                'users_exceeding_limit': users_exceeding_limit,
                'trial_limit': cls.TRIAL_LIMIT_PER_USER,
                'trial_abuse_risk': (users_exceeding_limit / total_users * 100) if total_users > 0 else 0,
            }
    
    @classmethod
    def enforce_trial_limits_daily(cls):
        """Daily cron job to enforce trial limits"""
        from .models import Subscription
        
        # Find users who might be trying to abuse trials
        from django.db.models import Count
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        
        # Get users with multiple trials across different stores
        potential_abusers = User.objects.annotate(
            trial_count=Count('stores__subscriptions', filter=models.Q(
                stores__subscriptions__trial_ends_at__isnull=False
            ))
        ).filter(
            trial_count__gt=cls.TRIAL_LIMIT_PER_USER
        )
        
        for user in potential_abusers:
            logger.warning(
                f"Potential trial abuse detected: User {user.id} has {user.trial_count} trials "
                f"(limit: {cls.TRIAL_LIMIT_PER_USER})"
            )
            
            # Flag user for review
            user.metadata = user.metadata or {}
            user.metadata.update({
                'trial_abuse_warning': {
                    'detected_at': timezone.now().isoformat(),
                    'trial_count': user.trial_count,
                    'limit': cls.TRIAL_LIMIT_PER_USER,
                    'action': 'flagged_for_review',
                }
            })
            user.save()

    @classmethod
    def change_plan(cls, subscription, new_plan, phone_number=None):
        """Change subscription plan with payment requirements"""
        # Validate new plan
        if new_plan not in cls.PLAN_DETAILS:
            return False, "Invalid plan selected."
        
        old_plan = subscription.plan
        old_price = cls.PLAN_DETAILS[old_plan]['price']
        new_price = cls.PLAN_DETAILS[new_plan]['price']
        
        # Determine if this is an upgrade, downgrade, or same plan
        is_upgrade = new_price > old_price
        is_downgrade = new_price < old_price
        is_same_plan = new_price == old_price
        
        if is_same_plan:
            return False, "You are already on this plan."
        
        # Check for any existing pending payments that might conflict
        existing_pending = MpesaPayment.objects.filter(
            subscription=subscription,
            status='pending',
            created_at__gte=timezone.now() - timedelta(hours=1)
        ).exists()
        
        if existing_pending:
            return False, "Cannot change plan while a payment is pending. Please wait for the current payment to complete."
        
        # Check for pending plan changes
        if subscription.metadata and subscription.metadata.get('pending_plan_change'):
            return False, "A plan change is already pending. Please complete the current plan change first."
        
        # Check subscription status and determine payment requirements
        if subscription.status in ['canceled', 'past_due']:
            # Clear any existing pending plan changes before setting new one
            metadata = subscription.metadata or {}
            pending_keys = [
                'pending_plan_change', 'pending_plan_change_at', 
                'pending_payment_amount', 'pending_change_description'
            ]
            for key in pending_keys:
                metadata.pop(key, None)
            subscription.metadata = metadata
            subscription.save()
            
            # Inactive subscription - requires full payment for new plan
            payment_required = True
            payment_amount = new_price
            change_immediate = False  # Will activate after payment
            description = f"Reactivate subscription with {new_plan.capitalize()} plan"
            
        elif subscription.status in ['active', 'trialing']:
            if is_upgrade:
                # Active subscription upgrade - calculate prorated amount
                payment_required = True
                if subscription.current_period_end:
                    # Calculate remaining days in current period
                    remaining_days = (subscription.current_period_end - timezone.now()).days
                    remaining_days = max(0, remaining_days)
                    
                    # Prorate the upgrade cost
                    daily_old_rate = old_price / 30
                    daily_new_rate = new_price / 30
                    daily_difference = daily_new_rate - daily_old_rate
                    
                    prorated_amount = daily_difference * remaining_days
                    payment_amount = max(0, prorated_amount)
                else:
                    payment_amount = new_price - old_price
                
                change_immediate = True  # Upgrade takes effect immediately
                description = f"Upgrade to {new_plan.capitalize()} plan"
                
            elif is_downgrade:
                # Downgrade - change immediately but keep current features until period end
                payment_required = False
                payment_amount = 0
                change_immediate = True
                description = f"Downgrade to {new_plan.capitalize()} plan (effective at period end)"
        
        with transaction.atomic():
            if change_immediate:
                # CRITICAL FIX: For upgrades requiring payment, DO NOT apply changes immediately
                # Wait for payment confirmation via webhook
                if payment_required and payment_amount > 0:
                    # Snapshot current metadata/state so we can fully rollback on failure
                    original_metadata = dict(subscription.metadata or {})
                    original_store_is_premium = getattr(subscription.store, 'is_premium', False)

                    # Store the intended plan change in metadata but DON'T apply it yet
                    subscription.metadata = subscription.metadata or {}
                    subscription.metadata.update({
                        'pending_plan_change': new_plan,
                        'pending_plan_change_at': timezone.now().isoformat(),
                        'pending_payment_amount': payment_amount,
                        'pending_old_plan': old_plan,
                        'pending_old_amount': old_price,
                        'pending_change_type': 'upgrade' if is_upgrade else 'downgrade',
                        'change_requires_payment': True,
                    })
                    subscription.save()

                    # Initiate payment - subscription will only change after successful payment
                    payment_success, payment_result = cls.process_payment(
                        subscription=subscription,
                        phone_number=phone_number or subscription.mpesa_phone.replace('+254', '')
                    )

                    if not payment_success:
                        # Restore original metadata and store premium flag to preserve trial/active state
                        subscription.metadata = original_metadata
                        subscription.save()

                        try:
                            subscription.store.is_premium = original_store_is_premium
                            subscription.store.save()
                        except Exception:
                            # Best-effort save; do not fail the rollback for store save issues
                            logger.exception("Failed to restore store.is_premium during plan-change rollback")

                        return False, f"Payment failed: {payment_result}. Plan change cancelled."

                    return True, f"Plan change initiated. Please complete payment of KSh {payment_amount} to activate {new_plan.capitalize()} plan."
                
                else:
                    # No payment required (downgrades or free changes) - apply immediately
                    subscription.plan = new_plan
                    subscription.amount = new_price
                    subscription.metadata = subscription.metadata or {}
                    subscription.metadata.update({
                        'plan_changed_at': timezone.now().isoformat(),
                        'old_plan': old_plan,
                        'new_plan': new_plan,
                        'change_type': 'upgrade' if is_upgrade else 'downgrade',
                        'immediate_change': True,
                    })
                    
                    if is_downgrade:
                        # For downgrades, store the original plan until period end
                        subscription.metadata['downgrade_from'] = old_plan
                        subscription.metadata['downgrade_at'] = timezone.now().isoformat()
                    
                    subscription.save()
                    return True, f"Plan changed to {new_plan.capitalize()} successfully!"
                    
            else:
                # For inactive subscriptions, create a pending plan change
                subscription.metadata = subscription.metadata or {}
                subscription.metadata.update({
                    'pending_plan_change': new_plan,
                    'pending_plan_change_at': timezone.now().isoformat(),
                    'pending_payment_amount': payment_amount,
                    'pending_change_description': description,
                })
                subscription.save()
                
                # Initiate payment
                original_metadata = dict(subscription.metadata or {})
                payment_success, payment_result = cls.process_payment(
                    subscription=subscription,
                    phone_number=phone_number or subscription.mpesa_phone.replace('+254', '')
                )

                if payment_success:
                    # Payment successful, activate the subscription with new plan
                    subscription.status = 'active'
                    subscription.plan = new_plan
                    subscription.amount = new_price
                    subscription.started_at = timezone.now()
                    subscription.current_period_end = timezone.now() + timedelta(days=30)
                    subscription.metadata.update({
                        'reactivated_at': timezone.now().isoformat(),
                        'reactivated_with_plan': new_plan,
                    })
                    subscription.save()
                    
                    # Enable premium features
                    subscription.store.is_premium = True
                    subscription.store.save()
                    
                    return True, f"Subscription reactivated with {new_plan.capitalize()} plan successfully!"
                else:
                    # Payment initiation failed - clear any pending keys and restore original metadata
                    subscription.metadata = original_metadata
                    subscription.save()
                    return False, f"Payment failed: {payment_result}. Plan change cancelled."

    @classmethod
    def renew_subscription(cls, subscription, phone_number=None):
        """Renew an expired or canceled subscription - requires successful payment"""
        # Check if subscription can be renewed
        if subscription.status not in ['canceled', 'past_due']:
            return False, "Only canceled or past-due subscriptions can be renewed."

        # Validate phone number is provided
        if not phone_number:
            return False, "Phone number is required for payment processing."

        # Get the store
        store = subscription.store

        # Process payment first - DO NOT activate until payment succeeds
        payment_success, payment_result = cls.process_payment(
            subscription=subscription,
            phone_number=phone_number
        )

        if not payment_success:
            return False, f"Payment failed: {payment_result}. Subscription not renewed."

        # Payment initiated successfully - subscription will be activated by webhook on payment success
        # Normalize and store phone number for future reference (fits DB max length)
        normalized = cls.normalize_phone_number(phone_number)
        subscription.mpesa_phone = normalized
        subscription.save()

        return True, "Payment initiated successfully. Subscription will be renewed upon payment confirmation."

    @classmethod
    def cancel_subscription(cls, subscription, cancel_at_period_end=True):
        """Cancel subscription with option to cancel immediately or at period end"""
        with transaction.atomic():
            # Clear any pending plan changes when cancelling
            metadata = subscription.metadata or {}
            pending_keys = [
                'pending_plan_change', 'pending_plan_change_at', 
                'pending_payment_amount', 'pending_change_description'
            ]
            for key in pending_keys:
                metadata.pop(key, None)
            subscription.metadata = metadata
            
            if cancel_at_period_end:
                # Schedule cancellation at period end (graceful)
                subscription.canceled_at = timezone.now()
                subscription.cancel_at_period_end = True
                subscription.metadata.update({
                    'cancelled_at': timezone.now().isoformat(),
                    'cancellation_type': 'scheduled',
                    'will_end_at': subscription.current_period_end.isoformat() if subscription.current_period_end else None,
                })
                
                logger.info(f"Subscription scheduled for cancellation at period end for store: {subscription.store.name}")
                
            else:
                # Cancel immediately
                subscription.status = 'canceled'
                subscription.canceled_at = timezone.now()
                subscription.cancel_at_period_end = False
                subscription.current_period_end = None
                
                # Immediately disable premium features
                subscription.store.is_premium = False
                subscription.store.save()
                
                subscription.metadata.update({
                    'cancelled_at': timezone.now().isoformat(),
                    'cancellation_type': 'immediate',
                    'premium_disabled_immediately': True,
                })
                
                logger.info(f"Subscription canceled immediately for store: {subscription.store.name}")
            
            subscription.save()
            return True

        @classmethod
        def get_subscription_summary_for_store(cls, store):
            """Get subscription summary for a specific store"""
            subscription = Subscription.objects.filter(
                store=store
            ).order_by('-created_at').first()
            
            if not subscription:
                return {
                    'has_subscription': False,
                    'status': 'none',
                    'is_active': False,
                    'is_trialing': False,
                    'can_renew': False,
                    'can_cancel': False,
                    'can_change_plan': False,
                }
            
            try:
                is_active = subscription.is_active()
            except Exception:
                is_active = subscription.status == 'active'

            return {
                'has_subscription': True,
                'status': subscription.status,
                'plan': subscription.plan,
                'amount': subscription.amount,
                'started_at': subscription.started_at,
                'current_period_end': subscription.current_period_end,
                'trial_ends_at': subscription.trial_ends_at,
                'canceled_at': subscription.canceled_at,
                'is_active': is_active,
                'is_trialing': subscription.status == 'trialing',
                'is_unpaid': subscription.status == 'unpaid',
                'is_expired': subscription.status in ['past_due', 'canceled'],
                'can_renew': subscription.status in ['canceled', 'past_due'],
                'can_cancel': subscription.status in ['active', 'trialing'],
                'can_change_plan': subscription.status in ['active', 'trialing'],
                'needs_payment': subscription.status in ['past_due', 'unpaid'],
                'trial_expired': subscription.status == 'trialing' and 
                                subscription.trial_ends_at and 
                                timezone.now() > subscription.trial_ends_at,
            }

    @classmethod
    def process_payment(cls, subscription, phone_number):
        """Process M-Pesa payment for a subscription"""
        # Check for existing pending payments to prevent duplicates
        existing_pending = MpesaPayment.objects.filter(
            subscription=subscription,
            status='pending',
            created_at__gte=timezone.now() - timedelta(hours=1)  # Within last hour
        ).exists()
        
        if existing_pending:
            return False, "A payment is already pending for this subscription. Please wait for it to complete."
        
        # Validate that subscription is in a state that allows payment
        try:
            is_active = subscription.is_active()
        except Exception:
            is_active = subscription.status == 'active'

        if is_active and not cls._is_payment_for_renewal(subscription):
            return False, "Subscription is already active and not due for renewal."
        
        try:
            mpesa = MpesaGateway()
            # Normalize using gateway then ensure it fits DB constraints
            try:
                phone_normalized = mpesa._normalize_phone(phone_number)
            except Exception:
                phone_normalized = cls.normalize_phone_number(phone_number)

            # Final pass to ensure DB-safe length/format
            phone_normalized = cls.normalize_phone_number(phone_normalized)
            
            # Initiate STK push
            response = mpesa.initiate_stk_push(
                phone=phone_normalized,
                amount=float(subscription.amount),
                account_reference=f"Sub-{subscription.id}"
            )
            
            # Create payment record
            MpesaPayment.objects.create(
                subscription=subscription,
                checkout_request_id=response['CheckoutRequestID'],
                merchant_request_id=response['MerchantRequestID'],
                phone_number=phone_normalized,
                amount=subscription.amount,
                status='pending',
                raw_response=response
            )
            
            logger.info(f"Payment initiated for subscription {subscription.id}: {response}")
            return True, "Payment initiated successfully"
            
        except Exception as e:
            logger.error(f"Payment initiation failed for subscription {subscription.id}: {str(e)}")
            return False, str(e)

    @classmethod
    def _is_payment_for_renewal(cls, subscription):
        """Check if payment is for subscription renewal"""
        if not subscription.current_period_end:
            return True  # No end date means it needs payment
        
        # Allow payment if within 3 days of expiration
        days_until_expiry = (subscription.current_period_end - timezone.now()).days
        return days_until_expiry <= 3

    @classmethod
    def validate_subscription_activation(cls, subscription):
        """Validate that subscription activation is allowed based on payment status"""
        # Check if there are any pending payments for this subscription
        recent_payments = MpesaPayment.objects.filter(
            subscription=subscription,
            status='pending',
            created_at__gte=timezone.now() - timedelta(hours=1)  # Within last hour
        ).exists()
        
        if recent_payments:
            return False, "Subscription has pending payments that must be completed first."
        
        # Check if subscription should remain inactive due to failed payments
        if subscription.status in ['canceled', 'past_due']:
            # Only allow activation if there's a successful recent payment
            successful_payments = MpesaPayment.objects.filter(
                subscription=subscription,
                status='completed',
                created_at__gte=timezone.now() - timedelta(hours=1)
            ).exists()
            
            if not successful_payments:
                return False, "Subscription requires successful payment to activate."
        
        return True, "Activation allowed."

    @classmethod
    def validate_payment_for_activation(cls, payment, subscription):
        """Strict validation that payment is legitimate for subscription activation"""
        # 1. Payment must be completed
        if payment.status != 'completed':
            return False, f"Payment status is {payment.status}, not completed"
        
        # 2. Payment amount must exactly match subscription amount
        if payment.amount != subscription.amount:
            return False, f"Payment amount {payment.amount} does not match subscription amount {subscription.amount}"
        
        # 3. Payment must be recent (within last 24 hours)
        if payment.created_at < timezone.now() - timedelta(hours=24):
            return False, "Payment is too old to activate subscription"
        
        # 4. Check for duplicate successful payments for this subscription in last 24 hours
        duplicate_payments = MpesaPayment.objects.filter(
            subscription=subscription,
            status='completed',
            created_at__gte=timezone.now() - timedelta(hours=24)
        ).exclude(id=payment.id).exists()
        
        if duplicate_payments:
            return False, "Multiple successful payments detected for this subscription"
        
        # 5. Validate subscription state allows activation
        # Allow activation if subscription is canceled/past_due/trialing or unpaid (reactivation, trial conversion, or first-time activation)
        if subscription.status in ['canceled', 'past_due', 'trialing', 'unpaid']:
            pass
        else:
            # For active subscriptions, ensure payment is for renewal
            try:
                is_active = subscription.is_active()
            except Exception:
                is_active = subscription.status == 'active'

            if is_active:
                if subscription.current_period_end and (subscription.current_period_end - timezone.now()).days > 3:
                    return False, "Subscription is active and not due for renewal"
            else:
                return False, f"Subscription status {subscription.status} does not allow activation"
        
        # 6. For plan changes, ensure the payment amount matches the pending plan
        metadata = subscription.metadata or {}
        if 'pending_plan_change' in metadata:
            pending_plan = metadata['pending_plan_change']
            pending_amount = metadata.get('pending_payment_amount')
            if pending_amount and payment.amount != pending_amount:
                return False, f"Payment amount does not match pending plan change amount {pending_amount}"
        
        return True, "Payment validation successful"

    @classmethod
    def activate_subscription_safely(cls, subscription, payment=None):
        """Safely activate a subscription - ONLY call this after payment validation"""
        # This method should ONLY be called from webhook after payment success
        # It provides a final safeguard against unauthorized activation
        
        if not payment:
            # If no payment provided, check for recent successful payment
            recent_successful_payment = MpesaPayment.objects.filter(
                subscription=subscription,
                status='completed',
                created_at__gte=timezone.now() - timedelta(hours=1)
            ).first()
            
            if not recent_successful_payment:
                return False, "No recent successful payment found for activation"
            
            payment = recent_successful_payment
        
        # Validate the payment one more time
        is_valid, message = cls.validate_payment_for_activation(payment, subscription)
        if not is_valid:
            return False, f"Payment validation failed: {message}"
        
        # Proceed with activation based on subscription state
        with transaction.atomic():
            original_status = subscription.status
            
            # Set status to active
            subscription.status = 'active'
            
            # Handle different activation types
            if original_status == 'trialing':
                # Trial conversion
                trial_record = UserTrial.record_trial_end(subscription, 'converted')
                subscription.metadata = subscription.metadata or {}
                subscription.metadata.update({
                    'trial_converted_at': timezone.now().isoformat(),
                    'converted_from_trial': True,
                    'trial_number': subscription.trial_number,
                })
                
                # Record conversion in trial
                if trial_record:
                    trial_record.conversion_attempts += 1
                    trial_record.save()
                
                logger.info(f"Trial #{subscription.trial_number} converted to paid for user {subscription.store.owner.id}")
            
            elif original_status in ['canceled', 'past_due', 'unpaid']:
                # Renewal or reactivation
                subscription.metadata = subscription.metadata or {}
                subscription.metadata.update({
                    'renewed_at': timezone.now().isoformat(),
                    'renewed_from_status': original_status,
                })
            
            # Handle pending plan changes
            metadata = subscription.metadata or {}
            if 'pending_plan_change' in metadata:
                new_plan = metadata['pending_plan_change']
                new_plan_details = cls.PLAN_DETAILS.get(new_plan)
                if new_plan_details:
                    subscription.plan = new_plan
                    subscription.amount = new_plan_details['price']
                    metadata['plan_changed_at'] = timezone.now().isoformat()
                    metadata['plan_change_source'] = 'payment_callback'
                
                # Clean up pending metadata
                metadata.pop('pending_plan_change', None)
                metadata.pop('pending_plan_change_at', None)
                metadata.pop('pending_payment_amount', None)
                metadata.pop('pending_change_description', None)
            
            # Set billing dates
            if original_status == 'trialing' and subscription.trial_ends_at and subscription.trial_ends_at > timezone.now():
                subscription.current_period_end = subscription.trial_ends_at + timedelta(days=30)
            else:
                subscription.current_period_end = timezone.now() + timedelta(days=30)
            
            subscription.canceled_at = None
            subscription.metadata = metadata
            subscription.metadata['last_payment_successful'] = timezone.now().isoformat()
            subscription.metadata['payment_reference'] = payment.checkout_request_id
            
            subscription.save()
            
            # Enable premium features
            subscription.store.is_premium = True
            subscription.store.save()
            
            logger.info(f"Subscription {subscription.id} safely activated after payment validation (from {original_status})")
            return True, "Subscription activated successfully"

    @classmethod
    def log_activation_attempt(cls, subscription, source, success, reason=None):
        """Log all subscription activation attempts for audit purposes"""
        log_data = {
            'subscription_id': subscription.id,
            'store_id': subscription.store.id,
            'user_id': subscription.store.owner.id if subscription.store.owner else None,
            'source': source,
            'success': success,
            'subscription_status': subscription.status,
            'plan': subscription.plan,
            'amount': subscription.amount,
            'timestamp': timezone.now().isoformat(),
        }
        
        if reason:
            log_data['reason'] = reason
        
        logger.info(f"ACTIVATION_ATTEMPT: {log_data}")
        
        # Also store in metadata for debugging
        if not success and reason:
            metadata = subscription.metadata or {}
            metadata['last_activation_failure'] = {
                'timestamp': timezone.now().isoformat(),
                'source': source,
                'reason': reason
            }
            subscription.metadata = metadata
            subscription.save()

    @classmethod
    def get_payment_history(cls, subscription, limit=10):
        """Get payment history for a subscription"""
        return subscription.payments.order_by('-created_at')[:limit]

    @classmethod
    def subscribe_immediately(cls, store, plan, phone_number):
        """Create a subscription that requires immediate payment - DO NOT activate until payment succeeds"""
        # Normalize phone number
        normalized_phone = cls.normalize_phone_number(phone_number)
        
        with transaction.atomic():
            # Create subscription in 'unpaid' status - NOT active
            subscription = Subscription.objects.create(
                store=store,
                plan=plan,
                status='unpaid',  # Changed from 'active' to 'unpaid'
                amount=cls.PLAN_DETAILS[plan]['price'],
                mpesa_phone=normalized_phone,
                metadata={
                    'created_via': 'immediate_subscription',
                    'requires_payment': True,
                    'phone_number': phone_number,
                }
            )
            
            # DO NOT enable premium features yet
            # store.is_premium = True
            # store.save()
            
            logger.info(f"Unpaid subscription created for store {store.id} - payment required before activation")
            return True, subscription