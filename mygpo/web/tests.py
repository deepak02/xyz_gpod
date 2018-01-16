import unittest
import doctest
import uuid

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

from mygpo.podcasts.models import Podcast, Episode, Slug
import mygpo.web.utils
from mygpo.test import create_auth_string, anon_request


class SimpleWebTests(TestCase):

    @classmethod
    def setUpClass(self):
        User = get_user_model()
        self.user = User(username='web-test', email='web-test@example.com')
        self.user.set_password('pwd')
        self.user.save()

        self.auth_string = create_auth_string('test', 'pwd')

    @classmethod
    def tearDownClass(self):
        self.user.delete()

    def test_access_parameterless_pages(self):
        pages = [
            'history',
            'suggestions',
            'tags',
            'subscriptions',
            'subscriptions-opml',
            'favorites',
            'account',
            'privacy',
            'delete-account',
            'share',
            'toplist',
            'episode-toplist',
            'devices',
            'device-create',
            'login',
            'logout',
            'home']

        self.access_pages(pages, [], True)

    def test_access_podcast_pages(self):
        pages = ['podcast', ]

    def access_pages(self, pages, args, login):
        if login:
            self.client.post('/login/', dict(
                login_username=self.user.username, pwd='pwd'))

        for page in pages:
            response = self.client.get(reverse(page, args=args), follow=True)
            self.assertEqual(response.status_code, 200)


class PodcastPageTests(TestCase):
    """ Test the podcast page """

    def setUp(self):
        # create a podcast and some episodes
        podcast = Podcast.objects.create(id=uuid.uuid1(),
                                         title='My Podcast',
                                         max_episode_order=1,
                                         )
        for n in range(20):
            episode = Episode.objects.get_or_create_for_url(
                podcast,
                'http://www.example.com/episode%d.mp3' % (n, ),
            ).object

            # we only need (the last) one
            self.episode_slug = Slug.objects.create(content_object=episode,
                                                    order=0,
                                                    scope=podcast.as_scope,
                                                    slug=str(n),
                                                    )

        self.podcast_slug = Slug.objects.create(content_object=podcast,
                                                order=n, scope=podcast.scope,
                                                slug='podcast')

    def test_podcast_queries(self):
        """ Test that the expected number of queries is executed """
        url = reverse('podcast-slug', args=(self.podcast_slug.slug, ))
        # the number of queries must be independent of the number of episodes

        with self.assertNumQueries(5):
            anon_request(url)

    def test_episode_queries(self):
        """ Test that the expected number of queries is executed """
        url = reverse('episode-slug', args=(self.podcast_slug.slug,
                                            self.episode_slug.slug))

        with self.assertNumQueries(5):
            anon_request(url)
