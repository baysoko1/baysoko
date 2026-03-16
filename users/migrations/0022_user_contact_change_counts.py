from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0021_alter_user_location_length'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='email_change_count',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user',
            name='phone_change_count',
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
