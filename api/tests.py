import io
import json
import uuid
from datetime import timedelta
from unittest import mock

from asgiref.testing import ApplicationCommunicator
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework.test import APITestCase, APITransactionTestCase

from pecker_picker.asgi import application
from .media import presigned_url, strip_image_metadata
from .models import (
    Album, AlbumGrant, Block, Conversation, ConversationMember, DeviceToken, Message, Persona,
    PersonaPhoto, ProfileImage, Report, User,
)


def make_user(phone, name, plus=False, lat=39.1, lng=-84.5):
    user = User.objects.create_user(phone_number=phone, latitude=lat, longitude=lng)
    if plus:
        user.subscription_tier = User.TIER_PLUS
        user.save()
    persona = user.get_active_persona()
    persona.display_name = name
    persona.save()
    return user, persona


class PersonaTestCase(APITestCase):
    def setUp(self):
        self.alice, self.alice_main = make_user('+15550100101', 'Alice')
        self.bob, self.bob_main = make_user('+15550100102', 'Bob')

    def as_user(self, user):
        self.client.force_authenticate(user)


class PersonaLimitTests(PersonaTestCase):
    def test_free_limit_is_two(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.post('/api/personas/', {'display_name': 'Alt'}).status_code, 201)
        res = self.client.post('/api/personas/', {'display_name': 'Third'})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['requires_tier'], User.TIER_PLUS)

    def test_plus_limit_is_twelve(self):
        self.alice.subscription_tier = User.TIER_PLUS
        self.alice.save()
        self.as_user(self.alice)
        for i in range(11):
            self.assertEqual(self.client.post('/api/personas/', {'display_name': f'P{i}'}).status_code, 201)
        res = self.client.post('/api/personas/', {'display_name': 'Too many'})
        self.assertEqual(res.status_code, 403)
        self.assertIsNone(res.data['requires_tier'])

    def test_duplicate_copies_fields_and_photos(self):
        self.alice_main.bio = 'hello'
        self.alice_main.save()
        img = ProfileImage.objects.create(user=self.alice, image='gallery/a.jpg')
        PersonaPhoto.objects.create(persona=self.alice_main, image=img)
        self.as_user(self.alice)
        res = self.client.post('/api/personas/', {'duplicate_from': self.alice_main.pk, 'display_name': 'Copy'})
        self.assertEqual(res.status_code, 201)
        copy = Persona.objects.get(pk=res.data['id'])
        self.assertEqual(copy.bio, 'hello')
        self.assertEqual(copy.display_name, 'Copy')
        self.assertEqual(list(copy.photos.values_list('image_id', flat=True)), [img.pk])

    def test_primary_cannot_be_deleted(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.delete(f'/api/personas/{self.alice_main.pk}/').status_code, 400)


class DowngradeLockTests(PersonaTestCase):
    def test_extras_lock_after_downgrade(self):
        self.alice.subscription_tier = User.TIER_PLUS
        self.alice.save()
        self.as_user(self.alice)
        ids = [self.client.post('/api/personas/', {'display_name': f'P{i}'}).data['id'] for i in range(3)]
        self.client.post(f'/api/personas/{ids[1]}/activate/')

        self.alice.subscription_expires_at = timezone.now() - timedelta(days=1)
        self.alice.save()

        listing = {p['id']: p['is_locked'] for p in self.client.get('/api/personas/').data}
        self.assertFalse(listing[self.alice_main.pk])
        self.assertFalse(listing[ids[1]])   # active / most recently used stays unlocked
        self.assertTrue(listing[ids[0]])
        self.assertTrue(listing[ids[2]])
        self.assertEqual(self.client.post(f'/api/personas/{ids[0]}/activate/').status_code, 403)


class DiscoverTests(PersonaTestCase):
    def test_only_active_persona_is_discoverable(self):
        self.as_user(self.alice)
        alt_id = self.client.post('/api/personas/', {'display_name': 'Alt'}).data['id']

        self.as_user(self.bob)
        names = [p['display_name'] for p in self.client.get('/api/profiles/').data]
        self.assertEqual(names, ['Alice'])

        self.as_user(self.alice)
        self.client.post(f'/api/personas/{alt_id}/activate/')
        self.as_user(self.bob)
        names = [p['display_name'] for p in self.client.get('/api/profiles/').data]
        self.assertEqual(names, ['Alt'])

    def test_inactive_persona_detail_hidden_without_conversation(self):
        alt = Persona.objects.create(user=self.alice, display_name='Alt')
        self.as_user(self.bob)
        self.assertEqual(self.client.get(f'/api/profiles/{alt.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/profiles/{self.alice_main.pk}/').status_code, 200)

    def test_block_hides_all_personas_both_ways(self):
        alt = Persona.objects.create(user=self.alice, display_name='Alt')
        self.as_user(self.bob)
        self.assertEqual(self.client.post('/api/blocks/', {'persona_id': alt.pk}).status_code, 204)
        self.assertEqual(self.client.get('/api/profiles/').data, [])
        self.as_user(self.alice)
        self.assertEqual(self.client.get('/api/profiles/').data, [])
        self.assertEqual(self.client.get(f'/api/profiles/{self.bob_main.pk}/').status_code, 404)
        self.assertTrue(Block.objects.filter(blocker=self.bob, blocked=self.alice, blocked_persona=alt).exists())

        self.as_user(self.bob)
        self.assertEqual([p['id'] for p in self.client.get('/api/blocks/').data], [alt.pk])
        self.client.delete(f'/api/blocks/{alt.pk}/')
        self.assertFalse(Block.objects.exists())


class ConversationTests(PersonaTestCase):
    def test_inbox_scoped_to_active_persona_with_unread_counts(self):
        self.as_user(self.alice)
        alt_id = self.client.post('/api/personas/', {'display_name': 'Alt'}).data['id']
        self.client.post(f'/api/personas/{alt_id}/activate/')

        self.as_user(self.bob)
        conv = self.client.post('/api/conversations/', {'persona_id': alt_id}).data
        self.client.post(f"/api/conversations/{conv['id']}/messages/", {'body': 'hi'})

        self.as_user(self.alice)
        self.client.post(f'/api/personas/{self.alice_main.pk}/activate/')
        self.assertEqual(self.client.get('/api/conversations/').data, [])
        unread = {p['id']: p['unread_count'] for p in self.client.get('/api/personas/').data}
        self.assertEqual(unread, {self.alice_main.pk: 0, alt_id: 1})
        self.assertEqual(self.client.get(f"/api/conversations/{conv['id']}/messages/").status_code, 404)

        self.client.post(f'/api/personas/{alt_id}/activate/')
        self.assertEqual(len(self.client.get('/api/conversations/').data), 1)
        self.assertEqual(self.client.get(f"/api/conversations/{conv['id']}/messages/").status_code, 200)
        self.assertEqual(Message.objects.get().sender_id, self.bob_main.pk)

    def test_deleting_persona_removes_its_conversations(self):
        alt = Persona.objects.create(user=self.alice, display_name='Alt')
        conv = Conversation.objects.create()
        conv.participants.add(alt, self.bob_main)
        self.as_user(self.alice)
        self.assertEqual(self.client.delete(f'/api/personas/{alt.pk}/').status_code, 204)
        self.assertFalse(Conversation.objects.exists())


class AlbumGrantTests(PersonaTestCase):
    def setUp(self):
        super().setUp()
        self.public = ProfileImage.objects.create(user=self.alice, image='gallery/public.jpg')
        self.private = ProfileImage.objects.create(user=self.alice, image='gallery/private.jpg')
        PersonaPhoto.objects.create(persona=self.alice_main, image=self.public)
        self.album = Album.objects.create(user=self.alice, name='Private')
        self.album.images.add(self.private)

    def gallery_ids(self):
        res = self.client.get(f'/api/profiles/{self.alice_main.pk}/')
        return [g['id'] for g in res.data['gallery_images']]

    def test_grant_reveals_album_to_grantee_persona_only(self):
        self.as_user(self.alice)
        res = self.client.post('/api/album-grants/', {'album_id': self.album.pk, 'grantee_id': self.bob_main.pk})
        self.assertEqual(res.status_code, 201)

        self.as_user(self.bob)
        self.assertEqual(self.gallery_ids(), [self.public.pk, self.private.pk])

        bob_alt = Persona.objects.create(user=self.bob, display_name='Bob Alt')
        self.client.post(f'/api/personas/{bob_alt.pk}/activate/')
        self.assertEqual(self.gallery_ids(), [self.public.pk])

    def test_expired_grant_hides_album(self):
        AlbumGrant.objects.create(
            grantor=self.alice_main, grantee=self.bob_main, album=self.album,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.as_user(self.bob)
        self.assertEqual(self.gallery_ids(), [self.public.pk])

    def test_persona_photos_must_come_from_own_library(self):
        bob_img = ProfileImage.objects.create(user=self.bob, image='gallery/bob.jpg')
        self.as_user(self.alice)
        url = f'/api/personas/{self.alice_main.pk}/photos/'
        self.assertEqual(self.client.put(url, {'image_ids': [bob_img.pk]}, format='json').status_code, 400)
        res = self.client.put(url, {'image_ids': [self.private.pk, self.public.pk]}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual([g['id'] for g in res.data['gallery_images']], [self.private.pk, self.public.pk])


class MessagingTestCase(PersonaTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()   # send throttle counts persist across tests otherwise
        self.conv = Conversation.objects.create()
        self.conv.participants.add(self.alice_main, self.bob_main)
        self.url = f'/api/conversations/{self.conv.pk}/messages/'

    def send(self, user, body='hi', **extra):
        self.as_user(user)
        return self.client.post(self.url, {'body': body, **extra}, format='json')

    def unread_for(self, user, persona):
        self.as_user(user)
        return {p['id']: p['unread_count'] for p in self.client.get('/api/personas/').data}[persona.pk]


class MessagingTests(MessagingTestCase):
    def test_cursor_pagination(self):
        ids = [self.send(self.alice, f'm{i}').data['id'] for i in range(5)]
        self.as_user(self.bob)
        latest = self.client.get(self.url, {'limit': 2}).data
        self.assertEqual([m['id'] for m in latest], ids[3:])
        older = self.client.get(self.url, {'limit': 2, 'before': ids[3]}).data
        self.assertEqual([m['id'] for m in older], ids[1:3])
        newer = self.client.get(self.url, {'after': ids[1]}).data
        self.assertEqual([m['id'] for m in newer], ids[2:])

    def test_client_id_makes_send_idempotent(self):
        cid = str(uuid.uuid4())
        first = self.send(self.alice, 'hello', client_id=cid)
        retry = self.send(self.alice, 'hello', client_id=cid)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.data['id'], retry.data['id'])
        self.assertEqual(Message.objects.count(), 1)

    def test_reply_must_be_in_same_conversation(self):
        other = Conversation.objects.create()
        other.participants.add(self.alice_main, Persona.objects.create(user=self.bob, display_name='B2'))
        foreign = Message.objects.create(conversation=other, sender=self.alice_main, body='x')
        self.assertEqual(self.send(self.alice, 'r', reply_to_id=foreign.pk).status_code, 400)
        target = self.send(self.bob, 'q').data
        res = self.send(self.alice, 'r', reply_to_id=target['id'])
        self.assertEqual(res.data['reply_to']['id'], target['id'])

    def test_read_receipts_and_unread_counts(self):
        m1 = self.send(self.alice, 'one').data['id']
        m2 = self.send(self.alice, 'two').data['id']
        self.assertEqual(self.unread_for(self.bob, self.bob_main), 2)
        self.assertEqual(self.unread_for(self.alice, self.alice_main), 0)

        self.as_user(self.bob)
        self.client.post(f'/api/conversations/{self.conv.pk}/read/', {'message_id': m1})
        self.assertEqual(self.unread_for(self.bob, self.bob_main), 1)
        # Receipts never move backwards.
        self.client.post(f'/api/conversations/{self.conv.pk}/read/', {'message_id': m2})
        self.client.post(f'/api/conversations/{self.conv.pk}/read/', {'message_id': m1})
        self.assertEqual(ConversationMember.objects.get(persona=self.bob_main).last_read_message_id, m2)

        self.as_user(self.alice)
        conv = self.client.get('/api/conversations/').data[0]
        bob = next(m for m in conv['members'] if m['persona_id'] == self.bob_main.pk)
        self.assertEqual((bob['last_read_message_id'], bob['last_delivered_message_id']), (m2, m2))
        self.assertEqual(conv['last_message']['id'], m2)

    def test_opening_chat_marks_read_but_paging_does_not(self):
        ids = [self.send(self.alice, f'm{i}').data['id'] for i in range(3)]
        self.as_user(self.bob)
        self.client.get(self.url, {'before': ids[2]})
        self.assertEqual(self.unread_for(self.bob, self.bob_main), 3)
        self.client.get(self.url)
        self.assertEqual(self.unread_for(self.bob, self.bob_main), 0)

    def test_blocked_user_cannot_send(self):
        Block.objects.create(blocker=self.bob, blocked=self.alice)
        self.assertEqual(self.send(self.alice).status_code, 403)

    def test_send_broadcasts_to_members(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.send(self.alice, 'ping')
        with mock.patch('api.realtime.send_to_users') as send:
            for cb in callbacks:
                cb()
        user_ids, payload = send.call_args.args
        self.assertEqual(user_ids, {self.alice.pk, self.bob.pk})
        self.assertEqual(payload['type'], 'message.new')
        self.assertEqual(payload['message']['body'], 'ping')

    def test_device_registration(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.post('/api/devices/', {'token': 't1', 'platform': 'ios'}).status_code, 204)
        self.as_user(self.bob)
        self.client.post('/api/devices/', {'token': 't1', 'platform': 'ios'})
        self.assertEqual(DeviceToken.objects.get(token='t1').user, self.bob)
        self.client.delete('/api/devices/', {'token': 't1'})
        self.assertFalse(DeviceToken.objects.exists())


class MessageInteractionTests(MessagingTestCase):
    def msg_url(self, mid, suffix=''):
        return f'{self.url}{mid}/{suffix}'

    def test_edit_and_unsend_only_own_within_window(self):
        mid = self.send(self.alice, 'tpyo').data['id']
        self.as_user(self.bob)
        self.assertEqual(self.client.patch(self.msg_url(mid), {'body': 'x'}, format='json').status_code, 403)

        self.as_user(self.alice)
        res = self.client.patch(self.msg_url(mid), {'body': 'typo'}, format='json')
        self.assertEqual((res.data['body'], res.data['edited_at'] is not None), ('typo', True))

        Message.objects.filter(pk=mid).update(created_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.client.delete(self.msg_url(mid)).status_code, 403)

        mid2 = self.send(self.alice, 'oops').data['id']
        self.client.put(self.msg_url(mid2, 'reaction/'), {'emoji': '😂'}, format='json')
        res = self.client.delete(self.msg_url(mid2))
        self.assertEqual((res.data['body'], res.data['reactions']), ('', []))
        self.assertIsNotNone(res.data['deleted_at'])

    def test_hide_is_per_member(self):
        mid = self.send(self.alice, 'secret').data['id']
        self.as_user(self.bob)
        self.client.post(self.msg_url(mid, 'hide/'))
        self.assertEqual(self.client.get(self.url).data, [])
        self.as_user(self.alice)
        self.assertEqual([m['id'] for m in self.client.get(self.url).data], [mid])

    def test_reactions_set_replace_clear(self):
        mid = self.send(self.alice, 'hi').data['id']
        self.as_user(self.bob)
        self.client.put(self.msg_url(mid, 'reaction/'), {'emoji': '❤️'}, format='json')
        self.client.put(self.msg_url(mid, 'reaction/'), {'emoji': '🔥'}, format='json')
        self.assertEqual(self.client.get(self.url).data[0]['reactions'], [{'persona_id': self.bob_main.pk, 'emoji': '🔥'}])
        self.assertEqual(self.client.put(self.msg_url(mid, 'reaction/'), {'emoji': 'a b'}, format='json').status_code, 400)
        self.client.delete(self.msg_url(mid, 'reaction/'))
        self.assertEqual(self.client.get(self.url).data[0]['reactions'], [])

    def test_search_and_around(self):
        ids = [self.send(self.alice, body).data['id'] for body in ['apple pie', 'banana', 'Apple tart', 'cherry']]
        self.as_user(self.bob)
        self.assertEqual([m['id'] for m in self.client.get(self.url, {'q': 'apple'}).data], [ids[2], ids[0]])
        around = self.client.get(self.url, {'around': ids[1], 'limit': 2}).data
        self.assertEqual([m['id'] for m in around], [ids[0], ids[1], ids[2]])

    def test_conversation_settings(self):
        other = Conversation.objects.create()
        carol, carol_main = make_user('+15550100103', 'Carol')
        other.participants.add(self.bob_main, carol_main)
        Conversation.objects.filter(pk=self.conv.pk).update(updated_at=timezone.now() - timedelta(days=1))

        self.as_user(self.bob)
        self.assertEqual([c['id'] for c in self.client.get('/api/conversations/').data], [other.pk, self.conv.pk])
        res = self.client.patch(f'/api/conversations/{self.conv.pk}/', {'pinned': True, 'marked_unread': True}, format='json')
        self.assertEqual(res.data['unread_count'], 1)
        self.assertEqual([c['id'] for c in self.client.get('/api/conversations/').data], [self.conv.pk, other.pk])

        self.client.get(self.url)   # opening the chat clears "mark unread"
        self.assertFalse(ConversationMember.objects.get(conversation=self.conv, persona=self.bob_main).marked_unread)

        self.client.patch(f'/api/conversations/{self.conv.pk}/', {'archived': True, 'muted_until': '2099-01-01T00:00:00Z'}, format='json')
        self.send(self.alice, 'wake up')
        self.as_user(self.bob)
        settings = next(c for c in self.client.get('/api/conversations/').data if c['id'] == self.conv.pk)['settings']
        self.assertFalse(settings['archived'])
        self.assertTrue(settings['muted_until'].startswith('2099'))
        self.assertEqual(self.client.patch(f'/api/conversations/{self.conv.pk}/', {'pinned': 'yes'}, format='json').status_code, 400)

    def test_forwarded_flag(self):
        self.assertEqual(self.send(self.alice, 'fwd', forwarded=True).data['metadata'], {'forwarded': True})

    def test_saved_phrases(self):
        self.as_user(self.alice)
        pid = self.client.post('/api/phrases/', {'text': 'Looking?'}).data['id']
        self.assertEqual(self.client.get('/api/phrases/').data, [{'id': pid, 'text': 'Looking?'}])
        self.as_user(self.bob)
        self.client.delete(f'/api/phrases/{pid}/')   # not Bob's: no effect
        self.as_user(self.alice)
        self.client.delete(f'/api/phrases/{pid}/')
        self.assertEqual(self.client.get('/api/phrases/').data, [])

    def test_report_with_message_evidence(self):
        mine = self.send(self.alice, 'hello').data['id']
        theirs = self.send(self.bob, 'rude').data['id']
        self.as_user(self.alice)
        data = {'persona_id': self.bob_main.pk, 'reason': 'harassment', 'message_id': mine}
        self.assertEqual(self.client.post('/api/reports/', data).status_code, 404)
        data['message_id'] = theirs
        self.assertEqual(self.client.post('/api/reports/', data).status_code, 204)
        report = Report.objects.get()
        self.assertEqual((report.message_id, report.conversation_id), (theirs, self.conv.pk))

    def test_activity_only_shown_for_active_persona(self):
        Persona.objects.filter(pk=self.bob_main.pk).update(last_active_at=timezone.now())
        self.as_user(self.alice)
        bob = lambda: next(m for m in self.client.get('/api/conversations/').data[0]['members'] if m['persona_id'] == self.bob_main.pk)
        self.assertIsNotNone(bob()['last_active_at'])
        bob_alt = Persona.objects.create(user=self.bob, display_name='Bob Alt')
        User.objects.filter(pk=self.bob.pk).update(active_persona=bob_alt)
        self.assertIsNone(bob()['last_active_at'])

def jpeg(name='p.jpg', with_exif=True):
    exif = Image.Exif()
    if with_exif:
        exif[0x010F] = 'PhoneMaker'
    buf = io.BytesIO()
    Image.new('RGB', (40, 30)).save(buf, format='JPEG', exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class ImageMessageTests(MessagingTestCase):
    def send_images(self, user, files, **extra):
        self.as_user(user)
        return self.client.post(self.url, {'kind': 'image', 'files': files, **extra}, format='multipart')

    @mock.patch('api.views.auto_tag', return_value={'tag_explicit': True})
    def test_send_photos_strips_metadata_and_tags(self, _):
        res = self.send_images(self.alice, [jpeg(), jpeg('q.jpg')], body='look')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['kind'], 'image')
        attachments = res.data['attachments']
        self.assertEqual([(a['width'], a['height'], a['explicit'], a['mime']) for a in attachments],
                         [(40, 30, True, 'image/jpeg')] * 2)
        stored = Message.objects.get().attachments.first().file
        with stored.open('rb') as f:
            self.assertEqual(dict(Image.open(f).getexif()), {})

    def test_photo_validation(self):
        self.assertEqual(self.send_images(self.alice, []).status_code, 400)
        self.assertEqual(self.send_images(self.alice, [jpeg() for _ in range(5)]).status_code, 400)
        junk = SimpleUploadedFile('x.jpg', b'nope', content_type='image/jpeg')
        self.assertEqual(self.send_images(self.alice, [junk]).status_code, 400)
        self.assertEqual(self.send_images(self.alice, [jpeg(), jpeg()], view_once='true').status_code, 400)
        self.as_user(self.alice)
        self.assertEqual(self.client.post(self.url, {'kind': 'voice', 'body': 'x'}).status_code, 400)

    def test_view_once_opens_exactly_once(self):
        res = self.send_images(self.alice, [jpeg()], view_once='true')
        attachment = res.data['attachments'][0]
        self.assertIsNone(attachment['url'])
        view_url = f"{self.url}{res.data['id']}/attachments/{attachment['id']}/view/"

        self.assertEqual(self.client.post(view_url).status_code, 403)   # sender can't open it
        self.as_user(self.bob)
        first = self.client.post(view_url)
        self.assertEqual((first.status_code, first['Content-Type']), (200, 'image/jpeg'))
        self.assertEqual(Image.open(io.BytesIO(first.content)).size, (40, 30))
        self.assertEqual(self.client.post(view_url).status_code, 410)
        self.assertFalse(Message.objects.get().attachments.get().file)
        self.assertTrue(self.client.get(self.url).data[0]['attachments'][0]['viewed'])

    def test_unsend_removes_photos(self):
        mid = self.send_images(self.alice, [jpeg()]).data['id']
        self.as_user(self.alice)
        res = self.client.delete(f'{self.url}{mid}/')
        self.assertEqual(res.data['attachments'], [])
        self.assertFalse(Message.objects.get().attachments.exists())

class SocketClient(ApplicationCommunicator):
    """Minimal WebSocket test client (channels.testing needs daphne)."""

    def __init__(self, headers=()):
        super().__init__(application, {
            'type': 'websocket', 'path': '/ws/chat/', 'headers': list(headers),
            'query_string': b'', 'subprotocols': [],
        })

    async def connect(self):
        await self.send_input({'type': 'websocket.connect'})
        return (await self.receive_output(1))['type'] == 'websocket.accept'

    async def send_json_to(self, data):
        await self.send_input({'type': 'websocket.receive', 'text': json.dumps(data)})

    async def receive_json_from(self):
        return json.loads((await self.receive_output(1))['text'])

    async def disconnect(self):
        await self.send_input({'type': 'websocket.disconnect', 'code': 1000})
        await self.wait(1)


class ChatSocketTests(APITransactionTestCase):
    # Transactional so on_commit fan-out actually runs.
    def setUp(self):
        self.alice, self.alice_main = make_user('+15550100101', 'Alice')
        self.bob, self.bob_main = make_user('+15550100102', 'Bob')
        self.conv = Conversation.objects.create()
        self.conv.participants.add(self.alice_main, self.bob_main)

    async def connect(self, user):
        token = str(AccessToken.for_user(user))
        comm = SocketClient(headers=[(b'authorization', f'Bearer {token}'.encode())])
        return comm, await comm.connect()

    async def test_rejects_missing_token(self):
        self.assertFalse(await SocketClient().connect())

    async def test_typing_relayed_to_other_member_only(self):
        alice, ok_a = await self.connect(self.alice)
        bob, ok_b = await self.connect(self.bob)
        self.assertTrue(ok_a and ok_b)
        await alice.send_json_to({'type': 'typing', 'conversation_id': self.conv.pk})
        event = await bob.receive_json_from()
        self.assertEqual(event, {'type': 'typing', 'conversation_id': self.conv.pk, 'persona_id': self.alice_main.pk})
        self.assertTrue(await alice.receive_nothing())
        await alice.disconnect()
        await bob.disconnect()

    async def test_ping_records_activity(self):
        bob, _ = await self.connect(self.bob)
        await database_sync_to_async(Persona.objects.filter(pk=self.bob_main.pk).update)(last_active_at=None)
        await bob.send_json_to({'type': 'ping'})
        self.assertTrue(await bob.receive_nothing())
        persona = await database_sync_to_async(Persona.objects.get)(pk=self.bob_main.pk)
        self.assertIsNotNone(persona.last_active_at)
        await bob.disconnect()

    async def test_server_events_reach_socket(self):
        bob, _ = await self.connect(self.bob)
        await get_channel_layer().group_send(f'user_{self.bob.pk}', {'type': 'chat.event', 'payload': {'type': 'x'}})
        self.assertEqual(await bob.receive_json_from(), {'type': 'x'})
        await bob.disconnect()


class MediaTests(APITestCase):
    @override_settings(
        MEDIA_PUBLIC_URL='http://media.example:9010', AWS_STORAGE_BUCKET_NAME='b',
        AWS_ACCESS_KEY_ID='k', AWS_SECRET_ACCESS_KEY='s', MEDIA_URL_EXPIRY_SECONDS=3600,
    )
    def test_presigned_url_stable_within_window(self):
        url = presigned_url('gallery/a b.jpg', now=1_000_000_000)
        self.assertTrue(url.startswith('http://media.example:9010/b/gallery/a%20b.jpg?'))
        self.assertIn('X-Amz-Expires=3600', url)
        self.assertEqual(url, presigned_url('gallery/a b.jpg', now=1_000_000_000 + 60))
        self.assertNotEqual(url, presigned_url('gallery/a b.jpg', now=1_000_000_000 + 3600))

    def test_strip_image_metadata_removes_exif(self):
        exif = Image.Exif()
        exif[0x010F] = 'PhoneMaker'
        buf = io.BytesIO()
        Image.new('RGB', (4, 4)).save(buf, format='JPEG', exif=exif)
        clean = strip_image_metadata(SimpleUploadedFile('p.jpg', buf.getvalue()))
        self.assertEqual(dict(Image.open(clean).getexif()), {})
        self.assertIsNone(strip_image_metadata(SimpleUploadedFile('x.jpg', b'not an image')))
