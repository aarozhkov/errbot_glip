import functools
import json
import logging
import os
from time import sleep
from typing import List

from errbot.backends.base import (ONLINE, Identifier, Message, Person, Room,
                                  RoomError, RoomOccupant)
from errbot.core import ErrBot
from errbot.utils import rate_limited
from rc_python import PubNub, RestClient

# TODO: Some times we got RC api erros. And fill Identifiers with None.
#       Must mitigate this behavior

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
    def __init__(self, info, chatid=None):
        super().__init__(info)
        self._room = chatid

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

    @property
    def chat(self):
        '''
            For each bot <-> person Direct message, Glip create separate
            conversation ID.
        '''
        return self._room or None

    @chat.setter
    def chat(self, room):
        self._room = room

    @property
    def aclattr(self):
        return self.id


class GlipRoom(GlipIdentifier, Room):
    '''
        ErrBot class replresents Group conversations room/chat
        Glip chat entity represents all kind of cinversations. Have several
        types:
            - Direct
            - Personal
            - Conversation
            - Group
            - Team
            - Everyone
    '''

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
        '''
            Glip only one antity for conversations: Chat.
            Chat have several types.
            This property devide Glip chat entity on 2 types: direct
            conversation and group conversation
        '''
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


class GlipRoomOccupant(GlipPerson, RoomOccupant):
    """
    ErrBot: class represents a person inside a room.
    Glip: class represents message creator inside chat
    with non-Direct type.
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

        # TODO: this line cause errbot config manadatory set BOT_IDENTITY
        identity = config.BOT_IDENTITY

        self.client_id = os.environ.get(
            'BOT_CLIENT_ID') or identity['client_id']
        self.client_secret = os.environ.get(
            'BOT_CLIENT_SECRET') or identity['client_secret']
        self.server = os.environ.get('BOT_SERVER') or identity['server']
        self.bot_token = os.environ.get('BOT_TOKEN') or identity['bot_token']

        self.rc_client = RestClient(self.client_id, self.client_secret,
                                    self.server)
        # log.debug(self.client_id, self.server, self.bot_token)
        self.rc_client.debug = True
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

    @lru_cache_ignoring_first_argument(128)
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

    @lru_cache_ignoring_first_argument(128)
    def glip_person(self, glip_user_id):
        '''
            Args: Ringcentral extension ID

            returns: glip user info
        '''
        try:
            glip_info = self.rc_client.get(
                '/restapi/v1.0/glip/persons/{id}'.format(
                    id=glip_user_id)).json()
            return glip_info
        except Exception as e:
            log.exception('Failed to load Glip user info %s', str(e))
        return None

    @lru_cache_ignoring_first_argument(128)
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

    def create_conversation(self, person: Identifier) -> GlipRoom:
        '''
            In case bot have to start Direct conversation with Person.
            Request Glip to create/find conversation with Given GlipPerson

        '''
        data = {'members': [{'id': person.id}]}
        try:
            chat_info = self.rc_client.post('/restapi/v1.0/glip/conversations',
                                            json=data).json()
            return GlipRoom(chat_info)
        except Exception as e:
            log.exception(
                'Failed to fetch private conversation chat with Person: %s',
                person.fullname)

    def parse_mentions(self, mentions: List) -> List:
        '''
        Got text representation from Glip Message and return Object list:

        :param mentions:
            Glip post mentions array:
                mentions: [
                    {
                        id: string - Internal identifier of user
                        type: string Enum: Person, Team, File, Link, Event, Task, Note, Card
                        name: Name of User
                    }
                ]

        TODO: implement parser for all mention types.

        :returns List of Identifiers:
            Return GlipPerson for all metioned Ids
        '''
        result = list()
        for item in mentions:
            if item['type'] == 'Person' and not item['id'].startswith('glip-'):
                # TODO handle glip only users.
                result.append(self.build_identifier(item['id']))
        log.debug(result)
        return result

    def serve_once(self):
        log.info("Initializing connection")

        try:
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
            self.pubnub.revoke()
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
            post = json.loads(stripped(message))['body']

            # Subscribtion events not described. Can't find any other types. WE
            # use only PostAdded
            if (post['eventType'] != 'PostAdded'
                    or self.bot_identifier.id == post['creatorId']):
                return

            room = self.query_room(post['groupId'])
            '''
            If Glip chat type is Direct. We must answer to same person in a same
            room. Person_from = person_to.

            If Glip chat type != Direct. We must answer this person in this room.
            Person become RoomOccupant for bot. 
            '''
            log.debug(post['text'])
            if room.is_direct:
                person_from = GlipPerson(self.glip_person(post['creatorId']))
                person_from.chat = room
                person_to = person_from
            else:
                person_from = GlipRoomOccupant(
                    self.glip_person(post['creatorId']), room)
                person_to = room

            message_instance = Message(body=post['text'],
                                       frm=person_from,
                                       to=person_to)
            # If got list of mentions. It is not a command and we must handle it
            # separatly.
            # TODO implement ability to use Bot mention as command prefix in R
            if post['mentions'] and not room.is_direct:
                mentions = self.parse_mentions(post['mentions'])
                self.callback_mention(message_instance, mentions)
            else:
                self.callback_message(message_instance)
            # self.callback_room_joined(self)  # TODO Implement

        except Exception as e:
            log.exception('Failed to handle message %s\n%s', str(e), message)

    @rate_limited(rate_limit)  # <---- Rate Limit
    def send_message(self, msg):
        super().send_message(msg)
        log.debug('Message: {frm}/{to}'.format(frm=msg.frm, to=msg.to))
        if isinstance(msg.to, GlipPerson):
            if not msg.to.chat:
                target = self.create_conversation(msg.to)
            else:
                target = msg.to.chat
        elif msg.to.id:  # This is room and not empty
            target = msg.to.id
        try:
            send_message = self.rc_client.post(
                'restapi/v1.0/glip/chats/{chatid}/posts'.format(chatid=target),
                json={
                    'text': msg.body
                }).json()
            log.debug(send_message)
        except Exception as e:
            log.exception('Got issue during send_message. %s' % e)

    def send_reply(self, mess, text):

        mess.body = text
        self.send_message(mess)

    def change_presence(self, status: str = ONLINE, message: str = '') -> None:
        pass

    def build_identifier(self, txtrep):
        """
        Convert a textual(Glip ID) representation into a :class:`~GlipPerson` or :class:`~GlipRoom`.
        Should 

        :param txtrep:
            Glip person ID

        :returns:
            GlipPerson without chat property
        """
        log.debug("building an identifier from %s" % txtrep)
        person_info = self.glip_person(txtrep)
        if person_info:
            return GlipPerson(person_info)
        return None

    def build_reply(self, msg, text=None, private=False, threaded=False):
        # TODO: reply to private MUST BE refactored!
        response = self.build_message(text)
        if private:
            # TODO implement ability to use Bot mention as command prefix in R
            reply_to = self.create_conversation(msg.frm)
        else:
            reply_to = msg.to
        response.frm = self.bot_identifier
        response.to = reply_to
        return response

    @property
    def mode(self):
        return 'Glip'

    @lru_cache_ignoring_first_argument(128)
    def query_room(self, room: str) -> GlipRoom:
        """
        Get Glip chat info by Chat id:
        :params room:
            Glip chat ID
        """
        try:
            chat_info = self.rc_client.get('/restapi/v1.0/glip/chats/' +
                                           room).json()
            log.debug(chat_info)
            return GlipRoom(chat_info)
        except Exception as e:
            log.exception('Failed to load group %s', str(e))

    def rooms(self):
        """
        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def prefix_groupchat_reply(self, message, identifier):
        super().prefix_groupchat_reply(message, identifier)
        message.body = '@{0}: {1}'.format(identifier.nick, message.body)
