"""Library for interacting with the WyBot API."""

import hashlib
import logging

import requests

from .const import TIMEOUT
from .wybot_models import DevicesResponse, Group, LoginResponse

_LOGGER = logging.getLogger(__name__)

# Send the user/password to get the Token
AUTH_URL = "https://api.wybotpool.com/api/user/login"


# Get all pools on the account
POOLS_URL = "https://api.wybotpool.com/api/env/pool"

# Given a Pool ID, get all the devices and the status
# The end should append the user id
DEVICES_URL = "https://api.wybotpool.com/api/group/"

# Send commands
COMMAND_URL = "https://api.wybotpool.com/api/device/ao"

DEFAULT_HEADER = {
    "Content-Type": "application/json",
    "User-Agent": "WYBOT/13 CFNetwork/1498.700.2 Darwin/23.6.0",
}


class WyBotHTTPClient:
    """Client for interacting with the WyBot API."""

    _token = None
    _user_id = None
    _password = None
    _username = None

    def __init__(self, username, password) -> None:
        """Init the wybot api."""
        self._username = username
        self._password = password

    def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        login_response = self.login()
        self._token = (
            login_response.metadata.token
            if login_response and login_response.metadata
            else None
        )
        self._user_id = (
            login_response.metadata.user_id
            if login_response and login_response.metadata
            else None
        )
        return self._token is not None

    def login(self) -> LoginResponse | None:
        """Authenticate the user and retrieve a token."""
        _LOGGER.debug("Grabbing a token with a user and password")
        if not self._password:
            _LOGGER.error("Password is not set")
            return None
        md5_hash = hashlib.md5()
        md5_hash.update(self._password.encode("utf-8"))
        md5_hex = md5_hash.hexdigest()
        auth_data = {
            "username": self._username,
            "password": md5_hex,
        }
        try:
            response = requests.post(
                AUTH_URL,
                json=auth_data,
                headers=DEFAULT_HEADER,
                allow_redirects=False,
                timeout=TIMEOUT,
            )
            response.close()
            if response.status_code != 200:
                _LOGGER.error("Error getting token: %s", response.text)
                return None

            json_response = response.json()
            return LoginResponse(**json_response)
        except Exception as e:
            _LOGGER.error("Error getting token: %s", e)
            return None

    def get_devices_and_status(self) -> DevicesResponse | None:
        """Grab all devices and statuses."""
        if self._user_id is None:
            _LOGGER.error("User ID is not set")
            return None
        device_url = DEVICES_URL + str(self._user_id)
        _LOGGER.debug("Grabbing devices and statuses: %s", device_url)
        try:
            response = requests.get(
                device_url,
                headers={**DEFAULT_HEADER, "Authorization": f"token {self._token}"},
                allow_redirects=False,
                timeout=TIMEOUT,
            )
            response.close()
            if response.status_code != 200:
                _LOGGER.error("Error getting devices: %s", response.text)
                return None

            json_response = response.json()
            return DevicesResponse(**json_response)
        except Exception as e:
            _LOGGER.error("Error getting token: %s", e)
            return None

    def get_indexed_current_grouped_devices(self) -> dict[str, Group]:
        """Return a dictionary of devices indexed by the grouped device_id."""
        response = self.get_devices_and_status()
        if response is None:
            return {}
        return {group.id: group for group in response.metadata.groups}
