from datetime import datetime
import collections

from django.db import transaction
from django.db.utils import IntegrityError

from mygpo.subscriptions.signals import subscription_changed
from mygpo.utils import to_maxlength

import logging
logger = logging.getLogger(__name__)

# we cannot import models in __init__.py, because it gets loaded while all
# apps are loaded; ideally all these methods would be moved into a different
# (non-__init__) module


@transaction.atomic
def subscribe(podcast, user, client, ref_url=None):
    """ subscribes user to the current podcast on one client

    Takes syned devices into account. """
    ref_url = ref_url or podcast.url
    now = datetime.utcnow()
    clients = _affected_clients(client)

    # fully execute subscriptions, before firing events
    changed = list(_perform_subscribe(podcast, user, clients, now, ref_url))
    _fire_events(podcast, user, changed, True)


@transaction.atomic
def unsubscribe(podcast, user, client):
    """ unsubscribes user from the current podcast on one client

    Takes syned devices into account. """
    now = datetime.utcnow()
    clients = _affected_clients(client)

    # fully execute unsubscriptions, before firing events
    # otherwise the first fired event might revert the unsubscribe
    changed = list(_perform_unsubscribe(podcast, user, clients, now))
    _fire_events(podcast, user, changed, False)


@transaction.atomic
def subscribe_all(podcast, user, ref_url=None):
    """ subscribes user to the current podcast on all clients """
    ref_url = ref_url or podcast.url
    now = datetime.utcnow()
    clients = user.client_set.all()

    # fully execute subscriptions, before firing events
    changed = list(_perform_subscribe(podcast, user, clients, now, ref_url))
    _fire_events(podcast, user, changed, True)


@transaction.atomic
def unsubscribe_all(podcast, user):
    """ unsubscribes user from the current podcast on all clients """
    now = datetime.utcnow()
    clients = user.client_set.filter(subscription__podcast=podcast)

    # fully execute subscriptions, before firing events
    changed = list(_perform_unsubscribe(podcast, user, clients, now))
    _fire_events(podcast, user, changed, False)


def _perform_subscribe(podcast, user, clients, timestamp, ref_url):
    """ Subscribes to a podcast on multiple clients

    Yields the clients on which a subscription was added, ie not those where
    the subscription already existed. """

    from mygpo.subscriptions.models import Subscription

    for client in clients:
        try:
            with transaction.atomic():
                subscription = Subscription.objects.create(
                    user=user,
                    client=client,
                    podcast=podcast,
                    ref_url=to_maxlength(Subscription, 'ref_url', ref_url),
                    created=timestamp,
                    modified=timestamp,
                )

        except IntegrityError as ie:
            msg = str(ie)
            if 'Key (user_id, client_id, podcast_id)' in msg:
                # Subscription already exists -- skip
                continue

            else:
                # unknown error
                raise

        logger.info('{user} subscribed to {podcast} on {client}'.format(
            user=user, podcast=podcast, client=client))

        from mygpo.history.models import HistoryEntry
        HistoryEntry.objects.create(
            timestamp=timestamp,
            podcast=podcast,
            user=user,
            client=client,
            action=HistoryEntry.SUBSCRIBE,
        )

        yield client


def _perform_unsubscribe(podcast, user, clients, timestamp):
    """ Unsubscribes from a podcast on multiple clients

    Yields the clients on which a subscription was removed, ie not those where
    the podcast was not subscribed. """

    from mygpo.subscriptions.models import Subscription
    for client in clients:

        try:
            subscription = Subscription.objects.get(
                user=user,
                client=client,
                podcast=podcast,
            )
        except Subscription.DoesNotExist:
            continue

        subscription.delete()

        logger.info('{user} unsubscribed from {podcast} on {client}'.format(
            user=user, podcast=podcast, client=client))

        from mygpo.history.models import HistoryEntry
        HistoryEntry.objects.create(
            timestamp=timestamp,
            podcast=podcast,
            user=user,
            client=client,
            action=HistoryEntry.UNSUBSCRIBE,
        )

        yield client


def get_subscribe_targets(podcast, user):
    """ Clients / SyncGroup on which the podcast can be subscribed

    This excludes all devices/syncgroups on which the podcast is already
    subscribed """

    from mygpo.users.models import Client
    clients = Client.objects.filter(user=user)\
                            .exclude(subscription__podcast=podcast,
                                     subscription__user=user)\
                            .select_related('sync_group')

    targets = set()
    for client in clients:
        if client.sync_group:
            targets.add(client.sync_group)
        else:
            targets.add(client)

    return targets


def get_subscribed_podcasts(user, only_public=False):
    """ Returns all subscribed podcasts for the user

    The attribute "url" contains the URL that was used when subscribing to
    the podcast """

    from mygpo.usersettings.models import UserSettings
    from mygpo.subscriptions.models import SubscribedPodcast, Subscription
    subscriptions = Subscription.objects.filter(user=user)\
                                        .order_by('podcast')\
                                        .distinct('podcast')\
                                        .select_related('podcast')
    private = UserSettings.objects.get_private_podcasts(user)

    podcasts = []
    for subscription in subscriptions:
        podcast = subscription.podcast
        public = subscription.podcast not in private

        # check if we want to include this podcast
        if only_public and not public:
            continue

        subpodcast = SubscribedPodcast(podcast, public, subscription.ref_url)
        podcasts.append(subpodcast)

    return podcasts


def get_subscription_history(user, client=None, since=None, until=None,
                             public_only=False):
    """ Returns chronologically ordered subscription history entries

    Setting device_id restricts the actions to a certain device
    """

    from mygpo.usersettings.models import UserSettings
    from mygpo.history.models import SUBSCRIPTION_ACTIONS, HistoryEntry
    logger.info('Subscription History for {user}'.format(user=user.username))
    history = HistoryEntry.objects.filter(user=user)\
                                  .filter(action__in=SUBSCRIPTION_ACTIONS)\
                                  .order_by('timestamp')

    if client:
        logger.info(u'... client {client_uid}'.format(client_uid=client.uid))
        history = history.filter(client=client)

    if since:
        logger.info('... since {since}'.format(since=since))
        history = history.filter(timestamp__gt=since)

    if until:
        logger.info('... until {until}'.format(until=until))
        history = history.filter(timestamp__lte=until)

    if public_only:
        logger.info('... only public')
        private = UserSettings.objects.get_private_podcasts(user)
        history = history.exclude(podcast__in=private)

    return history


def get_subscription_change_history(history):
    """ Actions that added/removed podcasts from the subscription list

    Returns an iterator of all subscription actions that either
     * added subscribed a podcast that hasn't been subscribed directly
       before the action (but could have been subscribed) earlier
     * removed a subscription of the podcast is not longer subscribed
       after the action

    This method assumes, that no subscriptions exist at the beginning of
    ``history``.
    """

    from mygpo.history.models import HistoryEntry
    subscriptions = collections.defaultdict(int)

    for entry in history:
        if entry.action == HistoryEntry.SUBSCRIBE:
            subscriptions[entry.podcast] += 1

            # a new subscription has been added
            if subscriptions[entry.podcast] == 1:
                yield entry

        elif entry.action == HistoryEntry.UNSUBSCRIBE:
            subscriptions[entry.podcast] -= 1

            # the last subscription has been removed
            if subscriptions[entry.podcast] == 0:
                yield entry


def subscription_diff(history):
    """ Calculates a diff of subscriptions based on a history (sub/unsub) """

    from mygpo.history.models import HistoryEntry
    subscriptions = collections.defaultdict(int)

    for entry in history:
        if entry.action == HistoryEntry.SUBSCRIBE:
            subscriptions[entry.podcast] += 1

        elif entry.action == HistoryEntry.UNSUBSCRIBE:
            subscriptions[entry.podcast] -= 1

    subscribe = [podcast for (podcast, value) in
                 subscriptions.items() if value > 0]
    unsubscribe = [podcast for (podcast, value) in
                   subscriptions.items() if value < 0]

    return subscribe, unsubscribe


def _affected_clients(client):
    """ the clients that are affected if the given one is to be changed """
    if client.sync_group:
        # if the client is synced, all are affected
        return client.sync_group.client_set.all()

    else:
        # if its not synced, only the client is affected
        return [client]


def _fire_events(podcast, user, clients, subscribed):
    """ Fire the events for subscription / unsubscription """
    for client in clients:
        subscription_changed.send(sender=podcast.__class__, instance=podcast,
                                  user=user, client=client,
                                  subscribed=subscribed)
