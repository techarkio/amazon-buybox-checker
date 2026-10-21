"""Read the HTTP proxies that the local browser sessions send their Amazon traffic through.

Input:  BUYBOX_CHECKER_PROXIES, comma-separated, each one either host:port:username:password (the format of
        Webshare's proxy list download), host:port, or a URL like http://username:password@host:port.
Output: Proxy objects, ready to pass to a Playwright browser context.
Constraints: Amazon ties a session's cookies to the IP that got them, so cookies are kept per zipcode and
        proxy (see zipcode.py) and never move to another IP. Use HTTP proxies: Chromium cannot log in to a
        SOCKS5 proxy. Passwords are never logged; a proxy is named host:port.
"""

import os
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

PROXIES_ENV = "BUYBOX_CHECKER_PROXIES"


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    username: str = ""
    password: str = ""
    scheme: str = "http"

    @property
    def name(self) -> str:
        """Return host:port, safe to show in logs and file names."""
        return f"{self.host}:{self.port}"

    def url(self) -> str:
        """Return the proxy as scheme://username:password@host:port, for HTTP clients."""
        login = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@" if self.username else ""
        return f"{self.scheme}://{login}{self.host}:{self.port}"

    def playwright_settings(self) -> dict:
        """Return the proxy option of browser.new_context()."""
        settings = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username:
            settings |= {"username": self.username, "password": self.password}
        return settings


def parse_proxy(text: str) -> Proxy:
    """Read one proxy written as host:port:username:password, host:port, or scheme://username:password@host:port."""
    text = text.strip()
    if "://" in text:
        url = urlparse(text)
        if not url.hostname or not url.port:
            raise ValueError(f"{PROXIES_ENV}: a proxy URL needs a host and a port, e.g. http://user:pass@host:port")
        return Proxy(url.hostname, url.port, unquote(url.username or ""), unquote(url.password or ""), url.scheme)
    parts = text.split(":")
    if len(parts) not in (2, 4):
        raise ValueError(f"{PROXIES_ENV}: write each proxy as host:port:username:password, or as "
                         f"http://username:password@host:port (got {len(parts)} colon-separated parts)")
    if not parts[1].isdigit():
        raise ValueError(f"{PROXIES_ENV}: the port of proxy {parts[0]} is not a number")
    return Proxy(parts[0], int(parts[1]), *parts[2:])


def proxies_from_env() -> list[Proxy]:
    """Return the proxies listed in BUYBOX_CHECKER_PROXIES (comma-separated), or [] when it is not set."""
    return [parse_proxy(entry) for entry in os.environ.get(PROXIES_ENV, "").split(",") if entry.strip()]
