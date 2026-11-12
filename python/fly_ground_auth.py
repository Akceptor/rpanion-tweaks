"""Owner credential validation, kept separate from public bridge settings."""

import base64
import binascii
import re


TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
TOKEN_ERROR = 'FLY_HOST_TOKEN must be a canonical base64url-encoded 32-byte secret'


def validate_host_token(value):
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ValueError(TOKEN_ERROR)
    try:
        decoded = base64.b64decode(value + '=', altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(TOKEN_ERROR) from None
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip('=') != value:
        raise ValueError(TOKEN_ERROR)
    return value


def host_token_from_env(env):
    return validate_host_token(env.get('FLY_HOST_TOKEN'))
