import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0042_user_areas'),
    ]

    operations = [
        migrations.CreateModel(
            name='Friend',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('friend', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='api.persona')),
                ('persona', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='friends_added', to='api.persona')),
            ],
            options={
                'unique_together': {('persona', 'friend')},
            },
        ),
        migrations.CreateModel(
            name='ProfileView',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('viewed_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('viewed', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='profile_views', to='api.persona')),
                ('viewer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='api.persona')),
            ],
            options={
                'unique_together': {('viewer', 'viewed')},
            },
        ),
    ]
