"""Lightweight URL resolution and error classification shared with metadata workers."""
import errno
import re
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener
from media import MediaError

FACEBOOK_HOSTS = ('facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.watch')

class DownloadFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def error_code(exc):
    if isinstance(exc, (DownloadFailure, MediaError)):
        return exc.code
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return 'storage_limit'
    detail = str(exc).lower()
    if 'file_too_large' in detail or 'larger than max-filesize' in detail:
        return 'file_too_large'
    if 'duration_limit' in detail:
        return 'duration_limit'
    if 'time_limit' in detail:
        return 'time_limit'
    if isinstance(exc, HTTPError) and exc.code == 429 or 'http error 429' in detail:
        return 'rate_limited'
    if isinstance(exc, HTTPError) and exc.code in (401, 403) or any(s in detail for s in ('http error 403', 'http error 401', 'login required', 'log in', 'sign in', 'private video', 'video unavailable')):
        return 'access_restricted'
    if 'unsupported url' in detail:
        return 'share_link'
    if 'requested format is not available' in detail:
        return 'format_unavailable'
    if 'unable to obtain file audio codec' in detail:
        return 'audio_unavailable'
    if any(s in detail for s in ('cannot parse data', 'unable to extract', 'no video formats found')):
        return 'extractor_error'
    if isinstance(exc, (URLError, TimeoutError)) or any(s in detail for s in ('timed out', 'unable to download', 'connection refused', 'connection reset')):
        return 'network_error'
    return 'download_failed'

class FacebookRedirectHandler(HTTPRedirectHandler):
    # Resolve share links without enabling yt-dlp's unrestricted generic extractor.
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            u = urlparse(newurl)
            if u.scheme not in ('https', 'http') or u.hostname not in FACEBOOK_HOSTS or u.username or u.password or u.port:
                raise ValueError()
        except (ValueError, TypeError):
            raise DownloadFailure('share_link') from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def resolve_facebook_share(url):
    u = urlparse(url)
    if u.hostname != 'fb.watch' and not re.match(r'^/share/(v|r)/', u.path):
        return url
    request = URLRequest(url, headers={'User-Agent': 'ClipKeep/2.0'})
    with build_opener(FacebookRedirectHandler()).open(request, timeout=20) as response:
        resolved = response.geturl()
    # Validate the final URL even when a response did not follow a redirect.
    from yt_dlp.extractor.facebook import FacebookIE, FacebookReelIE
    try:
        final = urlparse(resolved)
        if (final.scheme not in ('https', 'http') or final.hostname not in FACEBOOK_HOSTS
                or final.username or final.password or final.port
                or not any(ie.suitable(resolved) for ie in (FacebookIE, FacebookReelIE))):
            raise ValueError()
    except (ValueError, TypeError):
        raise DownloadFailure('share_link') from None
    return resolved

