from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from storefront.models import Store, Subscription
from storefront.utils.plan_permissions import PlanPermissions

User = get_user_model()


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class SellerAIFeatureTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='sellerai',
            email='sellerai@test.com',
            password='pass123'
        )
        self.store = Store.objects.create(owner=self.user, name='AI Store', slug='ai-store')
        self.client.login(username='sellerai', password='pass123')

    def _activate_plan(self, plan):
        Subscription.objects.create(
            store=self.store,
            plan=plan,
            status='active',
            amount=1000,
            started_at=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
        )

    def test_premium_plan_gets_seller_ai_access(self):
        self._activate_plan('premium')
        self.assertTrue(PlanPermissions.has_feature_access(self.user, 'seller_ai_assistant', store=self.store))
        self.assertTrue(PlanPermissions.has_feature_access(self.user, 'seller_ai_bulk_cleanup', store=self.store))
        self.assertFalse(PlanPermissions.has_feature_access(self.user, 'seller_ai_actions', store=self.store))

    def test_dashboard_shows_copilot_for_premium(self):
        self._activate_plan('premium')
        response = self.client.get(reverse('storefront:seller_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Baysoko AI Copilot')

    def test_bulk_ai_preflight_requires_ai_access(self):
        csv_file = SimpleUploadedFile(
            'products.csv',
            b'title,price,category\nPhone,1200,Electronics\n',
            content_type='text/csv',
        )
        response = self.client.post(
            reverse('storefront:ai_bulk_import_preflight', kwargs={'slug': self.store.slug}),
            {'file': csv_file},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 403)

    def test_bulk_ai_preflight_returns_analysis_for_premium(self):
        self._activate_plan('premium')
        csv_file = SimpleUploadedFile(
            'products.csv',
            (
                b'title,price,category,image_url,stock\n'
                b'Phone,1200,Electronics,https://example.com/p1.jpg,4\n'
                b'Cable,350,Accessories,,ten\n'
            ),
            content_type='text/csv',
        )
        response = self.client.post(
            reverse('storefront:ai_bulk_import_preflight', kwargs={'slug': self.store.slug}),
            {'file': csv_file},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertIn('confidence_score', payload['result']['stats'])
        self.assertTrue(payload['result']['warnings'])
