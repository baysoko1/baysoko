from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('storefront', '0026_storevideocomment_storevideolike'),
    ]

    operations = [
        migrations.AlterField(
            model_name='batchjob',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('completed', 'Completed'),
                    ('completed_with_errors', 'Completed With Errors'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name='exportjob',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('completed', 'Completed'),
                    ('completed_with_errors', 'Completed With Errors'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=32,
            ),
        ),
    ]
