from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0008_user_location'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='location_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
