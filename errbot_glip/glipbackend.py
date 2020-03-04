import functools
import json
import logging
import sys
from time import sleep
import os

from errbot.backends.base import (ONLINE, Identifier, Message, Person, Room,
                                  RoomError, RoomOccupant)
from errbot.core import ErrBot
from errbot.utils import rate_limited
from rc_python import PubNub, RestClient

log = logging.getLogger('errbot.backends.glip')

MESSAGE_SIZE_LIMIT = 50000
rate_limit = 3  # one message send per {rate_limit} seconds


def stripped(s):
    return ''.join([c for c in s if ord(c) > 31 or ord(c) == 9])


class Eql(object):
    def __init__(self, o):
        self.obj = o

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0


def lru_cache_ignoring_first_argument(*args, **kwargs):
    lru_decorator = functools.lru_cache(*args, **kwargs)

    def decorator(f):
        @lru_decorator
        def helper(arg1, *args, **kwargs):
            arg1 = arg1.obj
            return f(arg1, *args, **kwargs)

        @functools.wraps(f)
        def function(arg1, *args, **kwargs):
            arg1 = Eql(arg1)
            return helper(arg1, *args, **kwargs)

        return function

    return decorator


class GlipBotFilter(object):
    @staticmethod
    def filter(record):
        if record.getMessage() == "No new updates found.":
            return 0


class RoomsNotSupportedError(RoomError):
    def __init__(self, message=None):
        if message is None:
            message = ("Room operations are not supported")
        super().__init__(message)


class GlipIdentifier(Identifier):
    def __init__(self, info):
        self._info = info

    @property
    def id(self):
        return self._info['id']

    def __hash__(self):
        return self.id

    def __unicode__(self):
        return self.id

    def __str__(self):
        return str(self.id)

    def __eq__(self, other):
        return self.id == other.id


class GlipPerson(GlipIdentifier, Person):
    def __init__(self, info):
        super().__init__(info)

    @property
    def first_name(self):
        return self._info['firstName']

    @property
    def person(self):
        return self.fullname

    @property
    def last_name(self):
        return self._info['lastName']

    @property
    def email(self):
        return self._info['email']

    @property
    def location(self):
        return self._info['location']

    @property
    def fullname(self):
        if not self.first_name:
            return self.id
        fullname = self.first_name
        if self.last_name is not None:
            fullname += " " + self.last_name
        return fullname

    @property
    def nick(self):
        return self.fullname

    @property
    def client(self):
        return None

    def aclattr(self):
        return self.id


class GlipRoom(GlipIdentifier, Room):
    def __init__(self, info):
        super().__init__(info)

    @property
    def name(self):
        if self._info['type'] in ['Team', 'Everyone']:
            return self._info['name']
        else:
            return None

    @property
    def topic(self):
        if self._info['type'] in ['Team', 'Everyone']:
            return self._info['description']
        else:
            return None

    @property
    def private(self):
        """Return True if the room is a private Team or direct room"""
        if self._info['type'] == 'Direct':
            return True
        if self._info['type'] == 'Team' and not self._info['public']:
            return True

        return False

    @property
    def is_direct(self):
        return self._info['type'] == 'Direct'

    def join(self, username: str = None, password: str = None):
        raise RoomsNotSupportedError()

    def create(self):
        raise RoomsNotSupportedError()

    def leave(self, reason: str = None):
        raise RoomsNotSupportedError()

    def destroy(self):
        raise RoomsNotSupportedError()

    @property
    def joined(self):
        raise RoomsNotSupportedError()

    @property
    def exists(self):
        raise RoomsNotSupportedError()

    @property
    def occupants(self):
        # TODO Batch request persons and return array
        raise RoomsNotSupportedError()

    def invite(self, *args):
        raise RoomsNotSupportedError()


# TODO: Glip have single entity for all tipe of conversation:
# This entity have different type: Everyone, Team, Group, Direct, Personal
# ErrBot have 2 entityes: Private and Room Chat
# to mitigate this issue. There will be implemented Classes-workarounds


class GlipDirectPerson(GlipPerson):
    """
        This is class with chat id
    """

    def __init__(self, info):
        super().__init__(info)


class GlipRoomOccupant(GlipPerson, RoomOccupant):
    """
    ErrBot:
    This class represents a person inside a MUC.

    For Glip this class represents meesage creator inside chat with non-Direct
    type
    """

    def __init__(self, info, room):
        super().__init__(info)
        self._room = room

    @property
    def room(self):
        return self._room


class GlipBackend(ErrBot):
    def __init__(self, config):

        super().__init__(config)

        config.MESSAGE_SIZE_LIMIT = MESSAGE_SIZE_LIMIT
        logging.getLogger('Glip.bot').addFilter(GlipBotFilter())

        identity = config.BOT_IDENTITY

        self.client_id = os.environ.get('BOT_CLIENT_ID') or identity.get('client_id', None)
        self.client_secret = os.environ.get('BOT_CLIENT_SECRET') or identity.get('client_secret', None)
        self.server = os.environ.get('BOT_SERVER') or identity.get('server', None)
        self.bot_token = os.environ.get('BOT_TOKEN') or identity.get('bot_token', None)

        self.rc_client = RestClient(self.client_id, self.client_secret,
                                    self.server)
        # self.rc_client.debug = True
        if self.bot_token:
            self.rc_client.token = dict(access_token=self.bot_token)
        self.bot_identifier = self.bot_identity()  # Will be set in serve_once

        log.debug("RC client initialized")

        # TODO Platform observable

        # compact = config.COMPACT_OUTPUT if hasattr(config, 'COMPACT_OUTPUT') else False
        # enable_format('text', TEXT_CHRS, borders=not compact)
        # self.md_converter = text()

    def bot_identity(self):
        rc_user_id = self.rc_user('~')['id']
        return GlipPerson(self.glip_person(rc_user_id))

    # @lru_cache_ignoring_first_argument(128)
    def rc_user(self, user_id):
        '''
            RC User info lookup
            Some times we must use RC acc/exception ID
        '''
        try:
            return self.rc_client.get('/restapi/v1.0/account/~/extension/' +
                                      user_id).json()
        except Exception as e:
            log.exception('Failed to load rc user info %s', str(e))

    # @lru_cache_ignoring_first_argument(128)
    def glip_person(self, glip_user_id, room=None):
        '''
            This method use API permission: GLipInternal
            Args: Ringcentral extension ID

            returns: glip user info
        '''
        try:
            glip_info = self.rc_client.get(
                '/restapi/v1.0/glip/persons/{id}'.format(
                    id=glip_user_id)).json()
            return glip_info
        # return GlipPerson({
        #     'id': glip_info['id'],
        #     'email': glip_info['email'],
        #     'lastName': glip_info['lastName'],
        #     'firstName': glip_info['firstName']
        # })
        except Exception as e:
            log.exception('Failed to load Glip user info %s', str(e))

    # @lru_cache_ignoring_first_argument(128)
    def glip_person_1(self, glip_user_id, room=None):
        '''
            This method use API permission: GLipInternal
            Args: Ringcentral extension ID

            returns: glip user info
        '''
        try:
            glip_info = self.rc_client.get(
                '/restapi/v1.0/glip/persons/{id}'.format(
                    id=glip_user_id)).json()
            return GlipRoomOccupant(
                {
                    'id': glip_info['id'],
                    'email': glip_info['email'],
                    'lastName': glip_info['lastName'],
                    'firstName': glip_info['firstName']
                }, room)
        except Exception as e:
            log.exception('Failed to load Glip user info %s', str(e))

    # @lru_cache_ignoring_first_argument(128)
    def glip_person_lookup(self, search_string):
        '''
            This method use API permission: GLipInternal
            Args: SearchString (full or partial name/surname/email)

            returns: glip person ID
        '''
        try:
            glip_info = self.rc_client.get(
                '/restapi/v1.0/glip/lookup/contacts',
                params={
                    'searchString': search_string
                }).json()
            return GlipPerson({
                'id': glip_info['id'],
                'email': glip_info['email'],
                'lastName': glip_info['lastName'],
                'firstName': glip_info['firstName']
            })
        except Exception as e:
            log.exception('Failed to lookup Glip user info %s', str(e))

    # @lru_cache_ignoring_first_argument(128)
    def get_chat(self, _id):
        try:
            chat_info = self.rc_client.get('/restapi/v1.0/glip/chats/' +
                                           _id).json()
            log.debug(chat_info)
            return GlipRoom(chat_info)
        except Exception as e:
            log.exception('Failed to load group %s', str(e))

    def create_conversation(self, person: Identifier) -> GlipRoom:
        '''
            In case bot have to start Direct conversation with pesond from Grop
            chat.
            Request Glip to create/find conversation with Given GlipPerson
        '''
        data = {'members': [{'id': person.id}]}
        try:
            chat_info = self.rc_client.post('/restapi/v1.0/glip/conversations',
                                            json=data).json()
            return GlipRoom(chat_info)
        except Exception as e:
            log.exception(
                'Failed to fetch private conversation chat with Person: {}'.
                format(person.fullname))

    def serve_once(self):
        log.info("Initializing connection")
        s = None

        try:
            # self.authorize()

            # TODO: dynamic events list
            events = ['/restapi/v1.0/glip/posts']
            self.pubnub = PubNub(self.rc_client, events, self._handle_message)

            self.pubnub.subscribe()
            self.reset_reconnection_count()
            self.connect_callback()
            log.info('Connected')

            while True:
                sleep(0.1)

        except KeyboardInterrupt:
            log.info("Interrupt received, shutting down")
            if s:
                s.revoke()
            return True
        except:
            log.exception("Error reading from Glip updates stream")
        finally:
            log.debug("Triggering disconnect callback")
            self.disconnect_callback()

        return False

    def _get_message(self, id):
        return self.rc_client.get('/restapi/v1.0/glip/posts' + id).json()

    def _handle_message(self, message):
        try:
            # RC SDK Subscribtion send raw message.
            post = json.loads(stripped(message))['body']

            # Subscribtion events not described. Can't find any other types. WE
            # use only PostAdded
            if post['eventType'] != 'PostAdded':
                return

            room = self.get_chat(post['groupId'])
            log.debug(room.is_direct)
            if room.is_direct:
                sender = GlipPerson(self.glip_person(post['creatorId']))
                to = GlipDirectPerson({'id': room.id})
            else:
                sender = GlipRoomOccupant(self.glip_person(post['creatorId']),
                                          room)
                to = room

            message_instance = Message(body=post['text'], frm=sender, to=to)

            # self.callback_room_joined(self)  # TODO Implement
            self.callback_message(message_instance)

        except Exception as e:
            log.exception('Failed to handle message %s\n%s', str(e), message)

    @rate_limited(rate_limit)  # <---- Rate Limit
    def send_message(self, mess):
        super().send_message(mess)

        log.debug('Message: {frm}/{to}'.format(frm=mess.frm, to=mess.to))
        sent_message = self.rc_client.post(
            'restapi/v1.0/glip/chats/{chatid}/posts'.format(chatid=mess.to),
            json={
                'text': mess.body
            }).json()

    def send_reply(self, mess, text):

        mess.body = text
        self.send_message(mess)

    def change_presence(self, status: str = ONLINE, message: str = '') -> None:
        pass

    def build_identifier(self, txtrep):
        """
        Convert a textual representation into a :class:`~GlipPerson` or :class:`~GlipRoom`.
        """
        log.debug("building an identifier from %s" % txtrep)
        return GlipPerson({'id': txtrep})  # Can also be Room

    def build_reply(self, msg, text=None, private=False, threaded=False):
        # TODO: reply to private MUST BE refactored!
        log.debug('TEST1: {}, {}'.format(msg.frm.id, msg.to.id))
        response = self.build_message(text)
        if private:
            reply_to = self.create_conversation(msg.frm)
        else:
            reply_to = msg.to
        response.frm = self.bot_identifier
        response.to = reply_to
        log.debug('TEST1: {}, {}'.format(response.frm.id, response.to.id))
        return response

    @property
    def mode(self):
        return 'Glip'

    def query_room(self, room):
        """
        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def rooms(self):
        """
        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def prefix_groupchat_reply(self, message, identifier):
        super().prefix_groupchat_reply(message, identifier)
        message.body = '@{0}: {1}'.format(identifier.nick, message.body)
