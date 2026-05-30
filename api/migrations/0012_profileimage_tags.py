from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0011_profileimage_category_imagesharegrant'),
    ]

    operations = [
        migrations.RemoveField(model_name='profileimage', name='category'),
        migrations.AddField(
            model_name='profileimage', name='tag_face',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='profileimage', name='tag_torso',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='profileimage', name='tag_explicit',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='profileimage', name='tag_other',
            field=models.BooleanField(default=False),
        ),
    ]
