from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0014_expires_at_and_location_grant'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='subscription_tier',
            field=models.SmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user',
            name='subscription_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='stripe_customer_id',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='user',
            name='stripe_subscription_id',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
