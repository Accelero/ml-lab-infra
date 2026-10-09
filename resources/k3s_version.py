# Copyright (c) 2026 David Schmid
"""Resolve official K3s channels to validated installation releases."""

import http.client
import re
from contextlib import closing
from urllib.parse import unquote, urlsplit

_RELEASE = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\+k3s[0-9]+",
)
_CHANNEL = re.compile(r"stable|latest|testing|v[0-9]+\.[0-9]+")
_REDIRECTS = {301, 302, 303, 307, 308}
_REQUEST_TIMEOUT = 10
_RELEASE_PREFIX = "/k3s-io/k3s/releases/tag/"


def resolve_k3s_version(version: str) -> str:
    """Resolve channels with bounded I/O; exact releases require no network."""
    if _RELEASE.fullmatch(version):
        return version
    if not _CHANNEL.fullmatch(version):
        msg = "k3sVersion must be an exact K3s release or an official release channel."
        raise ValueError(msg)
    connection = http.client.HTTPSConnection("update.k3s.io", timeout=_REQUEST_TIMEOUT)
    with closing(connection):
        connection.request("GET", f"/v1-release/channels/{version}")
        with closing(connection.getresponse()) as response:
            if response.status not in _REDIRECTS:
                msg = f"K3s channel {version!r} returned HTTP {response.status}."
                raise RuntimeError(msg)
            location = response.getheader("Location", "")
    redirect = urlsplit(location)
    release = unquote(redirect.path.removeprefix(_RELEASE_PREFIX))
    if (
        redirect.scheme != "https"
        or redirect.netloc != "github.com"
        or not redirect.path.startswith(_RELEASE_PREFIX)
        or redirect.query
        or redirect.fragment
        or not _RELEASE.fullmatch(release)
    ):
        msg = f"K3s channel {version!r} did not redirect to an official release."
        raise RuntimeError(msg)
    return release
