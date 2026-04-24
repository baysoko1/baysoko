# storefront/urls_bulk.py
from django.urls import path
import importlib


def lazy_view(dotted_path):
     """Return a view function that imports the real view when called."""
     module_name, func_name = dotted_path.rsplit('.', 1)

     def _view(request, *args, **kwargs):
          module = importlib.import_module(module_name)
          view = getattr(module, func_name)
          return view(request, *args, **kwargs)

     return _view


bulk_patterns = [
    # Bulk Operations Dashboard
     path('dashboard/store/<slug:slug>/bulk/', lazy_view('storefront.views_bulk.bulk_operations_dashboard'), name='bulk_dashboard'),
    
    # Bulk Update Products
     path('dashboard/store/<slug:slug>/bulk/update/', lazy_view('storefront.views_bulk.bulk_update_products'), name='bulk_update_products'),
    
    # Bulk Import
     path('dashboard/store/<slug:slug>/bulk/import/', lazy_view('storefront.views_bulk.bulk_import_data'), name='bulk_import_data'),
    
    # Export Data
     path('dashboard/store/<slug:slug>/bulk/export/', lazy_view('storefront.views_bulk.export_data'), name='export_data'),
    
    # Templates Management
    path('dashboard/store/<slug:slug>/bulk/templates/', lazy_view('storefront.views_bulk.manage_templates'), name='manage_templates'),
    path('dashboard/store/<slug:slug>/bulk/templates/<int:template_id>/delete/', lazy_view('storefront.views_bulk.delete_template'), name='delete_template'),
    path('dashboard/store/<slug:slug>/bulk/templates/<int:template_id>/download/', lazy_view('storefront.views_bulk.download_template'), name='download_template'),
    path('dashboard/store/<slug:slug>/bulk/templates/sample/', lazy_view('storefront.views_bulk.generate_sample_template'), name='generate_sample_template'),
    
    # Batch Jobs
    path('dashboard/store/<slug:slug>/bulk/jobs/', lazy_view('storefront.views_bulk.bulk_job_list'), name='bulk_job_list'),
    path('dashboard/store/<slug:slug>/bulk/jobs/<int:job_id>/', lazy_view('storefront.views_bulk.bulk_job_detail'), name='bulk_job_detail'),
    path('dashboard/store/<slug:slug>/bulk/jobs/<int:job_id>/cancel/', lazy_view('storefront.views_bulk.cancel_job'), name='cancel_job'),
    path('dashboard/store/<slug:slug>/bulk/jobs/<int:job_id>/progress/', lazy_view('storefront.views_bulk.get_job_progress'), name='get_job_progress'),
    
    # Export Jobs
    path('dashboard/store/<slug:slug>/bulk/exports/', lazy_view('storefront.views_bulk.export_job_list'), name='export_job_list'),
    path('dashboard/store/<slug:slug>/bulk/exports/<int:job_id>/', lazy_view('storefront.views_bulk.export_job_detail'), name='export_job_detail'),
    path('dashboard/store/<slug:slug>/bulk/exports/<int:job_id>/download/', lazy_view('storefront.views_bulk.download_export'), name='download_export'),
    
    # AJAX Endpoints
    path('dashboard/store/<slug:slug>/bulk/ajax/export-columns/', lazy_view('storefront.views_bulk.get_export_columns'), name='get_export_columns'),
    path('dashboard/store/<slug:slug>/bulk/ajax/quick-action/', lazy_view('storefront.views_bulk.quick_bulk_action'), name='quick_bulk_action'),
    path('dashboard/store/<slug:slug>/bulk/ajax/ai-preflight/', lazy_view('storefront.views_bulk.ai_bulk_import_preflight'), name='ai_bulk_import_preflight'),
]

bundle_patterns = [
    # Bundle Dashboard
     path('dashboard/store/<slug:slug>/bundles/', lazy_view('storefront.views_bundles.bundle_dashboard'), name='bundle_dashboard'),
    
    # Bundle Management
    path('dashboard/store/<slug:slug>/bundles/list/', lazy_view('storefront.views_bundles.bundle_list'), name='bundle_list'),
    path('dashboard/store/<slug:slug>/bundles/create/', lazy_view('storefront.views_bundles.bundle_create'), name='bundle_create'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/', lazy_view('storefront.views_bundles.bundle_detail'), name='bundle_detail'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/edit/', lazy_view('storefront.views_bundles.bundle_edit'), name='bundle_edit'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/items/', lazy_view('storefront.views_bundles.bundle_items'), name='bundle_items'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/toggle/', lazy_view('storefront.views_bundles.bundle_toggle_active'), name='bundle_toggle_active'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/delete/', lazy_view('storefront.views_bundles.bundle_delete'), name='bundle_delete'),
    
    # Bundle Items
     path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/items/<int:item_id>/delete/', lazy_view('storefront.views_bundles.bundle_item_delete'), name='bundle_item_delete'),
    
    # Bundle Rules
    path('dashboard/store/<slug:slug>/bundles/rules/', lazy_view('storefront.views_bundles.bundle_rules'), name='bundle_rules'),
    path('dashboard/store/<slug:slug>/bundles/rules/<int:rule_id>/delete/', lazy_view('storefront.views_bundles.bundle_rule_delete'), name='bundle_rule_delete'),
    
    # Upsell Products
    path('dashboard/store/<slug:slug>/bundles/upsells/', lazy_view('storefront.views_bundles.upsell_products'), name='upsell_products'),
    path('dashboard/store/<slug:slug>/bundles/upsells/<int:upsell_id>/delete/', lazy_view('storefront.views_bundles.upsell_delete'), name='upsell_delete'),
    
    # Product Templates
    path('dashboard/store/<slug:slug>/bundles/templates/', lazy_view('storefront.views_bundles.product_templates'), name='product_templates'),
    path('dashboard/store/<slug:slug>/bundles/templates/<int:template_id>/delete/', lazy_view('storefront.views_bundles.template_delete'), name='template_delete'),
    path('dashboard/store/<slug:slug>/bundles/quick-create/', lazy_view('storefront.views_bundles.quick_product_create'), name='quick_product_create'),
    
    # Bulk Image Upload
     path('dashboard/store/<slug:slug>/bundles/bulk-images/', lazy_view('storefront.views_bundles.bulk_image_upload'), name='bulk_image_upload'),
    
    # Product Recommendations
     path('dashboard/store/<slug:slug>/bundles/recommendations/', lazy_view('storefront.views_bundles.product_recommendations'), name='product_recommendations'),
    
    # AJAX Endpoints
    path('dashboard/store/<slug:slug>/bundles/ajax/template-variables/<int:template_id>/', lazy_view('storefront.views_bundles.get_template_variables'), name='get_template_variables'),
    path('dashboard/store/<slug:slug>/bundles/<int:bundle_id>/ajax/update-item-order/', lazy_view('storefront.views_bundles.update_bundle_item_order'), name='update_bundle_item_order'),
    path('dashboard/store/<slug:slug>/bundles/ajax/update-bundle-order/', lazy_view('storefront.views_bundles.update_bundle_order'), name='update_bundle_order'),
]

