"""Role resolution from a Supabase access token.

test_gateway.py stubs TokenVerifier.verify out — it is the one piece that
would otherwise need a live Supabase project. These tests cover the piece that
stub hides, against HS256 tokens the verifier accepts without any network.

What they are really guarding is the agreement between two repositories: the
platform database decides who is a member (hub migration 0012 — every
authenticated account is an implicit viewer of every project) and this process
has to reach the same verdict. When those two disagree the failure is silent
and miserable to diagnose: the web app lets somebody into the console and the
gatekeeper closes their socket, which looks like a broken lab.
"""

import pathlib
import sys
import time

import jwt
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from primbio_gateway.auth import (  # noqa: E402
    AuthError,
    Identity,
    LeaseVerifier,
    TokenVerifier,
)

SECRET = 'test-secret-at-least-32-characters-long!'
PROJECT_ID = '11111111-1111-1111-1111-111111111111'
OTHER_PROJECT = '22222222-2222-2222-2222-222222222222'


def make_token(roles=None, **claims):
    payload = {
        'sub': '00000000-0000-0000-0000-0000000000aa',
        'aud': 'authenticated',
        'exp': int(time.time()) + 600,
        'email': 'someone@unal.edu.co',
        **claims,
    }
    if roles is not None:
        payload['project_roles'] = roles
    return jwt.encode(payload, SECRET, algorithm='HS256')


def verifier(**kwargs):
    return TokenVerifier(
        'https://example.supabase.co', PROJECT_ID, jwt_secret=SECRET, **kwargs
    )


def test_explicit_role_wins():
    identity = verifier().verify(make_token({PROJECT_ID: 'operator'}))
    assert identity.role == 'operator'
    assert identity.at_least('operator')


def test_authenticated_stranger_is_a_viewer():
    # No membership row on this project, so no entry in the claim: the DB
    # counts them as a viewer and so must we. Watching is all it grants —
    # actuation needs the operator role and a lease (policy.py).
    identity = verifier().verify(make_token({OTHER_PROJECT: 'admin'}))
    assert identity.role == 'viewer'
    assert not identity.at_least('operator')


def test_missing_claim_is_a_viewer():
    # A token minted before the access-token hook existed, or by a project
    # without it. Same verdict, and notably not a crash.
    identity = verifier().verify(make_token())
    assert identity.role == 'viewer'


def test_empty_default_role_closes_the_lab():
    # The escape hatch: LAB_DEFAULT_ROLE= restores deny-by-default for a lab
    # that must not be watchable by every account on the platform.
    with pytest.raises(AuthError):
        verifier(default_role='').verify(make_token({OTHER_PROJECT: 'admin'}))


def test_unknown_role_is_never_promoted():
    # An unrecognised value must not fall through to the default and become a
    # viewer, nor be treated as an unknown-but-probably-fine role.
    with pytest.raises(AuthError):
        verifier().verify(make_token({PROJECT_ID: 'superuser'}))


def test_malformed_claim_is_rejected():
    with pytest.raises(AuthError):
        verifier().verify(make_token(['operator']))


def test_expired_token_is_rejected():
    with pytest.raises(AuthError):
        verifier().verify(
            make_token({PROJECT_ID: 'owner'}, exp=int(time.time()) - 60)
        )


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode(
        {
            'sub': 'attacker',
            'aud': 'authenticated',
            'exp': int(time.time()) + 600,
            'project_roles': {PROJECT_ID: 'owner'},
        },
        'not-the-platform-secret-but-long-enough!',
        algorithm='HS256',
    )
    with pytest.raises(AuthError):
        verifier().verify(forged)


# ── One driver, whatever the control plane says ─────────────────────────────
#
# The lease token is verified in isolation, so two valid tokens for two
# different people both pass on their own merits. These cover the arbitration
# that stops the second one from reaching the arm — the property the web app's
# lease store is supposed to guarantee and, on a multi-instance deployment,
# did not.

LAB = 'dobot-cr3'
LEASE_SECRET = 'lease-secret-at-least-32-characters-long'

ANA = Identity('u-ana', 'ana@unal.edu.co', 'Ana', 'operator')
BETO = Identity('u-beto', 'beto@unal.edu.co', 'Beto', 'owner')


def lease(identity, ttl=10):
    # Whole seconds: `exp` has no finer resolution, so a fractional TTL would
    # round down to "already expired" and make these tests flap.
    return jwt.encode(
        {'sub': identity.user_id, 'aud': LAB, 'exp': int(time.time()) + ttl},
        LEASE_SECRET,
        algorithm='HS256',
    )


def leases():
    return LeaseVerifier(LEASE_SECRET, LAB)


def test_two_valid_lease_tokens_do_not_both_drive():
    """The bug this exists for: the app granted control to an operator and to
    the owner at the same time, and both tokens were perfectly valid."""
    verifier = leases()
    assert verifier.holds_lease(lease(ANA), ANA) is True
    assert verifier.holds_lease(lease(BETO), BETO) is False
    # And Ana keeps driving — being challenged does not cost her the lease.
    assert verifier.holds_lease(lease(ANA), ANA) is True


def test_the_holder_can_refresh_indefinitely():
    verifier = leases()
    for _ in range(5):
        assert verifier.holds_lease(lease(ANA), ANA) is True
    assert verifier.held is True


def test_a_lapsed_claim_frees_the_lease():
    """Nothing has to arrive for the arm to become available again: the
    incumbent's last token simply runs out."""
    verifier = leases()
    assert verifier.holds_lease(lease(ANA, ttl=1), ANA) is True
    time.sleep(1.2)
    assert verifier.held is False
    assert verifier.holds_lease(lease(BETO), BETO) is True


def test_standing_down_hands_over_at_once():
    verifier = leases()
    assert verifier.holds_lease(lease(ANA), ANA) is True
    verifier.release(ANA.user_id)
    assert verifier.holds_lease(lease(BETO), BETO) is True
    # Ana is now the one refused.
    assert verifier.holds_lease(lease(ANA), ANA) is False


def test_releasing_somebody_elses_lease_does_nothing():
    verifier = leases()
    assert verifier.holds_lease(lease(ANA), ANA) is True
    verifier.release(BETO.user_id)
    assert verifier.holds_lease(lease(BETO), BETO) is False


def test_an_invalid_token_never_claims_the_lease():
    verifier = leases()
    forged = jwt.encode(
        {'sub': BETO.user_id, 'aud': LAB, 'exp': int(time.time() + 60)},
        'not-the-lease-secret-but-long-enough!!',
        algorithm='HS256',
    )
    assert verifier.holds_lease(forged, BETO) is False
    assert verifier.holds_lease(lease(ANA, ttl=-5), ANA) is False
    # Replaying somebody else's live token does not claim it either.
    assert verifier.holds_lease(lease(ANA), BETO) is False
    assert verifier.held is False


def test_without_a_secret_nobody_holds_anything():
    verifier = LeaseVerifier('', LAB)
    assert verifier.holds_lease(lease(ANA), ANA) is False
    assert verifier.held is False
