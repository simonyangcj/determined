import collections
import contextlib
import itertools
import json
import shutil
from pathlib import Path
from typing import Any, Iterator, List, Tuple, no_type_check
from unittest import mock

import pytest
import responses
from responses import matchers, registries

from determined.cli import cli
from determined.common.api import authentication, certs
from tests.cli import util
from tests.confdir import use_test_config_dir

MOCK_MASTER_URL = "http://localhost:8080"
AUTH_V0_PATH = Path(__file__).parent / "auth_v0.json"
UNTRUSTED_CERT_PATH = Path(__file__).parents[1] / "common" / "untrusted-root" / "127.0.0.1-ca.crt"
AUTH_JSON = {
    "version": 1,
    "masters": {
        "http://localhost:8080": {
            "active_user": "bob",
            "tokens": {
                "determined": "det.token",
                "bob": "bob.token",
            },
        }
    },
}


class ScenarioSetMeta(type):
    """
    ScenarioSetMeta helps create "scenario sets", glob-like definitions of inputs and expectations.

    Example:

        MyTestSpec(metaclass=ScenarioSetMeta):
            field_1 = {"y": True, "n": False}
            field_2 = {"h": "hello", "b": "bye"}

        # Defined dynamically by the metaclass:
        #
        #   scenariotype = namedtuple("MyTestSpecScenario", ["field_1", "field_2"])
        #
        #   def __init__(self, field_1, field_2, *expected):
        #       self.field_1 = field_1
        #       self.field_2 = field_2
        #       # Note that extra args become "expected"
        #       self.expected = expected
        #
        #   def scenarios(self):
        #       for f1 in ("yn" if self.field_1 == "*" else self.field_1):
        #           for f2 in ("hb" if self.field_2 == "*" else self.field_2):
        #               f1_val = {"y": True, "n": False}[f1]
        #               f2_val = {"h": "hello", "b": "bye"}[f2]
        #               yield scenariotype(f1_val, f2_val)
    """

    @no_type_check
    def __new__(cls: type, name: str, bases: Tuple, dct: dict) -> Any:
        # Collect the "fields", which are dict-type attributes on the class.
        fields = collections.OrderedDict()
        for field, opts in dct.items():
            if field.startswith("_"):
                continue
            if not isinstance(opts, dict):
                continue
            fields[field] = opts

        # Build the namedtuple.
        scenariotype = collections.namedtuple(  # type: ignore
            name + "Scenario", [*fields, "expected"]
        )

        # Define the __init__ method.
        def __init__(self, *args, **kwargs):
            args, expected = args[: len(fields)], args[len(fields) :]
            # Read and validate the fields passed in.
            for (field, opts), arg in zip(fields.items(), args):
                if arg != "*" and not all(a in opts for a in arg):
                    raise ValueError(
                        f"unknown option {arg!r} not in allowable set {opts!r} for field {field!r}"
                    )
                setattr(self, field, arg)
            # Also track the expected.
            self.expected = expected

        # Define the scenarios() method.
        def scenarios(self):
            iterables = []
            for field, opts in fields.items():
                val = getattr(self, field)
                if val == "*":
                    # Yield one of every valid value for this field.
                    iterables.append(opts.values())
                else:
                    # Yield the corresponding value for each field key in the val string.
                    iterables.append([opts[v] for v in val])
            yield from (scenariotype(*x, self.expected) for x in itertools.product(*iterables))

        # Also define a __repr__ method().
        def __repr__(self):
            argstr = ", ".join(repr(getattr(self, field)) for field in fields)
            return f"{name}({argstr}, expected={self.expected!r})"

        # Actually construct the dynamic class.
        dct["__init__"] = __init__
        dct["scenarios"] = scenarios
        dct["scenariotype"] = scenariotype
        dct["__repr__"] = __repr__
        return super().__new__(cls, name, bases, dct)


class ScenarioSet(metaclass=ScenarioSetMeta):
    """This class just defines some things for mypy."""

    expected: List

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def scenarios(self) -> Iterator:
        pass


class Check:
    """A result in a Login scenario indicating a check of a particular token is expected."""

    def __init__(self, token: str) -> None:
        self.token = token

    def __repr__(self) -> str:
        return f"Check({self.token!r})"


class DoLogin:
    """A result in a Login scenario indicating a particular login call is expected."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def __repr__(self) -> str:
        return f"DoLogin({self.username!r}, {self.password!r})"


class Use:
    """A result in a Login scenario indicating a particular token should be returned."""

    def __init__(self, user: str, token: str) -> None:
        self.user = user
        self.token = token

    def __repr__(self) -> str:
        return f"Use({self.user!r}, {self.token!r})"


class Login(ScenarioSet):
    req_user = {"y": "user", "n": None}
    req_pass = {"y": "req_pass", "n": None}
    env_user = {"y": "user", "e": "extra", "n": None}
    env_pass = {"y": "env_pass", "n": None}
    env_token = {"y": "env_token", "n": None}
    cache = {"y": "cache", "x": "expired", "n": None}


# Some shortcut results to save on line length.
CheckCache = Check("cache")
CheckExpired = Check("expired")
UseCache = Use("user", "cache")
UseNew = Use("user", "new")
UseNewDetermined = Use("determined", "new")
UseEnv = Use("user", "env_token")


@pytest.mark.parametrize(
    "scenario_set",
    [
        #     requested user
        #      |   requested password
        #      |    |   DET_USER env var
        #      |    |    |   DET_PASS env var
        #      |    |    |    |   DET_USER_TOKEN
        #      |    |    |    |    |   token cache state
        #      |    |    |    |    |    |   Expected results...
        #      |    |    |    |    |    |    |
        #
        # Explicit user and password, no environment settings, or DET_USER is overridden by
        # explicitly requested user.
        Login("y", "y", "ne", "*", "*", "y", CheckCache, UseCache),
        Login("y", "y", "ne", "*", "*", "n", DoLogin("user", "req_pass"), UseNew),
        Login("y", "y", "ne", "*", "*", "x", CheckExpired, DoLogin("user", "req_pass"), UseNew),
        # ---
        # Explicit user, but password not provided.  Still no (relevant) environment settings.
        Login("y", "n", "ne", "*", "*", "y", CheckCache, UseCache),
        Login("y", "n", "ne", "*", "*", "n", DoLogin("user", "prompt_pass"), UseNew),
        Login("y", "n", "ne", "*", "*", "x", CheckExpired, DoLogin("user", "prompt_pass"), UseNew),
        # ---
        # Explicit user and password, DET_USER set but DET_USER_TOKEN not set.  DET_PASS is ignored.
        Login("y", "y", "y", "*", "n", "y", CheckCache, UseCache),
        Login("y", "y", "y", "*", "n", "n", DoLogin("user", "req_pass"), UseNew),
        Login("y", "y", "y", "*", "n", "x", CheckExpired, DoLogin("user", "req_pass"), UseNew),
        # ---
        # Explicit user and password, DET_USER (matches explicit user), DET_PASS, and DET_TOKEN set.
        Login("y", "y", "y", "y", "y", "y", CheckCache, UseCache),
        Login("y", "y", "y", "y", "y", "x", CheckExpired, UseEnv),
        Login("y", "y", "y", "y", "y", "n", UseEnv),
        # Explicit user but no password, DET_USER and DET_PASS set, and DET_USER_TOKEN unset.
        # DET_PASS still ignored since DET_USER/DET_PASS are meant to be processed as a unit, and
        # an explicitly-requested username overrides that unit.
        Login("y", "n", "y", "y", "n", "y", CheckCache, UseCache),
        Login("y", "n", "y", "y", "n", "n", DoLogin("user", "prompt_pass"), UseNew),
        Login("y", "n", "y", "y", "n", "x", CheckExpired, DoLogin("user", "prompt_pass"), UseNew),
        # ---
        # Explicit user but no password, DET_USER set but no other env.  DET_USER is ignored.
        Login("y", "n", "y", "n", "n", "y", CheckCache, UseCache),
        Login("y", "n", "y", "n", "n", "n", DoLogin("user", "prompt_pass"), UseNew),
        Login("y", "n", "y", "n", "n", "x", CheckExpired, DoLogin("user", "prompt_pass"), UseNew),
        # ---
        # DET_USER_TOKEN is overridden by a configured cache (the on-cluster `det user login` case).
        Login("*", "*", "y", "*", "y", "y", CheckCache, UseCache),
        # DET_USER_TOKEN can be used if user is explicit, so long as DET_USER matches explicit user.
        Login("y", "*", "y", "*", "y", "n", UseEnv),
        # No explicit user; token in env is used if cache is missing or invalid.
        Login("n", "*", "y", "*", "y", "n", UseEnv),
        Login("n", "*", "y", "*", "y", "x", CheckExpired, Use("user", "env_token")),
        # No explicit user; token in env is overridden by a configured cache.
        # (the on-cluster `det user login` case).
        Login("n", "*", "y", "*", "y", "y", CheckCache, Use("user", "cache")),
        Login("n", "*", "e", "n", "y", "y", CheckCache, Use("user", "cache")),
        Login("n", "*", "e", "y", "y", "y", CheckCache, Use("extra", "cache")),
        # ---
        # Nothing explicit; DET_USER and DET_PASS are set, DET_USER_TOKEN is unset.
        # Cache continues to work where it matches DET_USER, and there are no password prompts.
        Login("n", "*", "y", "y", "n", "y", CheckCache, UseCache),
        Login("n", "*", "y", "y", "n", "n", DoLogin("user", "env_pass"), UseNew),
        Login("n", "*", "y", "y", "n", "x", CheckExpired, DoLogin("user", "env_pass"), UseNew),
        # ---
        # Nothing explicit; and DET_USER is ignored without either DET_PASS or DET_USER_TOKEN.
        Login("n", "n", "*", "n", "n", "n", DoLogin("determined", ""), UseNewDetermined),
        # the username is taken from the cache but password must be provided again
        Login("n", "n", "*", "n", "n", "x", CheckExpired, DoLogin("user", "prompt_pass"), UseNew),
        Login("n", "n", "*", "n", "n", "y", CheckCache, UseCache),
        # ---
        # If password is explicit but username is not, we fall back to the default username.
        Login("n", "y", "n", "n", "n", "n", DoLogin("determined", "req_pass"), UseNewDetermined),
        # Other pass-but-not-user cases are governed by other effects (see several earlier cases).
        Login("n", "y", "yn", "*", "*", "y", CheckCache, UseCache),
    ],
)
@mock.patch("determined.common.api.authentication._is_token_valid")
@mock.patch("determined.common.api.authentication.do_login")
@mock.patch("getpass.getpass")
def test_login_scenarios(
    mock_getpass: mock.MagicMock,
    mock_do_login: mock.MagicMock,
    mock_is_token_valid: mock.MagicMock,
    scenario_set: Login,
) -> None:
    def getpass(*_: Any) -> str:
        return "prompt_pass"

    def _is_token_valid(master_url: str, token: str, cert: Any) -> bool:
        return token in ["cache", "env_token"]

    def do_login(*_: Any) -> str:
        return "new"

    mock_getpass.side_effect = getpass
    mock_is_token_valid.side_effect = _is_token_valid
    mock_do_login.side_effect = do_login

    for scenario in scenario_set.scenarios():
        with contextlib.ExitStack() as es:
            # Configure environment.
            es.enter_context(util.setenv_optional("DET_MASTER", MOCK_MASTER_URL))
            es.enter_context(util.setenv_optional("DET_USER", scenario.env_user))
            es.enter_context(util.setenv_optional("DET_PASS", scenario.env_pass))
            es.enter_context(util.setenv_optional("DET_USER_TOKEN", scenario.env_token))

            # Configure allowable TokenStore calls.
            mts = es.enter_context(util.MockTokenStore(strict=False))
            mts.get_token("user", retval=scenario.cache)
            mts.get_token("extra", retval=scenario.cache)
            mts.get_token("determined", retval=scenario.cache)
            mts.drop_user("user")
            mts.drop_user("extra")
            mts.set_token("user", "new")
            mts.set_token("determined", "new")

            # The cache active user is always "user".  This deserves some explanation.  Effectively,
            # since this is only called within the `default_load_user_password()` function, that
            # function will never show evidence that it checked the environment.  However, for the
            # remainder of the authentication flow, it never matters again that the user came from
            # the TokenStore.get_active_user(), so if we modeled that in the table it wouldn't
            # actually increase code coverage for the Authentication object, which is what we are
            # focusing on.
            mts.get_active_user(retval=scenario.cache and "user")

            try:
                auth = authentication.Authentication(
                    "master_url", scenario.req_user, scenario.req_pass, None
                )

                # Make sure we got the results we expected.
                for exp in scenario_set.expected:
                    if isinstance(exp, Check):
                        mock_is_token_valid.assert_has_calls(
                            [mock.call("master_url", exp.token, None)]
                        )
                    elif isinstance(exp, DoLogin):
                        mock_do_login.assert_has_calls(
                            [mock.call("master_url", exp.username, exp.password, None)]
                        )
                    elif isinstance(exp, Use):
                        assert auth.session.username == exp.user
                        assert auth.session.token == exp.token
                    else:
                        raise ValueError(f"unexpected result: {exp}")

                # Make sure we didn't get any unexpected results.
                if not any(isinstance(exp, Check) for exp in scenario_set.expected):
                    mock_is_token_valid.assert_not_called()
                if not any(isinstance(exp, DoLogin) for exp in scenario_set.expected):
                    mock_do_login.assert_not_called()

            except Exception as e:
                raise RuntimeError(
                    f"failed scenario_set: {scenario_set}, scenario={scenario}"
                ) from e


class GetToken:
    """A possible result in a Logout scenario."""

    def __init__(self, user: str) -> None:
        self.user = user

    def expect(self, rsps: responses.RequestsMock, mts: util.MockTokenStore, scenario: Any) -> None:
        mts.get_token(self.user, retval="cache_token" if scenario.user_in_cache else None)


class DoLogout:
    """A possible result in a Logout scenario."""

    def __init__(self, user: str) -> None:
        self.user = user

    def expect(self, rsps: responses.RequestsMock, mts: util.MockTokenStore, scenario: Any) -> None:
        mts.drop_user(self.user)
        rsps.post(
            f"{MOCK_MASTER_URL}/api/v1/auth/logout",
            status=200,
            match=[matchers.header_matcher({"Authorization": "Bearer cache_token"})],
        )


class GetActiveUser:
    """A possible result in a Logout scenario."""

    def expect(self, rsps: responses.RequestsMock, mts: util.MockTokenStore, scenario: Any) -> None:
        mts.get_active_user(retval="cache_user" if scenario.user_in_cache else None)


class Logout(ScenarioSet):
    user_explicit = {"y": "req_user", "n": None}
    user_in_env = {"y": "env_user", "n": None}
    user_in_cache = {"y": "cache_user", "n": None}


@pytest.mark.parametrize(
    "scenario_set",
    [
        # Explicit user is logged out if present in the cache.
        #       user_explicit
        #       |    user_in_env
        #       |    |    user_in_cache
        #       |    |    |
        Logout("y", "*", "y", GetToken("req_user"), DoLogout("req_user")),
        Logout("y", "*", "n", GetToken("req_user")),
        # Environment user is logged out if present in the cache.
        Logout("n", "y", "y", GetToken("env_user"), DoLogout("env_user")),
        Logout("n", "y", "n", GetToken("env_user")),
        # Cache active user is logged out.
        Logout("n", "n", "y", GetActiveUser(), GetToken("cache_user"), DoLogout("cache_user")),
        # When no user is found, it is a noop.
        Logout("n", "n", "n", GetActiveUser()),
    ],
)
def test_logout(scenario_set: Logout) -> None:
    for scenario in scenario_set.scenarios():
        with contextlib.ExitStack() as es:
            rsps = es.enter_context(
                responses.RequestsMock(
                    registry=registries.OrderedRegistry,
                    assert_all_requests_are_fired=True,
                )
            )
            mts = es.enter_context(util.MockTokenStore(strict=True))

            # Configure evironment variables.
            es.enter_context(util.setenv_optional("DET_MASTER", MOCK_MASTER_URL))
            es.enter_context(util.setenv_optional("DET_USER", scenario.user_in_env))
            env_pass = "env_pass" if scenario.user_in_env else None
            es.enter_context(util.setenv_optional("DET_PASS", env_pass))

            util.expect_get_info(rsps)

            for exp in scenario_set.expected:
                exp.expect(rsps, mts, scenario)

            # Actually call the cli.
            if scenario.user_explicit:
                cmd = ["-u", "req_user", "user", "logout"]
            else:
                cmd = ["user", "logout"]
            cli.main(cmd)


def test_auth_json_v0_upgrade() -> None:
    with use_test_config_dir() as config_dir:
        auth_json_path = config_dir / "auth.json"
        shutil.copy2(AUTH_V0_PATH, auth_json_path)
        ts = authentication.TokenStore(MOCK_MASTER_URL, auth_json_path)

        assert ts.get_active_user() == "determined"
        assert ts.get_token("determined") == "v2.public.this.is.a.test"

        ts.set_token("determined", "ai")

        ts2 = authentication.TokenStore(MOCK_MASTER_URL, auth_json_path)
        assert ts2.get_token("determined") == "ai"

        with auth_json_path.open() as fin:
            data = json.load(fin)
            assert data.get("version") == 1
            assert "masters" in data and list(data["masters"].keys()) == [MOCK_MASTER_URL]


def test_cert_v0_upgrade() -> None:
    with use_test_config_dir() as config_dir:
        cert_path = config_dir / "master.crt"
        shutil.copy2(UNTRUSTED_CERT_PATH, cert_path)
        with cert_path.open() as fin:
            cert_data = fin.read()

        cert = certs.default_load(MOCK_MASTER_URL)
        assert isinstance(cert.bundle, str)
        with open(cert.bundle) as fin:
            loaded_cert_data = fin.read()
        assert loaded_cert_data.endswith(cert_data)
        assert not cert_path.exists()

        v1_certs_path = config_dir / "certs.json"
        assert v1_certs_path.exists()

        # Load once again from v1.
        cert2 = certs.default_load(MOCK_MASTER_URL)
        assert isinstance(cert2.bundle, str)
        with open(cert2.bundle) as fin:
            loaded_cert_data = fin.read()
        assert loaded_cert_data.endswith(cert_data)
