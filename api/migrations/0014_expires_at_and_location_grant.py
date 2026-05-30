import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0013_user_blur_explicit'),
    ]

    operations = [
        migrations.AddField(
            model_name='imagesharegrant',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='LocationShareGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('level', models.SmallIntegerField(choices=[(1, 'General'), (2, 'Exact')], default=2)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('grantee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='location_grants_received', to=settings.AUTH_USER_MODEL)),
                ('grantor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='location_grants_given', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('grantor', 'grantee')},
            },
        ),
    ]
