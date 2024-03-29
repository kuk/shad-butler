
import re
import logging
from os import getenv
from dataclasses import dataclass
from datetime import (
    date as Date,
    datetime as Datetime,
)
from contextlib import AsyncExitStack

from aiogram import (
    Bot,
    Dispatcher,
    executor
)
from aiogram.types import (
    ChatType,
    ChatMemberStatus,
    BotCommand,
)
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler
from aiogram.utils.exceptions import (
    BadRequest,
    MessageToForwardNotFound,
    MessageIdInvalid,
)

import aiobotocore.session


#######
#
#   SECRETS
#
######

# Ask @alexkuk for .env


BOT_TOKEN = getenv('BOT_TOKEN')

AWS_KEY_ID = getenv('AWS_KEY_ID')
AWS_KEY = getenv('AWS_KEY')

DYNAMO_ENDPOINT = getenv('DYNAMO_ENDPOINT')

CHAT_ID = int(getenv('CHAT_ID'))


######
#
#   LOGGER
#
#######


LOG_LEVEL = getenv('LOG_LEVEL', logging.INFO)

log = logging.getLogger(__name__)
log.setLevel(LOG_LEVEL)
log.addHandler(logging.StreamHandler())


#######
#
#   OBJ
#
####


######
#  POST
######


CONTACTS = 'contacts'
CHATS = 'chats'
EVENT = 'event'
EVENTS_ARCHIVE = 'events_archive'
LECTURES_ARCHIVE = 'lectures_archive'
WHOIS_HOWTO = 'whois_howto'


@dataclass
class Post:
    message_id: int
    type: str
    event_date: Date = None


def find_posts(posts, message_id=None, type=None):
    for post in posts:
        if (
                message_id and post.message_id == message_id
                or type and post.type == type
        ):
            yield post


def find_post(posts, **kwargs):
    for post in find_posts(posts, **kwargs):
        return post


######
#   POST FOOTER
####

# #contacts
# #chats
# #event 2022-07-09


@dataclass
class PostFooter:
    type: str
    event_date: Date = None


EVENT_POST_FOOTER_PATTERN = re.compile(rf'''
\#{EVENT}
\s+
(\d\d\d\d-\d\d-\d\d)
''', re.X)

NAV_POST_FOOTER_PATTERN = re.compile(rf'''
\#(
{CHATS}|{CONTACTS}
|{EVENTS_ARCHIVE}|{LECTURES_ARCHIVE}
|{WHOIS_HOWTO}
)''', re.X)


def parse_post_footer(text):
    match = EVENT_POST_FOOTER_PATTERN.search(text)
    if match:
        event_date, = match.groups()
        event_date = Date.fromisoformat(event_date)
        return PostFooter(EVENT, event_date)

    match = NAV_POST_FOOTER_PATTERN.search(text)
    if match:
        type, = match.groups()
        return PostFooter(type)


######
#
#  DYNAMO
#
######


######
#   MANAGER
######


async def dynamo_client():
    session = aiobotocore.session.get_session()
    manager = session.create_client(
        'dynamodb',

        # Always ru-central1 for YC
        # https://cloud.yandex.ru/docs/ydb/docapi/tools/aws-setup
        region_name='ru-central1',

        endpoint_url=DYNAMO_ENDPOINT,
        aws_access_key_id=AWS_KEY_ID,
        aws_secret_access_key=AWS_KEY,
    )

    # https://github.com/aio-libs/aiobotocore/discussions/955
    exit_stack = AsyncExitStack()
    client = await exit_stack.enter_async_context(manager)
    return exit_stack, client


######
#  OPS
#####


S = 'S'
N = 'N'


async def dynamo_scan(client, table):
    response = await client.scan(
        TableName=table
    )
    return response['Items']


async def dynamo_put(client, table, item):
    await client.put_item(
        TableName=table,
        Item=item
    )


async def dynamo_get(client, table, key_name, key_type, key_value):
    response = await client.get_item(
        TableName=table,
        Key={
            key_name: {
                key_type: str(key_value)
            }
        }
    )
    return response.get('Item')


async def dynamo_delete(client, table, key_name, key_type, key_value):
    await client.delete_item(
        TableName=table,
        Key={
            key_name: {
                key_type: str(key_value)
            }
        }
    )


######
#   DE/SERIALIZE
####


def dynamo_parse_post(item):
    message_id = int(item['message_id']['N'])
    type = item['type']['S']

    event_date = None
    if 'event_date' in item:
        event_date = Date.fromisoformat(item['event_date']['S'])

    return Post(message_id, type, event_date)


def dynamo_format_post(post):
    item = {
        'type': {
            'S': post.type
        },
        'message_id': {
            'N': str(post.message_id)
        },
    }
    if post.event_date:
        item['event_date'] = {
            'S': post.event_date.isoformat()
        }
    return item


######
#   READ/WRITE
######


POSTS_TABLE = 'posts'
MESSAGE_ID_KEY = 'message_id'


async def read_posts(db):
    items = await dynamo_scan(db.client, POSTS_TABLE)
    return [dynamo_parse_post(_) for _ in items]


async def put_post(db, post):
    item = dynamo_format_post(post)
    await dynamo_put(db.client, POSTS_TABLE, item)


async def delete_post(db, message_id):
    await dynamo_delete(
        db.client, POSTS_TABLE,
        MESSAGE_ID_KEY, N, message_id
    )


######
#  DB
#######


class DB:
    def __init__(self):
        self.exit_stack = None
        self.client = None

    async def connect(self):
        self.exit_stack, self.client = await dynamo_client()

    async def close(self):
        await self.exit_stack.aclose()


DB.read_posts = read_posts
DB.put_post = put_post
DB.delete_post = delete_post


#######
#
#   HANDLERS
#
####


START_COMMAND = 'start'
FUTURE_EVENTS_COMMAND = 'future_events'
EVENTS_ARCHIVE_COMMAND = EVENTS_ARCHIVE
LECTURES_ARCHIVE_COMMAND = LECTURES_ARCHIVE
CHATS_COMMAND = CHATS
CONTACTS_COMMAND = CONTACTS
WHOIS_HOWTO_COMMAND = WHOIS_HOWTO

BOT_COMMANDS = [
    BotCommand(FUTURE_EVENTS_COMMAND, 'ближайшие эвенты'),
    BotCommand(EVENTS_ARCHIVE_COMMAND, 'архив эвентов'),
    BotCommand(LECTURES_ARCHIVE_COMMAND, 'архив лекций'),
    BotCommand(CHATS, 'тематические чаты'),
    BotCommand(CONTACTS_COMMAND, 'контакты кураторов'),
    BotCommand(WHOIS_HOWTO_COMMAND, 'мануал по #whois'),
    BotCommand(START_COMMAND, 'интро'),
]

START_TEXT = f'''Привет!
Это бот-дворецкий ШАД. Я умею напоминать какие мероприятия \
будут в скором времени, рассказывать про контакты кураторов, \
показывать локальные чаты. В скором буду уметь еще много чего \
интересного, но не все сразу :)

Если словил баг, то пиши моему разработчику @alexkuk
Если есть идеи как меня развивать, то пиши @tinicheva

Команды
/{FUTURE_EVENTS_COMMAND} — ближайшие эвенты
/{EVENTS_ARCHIVE_COMMAND} — архив эвентов
/{LECTURES_ARCHIVE_COMMAND} — архив лекций
/{CHATS_COMMAND} — тематические чаты
/{CONTACTS_COMMAND} — контакты кураторов
/{WHOIS_HOWTO_COMMAND} — мануал по #whois

Команды доступны снизу по кнопке "Меню"'''

NO_FUTURE_EVENTS_TEXT = (
    'В ближайшее время нет эвентов. '
    f'Список прошедших — /{EVENTS_ARCHIVE_COMMAND}'
)

MISSING_POSTS_TEXT = 'Не нашел постов с тегом #{type}.'
MISSING_FORWARD_TEXT = (
    'Хотел переслать пост {url}, но он исчез. '
    'Удалил из своей базы.'
)


######
#  START
######


async def handle_start_command(context, message):
    await message.answer(text=START_TEXT)
    await context.bot.set_my_commands(
        commands=BOT_COMMANDS
    )


######
#   OTHER
#####


async def handle_other(context, message):
    await message.answer(text=START_TEXT)


######
#   FORWARD
####


def message_url(chat_id, message_id):
    # -1001627609834, 21 -> https://t.me/c/1627609834/21
    # https://github.com/aiogram/aiogram/blob/master/aiogram/types/chat.py#L79

    chat_id = -1_000_000_000_000 - chat_id
    return f'https://t.me/c/{chat_id}/{message_id}'


async def forward_post(context, message, post):
    # Telegram Bot API missing delete event
    # https://github.com/tdlib/telegram-bot-api/issues/286#issuecomment-1154020149
    # Remove after forward fails. Rare in practice

    try:
        await context.bot.forward_message(
            chat_id=message.chat.id,
            from_chat_id=CHAT_ID,
            message_id=post.message_id
        )

    # No sure why 2 types of exceptions
    # Clear history, empty chat -> MessageIdInvalid
    # Remove single message -> MessageToForwardNotFound
    except (MessageToForwardNotFound, MessageIdInvalid):
        await context.db.delete_post(post.message_id)

        url = message_url(
            chat_id=CHAT_ID,
            message_id=post.message_id
        )
        text = MISSING_FORWARD_TEXT.format(url=url)
        await message.answer(text=text)


######
#   FUTURE EVENTS
#######


def select_future(posts, cap=3):
    today = Datetime.now().date()
    posts = [
        _ for _ in posts
        if _.event_date >= today
    ]
    posts.sort(key=lambda _: _.event_date)
    return posts[:cap]


async def handle_future_events_command(context, message):
    posts = await context.db.read_posts()
    posts = list(find_posts(posts, type=EVENT))
    if not posts:
        text = MISSING_POSTS_TEXT.format(type=EVENT)
        await message.answer(text=text)
        return

    posts = select_future(posts)
    if not posts:
        await message.answer(text=NO_FUTURE_EVENTS_TEXT)
        return

    for post in posts:
        await forward_post(context, message, post)


#######
#  NAV
####


async def handle_nav_command(context, message, type):
    posts = await context.db.read_posts()
    post = find_post(posts, type=type)
    if post:
        await forward_post(context, message, post)
    else:
        text = MISSING_POSTS_TEXT.format(type=type)
        await message.answer(text=text)


async def handle_chats_command(context, message):
    await handle_nav_command(context, message, CHATS)


async def handle_contacts_command(context, message):
    await handle_nav_command(context, message, CONTACTS)


async def handle_whois_howto_command(context, message):
    await handle_nav_command(context, message, WHOIS_HOWTO)


async def handle_events_archive_command(context, message):
    await handle_nav_command(context, message, EVENTS_ARCHIVE)


async def handle_lectures_archive_command(context, message):
    await handle_nav_command(context, message, LECTURES_ARCHIVE)


####
#  CHAT
#####


async def new_post(context, message, footer):
    post = Post(
        message.message_id, footer.type,
        footer.event_date
    )
    await context.db.put_post(post)


async def handle_chat_new_message(context, message):
    footer = parse_post_footer(message.text)
    if footer:
        await new_post(context, message, footer)


async def handle_chat_edited_message(context, message):
    footer = parse_post_footer(message.text)
    if footer:
        # Added footer to existing message
        await new_post(context, message, footer)
        return

    posts = await context.db.read_posts()
    post = find_post(posts, message_id=message.message_id)
    if post:
        # Removed footer from post
        await context.db.delete_post(post.message_id)


#####
#  SETUP
#####


def setup_handlers(context):
    context.dispatcher.register_message_handler(
        context.handle_start_command,
        chat_type=ChatType.PRIVATE,
        commands=START_COMMAND,
    )

    context.dispatcher.register_message_handler(
        context.handle_future_events_command,
        chat_type=ChatType.PRIVATE,
        commands=FUTURE_EVENTS_COMMAND,
    )
    context.dispatcher.register_message_handler(
        context.handle_chats_command,
        chat_type=ChatType.PRIVATE,
        commands=CHATS_COMMAND,
    )
    context.dispatcher.register_message_handler(
        context.handle_contacts_command,
        chat_type=ChatType.PRIVATE,
        commands=CONTACTS_COMMAND,
    )
    context.dispatcher.register_message_handler(
        context.handle_whois_howto_command,
        chat_type=ChatType.PRIVATE,
        commands=WHOIS_HOWTO_COMMAND,
    )
    context.dispatcher.register_message_handler(
        context.handle_events_archive_command,
        chat_type=ChatType.PRIVATE,
        commands=EVENTS_ARCHIVE_COMMAND,
    )
    context.dispatcher.register_message_handler(
        context.handle_lectures_archive_command,
        chat_type=ChatType.PRIVATE,
        commands=LECTURES_ARCHIVE_COMMAND,
    )

    context.dispatcher.register_message_handler(
        context.handle_other,
        chat_type=ChatType.PRIVATE,
    )

    context.dispatcher.register_message_handler(
        context.handle_chat_new_message,
        chat_id=CHAT_ID,
    )
    context.dispatcher.register_edited_message_handler(
        context.handle_chat_edited_message,
        chat_id=CHAT_ID,
    )


######
#
#   MIDDLEWARE
#
######


#######
#  LOGGING
######

# Do not log messages from superchat
# Do not log usernames
# YC Logging keeps only last 3 days of logs


class LoggingMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message, data):
        if message.chat.type == ChatType.PRIVATE:
            log.info(f'From id: {message.from_id} text: {message.text!r}')


#######
#  CHAT MEMBER
######


NOT_CHAT_MEMBER_TEXT = (
    'Не нашел тебя в чате выпускников ШАДа. '
    'Напиши, пожалуйста, кураторам. '
    'Бот отвечает только тем кто в чатике.'
)


class UserNotFound(BadRequest):
    match = 'user not found'


async def is_chat_member(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id
        )
    except UserNotFound:
        return False

    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return False

    return True


class ChatMemberMiddleware(BaseMiddleware):
    def __init__(self, context):
        self.context = context
        BaseMiddleware.__init__(self)

    # Only register_message_handler for private chats in
    # setup_handlers

    async def on_pre_process_message(self, message, data):
        if message.chat.type == ChatType.PRIVATE:
            if await is_chat_member(
                self.context.bot,
                chat_id=CHAT_ID,
                user_id=message.from_user.id
            ):
                return
            else:
                await message.answer(text=NOT_CHAT_MEMBER_TEXT)
                raise CancelHandler

        else:
            if message.chat.id == CHAT_ID:
                return
            else:
                raise CancelHandler


#######
#   SETUP
#########


def setup_middlewares(context):
    middlewares = [
        LoggingMiddleware(),
        ChatMemberMiddleware(context),
    ]
    for middleware in middlewares:
        context.dispatcher.middleware.setup(middleware)


#######
#
#   BOT
#
#####


########
#   WEBHOOK
######


async def on_startup(context, _):
    await context.db.connect()


async def on_shutdown(context, _):
    await context.db.close()


# YC Serverless Containers requires PORT env var
# https://cloud.yandex.ru/docs/serverless-containers/concepts/runtime#peremennye-okruzheniya
PORT = getenv('PORT', 8080)


def run(context):
    executor.start_webhook(
        dispatcher=context.dispatcher,

        # YC Serverless Container is assigned with endpoint
        # https://bba......v7v9.containers.yandexcloud.net/
        webhook_path='/',

        port=PORT,

        on_startup=context.on_startup,
        on_shutdown=context.on_shutdown,

        # Disable aiohttp "Running on ... Press CTRL+C"
        # Polutes YC Logging
        print=None
    )


########
#   CONTEXT
######


class BotContext:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.dispatcher = Dispatcher(self.bot)
        self.db = DB()


BotContext.handle_start_command = handle_start_command
BotContext.handle_future_events_command = handle_future_events_command
BotContext.handle_chats_command = handle_chats_command
BotContext.handle_contacts_command = handle_contacts_command
BotContext.handle_whois_howto_command = handle_whois_howto_command
BotContext.handle_events_archive_command = handle_events_archive_command
BotContext.handle_lectures_archive_command = handle_lectures_archive_command

BotContext.handle_other = handle_other

BotContext.handle_chat_new_message = handle_chat_new_message
BotContext.handle_chat_edited_message = handle_chat_edited_message

BotContext.setup_handlers = setup_handlers
BotContext.setup_middlewares = setup_middlewares

BotContext.on_startup = on_startup
BotContext.on_shutdown = on_shutdown
BotContext.run = run


######
#
#   MAIN
#
#####


if __name__ == '__main__':
    context = BotContext()
    context.setup_handlers()
    context.setup_middlewares()
    context.run()
