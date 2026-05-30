from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0010_user_neighborhood'),
    ]

    operations = [
        migrations.AddField(
            model_name='profileimage',
            name='category',
            field=models.CharField(
                blank=True,
                choices=[('', 'Uncategorized'), ('face', 'Face'), ('torso', 'Torso'), ('explicit', 'Explicit'), ('other', 'Other')],
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name='ImageShareGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(
                    choices=[('face', 'Face'), ('torso', 'Torso'), ('explicit', 'Explicit'), ('other', 'Other')],
                    max_length=10,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('grantor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='image_grants_given', to=settings.AUTH_USER_MODEL)),
                ('grantee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='image_grants_received', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('grantor', 'grantee', 'category')},
            },
        ),
    ]
