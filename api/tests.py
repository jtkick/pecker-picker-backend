from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from .models import Album, AlbumGrant, Block, Conversation, Message, Persona, PersonaPhoto, ProfileImage, User


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
