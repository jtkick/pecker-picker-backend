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
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework.test import APITestCase, APITransactionTestCase

import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.core.management import call_command
from django.core.management.base import CommandError

from pecker_picker.asgi import application
from . import discover, geo, lifeline
from .media import presigned_url, strip_image_metadata
from .models import (
    Album, AlbumGrant, Block, Conversation, ConversationMember, DeviceToken, DiscoverEvent, Lifeline, LocationShareGrant,
    Message, MessageAttachment, OTPVerification, Persona, PersonaMember, PersonaPhoto, ProfileImage, Report, User,
    WebAuthnChallenge,
)
from .views import DiscoverThrottle


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


class GroupPersonaTests(PersonaTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.alice.subscription_tier = User.TIER_PLUS
        self.alice.save()
        self.bob.pronouns, self.bob.height = 'he/him', 180
        self.bob.latitude = 39.3
        self.bob.save()
        self.carol, self.carol_main = make_user('+15550100103', 'Carol')
        self.as_user(self.alice)
        res = self.client.post('/api/personas/', {'display_name': 'A+B', 'group_type': 'couple'})
        self.assertEqual(res.status_code, 201)
        self.group = Persona.objects.get(pk=res.data['id'])

    def invite(self, persona, as_user=None):
        self.as_user(as_user or self.alice)
        return self.client.post(f'/api/personas/{self.group.pk}/members/', {'persona_id': persona.pk})

    def join_bob(self, **share):
        self.assertEqual(self.invite(self.bob_main).status_code, 201)
        self.as_user(self.bob)
        invitation = self.client.get('/api/invitations/').data[0]
        res = self.client.post(f'/api/invitations/{invitation["id"]}/accept/',
                               {'represent_persona_id': self.bob_main.pk, **share}, format='json')
        self.assertEqual(res.status_code, 200)
        return PersonaMember.objects.get(persona=self.group, user=self.bob)

    def listing(self, user):
        self.as_user(user)
        return {p['id']: p for p in self.client.get('/api/personas/').data}

    def test_only_plus_creates_groups(self):
        self.as_user(self.bob)
        res = self.client.post('/api/personas/', {'display_name': 'G', 'group_type': 'group'})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['requires_tier'], User.TIER_PLUS)
        owner = PersonaMember.objects.get(persona=self.group)
        self.assertEqual((owner.user, owner.role, owner.status), (self.alice, 'owner', 'accepted'))

    def test_free_invitee_joins_without_using_their_limit(self):
        self.join_bob()
        entry = self.listing(self.bob)[self.group.pk]
        self.assertEqual((entry['is_locked'], entry['role'], entry['can_edit'], entry['member_count']),
                         (False, 'member', False, 2))
        self.assertEqual(self.client.post('/api/personas/', {'display_name': 'Bob alt'}).status_code, 201)
        self.assertEqual(self.client.post(f'/api/personas/{self.group.pk}/activate/').status_code, 200)

    def test_edit_rights_and_share_flags(self):
        member = self.join_bob()
        url = f'/api/personas/{self.group.pk}/members/{member.pk}/'
        self.as_user(self.bob)
        self.assertEqual(self.client.patch(f'/api/personas/{self.group.pk}/', {'bio': 'x'}).status_code, 403)
        self.assertEqual(self.client.patch(url, {'can_edit': True}, format='json').status_code, 403)
        self.as_user(self.alice)
        self.assertEqual(self.client.patch(url, {'share_height': True}, format='json').status_code, 403)
        self.assertEqual(self.client.patch(url, {'can_edit': True}, format='json').status_code, 200)
        self.as_user(self.bob)
        self.assertEqual(self.client.patch(f'/api/personas/{self.group.pk}/', {'bio': 'x'}).status_code, 200)
        self.assertEqual(self.client.patch(url, {'share_height': True}, format='json').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/personas/{self.group.pk}/').status_code, 403)

    def test_discover_shows_only_shared_member_info_at_active_members_location(self):
        self.join_bob(share_profile=True, share_pronouns=True)
        self.group.location_sharing = 2   # capped to General for groups
        self.group.save()
        self.as_user(self.bob)
        self.client.post(f'/api/personas/{self.group.pk}/activate/')
        self.as_user(self.carol)
        profiles = {p['id']: p for p in self.client.get('/api/profiles/').data}
        self.assertIn(self.alice_main.pk, profiles)   # Alice still uses her own profile
        group = profiles[self.group.pk]
        self.assertEqual(group['member_count'], 2)
        self.assertIn({'is_verified': False}, group['members'])   # Alice shares nothing by default
        bob = next(m for m in group['members'] if 'display_name' in m)
        self.assertEqual(bob['display_name'], 'Bob')
        self.assertEqual(bob['pronouns'], 'he/him')
        self.assertNotIn('height', bob)
        self.assertNotEqual(group['latitude'], 39.3)
        self.assertAlmostEqual(group['latitude'], 39.3, delta=0.005)

    def test_messages_reach_all_members_and_only_they_see_who_sent(self):
        self.join_bob()
        conv = Conversation.objects.create()
        conv.participants.add(self.group, self.carol_main)
        for user in (self.alice, self.bob):
            self.as_user(user)
            self.client.post(f'/api/personas/{self.group.pk}/activate/')
        url = f'/api/conversations/{conv.pk}/messages/'
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.as_user(self.bob)
            self.assertEqual(self.client.post(url, {'body': 'hi'}).data['sent_by'], 'Bob')
        with mock.patch('api.views.send_to_users') as send:
            for cb in callbacks:
                cb()
        sent = {frozenset(c.args[0]): c.args[1]['message']['sent_by'] for c in send.call_args_list
                if c.args[1]['type'] == 'message.new'}
        self.assertEqual(sent, {frozenset({self.alice.pk, self.bob.pk}): 'Bob', frozenset({self.carol.pk}): None})
        self.as_user(self.alice)
        self.assertEqual(self.client.get(url).data[-1]['sent_by'], 'Bob')
        self.as_user(self.carol)
        self.assertIsNone(self.client.get(url).data[-1]['sent_by'])

    def test_owner_downgrade_locks_group_for_everyone(self):
        self.join_bob()
        self.as_user(self.bob)
        self.client.post(f'/api/personas/{self.group.pk}/activate/')
        self.alice.subscription_expires_at = timezone.now() - timedelta(days=1)
        self.alice.save()
        self.assertTrue(self.listing(self.bob)[self.group.pk]['is_locked'])
        self.assertEqual(self.client.patch(f'/api/personas/{self.group.pk}/', {'bio': 'x'}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/personas/{self.bob_main.pk}/activate/').status_code, 200)
        self.assertEqual(self.client.post(f'/api/personas/{self.group.pk}/activate/').status_code, 403)
        self.as_user(self.carol)
        self.assertNotIn(self.group.pk, [p['id'] for p in self.client.get('/api/profiles/').data])

    def test_blocks_hide_the_group(self):
        self.join_bob()
        self.as_user(self.alice)
        self.client.post(f'/api/personas/{self.group.pk}/activate/')
        self.as_user(self.carol)
        self.assertEqual(self.client.post('/api/blocks/', {'persona_id': self.group.pk}).status_code, 204)
        ids = [p['id'] for p in self.client.get('/api/profiles/').data]
        self.assertNotIn(self.group.pk, ids)
        self.assertIn(self.bob_main.pk, ids)   # members' own profiles stay visible
        self.assertTrue(Block.objects.filter(blocker=self.carol, blocked=None, blocked_persona=self.group).exists())
        self.client.delete(f'/api/blocks/{self.group.pk}/')
        self.assertIn(self.group.pk, [p['id'] for p in self.client.get('/api/profiles/').data])
        Block.objects.create(blocker=self.bob, blocked=self.carol)   # an account block with any member
        self.assertNotIn(self.group.pk, [p['id'] for p in self.client.get('/api/profiles/').data])

    def test_leaving_resets_active_persona_and_removes_own_photos(self):
        member = self.join_bob()
        member.can_edit = True
        member.save()
        img = ProfileImage.objects.create(user=self.bob, image='gallery/b.jpg')
        self.as_user(self.bob)
        self.client.post(f'/api/personas/{self.group.pk}/activate/')
        self.assertEqual(self.client.put(f'/api/personas/{self.group.pk}/photos/', {'image_ids': [img.pk]},
                                         format='json').status_code, 200)
        self.assertEqual(self.client.delete(f'/api/personas/{self.group.pk}/members/{member.pk}/').status_code, 204)
        self.bob.refresh_from_db()   # force_authenticate reuses this object
        self.assertEqual(self.client.get('/api/me/').data['active_persona_id'], self.bob_main.pk)
        self.assertNotIn(self.group.pk, self.listing(self.bob))
        self.assertFalse(self.group.photos.exists())
        self.assertEqual(self.listing(self.alice)[self.group.pk]['member_count'], 1)

    def test_cap_and_no_reinvite_after_decline(self):
        self.assertEqual(self.invite(self.bob_main).status_code, 201)
        self.assertEqual(self.invite(self.carol_main).status_code, 400)   # a couple is two people
        self.as_user(self.bob)
        invitation = self.client.get('/api/invitations/').data[0]
        self.assertEqual(invitation['group']['display_name'], 'A+B')
        self.assertEqual(invitation['invited_by']['display_name'], 'Alice')
        self.client.post(f'/api/invitations/{invitation["id"]}/decline/')
        self.assertEqual(self.invite(self.bob_main).status_code, 400)
        self.assertEqual(self.invite(self.carol_main, as_user=self.bob).status_code, 404)   # not a member


class DiscoverScoringTests(APITestCase):
    """The pure ranking functions in api.discover and api.geo."""

    def persona(self, types=(), idle=timedelta(0), **fields):
        now = timezone.now()
        return SimpleNamespace(relationship_types=list(types), last_active_at=now - idle, created_at=now, **fields)

    def test_visibility_window_depends_on_what_they_want(self):
        now = timezone.now()
        hookup = timedelta(hours=3)
        self.assertTrue(discover.is_visible(self.persona(['Hookups'], hookup - timedelta(minutes=1)), now))
        self.assertFalse(discover.is_visible(self.persona(['Hookups'], hookup + timedelta(minutes=1)), now))
        self.assertTrue(discover.is_visible(self.persona(['Long-term'], timedelta(days=20)), now))
        # With several, the most patient applies; nothing selected is Browsing.
        self.assertEqual(discover.freshness_profile(['Hookups', 'Dates']), discover.FRESHNESS['Dates'])
        self.assertEqual(discover.freshness_profile([]), discover.BROWSING_FRESHNESS)

    def test_freshness_halves_at_half_life_and_is_full_when_online(self):
        now = timezone.now()
        self.assertAlmostEqual(discover.freshness(self.persona(['Dates'], timedelta(days=1)), now), 0.5, places=3)
        self.assertEqual(discover.freshness(self.persona(['Hookups'], timedelta(minutes=2)), now), 1.0)

    def test_intent_compatibility(self):
        ic = discover.intent_compat
        self.assertEqual(ic(['Hookups'], ['Hookups']), 1.0)
        self.assertEqual(ic(['Hookups'], ['FWB']), 0.7)
        self.assertEqual(ic(['Hookups'], ['Dates']), 0.3)
        self.assertEqual(ic(['Friends'], ['Long-term']), 0.3)
        self.assertEqual(ic(['Hookups'], ['Marriage-minded']), 0.0)
        self.assertEqual(ic([], ['Hookups']), 0.5)

    def test_position_and_power_complement(self):
        pos = lambda position, is_side=False: SimpleNamespace(position=position, is_side=is_side)
        self.assertEqual(discover.position_compat(pos(4), pos(0)), 1.0)     # Top + Bottom
        self.assertEqual(discover.position_compat(pos(4), pos(4)), 0.0)     # Top + Top
        self.assertEqual(discover.position_compat(pos(2), pos(2)), 1.0)     # Vers + Vers
        self.assertEqual(discover.position_compat(pos(None, True), pos(None, True)), 1.0)
        self.assertEqual(discover.position_compat(pos(None, True), pos(4)), discover.SIDE_WITH_OTHER)
        self.assertIsNone(discover.position_compat(pos(None), pos(4)))

    def test_jitter_is_small_and_stable_within_a_day(self):
        today = timezone.now().date()
        self.assertEqual(discover.jitter(1, 2, today), discover.jitter(1, 2, today))
        self.assertLessEqual(abs(discover.jitter(1, 2, today)), discover.JITTER)

    def test_distance_labels_reveal_only_what_sharing_allows(self):
        self.assertEqual(geo.distance_label(0.4, 1), '< 1 km')
        self.assertEqual(geo.distance_label(4.2, 1), '5 km')
        self.assertEqual(geo.distance_label(80, 1), '50+ km')
        self.assertIsNone(geo.distance_label(4.2, 0))
        self.assertEqual(geo.distance_label(0.35, 2), '350 m')

    def test_filters_are_cleaned(self):
        cleaned = discover.clean_filters({'max_distance_km': 999, 'looking_for': ['Dates', 'nope'],
                                          'groups': 'all', 'online_now': 'yes', 'junk': 1})
        self.assertEqual(cleaned, {'max_distance_km': discover.MAX_RADIUS_KM, 'looking_for': ['Dates']})


class DiscoverFeedTests(PersonaTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.alice_main.relationship_types = ['Long-term']
        self.alice_main.save()

    def person(self, phone, name, types=(), idle=timedelta(0), **fields):
        user, persona = make_user(phone, name)
        persona.relationship_types = list(types)
        persona.last_active_at = timezone.now() - idle
        for k, v in fields.items():
            setattr(persona, k, v)
        persona.save()
        return persona

    def feed(self, **params):
        self.as_user(self.alice)
        res = self.client.get('/api/discover/', params)
        self.assertEqual(res.status_code, 200)
        return res.data

    def test_compatible_people_rank_first(self):
        self.bob_main.delete()
        hookup = self.person('+15550100201', 'Hookup', ['Hookups'])
        longterm = self.person('+15550100202', 'Longterm', ['Long-term'])
        self.assertEqual([p['id'] for p in self.feed()['results']], [longterm.pk, hookup.pk])

    def test_quiet_profiles_fade_by_intent(self):
        hookup = self.person('+15550100201', 'Hookup', ['Hookups'], idle=timedelta(hours=4))
        longterm = self.person('+15550100202', 'Longterm', ['Long-term'], idle=timedelta(days=10))
        ids = [p['id'] for p in self.feed()['results']]
        self.assertIn(longterm.pk, ids)
        self.assertNotIn(hookup.pk, ids)
        self.assertNotIn(hookup.pk, [p['id'] for p in self.client.get('/api/profiles/').data])   # old apps too

    def test_paging_continues_the_snapshot(self):
        people = [self.person(f'+155501003{i:02d}', f'P{i}', ['Dates']) for i in range(65)]
        first = self.feed()
        self.assertEqual(len(first['results']), discover.PAGE_SIZE)
        self.assertEqual(first['results'][0]['rank'], 0)
        # Activity changes mid-scroll must not reshuffle later pages.
        Persona.objects.filter(pk__in=[p.pk for p in people]).update(last_active_at=timezone.now())
        second = self.feed(cursor=first['next_cursor'])
        third = self.feed(cursor=second['next_cursor'])
        self.assertIsNone(third['next_cursor'])
        ids = [p['id'] for page in (first, second, third) for p in page['results']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 66)   # everyone plus Bob
        self.assertEqual(second['results'][0]['rank'], discover.PAGE_SIZE)

    def test_expired_cursor_starts_over(self):
        first = self.feed()
        cache.clear()
        again = self.feed(cursor='gone:30')
        self.assertEqual(again['results'][0]['rank'], 0)
        self.assertEqual([p['id'] for p in again['results']], [p['id'] for p in first['results']])

    def test_plus_filters_need_plus(self):
        self.alice_main.discover_filters = {'online_now': True, 'looking_for': ['Dates']}
        self.alice_main.save()
        dates = self.person('+15550100201', 'Dates', ['Dates'], idle=timedelta(hours=2))
        self.person('+15550100202', 'Friends', ['Friends'])
        self.assertEqual([p['id'] for p in self.feed()['results']], [dates.pk])   # free: looking_for only
        self.alice.subscription_tier = User.TIER_PLUS
        self.alice.save()
        self.assertEqual(self.feed()['results'], [])   # Plus: also online now, and Dates was idle 2 h

    def test_filters_saved_through_persona_are_cleaned(self):
        self.as_user(self.alice)
        res = self.client.patch(f'/api/personas/{self.alice_main.pk}/',
                                {'discover_filters': {'age_min': 25, 'bogus': True}}, format='json')
        self.assertEqual(res.data['discover_filters'], {'age_min': 25})

    def test_distance_is_banded_unless_exact_is_shared(self):
        self.bob_main.location_sharing = 1
        self.bob_main.save()
        self.bob.latitude, self.bob.longitude = 39.1 + 0.03, -84.5   # ~3.3 km north
        self.bob.save()
        self.assertIn(self.feed()['results'][0]['distance'], ('3 km', '5 km'))
        self.bob_main.location_sharing = 0
        self.bob_main.save()
        self.assertIsNone(self.feed()['results'][0]['distance'])
        LocationShareGrant.objects.create(grantor=self.bob_main, grantee=self.alice_main, level=2)
        self.assertRegex(self.feed()['results'][0]['distance'], r'^\d+\.\d km$')

    def test_map_shows_pins_in_the_box_except_off_location(self):
        self.bob_main.location_sharing = 1
        self.bob_main.save()
        hidden = self.person('+15550100201', 'Off', location_sharing=0)
        self.as_user(self.alice)
        pins = self.client.get('/api/discover/map/', {'bbox': '39.0,-84.6,39.2,-84.4'}).data
        self.assertEqual([p['id'] for p in pins], [self.bob_main.pk])
        self.assertNotIn(hidden.pk, [p['id'] for p in pins])
        self.assertEqual(self.client.get('/api/discover/map/', {'bbox': 'nope'}).status_code, 400)

    def test_opening_from_the_feed_is_logged(self):
        self.as_user(self.alice)
        self.client.get(f'/api/profiles/{self.bob_main.pk}/', {'rank': 3, 'sort': 'nearby'})
        event = DiscoverEvent.objects.get()
        self.assertEqual((event.kind, event.rank, event.sort), ('opened', 3, 'nearby'))
        self.client.get(f'/api/profiles/{self.bob_main.pk}/')   # not from the feed
        self.assertEqual(DiscoverEvent.objects.count(), 1)

    def test_throttled(self):
        with mock.patch.object(DiscoverThrottle, 'THROTTLE_RATES', {'discover': '2/min'}):
            self.feed()
            self.feed()
            self.assertEqual(self.client.get('/api/discover/').status_code, 429)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
@mock.patch('api.management.commands.demo_users.reverse_geocode', return_value='Downtown')
class DemoUserTests(PersonaTestCase):
    RIVER = {
        'handle': 'river', 'display_name': 'River', 'age': 29, 'looking_for': ['Hookups'],
        'position': 'vers', 'location': [39.1, -84.5], 'active_minutes_ago': 60 * 24 * 30,
        'photos': [{'file': '1.png', 'tags': ['face']}],
    }

    def setUp(self):
        super().setUp()
        cache.clear()
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / 'photos' / 'river').mkdir(parents=True)
        Image.new('RGB', (8, 8), 'red').save(self.dir / 'photos' / 'river' / '1.png')

    def sync(self, *entries):
        (self.dir / 'users.json').write_text(json.dumps(list(entries)))
        call_command('demo_users', 'sync', dir=str(self.dir), stdout=io.StringIO())

    def feed_ids(self, user):
        cache.clear()
        self.as_user(user)
        return [p['id'] for p in self.client.get('/api/discover/').data['results']]

    def test_sync_creates_then_updates_in_place(self, _):
        self.sync(self.RIVER)
        user = User.objects.get(demo_handle='river')
        persona = user.get_active_persona()
        self.assertEqual((persona.display_name, persona.position, persona.relationship_types), ('River', 2, ['Hookups']))
        image = user.gallery_images.get()
        self.assertTrue(image.tag_face)
        self.sync({**self.RIVER, 'display_name': 'River B', 'photos': [{'file': '1.png', 'tags': ['torso']}]})
        image.refresh_from_db()   # same photo kept, only its tags changed
        self.assertEqual((image.tag_face, image.tag_torso), (False, True))
        self.assertEqual(User.objects.filter(demo_handle='river').count(), 1)
        self.assertEqual(user.get_active_persona().photos.count(), 1)

    def test_only_testers_see_them_and_they_never_fade(self, _):
        self.sync(self.RIVER)
        river = User.objects.get(demo_handle='river').get_active_persona()
        self.assertNotIn(river.pk, self.feed_ids(self.alice))
        self.assertEqual(self.client.get(f'/api/profiles/{river.pk}/').status_code, 404)
        self.alice.is_tester = True
        self.alice.save()
        self.assertIn(river.pk, self.feed_ids(self.alice))   # idle 30 days, looking for hookups
        cache.clear()
        result = next(p for p in self.client.get('/api/discover/').data['results'] if p['id'] == river.pk)
        self.assertTrue(result['is_demo'])

    def test_beta_switch_shows_them_to_everyone_at_any_distance(self, _):
        self.sync(self.RIVER)
        river = User.objects.get(demo_handle='river').get_active_persona()
        far, _persona = make_user('+15550100109', 'Reviewer', lat=37.33, lng=-122.03)   # Cupertino
        self.assertNotIn(river.pk, self.feed_ids(far))
        with self.settings(DEMO_PROFILES_FOR_EVERYONE=True):
            self.assertIn(river.pk, self.feed_ids(far))

    def test_switching_off_and_on(self, _):
        self.sync(self.RIVER)
        self.alice.is_tester = True
        self.alice.save()
        river = User.objects.get(demo_handle='river').get_active_persona()
        call_command('demo_users', 'off', stdout=io.StringIO())
        self.assertNotIn(river.pk, self.feed_ids(self.alice))
        call_command('demo_users', 'on', 'river', stdout=io.StringIO())
        self.assertIn(river.pk, self.feed_ids(self.alice))

    def test_problems_in_the_file_are_reported_and_nothing_changes(self, _):
        with self.assertRaisesMessage(CommandError, 'unknown field "colour"'):
            self.sync({**self.RIVER, 'colour': 'red'})
        with self.assertRaisesMessage(CommandError, 'photo 2.png not found'):
            self.sync({**self.RIVER, 'photos': [{'file': '2.png'}]})
        self.assertFalse(User.objects.filter(demo_handle__isnull=False).exists())

    def test_testers_synthetic_and_remove(self, _):
        call_command('demo_users', 'tester', 'add', str(self.alice.pk), stdout=io.StringIO())
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_tester)
        call_command('demo_users', 'synthetic', '3', stdout=io.StringIO())
        self.assertEqual(User.objects.filter(demo_handle__startswith='synthetic_').count(), 3)
        call_command('demo_users', 'remove', 'synthetic_0000', stdout=io.StringIO())
        self.assertEqual(User.objects.filter(demo_handle__startswith='synthetic_').count(), 2)
        with self.assertRaises(CommandError):
            call_command('demo_users', 'tester', 'add', '999999', stdout=io.StringIO())


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


class HealthCheckTests(APITestCase):
    def test_ok_when_db_and_cache_answer(self):
        r = self.client.get('/api/health/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, {'status': 'ok', 'db': 'ok', 'cache': 'ok'})

    def test_503_when_cache_is_down(self):
        with mock.patch('api.views.cache') as down:
            down.set.side_effect = ConnectionError
            r = self.client.get('/api/health/')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['cache'], 'error')


class AuthThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()

    @override_settings(DEBUG=True)   # dev path: codes are logged, not texted
    def test_otp_send_is_limited_per_phone(self):
        codes = [
            self.client.post('/api/auth/otp/request/', {'phone_number': '+15550001111'}).status_code
            for _ in range(6)
        ]
        self.assertEqual(codes, [200] * 5 + [429])
        # Another number has its own allowance.
        r = self.client.post('/api/auth/otp/request/', {'phone_number': '+15550002222'})
        self.assertEqual(r.status_code, 200)

    def test_passkey_begin_is_limited_per_ip(self):
        codes = [self.client.post('/api/auth/passkey/authenticate/begin/').status_code for _ in range(31)]
        self.assertNotIn(429, codes[:30])
        self.assertEqual(codes[30], 429)


class PruneTests(APITestCase):
    def test_deletes_only_long_expired_rows(self):
        now = timezone.now()
        WebAuthnChallenge.objects.create(
            session_id=uuid.uuid4(), challenge='old', action='register', expires_at=now - timedelta(days=2),
        )
        fresh = WebAuthnChallenge.objects.create(
            session_id=uuid.uuid4(), challenge='new', action='register', expires_at=now - timedelta(minutes=1),
        )
        OTPVerification.objects.create(phone_number='+15550001111', code='123456', expires_at=now - timedelta(days=2))
        call_command('prune', stdout=io.StringIO())
        self.assertEqual(list(WebAuthnChallenge.objects.values_list('pk', flat=True)), [fresh.pk])
        self.assertFalse(OTPVerification.objects.exists())


@override_settings(FIREBASE_CREDENTIALS_FILE='creds.json')
class LifelineTests(MessagingTestCase):
    def setUp(self):
        super().setUp()
        self.carol, self.carol_main = make_user('+15550100103', 'Carol')
        push = mock.patch('api.push._send')
        self.push = push.start()
        self.addCleanup(push.stop)

    def start(self, ids=None, minutes=60):
        self.as_user(self.alice)
        return self.client.post('/api/lifelines/', {
            'recipient_persona_ids': ids or [self.bob_main.pk], 'duration_minutes': minutes,
        }, format='json')

    def run_alert(self, lifeline_id):
        Lifeline.objects.filter(pk=lifeline_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.captureOnCommitCallbacks(execute=True):
            return lifeline.alert_due()

    def test_recipients_must_be_chat_partners(self):
        self.assertEqual(self.start([self.carol_main.pk]).status_code, 400)
        Block.objects.create(blocker=self.bob, blocked=self.alice)
        self.assertEqual(self.start().status_code, 400)

    def test_one_open_lifeline(self):
        self.assertEqual(self.start().status_code, 201)
        self.assertEqual(self.start().status_code, 409)
        current = self.client.get('/api/lifelines/current/').data
        self.assertEqual(current['lifeline']['recipients'], [{'id': self.bob_main.pk, 'display_name': 'Bob'}])

    def test_snooze_extends_resets_warning_and_caps(self):
        res = self.start(minutes=12 * 60)
        Lifeline.objects.filter(pk=res.data['id']).update(warned_at=timezone.now())
        url = f"/api/lifelines/{res.data['id']}/snooze/"
        for _ in range(20):
            self.client.post(url, {'minutes': 60}, format='json')
        row = Lifeline.objects.get(pk=res.data['id'])
        self.assertIsNone(row.warned_at)
        self.assertEqual(row.expires_at, row.started_at + Lifeline.MAX_DURATION)
        self.assertEqual(self.client.post(url, {'minutes': 5}, format='json').status_code, 400)

    def test_warns_once(self):
        res = self.start(minutes=15)
        Lifeline.objects.filter(pk=res.data['id']).update(expires_at=timezone.now() + timedelta(minutes=5))
        with self.captureOnCommitCallbacks(execute=True):
            lifeline.warn_due()
            lifeline.warn_due()
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.push.call_args.args[0][0][0], self.alice.pk)

    def test_nothing_sent_before_expiry(self):
        self.start()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(lifeline.alert_due(), 0)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(LocationShareGrant.objects.exists())

    def test_alert_posts_message_shares_location_and_pushes(self):
        res = self.start()
        with mock.patch('api.realtime.send_to_users'):
            self.assertEqual(self.run_alert(res.data['id']), 1)
        msg = Message.objects.get()
        self.assertEqual((msg.conversation_id, msg.sender_id, msg.kind), (self.conv.pk, self.alice_main.pk, 'system'))
        self.assertEqual(msg.metadata['type'], 'lifeline.alert')
        self.assertEqual((msg.metadata['lat'], msg.metadata['lng']), (39.1, -84.5))
        grant = LocationShareGrant.objects.get(grantor=self.alice_main, grantee=self.bob_main)
        self.assertEqual(grant.level, 2)
        self.assertEqual([t[0] for t in self.push.call_args.args[0]], [self.bob.pk])
        self.assertEqual(Lifeline.objects.get().status, Lifeline.STATUS_ALERTED)

    def test_safe_check_in_restores_grants(self):
        ConversationMember.objects.filter(persona=self.bob_main).update(muted_until=timezone.now() + timedelta(days=1))
        conv = Conversation.objects.create()
        conv.participants.add(self.alice_main, self.carol_main)
        permanent = LocationShareGrant.objects.create(grantor=self.alice_main, grantee=self.carol_main, level=2)
        res = self.start([self.bob_main.pk, self.carol_main.pk])
        with mock.patch('api.realtime.send_to_users'):
            self.run_alert(res.data['id'])
            # Muted chats still get the alert.
            self.assertEqual(sorted(t[0] for t in self.push.call_args.args[0]), [self.bob.pk, self.carol.pk])
            with self.captureOnCommitCallbacks(execute=True):
                ended = self.client.post(f"/api/lifelines/{res.data['id']}/end/", {'reason': 'manual'}, format='json')
        self.assertEqual(ended.data['status'], 'ended')
        self.assertEqual(Message.objects.filter(metadata__type='lifeline.safe').count(), 2)
        self.assertEqual(list(LocationShareGrant.objects.values_list('pk', 'expires_at')), [(permanent.pk, None)])

    def test_end_before_expiry_is_silent(self):
        res = self.start()
        self.client.post(f"/api/lifelines/{res.data['id']}/end/", {'reason': 'arrived'}, format='json')
        with self.captureOnCommitCallbacks(execute=True):
            lifeline.alert_due()
        self.assertFalse(Message.objects.exists())
        self.assertEqual(Lifeline.objects.get().end_reason, 'arrived')

    def test_other_users_cannot_touch_it(self):
        res = self.start()
        self.as_user(self.bob)
        self.assertEqual(self.client.post(f"/api/lifelines/{res.data['id']}/end/").status_code, 404)


class AndroidOriginTests(APITestCase):
    def test_origin_is_base64url_of_signing_cert_hash(self):
        from pecker_picker.settings import android_app_origin
        fingerprint = ':'.join(f'{i:02X}' for i in range(32))
        self.assertEqual(
            android_app_origin(fingerprint),
            'android:apk-key-hash:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
        )


class OtpSendFailureTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_unconfigured_sms_in_production_is_an_error_not_code_sent(self):
        r = self.client.post('/api/auth/otp/request/', {'phone_number': '+15550001111'})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(OTPVerification.objects.exists())

    @override_settings(TWILIO_ENABLED=True)
    def test_twilio_refusal_is_reported(self):
        from twilio.base.exceptions import TwilioRestException
        with mock.patch('twilio.rest.Client') as client:
            client.return_value.verify.v2.services.return_value.verifications.create.side_effect = (
                TwilioRestException(400, 'https://verify.twilio.com', msg='Invalid parameter `To`', code=60200)
            )
            r = self.client.post('/api/auth/otp/request/', {'phone_number': '+1555'})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['detail'], "We couldn't send a code to that number.")


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class AccountDeletionTests(PersonaTestCase):
    def setUp(self):
        super().setUp()
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        self.storage = default_storage
        self.carol, self.carol_main = make_user('+15550100103', 'Carol')
        self.photo = ProfileImage.objects.create(user=self.alice, image=ContentFile(b'a', name='a.jpg'))
        self.ab = Conversation.objects.create()
        self.ab.participants.add(self.alice_main, self.bob_main)
        Message.objects.create(conversation=self.ab, sender=self.alice_main, sender_user=self.alice, body='hi')
        from_bob = Message.objects.create(conversation=self.ab, sender=self.bob_main, sender_user=self.bob, kind='image')
        self.bob_photo = MessageAttachment.objects.create(
            message=from_bob, mime='image/jpeg', file=ContentFile(b'b', name='b.jpg'),
        )
        self.bc = Conversation.objects.create()
        self.bc.participants.add(self.bob_main, self.carol_main)
        self.bob_to_carol = Message.objects.create(conversation=self.bc, sender=self.bob_main, sender_user=self.bob)
        # Sent by Alice as a member of a group persona someone else owns.
        self.as_group = Message.objects.create(conversation=self.bc, sender=self.bob_main, sender_user=self.alice)

    def delete_alice(self):
        self.as_user(self.alice)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.delete('/api/me/')

    def test_deletes_account_conversations_messages_and_files(self):
        files = [self.photo.image.name, self.bob_photo.file.name]
        refresh = RefreshToken.for_user(self.alice)
        self.assertEqual(self.delete_alice().status_code, 204)

        self.assertFalse(User.objects.filter(pk=self.alice.pk).exists())
        self.assertFalse(Persona.objects.filter(pk=self.alice_main.pk).exists())
        self.assertFalse(Conversation.objects.filter(pk=self.ab.pk).exists())
        self.assertFalse(Message.objects.filter(pk=self.as_group.pk).exists())
        self.assertEqual([n for n in files if self.storage.exists(n)], [])
        self.client.force_authenticate(None)
        r = self.client.post('/api/auth/token/refresh/', {'refresh': str(refresh)})
        self.assertEqual(r.status_code, 401)

    def test_other_peoples_conversations_stay(self):
        self.delete_alice()
        self.assertTrue(User.objects.filter(pk=self.bob.pk).exists())
        self.assertEqual(list(self.bc.messages.values_list('pk', flat=True)), [self.bob_to_carol.pk])

    @override_settings(STRIPE_SECRET_KEY='sk_test_x')
    def test_cancels_subscription_first_and_keeps_account_if_that_fails(self):
        import stripe
        User.objects.filter(pk=self.alice.pk).update(stripe_subscription_id='sub_1')
        self.alice.refresh_from_db()
        with mock.patch('stripe.Subscription.cancel', side_effect=stripe.APIConnectionError('down')) as cancel:
            r = self.delete_alice()
        cancel.assert_called_once_with('sub_1')
        self.assertEqual(r.status_code, 503)
        self.assertTrue(User.objects.filter(pk=self.alice.pk).exists())
        with mock.patch('stripe.Subscription.cancel') as cancel:
            self.assertEqual(self.delete_alice().status_code, 204)
        cancel.assert_called_once_with('sub_1')
