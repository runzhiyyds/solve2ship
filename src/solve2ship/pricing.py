"""Explicit, owner-local price reviews; no network, renewal or budget authority.

The seven-day maximum is a local review policy, not a provider price guarantee.
Amounts are conservative token-price estimates, never reconciled provider bills.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
import uuid

from .contracts import canonical_json, sha256, strict_json_loads


MAX_DOCUMENT_BYTES = 8192
MAX_REVIEW_SECONDS = 7 * 24 * 60 * 60
DOCUMENT_FIELDS = {
    'schema_version', 'base_url', 'model', 'documented_model_version', 'currency',
    'input_micro_per_token', 'output_micro_per_token', 'context_ceiling',
    'max_output_tokens', 'source', 'source_sha256', 'reviewed_at', 'valid_from',
    'expires_at',
}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z')


class PricingError(ValueError):
    """A controlled code without user-supplied document contents."""


def canonical_endpoint(base_url):
    if type(base_url) is not str or len(base_url) > 200:
        raise PricingError('pricing_endpoint_mismatch')
    try:
        url = urlsplit(base_url)
        valid = (url.scheme == 'https' and url.hostname == 'api.deepseek.com'
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.query and not url.fragment
                 and url.path in ('', '/', '/v1', '/v1/'))
    except ValueError:
        valid = False
    if not valid or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
        raise PricingError('pricing_endpoint_mismatch')
    return 'https://api.deepseek.com' + url.path.rstrip('/')


def _timestamp(value):
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise PricingError('pricing_invalid_document')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        raise PricingError('pricing_invalid_document') from None


def _clock(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not float('-inf') < now < float('inf'):
        raise PricingError('pricing_invalid_clock')
    return now


def validate_document(value, now=None):
    """Validate/copy a review document; an expired review remains auditable.

    Time parameters are UTC Unix seconds. Validity for new calls is checked by
    resolve(), while this function enforces the review's shape and maximum window.
    """
    now = _clock(now)
    if type(value) is not dict or value.keys() != DOCUMENT_FIELDS:
        raise PricingError('pricing_invalid_document')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise PricingError('pricing_invalid_document')
    if value['currency'] != 'CNY':
        raise PricingError('pricing_currency_not_supported')
    if value['model'] != 'deepseek-flash':
        raise PricingError('pricing_model_mismatch')
    result = dict(value)
    result['base_url'] = canonical_endpoint(value['base_url'])
    version = value['documented_model_version']
    if (type(version) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', version)
            or re.search(r'(?i)(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{20,}', version)):
        raise PricingError('pricing_invalid_document')
    for field, maximum in [('input_micro_per_token', 1000000), ('output_micro_per_token', 1000000),
                           ('context_ceiling', 2097152), ('max_output_tokens', 8192)]:
        if type(value[field]) is not int or not 1 <= value[field] <= maximum:
            raise PricingError('pricing_invalid_document')
    source = value['source']
    if type(source) is not str or len(source) > 1000 or any(ord(c) <= 32 or ord(c) == 127 for c in source):
        raise PricingError('pricing_invalid_source')
    try:
        url = urlsplit(source)
        valid = (url.scheme == 'https' and url.hostname == 'api-docs.deepseek.com'
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.query and not url.fragment and url.path.startswith('/'))
    except ValueError:
        valid = False
    if not valid or re.search(r'(?i)(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{20,}', source):
        raise PricingError('pricing_invalid_source')
    result['source'] = 'https://api-docs.deepseek.com' + url.path
    if type(value['source_sha256']) is not str or not _HASH.fullmatch(value['source_sha256']):
        raise PricingError('pricing_invalid_document')
    reviewed, start, end = (_timestamp(value[key]) for key in ('reviewed_at', 'valid_from', 'expires_at'))
    if reviewed > now:
        raise PricingError('pricing_review_in_future')
    if not reviewed <= start < end <= reviewed + MAX_REVIEW_SECONDS:
        raise PricingError('pricing_invalid_validity_window')
    return result


def _snapshot(document):
    digest = sha256(document)
    return {**document, 'id': digest, 'version_id': digest, 'verified_date': document['reviewed_at'][:10]}


def _read_json(path, *, dir_fd=None):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PricingError('pricing_unsafe_file')
        if info.st_size > MAX_DOCUMENT_BYTES:
            raise PricingError('pricing_document_too_large')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise PricingError('pricing_document_too_large')
        return strict_json_loads(raw)
    except FileNotFoundError:
        raise PricingError('price_snapshot_requires_review') from None
    except PricingError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise PricingError('pricing_file_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_directory(path, *, dir_fd=None):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PricingError('pricing_unsafe_directory')
        return descriptor
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(error, PricingError):
            raise
        if isinstance(error, FileNotFoundError):
            raise PricingError('price_snapshot_requires_review') from None
        if isinstance(error, OSError):
            raise PricingError('pricing_unsafe_directory') from None
        raise


def _write_temporary(directory_fd, value):
    name = '.review-' + uuid.uuid4().hex
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(canonical_json(value) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(name, dir_fd=directory_fd)
        raise
    return name


class PricingCatalog:
    def __init__(self, directory):
        self.directory = Path(directory)

    def preview(self, path):
        return _snapshot(validate_document(_read_json(path)))

    @contextmanager
    def _directories(self, *, create=False):
        if create:
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                raise PricingError('pricing_unsafe_directory') from None
        root_fd = _open_directory(self.directory)
        versions_fd = None
        try:
            if create:
                try:
                    os.mkdir('versions', mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            versions_fd = _open_directory('versions', dir_fd=root_fd)
            yield root_fd, versions_fd
        finally:
            if versions_fd is not None:
                os.close(versions_fd)
            os.close(root_fd)

    @staticmethod
    def _active(root_fd):
        active = _read_json('active.json', dir_fd=root_fd)
        if (type(active) is not dict or active.keys() != {'version_id'}
                or type(active['version_id']) is not str or not _HASH.fullmatch(active['version_id'])):
            raise PricingError('pricing_invalid_active_version')
        return active['version_id']

    @staticmethod
    def _version(versions_fd, version_id, now=None):
        if type(version_id) is not str or not _HASH.fullmatch(version_id):
            raise PricingError('pricing_invalid_version_id')
        document = validate_document(_read_json(version_id + '.json', dir_fd=versions_fd), now=now)
        snapshot = _snapshot(document)
        if snapshot['version_id'] != version_id:
            raise PricingError('pricing_version_hash_mismatch')
        return snapshot

    def import_reviewed(self, path, reviewed_sha256):
        document = validate_document(_read_json(path))
        snapshot = _snapshot(document)
        if type(reviewed_sha256) is not str or reviewed_sha256 != snapshot['version_id']:
            raise PricingError('pricing_review_hash_mismatch')
        with self._directories(create=True) as (root_fd, versions_fd):
            try:
                os.stat('active.json', dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                self._version(versions_fd, self._active(root_fd))
            temporary = _write_temporary(versions_fd, document)
            try:
                try:
                    # Link only a fully written file. A concurrent import never sees
                    # partial version bytes, and an existing version is never replaced.
                    os.link(temporary, snapshot['version_id'] + '.json', src_dir_fd=versions_fd,
                            dst_dir_fd=versions_fd, follow_symlinks=False)
                except FileExistsError:
                    self._version(versions_fd, snapshot['version_id'])
                os.fsync(versions_fd)
            finally:
                os.unlink(temporary, dir_fd=versions_fd)
            pointer = _write_temporary(root_fd, {'version_id': snapshot['version_id']})
            try:
                os.replace(pointer, 'active.json', src_dir_fd=root_fd, dst_dir_fd=root_fd)
                os.fsync(root_fd)
            finally:
                try:
                    os.unlink(pointer, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
        return snapshot

    @staticmethod
    def _binding_and_window(snapshot, base_url, model, now):
        if base_url is not None and canonical_endpoint(base_url) != snapshot['base_url']:
            raise PricingError('pricing_endpoint_mismatch')
        if model is not None and (type(model) is not str or model != snapshot['model']):
            raise PricingError('pricing_model_mismatch')
        if now < _timestamp(snapshot['valid_from']):
            raise PricingError('pricing_not_yet_valid')
        if now >= _timestamp(snapshot['expires_at']):
            raise PricingError('pricing_expired')

    def resolve(self, base_url, model, version_id=None, now=None):
        now = _clock(now)
        with self._directories() as (root_fd, versions_fd):
            selected = self._active(root_fd) if version_id is None else version_id
            snapshot = self._version(versions_fd, selected, now=now)
        if base_url is None or model is None:
            raise PricingError('pricing_binding_required')
        self._binding_and_window(snapshot, base_url, model, now)
        return snapshot

    def status(self, base_url=None, model=None):
        selected, snapshot = None, None
        try:
            now = _clock(None)
            with self._directories() as (root_fd, versions_fd):
                selected = self._active(root_fd)
                snapshot = self._version(versions_fd, selected, now=now)
            self._binding_and_window(snapshot, base_url, model, now)
        except (PricingError, OSError) as error:
            return {'ready': False, 'reason': str(error) if isinstance(error, PricingError) else 'pricing_file_unavailable',
                    'active_version_id': selected if snapshot else None, 'snapshot': snapshot}
        return {'ready': True, 'reason': None, 'active_version_id': selected, 'snapshot': snapshot}
