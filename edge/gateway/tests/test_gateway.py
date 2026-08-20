"""End-to-end tests for the gatekeeper against a stand-in foxglove_bridge.

The scenario under test is the one the lab exists for: one operator drives,
several people watch, and the watchers can tell what the driver is doing.

Written without pytest-asyncio (not installed on the lab images): each test is
a synchronous function that drives its own event loop.
"""

import asyncio
import json
import pathlib
import struct
import sys
import time

import aiohttp
import jwt
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from primbio_gateway import protocol  # noqa: E402
from primbio_gateway.auth import Identity  # noqa: E402
from primbio_gateway.server import GATEWAY_KEY, Config, build_app  # noqa: E402

LEASE_SECRET = 'test-secret-at-least-32-characters-long'
LAB_SLUG = 'dobot-cr3'
PROJECT_ID = 'cbadb67c-2001-4c1e-bea1-99e3199f3fa2'

OPERATOR = Identity('u-op', 'op@unal.edu.co', 'María Gómez', 'operator')
VIEWER = Identity('u-view', 'view@unal.edu.co', 'Juan Pérez', 'viewer')
OWNER = Identity('u-owner', 'owner@unal.edu.co', 'Sofía Ruiz', 'owner')

# Service ids the fake bridge advertises.
SERVICES = {10: '/weblab/jog', 11: '/weblab/estop', 12: '/weblab/enable'}


def lease_token(identity: Identity, ttl: float = 20.0) -> str:
    return jwt.encode(
        {'sub': identity.user_id, 'aud': LAB_SLUG, 'exp': int(time.time() + ttl)},
        LEASE_SECRET,
        algorithm='HS256',
    )


def service_call_frame(service_id: int, call_id: int, payload: bytes = b'{}') -> bytes:
    encoding = b'json'
    return (
        bytes([protocol.CLIENT_SERVICE_CALL_REQUEST])
        + struct.pack('<III', service_id, call_id, len(encoding))
        + encoding
        + payload
    )


class FakeBridge:
    """Minimal foxglove_bridge: advertises services, records what arrives."""

    def __init__(self):
        self.received: list[bytes] = []
        self.runner = None
        self.port = 0

    async def start(self):
        app = web.Application()
        app.add_routes([web.get('/', self._handle)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    async def _handle(self, request):
        ws = web.WebSocketResponse(protocols=['foxglove.websocket.v1'])
        await ws.prepare(request)
        await ws.send_str(json.dumps({'op': 'serverInfo', 'name': 'fake-bridge'}))
        await ws.send_str(
            json.dumps(
                {
                    'op': 'advertiseServices',
                    'services': [
                        {'id': sid, 'name': name, 'type': 'std_srvs/srv/Trigger'}
                        for sid, name in SERVICES.items()
                    ],
                }
            )
        )
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.BINARY:
                self.received.append(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
        return ws


class GatewayHarness:
    """Gateway wired to a fake bridge, with token verification stubbed out.

    Only `TokenVerifier.verify` is replaced — it is the one piece that needs a
    live Supabase project. Lease tokens are minted and verified for real, so
    the lease path under test is the production one.
    """

    def __init__(self, bridge: FakeBridge, identities: dict[str, Identity]):
        self.bridge = bridge
        self.identities = identities
        self.runner = None
        self.port = 0

    async def start(self):
        config = Config(
            supabase_url='https://example.supabase.co',
            project_id=PROJECT_ID,
            lab_slug=LAB_SLUG,
            lease_secret=LEASE_SECRET,
            jwt_secret='',
            bridge_url=f'http://127.0.0.1:{self.bridge.port}/',
            go2rtc_url='http://127.0.0.1:1',
            host='127.0.0.1',
            port=0,
            default_role='',
        )
        app = build_app(config)
        # Drop the startup JWKS probe: it would try to reach the internet.
        app.on_startup.clear()

        def fake_verify(token: str):
            from primbio_gateway.auth import AuthError

            if token not in self.identities:
                raise AuthError('unknown test token')
            return self.identities[token]

        app[GATEWAY_KEY].tokens.verify = fake_verify
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    def url(self, path='/ws'):
        return f'http://127.0.0.1:{self.port}{path}'


async def connect(session, harness, token, lease=''):
    """Open a session and complete the handshake, returning (ws, hello)."""
    ws = await session.ws_connect(harness.url(), protocols=['foxglove.websocket.v1'])
    await ws.send_str(json.dumps({'op': 'auth', 'token': token, 'lease': lease}))
    hello = json.loads((await ws.receive()).data)
    return ws, hello


async def read_until(ws, op, timeout=3.0, where=None):
    """Next JSON frame with the given op, or None if it never arrives.

    `where` narrows further — needed because a client also receives its own
    presence event, so "the next presence frame" is not necessarily the one
    about somebody else.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=deadline - time.monotonic())
        except asyncio.TimeoutError:
            return None
        if msg.type is not aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get('op') == op and (where is None or where(data)):
            return data
    return None


# ── The headline behaviour: a viewer sees what the operator does ────────────

def test_viewer_sees_the_operators_commands():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'op-token': OPERATOR, 'view-token': VIEWER})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                viewer_ws, viewer_hello = await connect(session, harness, 'view-token')
                operator_ws, operator_hello = await connect(
                    session, harness, 'op-token', lease_token(OPERATOR)
                )

                assert viewer_hello['user']['role'] == 'viewer'
                assert viewer_hello['holdsLease'] is False
                assert operator_hello['holdsLease'] is True

                # The operator must have learned the service table before it
                # can call anything by id.
                assert await read_until(operator_ws, 'advertiseServices')

                await operator_ws.send_bytes(service_call_frame(10, 1))

                # …and the viewer, who did nothing, is told about it.
                activity = await read_until(viewer_ws, 'activity')
                assert activity is not None, 'viewer never saw the command'
                assert activity['action'] == '/weblab/jog'
                assert activity['user']['name'] == 'María Gómez'
                assert activity['stop'] is False

                await asyncio.sleep(0.1)
                assert len(bridge.received) == 1

                await viewer_ws.close()
                await operator_ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


# ── A viewer cannot drive, and the bridge never hears about it ──────────────

def test_viewer_command_is_refused_and_never_reaches_ros():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'view-token': VIEWER})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                ws, _ = await connect(session, harness, 'view-token')
                assert await read_until(ws, 'advertiseServices')

                await ws.send_bytes(service_call_frame(10, 7))
                denied = await read_until(ws, 'denied')
                assert denied is not None
                assert denied['callId'] == 7

                await asyncio.sleep(0.1)
                assert bridge.received == [], 'a viewer reached the robot'
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


# ── Lease invariants, exercised through the real verifier ───────────────────

def test_operator_without_a_lease_cannot_move_but_can_stop():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'op-token': OPERATOR})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                ws, hello = await connect(session, harness, 'op-token')  # no lease
                assert hello['holdsLease'] is False
                assert await read_until(ws, 'advertiseServices')

                await ws.send_bytes(service_call_frame(12, 1))  # /weblab/enable
                assert await read_until(ws, 'denied') is not None
                await asyncio.sleep(0.1)
                assert bridge.received == []

                # The emergency stop is deliberately lease-free.
                await ws.send_bytes(service_call_frame(11, 2))  # /weblab/estop
                await asyncio.sleep(0.2)
                assert len(bridge.received) == 1, 'e-stop was blocked by the lease'
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_expired_and_foreign_lease_tokens_are_rejected():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'op-token': OPERATOR})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                # Expired.
                ws, hello = await connect(
                    session, harness, 'op-token', lease_token(OPERATOR, ttl=-5)
                )
                assert hello['holdsLease'] is False
                await ws.close()

                # Minted for somebody else — replaying it must not grant control.
                ws, hello = await connect(
                    session, harness, 'op-token', lease_token(VIEWER)
                )
                assert hello['holdsLease'] is False
                await ws.close()

                # Signed with the wrong secret.
                forged = jwt.encode(
                    {'sub': OPERATOR.user_id, 'aud': LAB_SLUG,
                     'exp': int(time.time() + 60)},
                    'not-the-real-secret',
                    algorithm='HS256',
                )
                ws, hello = await connect(session, harness, 'op-token', forged)
                assert hello['holdsLease'] is False
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_only_one_of_two_lease_holders_can_move_the_arm():
    """The regression this guard exists for.

    The web app handed control to an operator and to the owner at the same
    time — its in-memory lease store is per-instance, and the deployment runs
    many. Both tokens are genuine, so nothing about either one is refusable on
    its own merits. The gatekeeper is the only process that sees both, and it
    is what has to keep the arm down to one driver.
    """

    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(
            bridge, {'op-token': OPERATOR, 'owner-token': OWNER}
        )
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                first, hello = await connect(
                    session, harness, 'op-token', lease_token(OPERATOR)
                )
                assert hello['holdsLease'] is True
                assert await read_until(first, 'advertiseServices')

                second, hello = await connect(
                    session, harness, 'owner-token', lease_token(OWNER)
                )
                assert hello['holdsLease'] is False, (
                    'two clients were told they hold the same lease'
                )
                assert await read_until(second, 'advertiseServices')

                # Both ask the arm to move. Only the operator's call arrives.
                await first.send_bytes(service_call_frame(12, 1))
                await second.send_bytes(service_call_frame(12, 2))
                assert await read_until(second, 'denied') is not None
                await asyncio.sleep(0.2)
                assert len(bridge.received) == 1, (
                    'the arm took commands from two people at once'
                )

                # The owner can still stop it: a stop never needs the lease,
                # and that must survive being refused the lease.
                await second.send_bytes(service_call_frame(11, 3))
                await asyncio.sleep(0.2)
                assert len(bridge.received) == 2

                await first.close()
                await second.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_standing_down_lets_the_next_operator_drive():
    """A displaced operator's browser clears its token as soon as the state
    stream says somebody else is driving. That must hand the arm over at once,
    not one token lifetime later."""

    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(
            bridge, {'op-token': OPERATOR, 'owner-token': OWNER}
        )
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                first, _ = await connect(
                    session, harness, 'op-token', lease_token(OPERATOR)
                )
                assert await read_until(first, 'advertiseServices')
                second, hello = await connect(
                    session, harness, 'owner-token', lease_token(OWNER)
                )
                assert hello['holdsLease'] is False
                assert await read_until(second, 'advertiseServices')

                await first.send_str(json.dumps({'op': 'lease', 'token': ''}))
                ack = await read_until(first, 'leaseAck')
                assert ack['holdsLease'] is False

                # The owner's next refresh takes the lease.
                await second.send_str(
                    json.dumps({'op': 'lease', 'token': lease_token(OWNER)})
                )
                ack = await read_until(second, 'leaseAck')
                assert ack['holdsLease'] is True

                await second.send_bytes(service_call_frame(12, 1))
                await asyncio.sleep(0.2)
                assert len(bridge.received) == 1
                await first.close()
                await second.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_a_disconnected_holder_frees_the_arm():
    """The holder's tab dies. Nobody tells the gatekeeper anything except the
    socket closing, and the lab must not stay locked until their token runs
    out."""

    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(
            bridge, {'op-token': OPERATOR, 'owner-token': OWNER}
        )
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                first, hello = await connect(
                    session, harness, 'op-token', lease_token(OPERATOR)
                )
                assert hello['holdsLease'] is True
                assert await read_until(first, 'advertiseServices')
                await first.close()
                await asyncio.sleep(0.2)

                second, hello = await connect(
                    session, harness, 'owner-token', lease_token(OWNER)
                )
                assert hello['holdsLease'] is True
                await second.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_lease_can_be_refreshed_mid_session():
    """The browser heartbeats every few seconds; a fresh token must take
    effect without reconnecting, and that is what promotes a queued operator."""

    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'op-token': OPERATOR})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                ws, hello = await connect(session, harness, 'op-token')
                assert hello['holdsLease'] is False
                assert await read_until(ws, 'advertiseServices')

                await ws.send_str(
                    json.dumps({'op': 'lease', 'token': lease_token(OPERATOR)})
                )
                ack = await read_until(ws, 'leaseAck')
                assert ack['holdsLease'] is True

                await ws.send_bytes(service_call_frame(12, 3))
                await asyncio.sleep(0.2)
                assert len(bridge.received) == 1
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


# ── Unauthenticated sockets never reach the bridge ──────────────────────────

def test_socket_without_auth_frame_is_closed():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(
                    harness.url(), protocols=['foxglove.websocket.v1']
                )
                # Skip the handshake and go straight for the robot.
                await ws.send_bytes(service_call_frame(11, 1))
                msg = await asyncio.wait_for(ws.receive(), timeout=8)
                assert msg.type is aiohttp.WSMsgType.CLOSE
                assert bridge.received == []
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


def test_bad_token_is_rejected_before_any_upstream_connection():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'good': OPERATOR})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                ws = await session.ws_connect(
                    harness.url(), protocols=['foxglove.websocket.v1']
                )
                await ws.send_str(json.dumps({'op': 'auth', 'token': 'forged'}))
                msg = await asyncio.wait_for(ws.receive(), timeout=8)
                assert msg.type is aiohttp.WSMsgType.CLOSE
                await ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


# ── Presence: viewers learn who joined and left ─────────────────────────────

def test_presence_is_broadcast_to_existing_viewers():
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'op-token': OPERATOR, 'view-token': VIEWER})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                viewer_ws, _ = await connect(session, harness, 'view-token')
                operator_ws, _ = await connect(session, harness, 'op-token')

                about_operator = lambda frame: frame['user']['id'] == OPERATOR.user_id
                joined = await read_until(viewer_ws, 'presence', where=about_operator)
                assert joined['event'] == 'join'
                assert joined['user']['name'] == 'María Gómez'
                assert joined['user']['role'] == 'operator'
                assert joined['viewers'] == 2

                await operator_ws.close()
                left = await read_until(viewer_ws, 'presence', where=about_operator)
                assert left['event'] == 'leave'
                await viewer_ws.close()
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())


# ── Video: watching is what a viewer is for ─────────────────────────────────

def test_viewer_may_reach_the_cameras_and_anonymous_may_not():
    """The camera list and the streams behind it are open to any signed-in role.

    A spectator who cannot see the cameras cannot spectate, and since the web
    UI builds its wall from whatever /api/video/api/streams reports, refusing a
    viewer here empties the wall rather than hiding one control. Only *driving*
    is privileged.

    go2rtc is not running in this harness, so an authorized request fails
    upstream with 502. That is the point: anything other than 401 means the
    gatekeeper let it through to the camera server.
    """
    async def scenario():
        bridge = FakeBridge()
        await bridge.start()
        harness = GatewayHarness(bridge, {'view-token': VIEWER})
        await harness.start()
        try:
            async with aiohttp.ClientSession() as session:
                url = harness.url('/api/video/api/streams')

                async with session.get(url) as anonymous:
                    assert anonymous.status == 401

                async with session.get(
                    url, headers={'Authorization': 'Bearer view-token'}
                ) as viewer:
                    assert viewer.status != 401
                    assert viewer.status == 502

                # The MSE fallback cannot set headers on a WebSocket upgrade,
                # so the same token has to work as a query parameter.
                async with session.get(f'{url}?access_token=view-token') as query:
                    assert query.status != 401
        finally:
            await harness.stop()
            await bridge.stop()

    asyncio.run(scenario())
