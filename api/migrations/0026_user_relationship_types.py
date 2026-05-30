from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0025_user_interests_interest'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='relationship_types',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
