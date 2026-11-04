"""Flags, environment and startup validation.

Two conventions run through this module.

*Credentials come from the environment, and nowhere else.* This package never
asks anyone to paste a secret, never reads one from a prompt -- apart from the
callback address ``connect --manual`` takes, which carries the authorization
code -- and never writes one to a file. When something is missing it names the
variable and stops. A
tool that asks for a secret trains people to paste secrets into tools that ask.

*``--env-file`` takes an explicit path and performs no search.* python-dotenv's
default behaviour walks up from the current directory looking for a ``.env``,
which means the credentials a server starts with depend on where it was started
from. For a process that holds several companies' books open, that is not a
convenience.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit  # parsing only, no network capability

from dotenv import load_dotenv

from . import __version__, live_values
from .constants import (
    ATTACHMENT_HOST_HINT,
    API_BASES,
    CALLBACK_PORT,
    DEFAULT_REDIRECT_URI,
    ENV_PRODUCTION,
    ENV_SANDBOX,
    ENVIRONMENTS,
)
from .crypto import ENV_KEY_VAR, KeySource, parse_key_source
from .errors import StartupError
from .netpolicy import validate_api_base
from .registry import Registry, load_registry
from .store import AppCredentials, TokenStore, default_store_directory

ENV_CLIENT_ID: Final = "QBO_CLIENT_ID"
ENV_CLIENT_SECRET: Final = "QBO_CLIENT_SECRET"
ENV_ENTITIES_FILE: Final = "QBO_ENTITIES_FILE"
ENV_TOKEN_STORE: Final = "QBO_TOKEN_STORE"
ENV_KEY_SOURCE: Final = "QBO_TOKEN_KEY_SOURCE"
ENV_ENVIRONMENT: Final = "QBO_ENV"
ENV_ALLOW_DOCUMENTS: Final = "QBO_ALLOW_DOCUMENTS"
#: Comma-separated hostnames (exact, or ``*.suffix``) an attachment download
#: may contact. Empty means every download is refused, whatever
#: ``--allow-documents`` says: the storage host is observed on the first
#: refusal and pinned deliberately.
ENV_ATTACHMENT_HOSTS: Final = "QBO_ATTACHMENT_HOSTS"
ENV_REDIRECT_URI: Final = "QBO_REDIRECT_URI"
ENV_IMPORT_REFRESH_TOKEN: Final = "QBO_IMPORT_REFRESH_TOKEN"

#: The variables that can hold a secret: the app's client secret, the token
#: store key, and a refresh token being imported.
SECRET_ENVIRONMENT_VARIABLES: Final = (ENV_CLIENT_SECRET, ENV_KEY_VAR, ENV_IMPORT_REFRESH_TOKEN)


@dataclass(frozen=True, slots=True)
class Settings:
    entities_file: Path
    environment: str
    api_base: str
    token_store_dir: Path
    key_source: KeySource
    allow_plaintext_store: bool
    allow_documents: bool
    allow_custom_api_base: bool
    redirect_uri: str
    verbose: bool
    #: The port the OAuth callback listener binds. **Not settable from the CLI
    #: or the environment** -- ``settings_from_args`` always produces
    #: :data:`CALLBACK_PORT`, so the CLI's guarantee of never silently choosing
    #: another port is intact. It exists as a field only so the test suite can
    #: bind an ephemeral port instead of the real one; binding 8765 would keep
    #: the suite from running while any consent was in flight.
    callback_port: int = CALLBACK_PORT
    #: The download hosts the operator pinned (``QBO_ATTACHMENT_HOSTS`` or
    #: ``--attachment-host``). No host, no download.
    attachment_hosts: tuple[str, ...] = ()

    def credentials(self) -> AppCredentials | None:
        """App credentials from the environment, or ``None`` if absent.

        ``None`` rather than an exception: ``tools/list`` and ``list_entities``
        are answerable without them, and a server that refused to start without
        credentials could not tell an operator what it was missing.
        """
        client_id = os.environ.get(ENV_CLIENT_ID, "").strip()
        client_secret = os.environ.get(ENV_CLIENT_SECRET, "").strip()
        if not client_id or not client_secret:
            return None
        return AppCredentials(client_id=client_id, client_secret=client_secret)

    def require_credentials(self) -> AppCredentials:
        credentials = self.credentials()
        if credentials is None:
            raise StartupError(
                f"{ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} must be set in the environment. "
                f"This tool never asks you to paste a secret."
            )
        return credentials

    def load_registry(self) -> Registry:
        return load_registry(self.entities_file, expected_environment=self.environment)

    def open_store(self, *, credentials: AppCredentials | None = None) -> TokenStore:
        """Construct the store. Touches no disk until it is read or written.

        Constructing is deliberately cheap and side-effect free, so the server
        can build one inside a tool call without ``tools/list`` ever having
        created a directory.
        """
        return TokenStore(
            self.token_store_dir,
            environment=self.environment,
            key_source=self.key_source,
            allow_plaintext=self.allow_plaintext_store,
            credentials=credentials if credentials is not None else self.credentials(),
        )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--entities",
        metavar="PATH",
        help=f"path to entities.yaml (or set {ENV_ENTITIES_FILE})",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help="load environment variables from this exact file (no directory search)",
    )
    parser.add_argument(
        "--token-store",
        metavar="DIR",
        help=f"directory holding the token store (or set {ENV_TOKEN_STORE})",
    )
    parser.add_argument(
        "--allow-plaintext-store",
        action="store_true",
        help="permit an unencrypted token store (QBO_TOKEN_KEY_SOURCE=none)",
    )
    parser.add_argument(
        "--api-base",
        metavar="URL",
        help="Accounting API origin; restricted to Intuit hosts or a loopback mock",
    )
    parser.add_argument(
        "--allow-custom-api-base",
        action="store_true",
        help=(
            "permit an --api-base outside the Intuit hosts and loopback. Test "
            "only: any base that is not Intuit's is sent synthetic credentials "
            "or nothing"
        ),
    )
    parser.add_argument(
        "--redirect-uri",
        metavar="URL",
        help=(
            f"OAuth redirect URI registered with Intuit "
            f"(or set {ENV_REDIRECT_URI}); defaults to {DEFAULT_REDIRECT_URI}"
        ),
    )
    parser.add_argument(
        "--allow-documents",
        action="store_true",
        help=f"permit attachment downloads (or set {ENV_ALLOW_DOCUMENTS}=1)",
    )
    parser.add_argument(
        "--attachment-host",
        action="append",
        default=None,
        metavar="HOST",
        help=(
            f"a storage host attachment downloads may contact, exact or *.suffix; "
            f"repeatable (or set {ENV_ATTACHMENT_HOSTS}, comma-separated). Without one, "
            f"every download is refused; {ATTACHMENT_HOST_HINT}"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="verbose diagnostics on stderr (redacted; never on stdout)",
    )
    parser.add_argument("--version", action="version", version=f"qbo-multicompany-mcp {__version__}")


def register_environment_secrets() -> None:
    """Teach the redactor every secret the environment holds, before anything prints.

    Otherwise the client secret is learned only when app credentials are
    built, after the registry has loaded, and a registry error repeats the key
    or field it refuses: a secret pasted there by mistake would meet only the
    redactor's shape rules, which a punctuated value passes. Each entry point
    calls this first, and :func:`settings_from_args` again once an env file
    has been loaded.
    """
    live_values.register(
        *(os.environ.get(name, "").strip() for name in SECRET_ENVIRONMENT_VARIABLES)
    )


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Resolve flags and environment into :class:`Settings`.

    Every failure here raises :class:`StartupError`, which entry points render
    as one redacted line on stderr and exit status 2 -- no traceback. A
    traceback on a startup failure is noise wrapped around the one line that
    matters, and it occasionally carries a path or a value worth not printing.
    """
    env_file = getattr(args, "env_file", None)
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise StartupError(f"--env-file {path} does not exist")
        # override=False: a variable already exported wins over the file, so a
        # deliberate one-off in the shell is not silently replaced.
        load_dotenv(path, override=False)
        register_environment_secrets()

    entities = getattr(args, "entities", None) or os.environ.get(ENV_ENTITIES_FILE)
    if not entities:
        raise StartupError(
            f"no entities file: pass --entities or set {ENV_ENTITIES_FILE}"
        )

    environment = (os.environ.get(ENV_ENVIRONMENT) or ENV_SANDBOX).strip()
    if environment not in ENVIRONMENTS:
        raise StartupError(
            f"{ENV_ENVIRONMENT} must be one of {', '.join(ENVIRONMENTS)} "
            f"(defaults to {ENV_SANDBOX})"
        )

    key_source = parse_key_source(os.environ.get(ENV_KEY_SOURCE))
    allow_plaintext = bool(getattr(args, "allow_plaintext_store", False))
    allow_custom_base = bool(getattr(args, "allow_custom_api_base", False))

    api_base_arg = getattr(args, "api_base", None)
    api_base = validate_api_base(
        api_base_arg or API_BASES[environment],
        environment,
        allow_custom=allow_custom_base,
    )

    store_dir_raw = getattr(args, "token_store", None) or os.environ.get(ENV_TOKEN_STORE)
    token_store_dir = (
        Path(store_dir_raw).expanduser() if store_dir_raw else default_store_directory()
    )

    hosts_raw = list(getattr(args, "attachment_host", None) or [])
    hosts_raw.extend(os.environ.get(ENV_ATTACHMENT_HOSTS, "").split(","))
    attachment_hosts = tuple(dict.fromkeys(h.strip().lower() for h in hosts_raw if h.strip()))
    allow_documents = bool(getattr(args, "allow_documents", False)) or (
        os.environ.get(ENV_ALLOW_DOCUMENTS, "") == "1"
    )

    redirect_uri = validate_redirect_uri(
        getattr(args, "redirect_uri", None) or os.environ.get(ENV_REDIRECT_URI) or
        DEFAULT_REDIRECT_URI
    )

    return Settings(
        entities_file=Path(entities).expanduser(),
        environment=environment,
        api_base=api_base,
        token_store_dir=token_store_dir,
        key_source=key_source,
        allow_plaintext_store=allow_plaintext,
        allow_documents=allow_documents,
        attachment_hosts=attachment_hosts,
        allow_custom_api_base=allow_custom_base,
        redirect_uri=redirect_uri,
        verbose=bool(getattr(args, "verbose", False)),
    )


def validate_redirect_uri(value: str) -> str:
    """Validate the redirect URI that will be sent to Intuit.

    The developer portal's Redirect URIs screen states (read 2026-09-20;
    Intuit's page on redirect URIs states the IP-address rule too and permits
    plain-http localhost for sandbox): "The URI itself cannot be an IP address
    or a URI fragment or relative path."
    That rule is why the default is
    ``http://localhost:8765/callback`` and not the ``127.0.0.1`` literal --
    the literal cannot be registered, and a live consent attempt using it
    failed with "The redirect_uri query parameter value is invalid".

    Two values are accepted:

    ``http://localhost:8765/callback``
        The default. Plain http is acceptable to Intuit for loopback
        development, and the local listener binds both loopback literals to
        receive it whichever family the browser resolves ``localhost`` to.

    An ``https://`` URL on a host the operator controls
        What production needs: register an HTTPS forwarder page (see
        ``docs/callback.html``) that navigates the browser to the local
        listener. A single hard-coded constant cannot serve both cases, which
        is why this is configurable at all.

    Rejected, with the reason named:

    * an IP literal, v4 or v6 -- Intuit will not register one;
    * a query string, because Intuit appends ``?code=...&state=...&realmId=...``
      (its Set up OAuth 2.0 page) and a pre-existing query would produce the
      duplicate parameters the callback parser refuses;
    * a fragment, which Intuit's own rule excludes and which never survives to
      the server anyway;
    * userinfo, which has no business in a URI a browser is sent to;
    * any other scheme, including plain http on a non-default host.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise StartupError(
            f"{ENV_REDIRECT_URI} is empty; unset it to use the default "
            f"{DEFAULT_REDIRECT_URI}"
        )
    if candidate == DEFAULT_REDIRECT_URI:
        return candidate

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise StartupError("--redirect-uri is not a valid URL") from exc

    if parts.scheme != "https":
        raise StartupError(
            f"--redirect-uri must be https, or exactly the loopback default "
            f"{DEFAULT_REDIRECT_URI}. Intuit does not accept an IP address as a "
            f"redirect URI, so http is only usable with the 'localhost' default."
        )
    if not parts.hostname:
        raise StartupError("--redirect-uri has no host")
    if _is_ip_literal(parts.hostname):
        raise StartupError(
            "--redirect-uri must name a host, not an IP address: Intuit's console "
            "states the URI cannot be an IP address and will refuse to register one."
        )
    if parts.query:
        raise StartupError(
            "--redirect-uri must not carry a query string: Intuit appends the "
            "authorization code, state and realm id as query parameters."
        )
    if parts.fragment:
        raise StartupError("--redirect-uri must not carry a fragment")
    if parts.username or parts.password:
        raise StartupError("--redirect-uri must not carry userinfo")

    return candidate


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def is_production(settings: Settings) -> bool:
    return settings.environment == ENV_PRODUCTION
