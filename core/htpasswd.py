#!/usr/bin/env python
"""
Decorator, configuration, and error handler for basic and token
authentication using htpasswd files
"""
from __future__ import absolute_import, unicode_literals
from functools import wraps
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import time

from flask import request, Response, current_app, g, abort, make_response
from itsdangerous import BadSignature
from authlib.jose import JsonWebSignature as Serializer
from passlib.apache import HtpasswdFile


log = logging.getLogger(__name__)  # pylint: disable=invalid-name


def client_address(environ):
    """
    Address of the client, used to count failed logins.

    Behind the local nginx every request comes from 127.0.0.1, so the address nginx
    adds at the end of X-Forwarded-For is used. That header is ignored when the
    request does not come from this machine: anyone reaching the panel directly could
    write anything in it. ProxyFix keeps the real peer in werkzeug.proxy_fix.orig
    (websocket requests skip ProxyFix, so there REMOTE_ADDR is still the real peer).
    """
    peer = environ.get('werkzeug.proxy_fix.orig', {}).get('REMOTE_ADDR') or environ.get('REMOTE_ADDR') or ''
    forwarded = environ.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
    if forwarded and _is_loopback(peer):
        return forwarded
    return peer


def _is_loopback(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip).is_loopback


class FailedLogins:
    """
    Refuse an address for a while after too many failed logins.

    Only different wrong credentials are counted: a forgotten browser tab that keeps
    sending an old password is not someone guessing. A successful login does not
    reset the count, otherwise one valid account could be used to guess another one.
    Everything stays in memory: restarting the panel clears it.
    """

    def __init__(self, max_failures, window, block_time):
        self.max_failures = max_failures  # 0 disables the limit
        self.window = window
        self.block_time = block_time
        self.clock = time.monotonic
        self._key = os.urandom(32)  # the fingerprints below mean nothing outside this process
        self._failures = {}  # address -> {fingerprint of the credentials: time of the last try}
        self._blocked = {}  # address -> time when it may try again

    def retry_after(self, address):
        """Seconds before this address may log in again, 0 if it is not blocked."""
        until = self._blocked.get(address)
        if until is None:
            return 0
        left = until - self.clock()
        if left <= 0:
            del self._blocked[address]
            return 0
        return math.ceil(left)

    def failed(self, address, credentials):
        """Count a failed login. Returns True when this one gets the address blocked."""
        if self.max_failures <= 0:
            return False
        now = self.clock()
        if len(self._failures) > 1000:  # keep memory bounded when many addresses fail
            self._forget(now)
        tries = self._failures.setdefault(address, {})
        tries[hmac.new(self._key, json.dumps(credentials).encode(), hashlib.sha256).digest()] = now
        for old in [f for f, t in tries.items() if now - t > self.window]:
            del tries[old]
        if len(tries) < self.max_failures:
            return False
        del self._failures[address]
        self._blocked[address] = now + self.block_time
        return True

    def _forget(self, now):
        self._failures = {a: tries for a, tries in self._failures.items()
                          if now - max(tries.values(), default=0) <= self.window}
        self._blocked = {a: until for a, until in self._blocked.items() if until > now}


class HtPasswdAuth:
    """Configure htpasswd based basic and token authentication."""

    def __init__(self, app=None):
        """Boiler plate extension init with log_level being declared"""
        self.users = HtpasswdFile()
        self.failed_logins = FailedLogins(0, 0, 0)
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """
        Find and configure the user database from specified file
        """
        # pylint: disable=inconsistent-return-statements
        app.config.setdefault('FLASK_AUTH_ALL', False)
        app.config.setdefault('FLASK_AUTH_REALM', 'Login Required')
        # Default set to bad file to trigger IOError
        app.config.setdefault('FLASK_HTPASSWD_PATH', '/^^^/^^^')
        # After LOGIN_MAX_FAILURES different wrong passwords from one address within
        # LOGIN_FAILURE_WINDOW seconds, refuse it for LOGIN_BLOCK_TIME seconds (0 = no limit)
        app.config.setdefault('LOGIN_MAX_FAILURES', 5)
        app.config.setdefault('LOGIN_FAILURE_WINDOW', 600)
        app.config.setdefault('LOGIN_BLOCK_TIME', 900)
        self.failed_logins = FailedLogins(
            app.config['LOGIN_MAX_FAILURES'],
            app.config['LOGIN_FAILURE_WINDOW'],
            app.config['LOGIN_BLOCK_TIME']
        )

        # Load up user database
        try:
            self.load_users(app)
        except IOError:
            log.critical(
                'No htpasswd file loaded, please set `FLASK_HTPASSWD`'
                'or `FLASK_HTPASSWD_PATH` environment variable to a '
                'valid apache htpasswd file.'
            )

        # Allow requiring auth for entire app, with pre request method
        @app.before_request
        def require_auth():
            # pylint: disable=unused-variable
            """Pre request processing for enabling full app authentication."""
            if not current_app.config['FLASK_AUTH_ALL']:
                return None
            is_valid, user = self.authenticate()
            if not is_valid:
                return self.auth_failed()
            g.user = user

    def load_users(self, app):
        """
        Load users from configured file.

        Args:
            app (flask.Flask): Flask application to load users from.

        Raises:
            IOError: If the configured htpasswd file does not exist.
        Returns:
            None
        """
        self.users = HtpasswdFile(
            app.config['FLASK_HTPASSWD_PATH']
        )

    def check_basic_auth(self, username, password):
        """
        This function is called to check if a username /
        password combination is valid via the htpasswd file.
        """
        if not isinstance(username, str) or not isinstance(password, str):
            return (False, None)  # not a complete Basic login: refused, not an error
        valid = self.users.check_password(
            username, password
        )
        if not valid:
            user_ip = client_address(request.environ)
            log.warning('Invalid login from %s at %s'%(username, user_ip))
            valid = False
        return (
            valid,
            username
        )

    @staticmethod
    def get_signature():
        """
        Setup crypto sig.
        """
        return Serializer.serialize(current_app.config['FLASK_SECRET'])

    def get_hashhash(self, username):
        """
        Generate a digest of the htpasswd hash
        """
        return hashlib.sha256(
            self.users.get_hash(username)
        ).hexdigest()

    def generate_token(self, username):
        """
        assumes user exists in htpasswd file.

        Return the token for the given user by signing a token of
        the username and a hash of the htpasswd string.
        """
        serializer = self.get_signature()
        return serializer.dumps(
            {
                'username': username,
                'hashhash': self.get_hashhash(username)
            }
        ).decode('UTF-8')

    def check_token_auth(self, token):
        """
        Check to see who this is and if their token gets
        them into the system.
        """
        serializer = self.get_signature()

        try:
            data = serializer.loads(token)
        except BadSignature:
            log.warning('Received bad token signature')
            return False, None
        if data['username'] not in self.users.users():
            log.warning(
                'Token auth signed message, but invalid user %s',
                data['username']
            )
            return False, None
        if data['hashhash'] != self.get_hashhash(data['username']):
            log.warning(
                'Token and password do not match, %s '
                'needs to regenerate token',
                data['username']
            )
            return False, None
        return True, data['username']

    @staticmethod
    def auth_failed():
        """
        Sends a 401 response that enables basic auth
        (429 when the address is refused after too many failed logins)
        """
        retry_after = g.get('login_retry_after')
        if retry_after:
            minutes = math.ceil(retry_after / 60)
            r = make_response('Too many failed logins. Try again in {0} minute{1}.\n'.format(
                minutes, '' if minutes == 1 else 's'), 429)
            r.headers.set('Retry-After', str(retry_after))
            return r
        if request.endpoint == "auth":
            r = make_response()
            r.status_code = 401
            r.headers.set('WWW-Authenticate', 'basic realm="{0}"'.format(current_app.config['FLASK_AUTH_REALM']))
            return r
        else:
            return abort(401)

    def authenticate(self):
        """Authenticate user by any means and return either true or false.

        Args:

        Returns:
            tuple (is_valid, username): True is valid user, False if not
        """
        address = client_address(request.environ)
        retry_after = self.failed_logins.retry_after(address)
        if retry_after:  # refused for now: the password is not even checked
            g.login_retry_after = retry_after
            return (False, None)
        basic_auth = request.authorization
        is_valid = False
        user = None
        credentials = None
        # Werkzeug also parses other schemes ("token ...", "Bearer ..."): only Basic carries a password
        if basic_auth and getattr(basic_auth, "type", "basic") == "basic":
            credentials = ['basic', basic_auth.username, basic_auth.password]
            is_valid, user = self.check_basic_auth(
                basic_auth.username, basic_auth.password
            )
        else:  # Try token auth
            token = request.headers.get('Authorization', None)
            param_token = request.args.get('access_token')
            if token or param_token:
                if token:
                    # slice the 'token ' piece of the header (following
                    # github style):
                    token = token[6:]
                else:
                    # Grab it from query dict instead
                    token = param_token
                log.debug('Received token: %s', token)

                credentials = ['token', token]
                try:
                    is_valid, user = self.check_token_auth(token)
                except Exception:  # a broken token check must refuse, never fail open or crash the caller
                    log.warning('Token authentication failed', exc_info=True)
                    is_valid, user = False, None
        if credentials and not is_valid and self.failed_logins.failed(address, credentials):
            log.warning('Too many failed logins from %s: refused for %d minutes',
                        address, math.ceil(self.failed_logins.block_time / 60))
        return (is_valid, user)

    def required(self, func):
        """
        Decorator function with basic and token authentication handler
        """
        @wraps(func)
        def decorated(*args, **kwargs):
            """
            Actual wrapper to run the auth checks.
            """
            is_valid, user = self.authenticate()
            if not is_valid:
                return self.auth_failed()
            g.user = user  # templates (the sidebar) can tell who is logged in
            kwargs['user'] = user
            return func(*args, **kwargs)
        return decorated
