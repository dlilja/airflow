# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# mypy: disable-error-code=var-annotated
from __future__ import annotations

import datetime
import logging
from typing import Any
from uuid import uuid4

import re2
from flask import Flask, g, session, url_for
from flask_appbuilder import AppBuilder
from flask_appbuilder.const import (
    AUTH_DB,
    AUTH_LDAP,
    LOGMSG_ERR_SEC_ADD_REGISTER_USER,
    LOGMSG_ERR_SEC_AUTH_LDAP,
    LOGMSG_ERR_SEC_AUTH_LDAP_TLS,
    LOGMSG_WAR_SEC_LOGIN_FAILED,
    LOGMSG_WAR_SEC_NOLDAP_OBJ,
)
from flask_appbuilder.security.registerviews import (
    RegisterUserDBView,
    RegisterUserOAuthView,
    RegisterUserOIDView,
)
from flask_appbuilder.security.views import (
    AuthDBView,
    AuthLDAPView,
    AuthOAuthView,
    AuthOIDView,
    AuthRemoteUserView,
    PermissionModelView,
    RegisterUserModelView,
    ResetMyPasswordView,
    ResetPasswordView,
    RoleModelView,
    UserInfoEditView,
    UserLDAPModelView,
    UserOAuthModelView,
    UserOIDModelView,
    UserRemoteUserModelView,
    UserStatsChartView,
)
from flask_jwt_extended import current_user as current_user_jwt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash

from airflow.auth.managers.fab.models import Action, Permission, RegisterUser, Resource, Role, User
from airflow.configuration import conf
from airflow.www.extensions.init_auth_manager import get_auth_manager

# This product contains a modified portion of 'Flask App Builder' developed by Daniel Vaz Gaspar.
# (https://github.com/dpgaspar/Flask-AppBuilder).
# Copyright 2013, Daniel Vaz Gaspar
log = logging.getLogger(__name__)


def _oauth_tokengetter(token=None):
    """Default function to return the current user oauth token from session cookie."""
    token = session.get("oauth")
    log.debug("Token Get: %s", token)
    return token


class BaseSecurityManager:
    """Base class to define the Security Manager interface."""

    appbuilder: AppBuilder
    """The appbuilder instance for the current security manager."""
    auth_view = None
    """ The obj instance for authentication view """
    user_view = None
    """ The obj instance for user view """
    registeruser_view = None
    """ The obj instance for registering user view """
    lm = None
    """ Flask-Login LoginManager """
    jwt_manager = None
    """ Flask-JWT-Extended """
    oid = None
    """ Flask-OpenID OpenID """
    oauth = None
    """ Flask-OAuth """
    oauth_remotes: dict[str, Any]
    """ OAuth email whitelists """

    oauth_user_info = None

    user_model: type[User]
    """ Override to set your own User Model """
    role_model: type[Role]
    """ Override to set your own Role Model """
    action_model: type[Action]
    """ Override to set your own Action Model """
    resource_model: type[Resource]
    """ Override to set your own Resource Model """
    permission_model: type[Permission]
    """ Override to set your own Permission Model """
    registeruser_model: type[RegisterUser]
    """ Override to set your own RegisterUser Model """

    userldapmodelview = UserLDAPModelView
    """ Override if you want your own user ldap view """
    useroidmodelview = UserOIDModelView
    """ Override if you want your own user OID view """
    useroauthmodelview = UserOAuthModelView
    """ Override if you want your own user OAuth view """
    userremoteusermodelview = UserRemoteUserModelView
    """ Override if you want your own user REMOTE_USER view """
    registerusermodelview = RegisterUserModelView

    authdbview = AuthDBView
    """ Override if you want your own Authentication DB view """
    authldapview = AuthLDAPView
    """ Override if you want your own Authentication LDAP view """
    authoidview = AuthOIDView
    """ Override if you want your own Authentication OID view """
    authoauthview = AuthOAuthView
    """ Override if you want your own Authentication OAuth view """
    authremoteuserview = AuthRemoteUserView
    """ Override if you want your own Authentication REMOTE_USER view """

    registeruserdbview = RegisterUserDBView
    """ Override if you want your own register user db view """
    registeruseroidview = RegisterUserOIDView
    """ Override if you want your own register user OpenID view """
    registeruseroauthview = RegisterUserOAuthView
    """ Override if you want your own register user OAuth view """

    resetmypasswordview = ResetMyPasswordView
    """ Override if you want your own reset my password view """
    resetpasswordview = ResetPasswordView
    """ Override if you want your own reset password view """
    userinfoeditview = UserInfoEditView
    """ Override if you want your own User information edit view """

    rolemodelview = RoleModelView
    actionmodelview = PermissionModelView
    userstatschartview = UserStatsChartView
    permissionmodelview = PermissionModelView

    def __init__(self, appbuilder):
        self.appbuilder = appbuilder
        app = self.appbuilder.get_app

        # Setup Flask-Limiter
        self.limiter = self.create_limiter(app)

    def create_limiter(self, app: Flask) -> Limiter:
        limiter = Limiter(key_func=get_remote_address)
        limiter.init_app(app)
        return limiter

    def get_roles_from_keys(self, role_keys: list[str]) -> set[Role]:
        """
        Construct a list of FAB role objects, from a list of keys.

        NOTE:
        - keys are things like: "LDAP group DNs" or "OAUTH group names"
        - we use AUTH_ROLES_MAPPING to map from keys, to FAB role names

        :param role_keys: the list of FAB role keys
        :return: a list of Role
        """
        _roles = set()
        _role_keys = set(role_keys)
        for role_key, fab_role_names in self.auth_roles_mapping.items():
            if role_key in _role_keys:
                for fab_role_name in fab_role_names:
                    fab_role = self.find_role(fab_role_name)
                    if fab_role:
                        _roles.add(fab_role)
                    else:
                        log.warning("Can't find role specified in AUTH_ROLES_MAPPING: %s", fab_role_name)
        return _roles

    @property
    def auth_type_provider_name(self):
        provider_to_auth_type = {AUTH_DB: "db", AUTH_LDAP: "ldap"}
        return provider_to_auth_type.get(self.auth_type)

    @property
    def get_url_for_registeruser(self):
        """Gets the URL for Register User."""
        return url_for(f"{self.registeruser_view.endpoint}.{self.registeruser_view.default_view}")

    @property
    def get_user_datamodel(self):
        """Gets the User data model."""
        return self.user_view.datamodel

    @property
    def get_register_user_datamodel(self):
        """Gets the Register User data model."""
        return self.registerusermodelview.datamodel

    @property
    def builtin_roles(self):
        """Get the builtin roles."""
        return self._builtin_roles

    @property
    def api_login_allow_multiple_providers(self):
        return self.appbuilder.get_app.config["AUTH_API_LOGIN_ALLOW_MULTIPLE_PROVIDERS"]

    @property
    def auth_username_ci(self):
        """Gets the auth username for CI."""
        return self.appbuilder.get_app.config.get("AUTH_USERNAME_CI", True)

    @property
    def auth_role_admin(self):
        """Gets the admin role."""
        return self.appbuilder.get_app.config["AUTH_ROLE_ADMIN"]

    @property
    def auth_ldap_server(self):
        """Gets the LDAP server object."""
        return self.appbuilder.get_app.config["AUTH_LDAP_SERVER"]

    @property
    def auth_ldap_use_tls(self):
        """Should LDAP use TLS."""
        return self.appbuilder.get_app.config["AUTH_LDAP_USE_TLS"]

    @property
    def auth_user_registration(self):
        """Will user self registration be allowed."""
        return self.appbuilder.get_app.config["AUTH_USER_REGISTRATION"]

    @property
    def auth_user_registration_role(self):
        """The default user self registration role."""
        return self.appbuilder.get_app.config["AUTH_USER_REGISTRATION_ROLE"]

    @property
    def auth_user_registration_role_jmespath(self) -> str:
        """The JMESPATH role to use for user registration."""
        return self.appbuilder.get_app.config["AUTH_USER_REGISTRATION_ROLE_JMESPATH"]

    @property
    def auth_roles_mapping(self) -> dict[str, list[str]]:
        """The mapping of auth roles."""
        return self.appbuilder.get_app.config["AUTH_ROLES_MAPPING"]

    @property
    def auth_roles_sync_at_login(self) -> bool:
        """Should roles be synced at login."""
        return self.appbuilder.get_app.config["AUTH_ROLES_SYNC_AT_LOGIN"]

    @property
    def auth_ldap_search(self):
        """LDAP search object."""
        return self.appbuilder.get_app.config["AUTH_LDAP_SEARCH"]

    @property
    def auth_ldap_search_filter(self):
        """LDAP search filter."""
        return self.appbuilder.get_app.config["AUTH_LDAP_SEARCH_FILTER"]

    @property
    def auth_ldap_bind_user(self):
        """LDAP bind user."""
        return self.appbuilder.get_app.config["AUTH_LDAP_BIND_USER"]

    @property
    def auth_ldap_bind_password(self):
        """LDAP bind password."""
        return self.appbuilder.get_app.config["AUTH_LDAP_BIND_PASSWORD"]

    @property
    def auth_ldap_append_domain(self):
        """LDAP append domain."""
        return self.appbuilder.get_app.config["AUTH_LDAP_APPEND_DOMAIN"]

    @property
    def auth_ldap_username_format(self):
        """LDAP username format."""
        return self.appbuilder.get_app.config["AUTH_LDAP_USERNAME_FORMAT"]

    @property
    def auth_ldap_uid_field(self):
        """LDAP UID field."""
        return self.appbuilder.get_app.config["AUTH_LDAP_UID_FIELD"]

    @property
    def auth_ldap_group_field(self) -> str:
        """LDAP group field."""
        return self.appbuilder.get_app.config["AUTH_LDAP_GROUP_FIELD"]

    @property
    def auth_ldap_firstname_field(self):
        """LDAP first name field."""
        return self.appbuilder.get_app.config["AUTH_LDAP_FIRSTNAME_FIELD"]

    @property
    def auth_ldap_lastname_field(self):
        """LDAP last name field."""
        return self.appbuilder.get_app.config["AUTH_LDAP_LASTNAME_FIELD"]

    @property
    def auth_ldap_email_field(self):
        """LDAP email field."""
        return self.appbuilder.get_app.config["AUTH_LDAP_EMAIL_FIELD"]

    @property
    def auth_ldap_bind_first(self):
        """LDAP bind first."""
        return self.appbuilder.get_app.config["AUTH_LDAP_BIND_FIRST"]

    @property
    def auth_ldap_allow_self_signed(self):
        """LDAP allow self signed."""
        return self.appbuilder.get_app.config["AUTH_LDAP_ALLOW_SELF_SIGNED"]

    @property
    def auth_ldap_tls_demand(self):
        """LDAP TLS demand."""
        return self.appbuilder.get_app.config["AUTH_LDAP_TLS_DEMAND"]

    @property
    def auth_ldap_tls_cacertdir(self):
        """LDAP TLS CA certificate directory."""
        return self.appbuilder.get_app.config["AUTH_LDAP_TLS_CACERTDIR"]

    @property
    def auth_ldap_tls_cacertfile(self):
        """LDAP TLS CA certificate file."""
        return self.appbuilder.get_app.config["AUTH_LDAP_TLS_CACERTFILE"]

    @property
    def auth_ldap_tls_certfile(self):
        """LDAP TLS certificate file."""
        return self.appbuilder.get_app.config["AUTH_LDAP_TLS_CERTFILE"]

    @property
    def auth_ldap_tls_keyfile(self):
        """LDAP TLS key file."""
        return self.appbuilder.get_app.config["AUTH_LDAP_TLS_KEYFILE"]

    @property
    def openid_providers(self):
        """Openid providers."""
        return self.appbuilder.get_app.config["OPENID_PROVIDERS"]

    @property
    def current_user(self):
        """Current user object."""
        if get_auth_manager().is_logged_in():
            return g.user
        elif current_user_jwt:
            return current_user_jwt

    def oauth_user_info_getter(self, f):
        """
        Decorator function to be the OAuth user info getter for all the providers.

        Receives provider and response return a dict with the information returned from the provider.
        The returned user info dict should have it's keys with the same name as the User Model.

        Use it like this an example for GitHub ::

            @appbuilder.sm.oauth_user_info_getter
            def my_oauth_user_info(sm, provider, response=None):
                if provider == 'github':
                    me = sm.oauth_remotes[provider].get('user')
                    return {'username': me.data.get('login')}
                else:
                    return {}
        """

        def wraps(provider, response=None):
            ret = f(self, provider, response=response)
            # Checks if decorator is well behaved and returns a dict as supposed.
            if not type(ret) == dict:
                log.error("OAuth user info decorated function did not returned a dict, but: %s", type(ret))
                return {}
            return ret

        self.oauth_user_info = wraps
        return wraps

    def get_oauth_token_key_name(self, provider):
        """
        Returns the token_key name for the oauth provider.

        If none is configured defaults to oauth_token
        this is configured using OAUTH_PROVIDERS and token_key key.
        """
        for _provider in self.oauth_providers:
            if _provider["name"] == provider:
                return _provider.get("token_key", "oauth_token")

    def get_oauth_token_secret_name(self, provider):
        """Gety the ``token_secret`` name for the oauth provider.

        If none is configured, defaults to ``oauth_secret``. This is configured
        using ``OAUTH_PROVIDERS`` and ``token_secret``.
        """
        for _provider in self.oauth_providers:
            if _provider["name"] == provider:
                return _provider.get("token_secret", "oauth_token_secret")

    def set_oauth_session(self, provider, oauth_response):
        """Set the current session with OAuth user secrets."""
        # Get this provider key names for token_key and token_secret
        token_key = self.appbuilder.sm.get_oauth_token_key_name(provider)
        token_secret = self.appbuilder.sm.get_oauth_token_secret_name(provider)
        # Save users token on encrypted session cookie
        session["oauth"] = (
            oauth_response[token_key],
            oauth_response.get(token_secret, ""),
        )
        session["oauth_provider"] = provider

    def update_user_auth_stat(self, user, success=True):
        """Update user authentication stats.

        This is done upon successful/unsuccessful authentication attempts.

        :param user:
            The identified (but possibly not successfully authenticated) user
            model
        :param success:
            Defaults to true, if true increments login_count, updates
            last_login, and resets fail_login_count to 0, if false increments
            fail_login_count on user model.
        """
        if not user.login_count:
            user.login_count = 0
        if not user.fail_login_count:
            user.fail_login_count = 0
        if success:
            user.login_count += 1
            user.last_login = datetime.datetime.now()
            user.fail_login_count = 0
        else:
            user.fail_login_count += 1
        self.update_user(user)

    def _rotate_session_id(self):
        """Rotate the session ID.

        We need to do this upon successful authentication when using the
        database session backend.
        """
        if conf.get("webserver", "SESSION_BACKEND") == "database":
            session.sid = str(uuid4())

    def auth_user_db(self, username, password):
        """
        Method for authenticating user, auth db style.

        :param username:
            The username or registered email address
        :param password:
            The password, will be tested against hashed password on db
        """
        if username is None or username == "":
            return None
        user = self.find_user(username=username)
        if user is None:
            user = self.find_user(email=username)
        if user is None or (not user.is_active):
            # Balance failure and success
            check_password_hash(
                "pbkdf2:sha256:150000$Z3t6fmj2$22da622d94a1f8118"
                "c0976a03d2f18f680bfff877c9a965db9eedc51bc0be87c",
                "password",
            )
            log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(username))
            return None
        elif check_password_hash(user.password, password):
            self._rotate_session_id()
            self.update_user_auth_stat(user, True)
            return user
        else:
            self.update_user_auth_stat(user, False)
            log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(username))
            return None

    def _search_ldap(self, ldap, con, username):
        """
        Searches LDAP for user.

        :param ldap: The ldap module reference
        :param con: The ldap connection
        :param username: username to match with AUTH_LDAP_UID_FIELD
        :return: ldap object array
        """
        # always check AUTH_LDAP_SEARCH is set before calling this method
        assert self.auth_ldap_search, "AUTH_LDAP_SEARCH must be set"

        # build the filter string for the LDAP search
        if self.auth_ldap_search_filter:
            filter_str = f"(&{self.auth_ldap_search_filter}({self.auth_ldap_uid_field}={username}))"
        else:
            filter_str = f"({self.auth_ldap_uid_field}={username})"

        # build what fields to request in the LDAP search
        request_fields = [
            self.auth_ldap_firstname_field,
            self.auth_ldap_lastname_field,
            self.auth_ldap_email_field,
        ]
        if self.auth_roles_mapping:
            request_fields.append(self.auth_ldap_group_field)

        # perform the LDAP search
        log.debug(
            "LDAP search for %r with fields %s in scope %r", filter_str, request_fields, self.auth_ldap_search
        )
        raw_search_result = con.search_s(
            self.auth_ldap_search, ldap.SCOPE_SUBTREE, filter_str, request_fields
        )
        log.debug("LDAP search returned: %s", raw_search_result)

        # Remove any search referrals from results
        search_result = [
            (dn, attrs) for dn, attrs in raw_search_result if dn is not None and isinstance(attrs, dict)
        ]

        # only continue if 0 or 1 results were returned
        if len(search_result) > 1:
            log.error(
                "LDAP search for %r in scope '%a' returned multiple results",
                self.auth_ldap_search,
                filter_str,
            )
            return None, None

        try:
            # extract the DN
            user_dn = search_result[0][0]
            # extract the other attributes
            user_info = search_result[0][1]
            # return
            return user_dn, user_info
        except (IndexError, NameError):
            return None, None

    def _ldap_calculate_user_roles(self, user_attributes: dict[str, list[bytes]]) -> list[str]:
        user_role_objects = set()

        # apply AUTH_ROLES_MAPPING
        if self.auth_roles_mapping:
            user_role_keys = self.ldap_extract_list(user_attributes, self.auth_ldap_group_field)
            user_role_objects.update(self.get_roles_from_keys(user_role_keys))

        # apply AUTH_USER_REGISTRATION
        if self.auth_user_registration:
            registration_role_name = self.auth_user_registration_role

            # lookup registration role in flask db
            fab_role = self.find_role(registration_role_name)
            if fab_role:
                user_role_objects.add(fab_role)
            else:
                log.warning("Can't find AUTH_USER_REGISTRATION role: %s", registration_role_name)

        return list(user_role_objects)

    def _ldap_bind_indirect(self, ldap, con) -> None:
        """
        Attempt to bind to LDAP using the AUTH_LDAP_BIND_USER.

        :param ldap: The ldap module reference
        :param con: The ldap connection
        """
        # always check AUTH_LDAP_BIND_USER is set before calling this method
        assert self.auth_ldap_bind_user, "AUTH_LDAP_BIND_USER must be set"

        try:
            log.debug("LDAP bind indirect TRY with username: %r", self.auth_ldap_bind_user)
            con.simple_bind_s(self.auth_ldap_bind_user, self.auth_ldap_bind_password)
            log.debug("LDAP bind indirect SUCCESS with username: %r", self.auth_ldap_bind_user)
        except ldap.INVALID_CREDENTIALS as ex:
            log.error("AUTH_LDAP_BIND_USER and AUTH_LDAP_BIND_PASSWORD are not valid LDAP bind credentials")
            raise ex

    @staticmethod
    def _ldap_bind(ldap, con, dn: str, password: str) -> bool:
        """Validates/binds the provided dn/password with the LDAP sever."""
        try:
            log.debug("LDAP bind TRY with username: %r", dn)
            con.simple_bind_s(dn, password)
            log.debug("LDAP bind SUCCESS with username: %r", dn)
            return True
        except ldap.INVALID_CREDENTIALS:
            return False

    @staticmethod
    def ldap_extract(ldap_dict: dict[str, list[bytes]], field_name: str, fallback: str) -> str:
        raw_value = ldap_dict.get(field_name, [b""])
        # decode - if empty string, default to fallback, otherwise take first element
        return raw_value[0].decode("utf-8") or fallback

    @staticmethod
    def ldap_extract_list(ldap_dict: dict[str, list[bytes]], field_name: str) -> list[str]:
        raw_list = ldap_dict.get(field_name, [])
        # decode - removing empty strings
        return [x.decode("utf-8") for x in raw_list if x.decode("utf-8")]

    def auth_user_ldap(self, username, password):
        """
        Method for authenticating user with LDAP.

        NOTE: this depends on python-ldap module.

        :param username: the username
        :param password: the password
        """
        # If no username is provided, go away
        if (username is None) or username == "":
            return None

        # Search the DB for this user
        user = self.find_user(username=username)

        # If user is not active, go away
        if user and (not user.is_active):
            return None

        # If user is not registered, and not self-registration, go away
        if (not user) and (not self.auth_user_registration):
            return None

        # Ensure python-ldap is installed
        try:
            import ldap
        except ImportError:
            log.error("python-ldap library is not installed")
            return None

        try:
            # LDAP certificate settings
            if self.auth_ldap_tls_cacertdir:
                ldap.set_option(ldap.OPT_X_TLS_CACERTDIR, self.auth_ldap_tls_cacertdir)
            if self.auth_ldap_tls_cacertfile:
                ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, self.auth_ldap_tls_cacertfile)
            if self.auth_ldap_tls_certfile:
                ldap.set_option(ldap.OPT_X_TLS_CERTFILE, self.auth_ldap_tls_certfile)
            if self.auth_ldap_tls_keyfile:
                ldap.set_option(ldap.OPT_X_TLS_KEYFILE, self.auth_ldap_tls_keyfile)
            if self.auth_ldap_allow_self_signed:
                ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_ALLOW)
                ldap.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
            elif self.auth_ldap_tls_demand:
                ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
                ldap.set_option(ldap.OPT_X_TLS_NEWCTX, 0)

            # Initialise LDAP connection
            con = ldap.initialize(self.auth_ldap_server)
            con.set_option(ldap.OPT_REFERRALS, 0)
            if self.auth_ldap_use_tls:
                try:
                    con.start_tls_s()
                except Exception:
                    log.error(LOGMSG_ERR_SEC_AUTH_LDAP_TLS.format(self.auth_ldap_server))
                    return None

            # Define variables, so we can check if they are set in later steps
            user_dn = None
            user_attributes = {}

            # Flow 1 - (Indirect Search Bind):
            #  - in this flow, special bind credentials are used to perform the
            #    LDAP search
            #  - in this flow, AUTH_LDAP_SEARCH must be set
            if self.auth_ldap_bind_user:
                # Bind with AUTH_LDAP_BIND_USER/AUTH_LDAP_BIND_PASSWORD
                # (authorizes for LDAP search)
                self._ldap_bind_indirect(ldap, con)

                # Search for `username`
                #  - returns the `user_dn` needed for binding to validate credentials
                #  - returns the `user_attributes` needed for
                #    AUTH_USER_REGISTRATION/AUTH_ROLES_SYNC_AT_LOGIN
                if self.auth_ldap_search:
                    user_dn, user_attributes = self._search_ldap(ldap, con, username)
                else:
                    log.error("AUTH_LDAP_SEARCH must be set when using AUTH_LDAP_BIND_USER")
                    return None

                # If search failed, go away
                if user_dn is None:
                    log.info(LOGMSG_WAR_SEC_NOLDAP_OBJ.format(username))
                    return None

                # Bind with user_dn/password (validates credentials)
                if not self._ldap_bind(ldap, con, user_dn, password):
                    if user:
                        self.update_user_auth_stat(user, False)

                    # Invalid credentials, go away
                    log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(username))
                    return None

            # Flow 2 - (Direct Search Bind):
            #  - in this flow, the credentials provided by the end-user are used
            #    to perform the LDAP search
            #  - in this flow, we only search LDAP if AUTH_LDAP_SEARCH is set
            #     - features like AUTH_USER_REGISTRATION & AUTH_ROLES_SYNC_AT_LOGIN
            #       will only work if AUTH_LDAP_SEARCH is set
            else:
                # Copy the provided username (so we can apply formatters)
                bind_username = username

                # update `bind_username` by applying AUTH_LDAP_APPEND_DOMAIN
                #  - for Microsoft AD, which allows binding with userPrincipalName
                if self.auth_ldap_append_domain:
                    bind_username = bind_username + "@" + self.auth_ldap_append_domain

                # Update `bind_username` by applying AUTH_LDAP_USERNAME_FORMAT
                #  - for transforming the username into a DN,
                #    for example: "uid=%s,ou=example,o=test"
                if self.auth_ldap_username_format:
                    bind_username = self.auth_ldap_username_format % bind_username

                # Bind with bind_username/password
                # (validates credentials & authorizes for LDAP search)
                if not self._ldap_bind(ldap, con, bind_username, password):
                    if user:
                        self.update_user_auth_stat(user, False)

                    # Invalid credentials, go away
                    log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(bind_username))
                    return None

                # Search for `username` (if AUTH_LDAP_SEARCH is set)
                #  - returns the `user_attributes`
                #    needed for AUTH_USER_REGISTRATION/AUTH_ROLES_SYNC_AT_LOGIN
                #  - we search on `username` not `bind_username`,
                #    because AUTH_LDAP_APPEND_DOMAIN and AUTH_LDAP_USERNAME_FORMAT
                #    would result in an invalid search filter
                if self.auth_ldap_search:
                    user_dn, user_attributes = self._search_ldap(ldap, con, username)

                    # If search failed, go away
                    if user_dn is None:
                        log.info(LOGMSG_WAR_SEC_NOLDAP_OBJ.format(username))
                        return None

            # Sync the user's roles
            if user and user_attributes and self.auth_roles_sync_at_login:
                user.roles = self._ldap_calculate_user_roles(user_attributes)
                log.debug("Calculated new roles for user=%r as: %s", user_dn, user.roles)

            # If the user is new, register them
            if (not user) and user_attributes and self.auth_user_registration:
                user = self.add_user(
                    username=username,
                    first_name=self.ldap_extract(user_attributes, self.auth_ldap_firstname_field, ""),
                    last_name=self.ldap_extract(user_attributes, self.auth_ldap_lastname_field, ""),
                    email=self.ldap_extract(
                        user_attributes,
                        self.auth_ldap_email_field,
                        f"{username}@email.notfound",
                    ),
                    role=self._ldap_calculate_user_roles(user_attributes),
                )
                log.debug("New user registered: %s", user)

                # If user registration failed, go away
                if not user:
                    log.info(LOGMSG_ERR_SEC_ADD_REGISTER_USER.format(username))
                    return None

            # LOGIN SUCCESS (only if user is now registered)
            if user:
                self._rotate_session_id()
                self.update_user_auth_stat(user)
                return user
            else:
                return None

        except ldap.LDAPError as e:
            msg = None
            if isinstance(e, dict):
                msg = getattr(e, "message", None)
            if (msg is not None) and ("desc" in msg):
                log.error(LOGMSG_ERR_SEC_AUTH_LDAP.format(e.message["desc"]))
                return None
            else:
                log.error(e)
                return None

    def auth_user_oid(self, email):
        """
        Openid user Authentication.

        :param email: user's email to authenticate
        """
        user = self.find_user(email=email)
        if user is None or (not user.is_active):
            log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(email))
            return None
        else:
            self._rotate_session_id()
            self.update_user_auth_stat(user)
            return user

    def auth_user_remote_user(self, username):
        """
        REMOTE_USER user Authentication.

        :param username: user's username for remote auth
        """
        user = self.find_user(username=username)

        # User does not exist, create one if auto user registration.
        if user is None and self.auth_user_registration:
            user = self.add_user(
                # All we have is REMOTE_USER, so we set
                # the other fields to blank.
                username=username,
                first_name=username,
                last_name="-",
                email=username + "@email.notfound",
                role=self.find_role(self.auth_user_registration_role),
            )

        # If user does not exist on the DB and not auto user registration,
        # or user is inactive, go away.
        elif user is None or (not user.is_active):
            log.info(LOGMSG_WAR_SEC_LOGIN_FAILED.format(username))
            return None

        self._rotate_session_id()
        self.update_user_auth_stat(user)
        return user

    def _oauth_calculate_user_roles(self, userinfo) -> list[str]:
        user_role_objects = set()

        # apply AUTH_ROLES_MAPPING
        if self.auth_roles_mapping:
            user_role_keys = userinfo.get("role_keys", [])
            user_role_objects.update(self.get_roles_from_keys(user_role_keys))

        # apply AUTH_USER_REGISTRATION_ROLE
        if self.auth_user_registration:
            registration_role_name = self.auth_user_registration_role

            # if AUTH_USER_REGISTRATION_ROLE_JMESPATH is set,
            # use it for the registration role
            if self.auth_user_registration_role_jmespath:
                import jmespath

                registration_role_name = jmespath.search(self.auth_user_registration_role_jmespath, userinfo)

            # lookup registration role in flask db
            fab_role = self.find_role(registration_role_name)
            if fab_role:
                user_role_objects.add(fab_role)
            else:
                log.warning("Can't find AUTH_USER_REGISTRATION role: %s", registration_role_name)

        return list(user_role_objects)

    def auth_user_oauth(self, userinfo):
        """
        Method for authenticating user with OAuth.

        :userinfo: dict with user information
                   (keys are the same as User model columns)
        """
        # extract the username from `userinfo`
        if "username" in userinfo:
            username = userinfo["username"]
        elif "email" in userinfo:
            username = userinfo["email"]
        else:
            log.error("OAUTH userinfo does not have username or email %s", userinfo)
            return None

        # If username is empty, go away
        if (username is None) or username == "":
            return None

        # Search the DB for this user
        user = self.find_user(username=username)

        # If user is not active, go away
        if user and (not user.is_active):
            return None

        # If user is not registered, and not self-registration, go away
        if (not user) and (not self.auth_user_registration):
            return None

        # Sync the user's roles
        if user and self.auth_roles_sync_at_login:
            user.roles = self._oauth_calculate_user_roles(userinfo)
            log.debug("Calculated new roles for user=%r as: %s", username, user.roles)

        # If the user is new, register them
        if (not user) and self.auth_user_registration:
            user = self.add_user(
                username=username,
                first_name=userinfo.get("first_name", ""),
                last_name=userinfo.get("last_name", ""),
                email=userinfo.get("email", "") or f"{username}@email.notfound",
                role=self._oauth_calculate_user_roles(userinfo),
            )
            log.debug("New user registered: %s", user)

            # If user registration failed, go away
            if not user:
                log.error("Error creating a new OAuth user %s", username)
                return None

        # LOGIN SUCCESS (only if user is now registered)
        if user:
            self._rotate_session_id()
            self.update_user_auth_stat(user)
            return user
        else:
            return None

    def _has_access_builtin_roles(self, role, action_name: str, resource_name: str) -> bool:
        """Checks permission on builtin role."""
        perms = self.builtin_roles.get(role.name, [])
        for _resource_name, _action_name in perms:
            if re2.match(_resource_name, resource_name) and re2.match(_action_name, action_name):
                return True
        return False

    def _get_user_permission_resources(
        self, user: User | None, action_name: str, resource_names: list[str] | None = None
    ) -> set[str]:
        """Get resource names with a certain action name that a user has access to.

        Mainly used to fetch all menu permissions on a single db call, will also
        check public permissions and builtin roles
        """
        if not resource_names:
            resource_names = []

        db_role_ids = []
        if user is None:
            # include public role
            roles = [self.get_public_role()]
        else:
            roles = user.roles
        # First check against builtin (statically configured) roles
        # because no database query is needed
        result = set()
        for role in roles:
            if role.name in self.builtin_roles:
                for resource_name in resource_names:
                    if self._has_access_builtin_roles(role, action_name, resource_name):
                        result.add(resource_name)
            else:
                db_role_ids.append(role.id)
        # Then check against database-stored roles
        role_resource_names = [
            perm.resource.name for perm in self.filter_roles_by_perm_with_action(action_name, db_role_ids)
        ]
        result.update(role_resource_names)
        return result

    def get_user_menu_access(self, menu_names: list[str] | None = None) -> set[str]:
        if get_auth_manager().is_logged_in():
            return self._get_user_permission_resources(g.user, "menu_access", resource_names=menu_names)
        elif current_user_jwt:
            return self._get_user_permission_resources(
                # the current_user_jwt is a lazy proxy, so we need to ignore type checking
                current_user_jwt,  # type: ignore[arg-type]
                "menu_access",
                resource_names=menu_names,
            )
        else:
            return self._get_user_permission_resources(None, "menu_access", resource_names=menu_names)

    def add_limit_view(self, baseview):
        if not baseview.limits:
            return

        for limit in baseview.limits:
            self.limiter.limit(
                limit_value=limit.limit_value,
                key_func=limit.key_func,
                per_method=limit.per_method,
                methods=limit.methods,
                error_message=limit.error_message,
                exempt_when=limit.exempt_when,
                override_defaults=limit.override_defaults,
                deduct_when=limit.deduct_when,
                on_breach=limit.on_breach,
                cost=limit.cost,
            )(baseview.blueprint)

    def add_permissions_view(self, base_action_names, resource_name):  # Keep name for compatibility with FAB.
        """
        Adds an action on a resource to the backend.

        :param base_action_names:
            list of permissions from view (all exposed methods):
             'can_add','can_edit' etc...
        :param resource_name:
            name of the resource to add
        """
        resource = self.create_resource(resource_name)
        perms = self.get_resource_permissions(resource)

        if not perms:
            # No permissions yet on this view
            for action_name in base_action_names:
                action = self.create_permission(action_name, resource_name)
                if self.auth_role_admin not in self.builtin_roles:
                    admin_role = self.find_role(self.auth_role_admin)
                    self.add_permission_to_role(admin_role, action)
        else:
            # Permissions on this view exist but....
            admin_role = self.find_role(self.auth_role_admin)
            for action_name in base_action_names:
                # Check if base view permissions exist
                if not self.perms_include_action(perms, action_name):
                    action = self.create_permission(action_name, resource_name)
                    if self.auth_role_admin not in self.builtin_roles:
                        self.add_permission_to_role(admin_role, action)
            for perm in perms:
                if perm.action is None:
                    # Skip this perm, it has a null permission
                    continue
                if perm.action.name not in base_action_names:
                    # perm to delete
                    roles = self.get_all_roles()
                    # del permission from all roles
                    for role in roles:
                        # TODO: An action can't be removed from a role.
                        # This is a bug in FAB. It has been reported.
                        self.remove_permission_from_role(role, perm)
                    self.delete_permission(perm.action.name, resource_name)
                elif self.auth_role_admin not in self.builtin_roles and perm not in admin_role.permissions:
                    # Role Admin must have all permissions
                    self.add_permission_to_role(admin_role, perm)

    def add_permissions_menu(self, resource_name):
        """
        Adds menu_access to resource on permission_resource.

        :param resource_name:
            The resource name
        """
        self.create_resource(resource_name)
        perm = self.get_permission("menu_access", resource_name)
        if not perm:
            perm = self.create_permission("menu_access", resource_name)
        if self.auth_role_admin not in self.builtin_roles:
            role_admin = self.find_role(self.auth_role_admin)
            self.add_permission_to_role(role_admin, perm)

    def security_cleanup(self, baseviews, menus):
        """
        Will cleanup all unused permissions from the database.

        :param baseviews: A list of BaseViews class
        :param menus: Menu class
        """
        resources = self.get_all_resources()
        roles = self.get_all_roles()
        for resource in resources:
            found = False
            for baseview in baseviews:
                if resource.name == baseview.class_permission_name:
                    found = True
                    break
            if menus.find(resource.name):
                found = True
            if not found:
                permissions = self.get_resource_permissions(resource)
                for permission in permissions:
                    for role in roles:
                        self.remove_permission_from_role(role, permission)
                    self.delete_permission(permission.action.name, resource.name)
                self.delete_resource(resource.name)

    def find_user(self, username=None, email=None):
        """Generic function find a user by it's username or email."""
        raise NotImplementedError

    def get_role_permissions_from_db(self, role_id: int) -> list[Permission]:
        """Get all DB permissions from a role id."""
        raise NotImplementedError

    def add_user(self, username, first_name, last_name, email, role, password=""):
        """Generic function to create user."""
        raise NotImplementedError

    def update_user(self, user):
        """
        Generic function to update user.

        :param user: User model to update to database
        """
        raise NotImplementedError

    def find_role(self, name):
        raise NotImplementedError

    def get_all_roles(self):
        raise NotImplementedError

    def get_public_role(self):
        """Returns all permissions from public role."""
        raise NotImplementedError

    def filter_roles_by_perm_with_action(self, permission_name: str, role_ids: list[int]):
        raise NotImplementedError

    def permission_exists_in_one_or_more_roles(
        self, resource_name: str, action_name: str, role_ids: list[int]
    ) -> bool:
        """Finds and returns permission views for a group of roles."""
        raise NotImplementedError

    """
    ----------------------
     PRIMITIVES VIEW MENU
    ----------------------
    """

    def get_all_resources(self) -> list[Resource]:
        """
        Gets all existing resource records.

        :return: List of all resources
        """
        raise NotImplementedError

    def create_resource(self, name):
        """
        Create a resource with the given name.

        :param name: The name of the resource to create created.
        """
        raise NotImplementedError

    def delete_resource(self, name):
        """
        Deletes a Resource from the backend.

        :param name:
            name of the Resource
        """
        raise NotImplementedError

    """
    ----------------------
     PERMISSION VIEW MENU
    ----------------------
    """

    def get_permission(self, action_name: str, resource_name: str) -> Permission | None:
        """
        Gets a permission made with the given action->resource pair, if the permission already exists.

        :param action_name: Name of action
        :param resource_name: Name of resource
        :return: The existing permission
        """
        raise NotImplementedError

    def get_resource_permissions(self, resource) -> Permission:
        """
        Retrieve permission pairs associated with a specific resource object.

        :param resource: Object representing a single resource.
        :return: Action objects representing resource->action pair
        """
        raise NotImplementedError

    def create_permission(self, action_name: str, resource_name: str) -> Permission | None:
        """
        Creates a permission linking an action and resource.

        :param action_name: Name of existing action
        :param resource_name: Name of existing resource
        :return: Resource created
        """
        raise NotImplementedError

    def delete_permission(self, action_name: str, resource_name: str) -> None:
        """
        Deletes the permission linking an action->resource pair.

        Doesn't delete the underlying action or resource.

        :param action_name: Name of existing action
        :param resource_name: Name of existing resource
        :return: None
        """
        raise NotImplementedError

    def perms_include_action(self, perms, action_name):
        raise NotImplementedError

    def add_permission_to_role(self, role, permission) -> None:
        """
        Add an existing permission pair to a role.

        :param role: The role about to get a new permission.
        :param permission: The permission pair to add to a role.
        :return: None
        """
        raise NotImplementedError

    def remove_permission_from_role(self, role, permission) -> None:
        """
        Remove a permission pair from a role.

        :param role: User role containing permissions.
        :param permission: Object representing resource-> action pair
        """
        raise NotImplementedError

    @staticmethod
    def before_request():
        """Hook runs before request."""
        g.user = get_auth_manager().get_user()
