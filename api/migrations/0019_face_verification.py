from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0018_power_position_expand'),
    ]

    operations = [
        migrations.CreateModel(
            name='FaceVerification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('verified', 'Verified'), ('rejected', 'Rejected')],
                    default='pending',
                    max_length=10,
                )),
                ('submitted_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='verification',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]
