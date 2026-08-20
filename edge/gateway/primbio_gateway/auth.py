"""Identity for the edge gatekeeper: Supabase access tokens and lease tokens.

Two independent, unrelated credentials meet here, and keeping them separate is
the whole point of the design:

* the **Supabase access token** says *who you are* and *what role you hold on
  this lab's project*. It is issued by the platform's Supabase project, signed
  with that project's keys, and carries the ``project_roles`` claim injected by
  the platform's custom access token hook (hub migration 0004). Verifying it
  needs no database round trip and no service-role key — the Pi holds no
  platform credentials at all.

* the **lease token** says *you currently hold the hardware control lease*. It
  is minted by the lab web app (which owns the Redis lease) and signed with a
  secret shared only between that app and this gatekeeper. It is short-lived by
  design: if the lease expires because an operator's browser died, the token
  stops being reissued and actuation stops within seconds without this process
  having to know anything about Redis.

Neither credential alone is enough to move the arm; the emergency stop
deliberately needs only the first.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp
import jwt
from jwt import PyJWKClient

log = logging.getLogger(__name__)

# Roles the platform defines, ordered. Anything outside this set is treated as
# no access at all rather than as an unknown-but-probably-fine role.
ROLE_LEVEL = {'viewer': 0, 'operator': 1, 'admin': 2, 'owner': 3}

_JWKS_CACHE_TTL = 600.0


@dataclass(frozen=True)
class Identity:
    """An authenticated person on this lab."""

    user_id: str
    email: str
    name: str
    role: str

    def at_least(self, role: str) -> bool:
        return ROLE_LEVEL.get(self.role, -1) >= ROLE_LEVEL[role]


class AuthError(Exception):
    """Raised for every rejection. The message is logged, never sent verbatim
    to the client — an unauthenticated caller learns only that it failed."""


class TokenVerifier:
    """Verifies Supabase access tokens against the project's public keys."""

    def __init__(
        self,
        supabase_url: str,
        project_id: str,
        jwt_secret: str = '',
        default_role: str = 'viewer',
    ):
        self.supabase_url = supabase_url.rstrip('/')
        self.project_id = project_id
        # Legacy projects sign access tokens with a shared HS256 secret. Newer
        # ones publish an asymmetric JWKS; we prefer that and never require the
        # symmetric secret to be present.
        self.jwt_secret = jwt_secret
        # Role granted to an authenticated user who holds no role on this
        # project.
        #
        # 'viewer' by default because that is what the platform database says:
        # since migration 0012 every authenticated account is an implicit
        # viewer of every project, and only *elevation* is granted explicitly
        # through project_members. Denying by default here contradicted that —
        # the web app let a signed-in classmate into the console while the
        # gatekeeper closed their socket 4403 and 401'd the camera list, which
        # reads as a broken lab rather than as a permission decision.
        #
        # It is not a hole: the token must still be signed by this platform's
        # Supabase project, whose signup trigger admits only institutional
        # addresses. A viewer may watch and nothing else — every actuation
        # needs the operator role *and* a live lease (see policy.py).
        #
        # A lab that must stay closed sets LAB_DEFAULT_ROLE= (empty) and gets
        # the old deny-by-default back.
        self.default_role = default_role
        self._jwk_client: Optional[PyJWKClient] = None
        self._jwk_client_at = 0.0

    @property
    def jwks_url(self) -> str:
        return f'{self.supabase_url}/auth/v1/.well-known/jwks.json'

    def _jwks(self) -> PyJWKClient:
        now = time.time()
        if self._jwk_client is None or now - self._jwk_client_at > _JWKS_CACHE_TTL:
            # PyJWKClient keeps its own small cache; recreating it periodically
            # is what picks up a key rotation.
            self._jwk_client = PyJWKClient(self.jwks_url, cache_keys=True)
            self._jwk_client_at = now
        return self._jwk_client

    def _decode(self, token: str) -> Dict[str, Any]:
        options = {'require': ['exp', 'sub'], 'verify_aud': True}
        header = jwt.get_unverified_header(token)
        alg = header.get('alg', '')

        if alg.startswith(('RS', 'ES', 'PS')):
            key = self._jwks().get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience='authenticated',
                options=options,
            )

        if alg == 'HS256':
            if not self.jwt_secret:
                raise AuthError(
                    'token is HS256 but SUPABASE_JWT_SECRET is not configured'
                )
            return jwt.decode(
                token,
                self.jwt_secret,
                algorithms=['HS256'],
                audience='authenticated',
                options=options,
            )

        raise AuthError(f'unsupported token algorithm {alg!r}')

    def verify(self, token: str) -> Identity:
        """Resolve an access token to an Identity, or raise AuthError.

        The role comes from the JWT's ``project_roles`` claim, so a user whose
        role was revoked keeps their old role until their token refreshes
        (Supabase default: one hour). The lease is the short-lived credential;
        this one is not meant to be. Revoking access immediately is done by
        removing the membership *and* letting the web app refuse to mint lease
        tokens, which takes effect within one lease TTL.
        """
        if not token:
            raise AuthError('no token supplied')
        try:
            claims = self._decode(token)
        except AuthError:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise AuthError('token expired') from exc
        except Exception as exc:  # invalid signature, malformed, JWKS down…
            raise AuthError(f'token rejected: {exc}') from exc

        roles = claims.get('project_roles') or {}
        if not isinstance(roles, dict):
            raise AuthError('project_roles claim is malformed')
        role = roles.get(self.project_id) or self.default_role
        if role not in ROLE_LEVEL:
            raise AuthError(
                f"user {claims.get('sub')} holds no role on project {self.project_id}"
            )

        meta = claims.get('user_metadata') or {}
        email = claims.get('email') or ''
        return Identity(
            user_id=str(claims['sub']),
            email=email,
            name=meta.get('full_name') or meta.get('name') or email or 'usuario',
            role=role,
        )


class LeaseVerifier:
    """Verifies the control-lease token, and arbitrates who is actually driving.

    Verifying the token is not by itself enough to guarantee one driver. A
    lease token is legitimate *in isolation* — signed by the app, unexpired,
    bound to the person presenting it — so if the control plane ever hands the
    lease to two people at once, every check below passes for both of them and
    the arm obeys two browsers.

    That is not hypothetical. The web app's fallback lease store is in-process,
    so a deployment running more than one instance of it granted control
    independently in each, and Cloudflare Workers runs many — an operator and
    the owner drove the same arm at once. The app now keeps the lease in one
    shared place; this class is why that is not the only thing standing between
    the hardware and two operators.

    This process is the single point every client of this one robot passes
    through, so it is the only place that can hold the invariant whatever the
    control plane believes. The first valid token seen takes the lease and
    keeps it until it lapses; anybody else's token is refused meanwhile,
    exactly as if they held none.
    """

    def __init__(self, secret: str, lab_slug: str):
        self.secret = secret
        self.lab_slug = lab_slug
        # Who is driving, and the expiry of the newest token they have shown.
        # Deliberately not persisted: a restart drops every socket, so there is
        # nobody left to protect an incumbent from.
        self._holder: Optional[str] = None
        self._holder_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    def holds_lease(self, token: str, identity: Identity) -> bool:
        """True only if `token` is a live lease token belonging to `identity`.

        Every failure is a plain False: a caller must not be able to tell a
        forged token from an expired one from a token minted for someone else.
        """
        if not self.enabled or not token:
            return False
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=['HS256'],
                audience=self.lab_slug,
                options={'require': ['exp', 'sub', 'aud']},
            )
        except Exception as exc:
            log.debug('lease token rejected: %s', exc)
            return False
        # A lease token is bound to one person: replaying someone else's does
        # not grant control.
        if str(claims.get('sub')) != identity.user_id:
            return False
        return self._claim(identity.user_id, float(claims['exp']))

    def _claim(self, user_id: str, expires: float) -> bool:
        """Take or refresh the lease, unless somebody else still holds it."""
        now = time.time()
        held = self._holder is not None and now < self._holder_until
        if held and self._holder != user_id:
            log.warning(
                '[lease] refused %s: %s is driving for another %.0fs — two '
                'clients hold a valid lease token, so the control plane has '
                'granted control twice',
                user_id,
                self._holder,
                self._holder_until - now,
            )
            return False
        # Extending rather than overwriting: a client with two tabs open shows
        # tokens of different ages, and the lease should follow the newest.
        self._holder_until = max(self._holder_until, expires) if held else expires
        self._holder = user_id
        return True

    def release(self, user_id: str) -> None:
        """Give the lease up early, if this is the person who holds it.

        A cooperative browser clears its token the moment the state stream says
        somebody else is driving. Honouring that immediately is what keeps an
        admin's force from having to wait out the displaced operator's last
        token; without it the takeover is still correct, just up to one token
        lifetime slower.
        """
        if self._holder == user_id:
            self._holder = None
            self._holder_until = 0.0

    @property
    def held(self) -> bool:
        """True while somebody's lease claim is live. Identity is deliberately
        not exposed: /health answers unauthenticated callers."""
        return self._holder is not None and time.time() < self._holder_until


async def probe_jwks(session: aiohttp.ClientSession, url: str) -> bool:
    """Startup sanity check: is the platform's key set reachable?

    Only advisory — a failure is logged, not fatal. The Pi may well boot before
    its uplink is up, and refusing to start would leave the hardware
    unreachable for a transient DNS hiccup.
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning('[auth] JWKS probe failed (%s): %s', url, exc)
        return False
