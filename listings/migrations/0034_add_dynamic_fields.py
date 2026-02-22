from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0033_alter_listing_image_alter_listingimage_image_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='fields_schema',
            field=models.JSONField(blank=True, default=dict, help_text='JSON schema for category-specific fields'),
        ),
        migrations.AddField(
            model_name='listing',
            name='dynamic_fields',
            field=models.JSONField(blank=True, default=dict, help_text='Stores category-specific field values'),
        ),
    ]
