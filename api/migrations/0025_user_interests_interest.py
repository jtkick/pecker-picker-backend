from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0024_share_kinks'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='interests',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='Interest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='api.interest')),
            ],
            options={
                'ordering': ['name'],
            },
        ),
    ]
