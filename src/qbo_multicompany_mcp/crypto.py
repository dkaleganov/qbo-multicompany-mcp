"""Key sources and authenticated encryption for the token store.

The whole store document is encrypted as one blob under AES-256-GCM, rather
than field by field. Encrypting only the token strings would leave the realm
ids, entity keys and consent timestamps in clear, and that set is enough to
tell an attacker which companies are reachable and which credential is about
to expire.

Three key sources:

``keychain``
    A 32-byte key in the macOS login keychain. The default, and the only source
    where the key never exists in an environment variable, a shell history, or
    a process listing.
``env``
    ``QBO_TOKEN_KEY`` -- base64 or hex, 32 bytes decoded. For containers and CI,
    where a keychain does not exist.
``none``
    Plaintext. Requires ``--allow-plaintext-store``; without it the auth CLI
    refuses to write and the first data call returns a fixed error naming the
    flag.

The AEAD's associated data binds a store file to its environment, so a sandbox
document cannot be decrypted as a production one even under the same key.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shlex
import subprocess
import sys
from enum import StrEnum
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import ConfigurationError, StartupError, StoreError

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
MAGIC: Final = b"QBOMC1\n"

KEYCHAIN_SERVICE: Final = "qbo-multicompany-mcp"
ENV_KEY_VAR: Final = "QBO_TOKEN_KEY"


class KeySource(StrEnum):
    NONE = "none"
    KEYCHAIN = "keychain"
    ENV = "env"


def parse_key_source(value: str | None, *, default: KeySource = KeySource.KEYCHAIN) -> KeySource:
    if value is None or value == "":
        return default
    try:
        return KeySource(value)
    except ValueError:
        raise StartupError(
            f"QBO_TOKEN_KEY_SOURCE must be one of "
            f"{', '.join(s.value for s in KeySource)}"
        ) from None


def keychain_account(environment: str) -> str:
    return f"token-key-{environment}"


def protection_label(source: KeySource) -> str:
    """Human-readable protection, reported by ``server_info``."""
    return {
        KeySource.KEYCHAIN: "aes-256-gcm (key in macOS login keychain)",
        KeySource.ENV: f"aes-256-gcm (key from {ENV_KEY_VAR})",
        KeySource.NONE: "none (plaintext on disk)",
    }[source]


def store_file_mode(source: KeySource) -> int:
    """The protection mode a store file is created with.

    The temp file of a write is created with this mode directly rather than
    chmod'd afterwards, so the file is never briefly readable by others.
    ``0o600`` either way: encryption is not an excuse for loose permissions.
    """
    return 0o600


# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------


#: A 32-byte key spelled in hex, which is the only hex form this accepts.
#: The shape is checked before decoding because every 64-character hex string
#: is also valid base64: trying decoders in order would make base64 win every
#: time, producing 48 bytes and rejecting the key for its length. The encoding
#: is a property of the string, not of whichever decoder happens to answer
#: first.
_HEX_KEY: Final = re.compile(f"\\A[0-9a-fA-F]{{{KEY_BYTES * 2}}}\\Z")


def _decode_key(raw: str, *, where: str) -> bytes:
    text = raw.strip()
    if _HEX_KEY.match(text):
        key = _from_hex(text)
    else:
        key = _from_base64(text)
        if key is None:
            key = _from_hex(text)
    if key is None:
        raise ConfigurationError(f"{where} is not valid base64 or hex")
    if len(key) != KEY_BYTES:
        raise ConfigurationError(f"{where} must decode to {KEY_BYTES} bytes")
    return key


def _from_base64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _from_hex(text: str) -> bytes | None:
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encode_key(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def load_key(source: KeySource, environment: str) -> bytes | None:
    """Return the key for ``source``, or ``None`` for :attr:`KeySource.NONE`."""
    if source is KeySource.NONE:
        return None
    if source is KeySource.ENV:
        raw = os.environ.get(ENV_KEY_VAR)
        if not raw:
            raise ConfigurationError(
                f"QBO_TOKEN_KEY_SOURCE=env but {ENV_KEY_VAR} is not set. "
                f"Set it in the environment; this server never reads a secret from "
                f"a prompt or a file."
            )
        return _decode_key(raw, where=ENV_KEY_VAR)
    return _keychain_read(environment)


# --------------------------------------------------------------------------
# macOS keychain
# --------------------------------------------------------------------------


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ConfigurationError(
            "QBO_TOKEN_KEY_SOURCE=keychain is macOS-only. "
            "Use QBO_TOKEN_KEY_SOURCE=env with QBO_TOKEN_KEY elsewhere."
        )


def _security(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/usr/bin/security", *args],
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
    )


def _keychain_read(environment: str) -> bytes:
    _require_macos()
    account = keychain_account(environment)
    result = _security(
        "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"
    )
    if result.returncode != 0:
        raise ConfigurationError(
            f"No token store key in the login keychain for environment "
            f"'{environment}'. If a token store already exists, restore your copy of the "
            "key, because a new key cannot read it. Otherwise run: "
            "qbo-multicompany-mcp-auth init-key"
        )
    return _decode_key(result.stdout, where="keychain key")


def keychain_has_key(environment: str) -> bool:
    _require_macos()
    result = _security(
        "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", keychain_account(environment)
    )
    return result.returncode == 0


def keychain_create_key(environment: str) -> bool:
    """Create the key if absent. Returns ``True`` if one was created.

    Idempotent, and it never overwrites. Overwriting would render every stored
    refresh token permanently undecryptable while leaving the file in place --
    which looks exactly like corruption and is unrecoverable, since the tokens
    are rotated and no backup can hold a live one.
    """
    _require_macos()
    if keychain_has_key(environment):
        return False
    key = encode_key(generate_key())
    # Through stdin with ``security -i``: an argument would put the key in the
    # child's process listing for as long as it runs.
    command = shlex.join(
        [
            "add-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            keychain_account(environment),
            "-T",
            "/usr/bin/security",
            "-w",
            key,
        ]
    )
    result = _security("-i", input_text=f"{command}\n")
    if result.returncode != 0:
        raise ConfigurationError("could not add the token store key to the login keychain")
    return True


def keychain_export_key(environment: str) -> str:
    """Return the key, base64-encoded, for an operator to copy.

    Guarded by an interactive-terminal check at the call site: printing key
    material into a pipe means it lands in a file, a CI log or a scrollback
    buffer that outlives the session.
    """
    return encode_key(_keychain_read(environment))


# --------------------------------------------------------------------------
# AEAD
# --------------------------------------------------------------------------


def _aad(environment: str) -> bytes:
    return b"qbo-multicompany-mcp/store/v1/" + environment.encode("ascii")


def seal(key: bytes | None, plaintext: bytes, *, environment: str) -> bytes:
    if key is None:
        return plaintext
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(environment))
    return MAGIC + nonce + ciphertext


def unseal(key: bytes | None, blob: bytes, *, environment: str) -> bytes:
    if key is None:
        if blob.startswith(MAGIC):
            raise StoreError(
                "token store is encrypted but QBO_TOKEN_KEY_SOURCE=none. "
                "Set QBO_TOKEN_KEY_SOURCE to the source that wrote it."
            )
        return blob
    if not blob.startswith(MAGIC):
        raise StoreError(
            "token store is not in this package's encrypted format "
            "(it may have been written with QBO_TOKEN_KEY_SOURCE=none)."
        )
    body = blob[len(MAGIC) :]
    if len(body) <= NONCE_BYTES:
        raise StoreError("token store is truncated")
    nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _aad(environment))
    except InvalidTag as exc:
        raise StoreError(
            "token store failed authentication: wrong key, wrong environment, "
            "or the file was modified."
        ) from exc
