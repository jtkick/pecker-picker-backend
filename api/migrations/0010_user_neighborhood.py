from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0009_user_location_updated_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='neighborhood',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
