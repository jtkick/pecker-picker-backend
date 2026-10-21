import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def is_read_to_last_read(apps, schema_editor):
    """Each member has read everything they sent plus every incoming message flagged is_read."""
    ConversationMember = apps.get_model('api', 'ConversationMember')
    Message = apps.get_model('api', 'Message')
    for member in ConversationMember.objects.all():
        last_id = (
            Message.objects
            .filter(conversation_id=member.conversation_id)
            .filter(Q(sender_id=member.persona_id) | Q(is_read=True))
            .order_by('-id')
            .values_list('id', flat=True)
            .first()
        )
        if last_id is not None:
            member.last_read_message_id = last_id
            member.last_delivered_message_id = last_id
            member.save(update_fields=['last_read_message', 'last_delivered_message'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0029_personas_and_albums'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Promote the auto-created participants table to an explicit through model,
        # keeping its rows: state-only here, then rename the table.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='ConversationMember',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='members', to='api.conversation')),
                        ('persona', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='api.persona')),
                    ],
                    options={
                        'db_table': 'api_conversation_participants',
                        'unique_together': {('conversation', 'persona')},
                    },
                ),
                migrations.AlterField(
                    model_name='conversation',
                    name='participants',
                    field=models.ManyToManyField(related_name='conversations', through='api.ConversationMember', to='api.persona'),
                ),
            ],
            database_operations=[],
        ),
        migrations.AlterModelTable(name='conversationmember', table=None),
        migrations.AddField(
            model_name='conversationmember',
            name='role',
            field=models.SmallIntegerField(choices=[(0, 'Member'), (1, 'Admin'), (2, 'Owner')], default=0),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='joined_at',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='left_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='last_read_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='api.message'),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='last_delivered_message',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='api.message'),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='muted_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='archived',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='conversationmember',
            name='pinned',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='message',
            name='kind',
            field=models.CharField(choices=[('text', 'Text'), ('image', 'Image'), ('video', 'Video'), ('voice', 'Voice'), ('album', 'Album'), ('location', 'Location'), ('live_location', 'Live location'), ('profile', 'Profile'), ('system', 'System')], default='text', max_length=20),
        ),
        migrations.AddField(
            model_name='message',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='message',
            name='client_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='reply_to',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='api.message'),
        ),
        migrations.AddField(
            model_name='message',
            name='edited_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='message',
            name='body',
            field=models.TextField(blank=True, max_length=2000),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(fields=('sender', 'client_id'), name='unique_message_client_id'),
        ),
        migrations.RunPython(is_read_to_last_read, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='message',
            name='is_read',
        ),
        migrations.CreateModel(
            name='DeviceToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(max_length=512, unique=True)),
                ('platform', models.CharField(choices=[('ios', 'iOS'), ('android', 'Android')], max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='device_tokens', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
