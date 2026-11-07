"""WhatsApp group ownership and verified native mention presentation."""
from __future__ import annotations

import copy
import re


def field(value, key):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def is_group(value):
    return field(value, 'is_group') is True or field(value, 'conversation_kind') == 'group'


class UnsupportedGroupSDK(PermissionError):
    """The installed response models cannot retain group ownership metadata."""


def require_metadata(value):
    if not isinstance(value, dict) and not hasattr(value, 'is_group'):
        raise UnsupportedGroupSDK('WhatsApp requires Inkbox SDK 0.7.29 or newer for groups')


def valid_group(value):
    participants = field(value, 'participants')
    return (field(value, 'is_group') is True and field(value, 'remote_number') is None
            and not field(value, 'unresolved_participant_count') and isinstance(participants, list)
            and bool(participants)
            and all(isinstance(p, str) and re.fullmatch(r'\+[1-9][0-9]{6,14}', p) for p in participants)
            and len(set(participants)) == len(participants)
            and field(value, 'local_number') not in participants)


def check_destination(adapter, identity, conversation_id, *, companion=False):
    """Check the actual destination's current roster using native host policy."""
    value = identity.get_whatsapp_conversation(conversation_id)
    if field(value, 'remote_number') is None:
        require_metadata(value)
    if str(field(value, 'id')) != str(conversation_id) or str(field(value, 'agent_identity_id')) != str(identity.id):
        raise PermissionError('WhatsApp conversation ownership changed')
    if not is_group(value):
        return value
    if not valid_group(value):
        raise PermissionError('WhatsApp group participants are unavailable')
    owner = getattr(adapter, 'gateway_runner', None) or getattr(getattr(adapter, '_message_handler', None), '__self__', None)
    if owner is None or not callable(getattr(owner, '_is_user_authorized', None)):
        raise PermissionError('WhatsApp group authorization is unavailable')
    for phone in field(value, 'participants'):
        source = adapter.build_source(chat_id='whatsapp:' + str(conversation_id), chat_type='group',
                                      user_id=phone, user_id_alt=phone, user_name=phone)
        if companion:
            source.role_authorized = True
        if not owner._is_user_authorized(source):
            raise PermissionError('WhatsApp group participant is not allowed')
    return value


def current_mentions(item):
    result = []
    for mention in item.get('mentions') or []:
        if not isinstance(mention, dict) or type(mention.get('is_agent')) is not bool:
            continue
        token, phone = mention.get('text'), mention.get('phone_number')
        if token is not None and (not isinstance(token, str) or not re.fullmatch(r'@[0-9]+', token)):
            continue
        if phone is not None and (not isinstance(phone, str) or not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone)):
            continue
        result.append({'text': token, 'phone_number': phone, 'is_agent': mention['is_agent']})
    return result


def attention_text(item):
    text = str(item.get('content') or '')
    for mention in current_mentions(item):
        if mention['text']:
            text = re.sub(re.escape(mention['text']) + r'(?![\w])', ' ', text)
    return text


def control_text(item, handle):
    text = str(item.get('content') or '').strip()
    for mention in current_mentions(item):
        if mention['is_agent'] and mention['text']:
            text = re.sub(r'^' + re.escape(mention['text']) + r'(?![\w])\s*', '', text)
    from .conversation import control_text as ordinary_control
    return ordinary_control(text, handle)


async def render_mentions(adapter, item):
    """Resolve names only; quoted mention data never imports contact memory."""
    raw = str(item.get('content') or '')
    mentions = copy.deepcopy(current_mentions(item))
    replacements = {}
    for mention in mentions:
        if mention['is_agent']:
            name = str(adapter._identity_handle).lstrip('@')
        else:
            phone = mention['phone_number']
            contact = None
            if phone:
                try:
                    contact = await adapter._resolve_contact_full(kind='phone', value=phone)
                except Exception:
                    pass
            name = (contact or {}).get('name') or phone or 'unresolved mention'
        mention['display_name'] = name
        if mention['text']:
            replacements[mention['text']] = '@' + name
    if replacements:
        pattern = r'(?<![\w@])(?:' + '|'.join(re.escape(v) for v in sorted(replacements, key=len, reverse=True)) + r')(?![\w])'
        rendered = re.sub(pattern, lambda match: replacements[match.group()], raw)
    else:
        rendered = raw
    return rendered, mentions


def preflight_error(exc):
    """Separate failed authorization reads from uncertain outbound effects."""
    from httpx import TransportError
    from .imessage_state import ReplyPreflightError
    if isinstance(exc, ReplyPreflightError):
        return exc
    status = getattr(exc, 'status_code', None)
    retryable = (isinstance(exc, (TimeoutError, ConnectionError, TransportError))
                 or status in {408, 409, 429} or isinstance(status, int) and status >= 500)
    return ReplyPreflightError('WhatsApp authorization unavailable; no send attempted',
                               retryable=retryable, status_code=status)
