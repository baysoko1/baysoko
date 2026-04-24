# storefront/models_bulk.py
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import FileExtensionValidator
import uuid

User = get_user_model()

class BatchJob(models.Model):
    """Track bulk operations"""
    JOB_TYPES = [
        ('product_update', 'Product Update'),
        ('price_update', 'Price Update'),
        ('stock_update', 'Stock Update'),
        ('status_update', 'Status Update'),
        ('image_update', 'Image Update'),
        ('category_update', 'Category Update'),
        ('export', 'Export Products'),
        ('import', 'Import Products'),
    ]
    
    JOB_STATUS = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('completed_with_errors', 'Completed With Errors'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    store = models.ForeignKey('Store', on_delete=models.CASCADE, related_name='batch_jobs')
    job_type = models.CharField(max_length=20, choices=JOB_TYPES)
    status = models.CharField(max_length=32, choices=JOB_STATUS, default='pending')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    parameters = models.JSONField(default=dict)  # Store job parameters
    results = models.JSONField(default=dict)  # Store job results
    total_items = models.IntegerField(default=0)
    processed_items = models.IntegerField(default=0)
    success_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    errors = models.JSONField(default=list)  # Store error details
    file = models.FileField(
        upload_to='bulk_operations/',
        null=True,
        blank=True,
        validators=[FileExtensionValidator(allowed_extensions=['csv', 'xlsx', 'xls'])]
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['store', 'status', 'created_at']),
        ]
    
    def __str__(self):
        return f"{self.get_job_type_display()} - {self.store.name} ({self.status})"
    
    @property
    def progress_percentage(self):
        if self.total_items == 0:
            return 0
        return int((self.processed_items / self.total_items) * 100)
    
    @property
    def duration(self):
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

class ExportJob(models.Model):
    """Track export jobs"""
    FORMAT_CHOICES = [
        ('csv', 'CSV'),
        ('excel', 'Excel'),
        ('json', 'JSON'),
        ('pdf', 'PDF'),
    ]
    
    store = models.ForeignKey('Store', on_delete=models.CASCADE, related_name='export_jobs')
    export_type = models.CharField(max_length=50)  # 'products', 'customers', 'orders'
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default='csv')
    filters = models.JSONField(default=dict)  # Store filter criteria
    columns = models.JSONField(default=list)  # Columns to include
    file = models.FileField(upload_to='exports/', null=True, blank=True)
    file_size = models.IntegerField(default=0)
    download_count = models.IntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    status = models.CharField(max_length=32, choices=BatchJob.JOB_STATUS, default='pending')
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.export_type} Export - {self.store.name}"
    
    @property
    def filename(self):
        if self.file:
            return self.file.name.split('/')[-1]
        return None

class ImportTemplate(models.Model):
    """Store import templates for different purposes"""
    TEMPLATE_TYPES = [
        ('products', 'Products'),
        ('customers', 'Customers'),
        ('orders', 'Orders'),
        ('inventory', 'Inventory'),
    ]
    
    store = models.ForeignKey('Store', on_delete=models.CASCADE, related_name='import_templates')
    name = models.CharField(max_length=200)
    template_type = models.CharField(max_length=20, choices=TEMPLATE_TYPES)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to='import_templates/')
    field_mapping = models.JSONField(default=dict)  # CSV column to model field mapping
    required_fields = models.JSONField(default=list)
    sample_data = models.JSONField(default=dict)
    download_count = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = ['store', 'name', 'template_type']
    
    def __str__(self):
        return f"{self.name} ({self.get_template_type_display()})"

class BulkOperationLog(models.Model):
    """Detailed log for each item in bulk operation"""
    batch_job = models.ForeignKey(BatchJob, on_delete=models.CASCADE, related_name='logs')
    item_identifier = models.CharField(max_length=500)  # Could be product ID, SKU, etc.
    action = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=[('success', 'Success'), ('error', 'Error')])
    details = models.JSONField(default=dict)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['batch_job', 'status']),
        ]
    
    def __str__(self):
        return f"{self.item_identifier} - {self.status}"
