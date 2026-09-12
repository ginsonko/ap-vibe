"""Authenticated local encryption for POSIX, with an owner-only master file.

This protects against accidental plaintext disclosure and other OS users. It
does not protect against malware already running as the same user. Windows
continues using DPAPI. The master file must accompany a private data backup.
"""
import os
from pathlib import Path
import stat
import time

from .contracts import ContractError
from .platform_paths import private_dir

PREFIX = b'APVS1'


def _master(create):
    directory = private_dir() / 'credentials'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        raise ContractError('credential_directory_not_owned')
    os.chmod(directory, 0o700)
    path = directory / 'master.key'
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as target:
                target.write(os.urandom(32)); target.flush(); os.fsync(target.fileno())
    for attempt in range(20):
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ContractError('credential_master_unavailable_keep_original_backup') from exc
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise ContractError('credential_master_not_owned')
            os.fchmod(source.fileno(), 0o600)
            key = source.read(33)
        if len(key) == 32:
            return key
        if not create or attempt == 19:
            raise ContractError('credential_master_invalid_keep_original_backup')
        time.sleep(0.05)


def protect(raw, decrypt=False):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise ContractError('secure_storage_dependency_missing_install_cryptography') from exc
    if decrypt and (not raw.startswith(PREFIX) or len(raw) < len(PREFIX) + 28):
        raise ContractError('credential_format_not_portable_reenter_key_on_this_system')
    cipher = AESGCM(_master(create=not decrypt))
    if not decrypt:
        nonce = os.urandom(12)
        return PREFIX + nonce + cipher.encrypt(nonce, raw, PREFIX)
    try:
        return cipher.decrypt(raw[5:17], raw[17:], PREFIX)
    except InvalidTag as exc:
        raise ContractError('credential_decryption_failed_keep_original_backup') from exc
