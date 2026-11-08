"""Local BYOK profile preparation. No network requests or model execution."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlsplit


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def status(self):
        return {"configured": True, "base_url": self.base_url, "model": self.model,
                "api_key_configured": bool(self.api_key), "connection_verified": False}


def validate_profile(value):
    if type(value) is not dict or value.keys() != {'base_url', 'model', 'api_key'}:
        raise ProfileError('Profile requires base_url, model and api_key')
    if any(type(item) is not str or not item or len(item) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in item)
           for item in value.values()):
        raise ProfileError('Profile values must be non-empty strings without control characters')
    try:
        url = urlsplit(value['base_url'])
        url.port
    except ValueError:
        raise ProfileError('Invalid model service URL') from None
    local = url.hostname in {'localhost', '127.0.0.1', '::1'}
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.scheme not in {'https', 'http'} or (url.scheme == 'http' and not local)):
        raise ProfileError('Use HTTPS, or HTTP on loopback; credentials/query/fragment in URLs are not accepted')
    if len(value['model']) > 200 or not value['model'].strip() or not value['api_key'].isascii() or ' ' in value['api_key']:
        raise ProfileError('Invalid model name or API key format')
    return ModelProfile(value['base_url'].rstrip('/'), value['model'], value['api_key'])


def save_profile(path: Path, value):
    profile = validate_profile(value)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProfileError('Refusing a symbolic-link profile')
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump({'base_url': profile.base_url, 'model': profile.model, 'api_key': profile.api_key}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return profile


def load_profile(path: Path):
    # O_NOFOLLOW + fstat binds permission/ownership checks to the file actually read.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ProfileError('Profile must be an owner-only regular file (chmod 600)')
        if info.st_size > 16384:
            raise ProfileError('Profile is too large')
        handle = os.fdopen(descriptor, encoding='utf-8')
        descriptor = None  # fdopen now owns and closes the file descriptor.
        with handle:
            try:
                text = handle.read(16385)
                if len(text) > 16384:
                    raise ProfileError('Profile is too large')
                value = json.loads(text)
            except (ValueError, UnicodeError):
                raise ProfileError('Invalid profile JSON') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return validate_profile(value)
