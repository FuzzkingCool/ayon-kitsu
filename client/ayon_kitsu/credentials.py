"""Kitsu credentials functions."""

import os
from typing import Optional, Tuple, Union

import gazu
from ayon_core.lib import AYONSecureRegistry, emit_event


def normalize_kitsu_host(url: str) -> str:
    """Ensure host URL ends with /api for gazu (e.g. https://studio.example.com/api)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if not url.endswith("/api"):
        url = f"{url}/api"
    return url


def validate_credentials(
    login: str,
    password: str,
    kitsu_url: Optional[str] = None,
) -> bool:
    """Validate credentials by trying to connect to Kitsu host URL.

    Args:
        login (str): Kitsu user login
        password (str): Kitsu user password
        kitsu_url (str, optional): Kitsu host URL. Defaults to None.

    Returns:
        bool: Are credentials valid?
    """

    if kitsu_url is None:
        kitsu_url = os.environ.get("KITSU_SERVER")
        if kitsu_url is None:
            raise ValueError("KITSU_SERVER environment variable is not set")

    kitsu_url = normalize_kitsu_host(kitsu_url)
    validate_host(kitsu_url)

    # Authenticate
    try:
        gazu.log_in(login, password)
    except gazu.exception.AuthFailedException:
        return False

    # TODO remove this event trigger
    # - for what is this used?
    emit_event("kitsu.user.logged", data={"username": login}, source="kitsu")

    return True


def validate_host(kitsu_url: str) -> bool:
    """Validate credentials by trying to connect to Kitsu host URL.

    Args:
        kitsu_url (str): Kitsu host URL (with or without /api; normalized internally).

    Returns:
        bool: Is host valid?

    Raises:
        gazu.exception.HostException: If host is unreachable or invalid.
    """
    kitsu_url = normalize_kitsu_host(kitsu_url)
    gazu.set_host(kitsu_url)
    client = gazu.client.default_client
    # Run reachability check ourselves so we can raise with the real cause;
    # gazu.client.host_is_valid() returns False on failure and hides the exception.
    try:
        response = client.session.head(kitsu_url, timeout=15)
        if response.status_code != 200:
            raise gazu.exception.HostException(
                f"Host '{kitsu_url}' returned HTTP {response.status_code} (expected 200)."
            )
    except gazu.exception.HostException:
        raise
    except Exception as e:
        raise gazu.exception.HostException(
            f"Host '{kitsu_url}' unreachable: {e!s}"
        ) from e
    # HEAD 200 is sufficient. gazu.client.host_is_valid() also POSTs auth/login with
    # empty email; some Kitsu setups return a response that gazu doesn't treat as
    # "valid host", causing false "API validation failed" on macOS and elsewhere.
    return True


def clear_credentials():
    """Clear credentials in Secure Registry."""
    (login, passsword) = load_credentials()
    if login is None and passsword is None:
        return

    # Get user registry
    user_registry = AYONSecureRegistry("kitsu_user")

    # Set local settings
    if login is not None:
        user_registry.delete_item("login")
    if passsword is not None:
        user_registry.delete_item("password")


def save_credentials(login: str, password: str):
    """Save credentials in Secure Registry.

    Args:
        login (str): Kitsu user login
        password (str): Kitsu user password
    """
    # Get user registry
    user_registry = AYONSecureRegistry("kitsu_user")

    # Set local settings
    user_registry.set_item("login", login)
    user_registry.set_item("password", password)


def load_credentials() -> Tuple[Union[object, None], Union[object, None]]:
    """Load registered credentials.

    Returns:
        Tuple[str, str]: (Login, Password)
    """
    # Get user registry
    user_registry = AYONSecureRegistry("kitsu_user")

    return (
        user_registry.get_item("login", None),
        user_registry.get_item("password", None),
    )
