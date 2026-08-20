"""The edge gatekeeper: the only door into this lab's ROS 2 graph.

``foxglove_bridge`` has no authentication of its own, so it is bound to
localhost and this process is what the Cloudflare Tunnel actually reaches. Every
browser WebSocket terminates here; the gatekeeper authenticates it, decides
whether each frame may actuate the robot (``policy.py``), and only then relays
it to the bridge.

It also does the thing this lab exists for: **fan-out of what the operator is
doing**. One person holds the control lease; everyone else is a spectator, and
a spectator who can only see joint angles cannot tell a deliberate move from a
fault. So every authorized command is broadcast to every connected session as
an ``activity`` frame, immediately, with the name of the person who issued it.
Telemetry answers *what the arm is doing*; the activity feed answers *who asked
it to, and for what*.

Run it with the ROS 2 workspace sourced or not — it never imports rclpy. It
speaks to ROS exclusively through the bridge, which means it keeps working (and
keeps refusing unauthorized callers) even while the robot stack is restarting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import aiohttp
from aiohttp import web

from . import cdr, policy, protocol
from .auth import AuthError, Identity, LeaseVerifier, TokenVerifier, probe_jwks

log = logging.getLogger('primbio.gateway')

# What the browser speaks to us. Kept as the original name because that is what
# the web client sends.
FOXGLOVE_SUBPROTOCOL = 'foxglove.websocket.v1'

# What the bridge might speak. foxglove_bridge renamed the subprotocol at 3.x
# ('foxglove.sdk.v1') and rejects the old name with a bare HTTP 400, which
# reads like a routing fault rather than a negotiation one. Offering both lets
# the server pick and keeps this working across bridge versions.
UPSTREAM_SUBPROTOCOLS = ['foxglove.sdk.v1', 'foxglove.websocket.v1']

# A socket that has not identified itself this quickly is closed. Short on
# purpose: an unauthenticated socket is a resource an anonymous caller controls.
AUTH_TIMEOUT_S = 5.0

# WebSocket close codes (4000-4999 is the application-defined range).
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_UPSTREAM_DOWN = 4502

# Cap on how much of a service-call payload is echoed into the activity feed.
ACTIVITY_DETAIL_LIMIT = 512


# eq=False keeps identity semantics: two connections from the same person are
# two sessions, and the registry below is a set of live sockets.
@dataclass(eq=False)
class Session:
    """One authenticated browser connection."""

    id: str
    identity: Identity
    ws: web.WebSocketResponse
    lease_token: str = ''
    # serviceId → name, learned from the bridge's advertiseServices frames.
    services: Dict[int, str] = field(default_factory=dict)
    # channelId → topic, learned from this client's own advertise frames.
    channels: Dict[int, str] = field(default_factory=dict)

    def holds_lease(self, leases: LeaseVerifier) -> bool:
        return leases.holds_lease(self.lease_token, self.identity)


class Gateway:
    def __init__(self, config: 'Config'):
        self.config = config
        self.tokens = TokenVerifier(
            supabase_url=config.supabase_url,
            project_id=config.project_id,
            jwt_secret=config.jwt_secret,
            default_role=config.default_role,
        )
        self.leases = LeaseVerifier(config.lease_secret, config.lab_slug)
        self.sessions: Set[Session] = set()

    # ── Activity fan-out ────────────────────────────────────────────────────

    async def broadcast(self, message: dict) -> None:
        """Send a frame to every live session. Never raises: one dead socket
        must not stop the others from being told what just happened."""
        payload = json.dumps(message)
        for session in list(self.sessions):
            try:
                await session.ws.send_str(payload)
            except Exception:
                self.sessions.discard(session)

    async def announce(
        self,
        session: Session,
        action: str,
        detail: Any = None,
        is_stop: bool = False,
    ) -> None:
        await self.broadcast(
            {
                'op': 'activity',
                'id': uuid.uuid4().hex[:12],
                'ts': int(time.time() * 1000),
                'user': {
                    'id': session.identity.user_id,
                    'name': session.identity.name,
                },
                'action': action,
                'detail': detail,
                'stop': is_stop,
            }
        )

    async def announce_presence(self, event: str, session: Session) -> None:
        await self.broadcast(
            {
                'op': 'presence',
                'event': event,
                'ts': int(time.time() * 1000),
                'user': {
                    'id': session.identity.user_id,
                    'name': session.identity.name,
                    'role': session.identity.role,
                },
                'viewers': len(self.sessions),
            }
        )

    # ── WebSocket entry point ───────────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(
            protocols=[FOXGLOVE_SUBPROTOCOL], heartbeat=20.0, max_msg_size=16 * 1024 * 1024
        )
        await ws.prepare(request)

        try:
            identity, lease_token = await self._authenticate(ws)
        except AuthError as exc:
            log.info('[ws] rejected from %s: %s', request.remote, exc)
            await ws.close(code=CLOSE_UNAUTHORIZED, message=b'unauthorized')
            return ws

        session = Session(
            id=uuid.uuid4().hex[:12],
            identity=identity,
            ws=ws,
            lease_token=lease_token,
        )

        # Connect to the bridge only after the client has proved who it is, so
        # an unauthenticated caller can never cause an upstream connection.
        try:
            upstream_session, upstream = await self._connect_upstream()
        except Exception as exc:
            log.warning('[ws] upstream unavailable: %s', exc)
            await ws.send_str(
                json.dumps({'op': 'gatewayError', 'reason': 'robot bridge unavailable'})
            )
            await ws.close(code=CLOSE_UPSTREAM_DOWN, message=b'bridge unavailable')
            return ws

        self.sessions.add(session)
        log.info(
            '[ws] %s (%s, role=%s) connected — %d viewer(s)',
            identity.name,
            identity.email,
            identity.role,
            len(self.sessions),
        )
        await ws.send_str(
            json.dumps(
                {
                    'op': 'hello',
                    'session': session.id,
                    'user': {
                        'id': identity.user_id,
                        'name': identity.name,
                        'email': identity.email,
                        'role': identity.role,
                    },
                    'holdsLease': session.holds_lease(self.leases),
                    'viewers': len(self.sessions),
                }
            )
        )
        await self.announce_presence('join', session)

        # Whichever side goes away first ends the session: gathering both would
        # leave the bridge pump waiting forever on a socket whose browser is
        # already gone, leaking an upstream connection per disconnect.
        pumps = [
            asyncio.create_task(self._pump_client_to_bridge(session, upstream)),
            asyncio.create_task(self._pump_bridge_to_client(session, upstream)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            self.sessions.discard(session)
            self._release_lease(session)
            await self.announce_presence('leave', session)
            log.info(
                '[ws] %s disconnected — %d viewer(s)', identity.name, len(self.sessions)
            )
            for closeable in (upstream, upstream_session):
                try:
                    await closeable.close()
                except Exception:
                    pass
        return ws

    async def _authenticate(self, ws: web.WebSocketResponse) -> tuple[Identity, str]:
        """Consume the mandatory first frame and resolve it to an identity."""
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=AUTH_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise AuthError('no auth frame within timeout') from exc

        if first.type is not aiohttp.WSMsgType.TEXT:
            raise AuthError('first frame was not a text auth frame')
        try:
            message = json.loads(first.data)
        except Exception as exc:
            raise AuthError('first frame was not valid JSON') from exc
        if message.get('op') != 'auth':
            raise AuthError(f"first frame op was {message.get('op')!r}, expected 'auth'")

        identity = self.tokens.verify(str(message.get('token') or ''))
        return identity, str(message.get('lease') or '')

    async def _connect_upstream(self):
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                self.config.bridge_url,
                protocols=UPSTREAM_SUBPROTOCOLS,
                heartbeat=20.0,
                max_msg_size=16 * 1024 * 1024,
            )
        except Exception:
            await session.close()
            raise
        return session, ws

    # ── Frame pumps ─────────────────────────────────────────────────────────

    async def _pump_client_to_bridge(
        self, session: Session, upstream: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Police everything the browser sends before it reaches ROS."""
        async for msg in session.ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self._handle_client_text(session, upstream, msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await self._handle_client_binary(session, upstream, msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    async def _handle_client_text(
        self, session: Session, upstream: aiohttp.ClientWebSocketResponse, raw: str
    ) -> None:
        try:
            message = json.loads(raw)
        except Exception:
            await self._deny(session, 'malformed frame')
            return
        op = str(message.get('op') or '')

        # Lease refresh is ours, not the bridge's: never forwarded.
        if op == 'lease':
            session.lease_token = str(message.get('token') or '')
            # An empty token is the browser saying it has stopped driving —
            # which it does as soon as the state stream tells it somebody else
            # is. Letting go of the claim here is what makes a handover or an
            # admin force take effect at once instead of one token later.
            if not session.lease_token:
                self._release_lease(session)
            await session.ws.send_str(
                json.dumps(
                    {'op': 'leaseAck', 'holdsLease': session.holds_lease(self.leases)}
                )
            )
            return

        decision = policy.check_json_op(op, session.identity, session.holds_lease(self.leases))
        if not decision.allowed:
            await self._deny(session, decision.reason, op=op)
            return

        if op == 'advertise':
            session.channels.update(protocol.channel_topics(message))
        elif op == 'unadvertise':
            for channel_id in message.get('channelIds') or []:
                session.channels.pop(int(channel_id), None)

        if decision.activity:
            await self.announce(
                session, decision.activity, message.get('parameters'), decision.is_stop
            )
        await upstream.send_str(raw)

    async def _handle_client_binary(
        self, session: Session, upstream: aiohttp.ClientWebSocketResponse, data: bytes
    ) -> None:
        if not data:
            return
        opcode = data[0]

        if opcode == protocol.CLIENT_SERVICE_CALL_REQUEST:
            call = protocol.parse_service_call(data)
            if call is None:
                await self._deny(session, 'malformed service call')
                return
            name = session.services.get(call.service_id)
            decision = policy.check_service_call(
                name, session.identity, session.holds_lease(self.leases)
            )
            if not decision.allowed:
                log.info(
                    '[policy] denied %s -> %s: %s',
                    session.identity.email,
                    name or f'service#{call.service_id}',
                    decision.reason,
                )
                await self._deny(session, decision.reason, call_id=call.call_id)
                return
            if decision.activity:
                await self.announce(
                    session,
                    decision.activity,
                    _decode_detail(call, name),
                    decision.is_stop,
                )
            await upstream.send_bytes(data)
            return

        if opcode == protocol.CLIENT_MESSAGE_DATA:
            channel_id = protocol.parse_client_publish(data)
            topic = session.channels.get(channel_id) if channel_id is not None else None
            decision = policy.check_publish(
                topic, session.identity, session.holds_lease(self.leases)
            )
            if not decision.allowed:
                await self._deny(session, decision.reason)
                return
            await upstream.send_bytes(data)
            return

        await self._deny(session, f'binary opcode {opcode} is not allowed')

    async def _pump_bridge_to_client(
        self, session: Session, upstream: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Relay the bridge's frames, learning the service table on the way."""
        async for msg in upstream:
            if msg.type is aiohttp.WSMsgType.TEXT:
                try:
                    message = json.loads(msg.data)
                    if message.get('op') == 'advertiseServices':
                        session.services.update(protocol.service_names(message))
                    elif message.get('op') == 'unadvertiseServices':
                        for service_id in message.get('serviceIds') or []:
                            session.services.pop(int(service_id), None)
                except Exception:
                    pass
                await session.ws.send_str(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await session.ws.send_bytes(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    def _release_lease(self, session: Session) -> None:
        """Drop this person's claim on the lease — unless they are still
        driving from somewhere else.

        Two tabs of the same person are two sessions (Session is compared by
        identity, not by value), so one of them closing or standing down must
        not hand the arm to the next claimant while the other is still holding
        a token.
        """
        user_id = session.identity.user_id
        for other in self.sessions:
            if other is not session and other.identity.user_id == user_id and other.lease_token:
                return
        self.leases.release(user_id)

    async def _deny(
        self,
        session: Session,
        reason: str,
        op: str = '',
        call_id: Optional[int] = None,
    ) -> None:
        await session.ws.send_str(
            json.dumps(
                {
                    'op': 'denied',
                    'reason': reason,
                    'request': op or None,
                    'callId': call_id,
                }
            )
        )

    # ── HTTP: emergency stop and health ─────────────────────────────────────

    async def handle_estop(self, request: web.Request) -> web.Response:
        """Server-to-server emergency stop from the lab web app.

        A second, independent path to the same stop the browser can trigger
        over its own socket: if the operator's WebSocket is wedged, the app can
        still reach the hardware. Requires the operator role and, like every
        other stop, no lease.
        """
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.lower().startswith('bearer ') else ''
        try:
            identity = self.tokens.verify(token)
        except AuthError as exc:
            log.info('[estop] rejected: %s', exc)
            return web.json_response({'error': 'unauthorized'}, status=401)
        if not identity.at_least('operator'):
            return web.json_response({'error': 'operator role required'}, status=403)

        ok, detail = await self._call_service_once('/weblab/estop')
        await self.broadcast(
            {
                'op': 'activity',
                'id': uuid.uuid4().hex[:12],
                'ts': int(time.time() * 1000),
                'user': {'id': identity.user_id, 'name': identity.name},
                'action': '/weblab/estop',
                'detail': {'via': 'http', 'delivered': ok},
                'stop': True,
            }
        )
        log.warning('[estop] triggered by %s — delivered=%s', identity.email, ok)
        return web.json_response({'ok': ok, 'detail': detail}, status=200 if ok else 502)

    async def _call_service_once(self, service_name: str) -> tuple[bool, str]:
        """Open a throwaway bridge connection and call one service.

        Used only by the HTTP e-stop path. A dedicated connection avoids
        entangling a safety command with any browser session's state.
        """
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(
                    self.config.bridge_url, protocols=UPSTREAM_SUBPROTOCOLS
                ) as ws:
                    service_id = await self._await_service_id(ws, service_name)
                    if service_id is None:
                        return False, f'{service_name} is not advertised'
                    # CDR, not JSON. The bridge advertises `cdr` as the request
                    # encoding for every ROS 2 service and answers anything
                    # else with "Unsupported encoding" — which on this path
                    # means the backup emergency stop is refused before it ever
                    # reaches ROS, while this function cheerfully reported it
                    # dispatched. The stop is the one call that must not have a
                    # silent failure mode.
                    call_id = 1
                    encoding = b'cdr'
                    frame = (
                        bytes([protocol.CLIENT_SERVICE_CALL_REQUEST])
                        + struct.pack('<III', service_id, call_id, len(encoding))
                        + encoding
                        + cdr.EMPTY_REQUEST
                    )
                    await ws.send_bytes(frame)
                    # The stop has been handed to ROS; the response only tells
                    # us how it went, so a slow reply must not delay the caller
                    # for long.
                    return await self._await_call_result(ws, call_id)
        except Exception as exc:
            return False, str(exc)

    async def _await_call_result(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        call_id: int,
        timeout: float = 4.0,
    ) -> tuple[bool, str]:
        """Wait for the bridge's verdict on one service call.

        The bridge interleaves advertisements and status frames with replies,
        so this reads until it sees something about *this* call rather than
        treating the next frame as the answer — which is how a stray advertise
        used to be reported as a successful stop.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = await asyncio.wait_for(
                    ws.receive(), timeout=deadline - time.monotonic()
                )
            except asyncio.TimeoutError:
                break
            if reply.type is aiohttp.WSMsgType.TEXT:
                try:
                    message = json.loads(reply.data)
                except Exception:
                    continue
                if (
                    message.get('op') == 'serviceCallFailure'
                    and message.get('callId') == call_id
                ):
                    return False, str(message.get('message') or 'call failed')
            elif reply.type is aiohttp.WSMsgType.BINARY:
                data = reply.data
                if not data or data[0] != protocol.SERVER_SERVICE_CALL_RESPONSE:
                    continue
                if len(data) < 13:
                    continue
                _sid, reply_call_id, encoding_len = struct.unpack_from(
                    '<III', data, 1)
                if reply_call_id != call_id:
                    continue
                result = cdr.read_result(data[13 + encoding_len:])
                if result is None:
                    return True, 'dispatched (unreadable response)'
                return bool(result['ok']), str(result['message'] or 'ok')
            elif reply.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                return False, 'bridge closed the connection'
        return True, f'dispatched (no response within {timeout:g}s)'

    async def _await_service_id(
        self, ws: aiohttp.ClientWebSocketResponse, name: str, timeout: float = 5.0
    ) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                message = json.loads(msg.data)
            except Exception:
                continue
            if message.get('op') == 'advertiseServices':
                for service_id, service_name in protocol.service_names(message).items():
                    if service_name == name:
                        return service_id
        return None

    # ── HTTP: video passthrough ─────────────────────────────────────────────

    async def handle_video(self, request: web.Request) -> web.StreamResponse:
        """Proxy /api/video/* to go2rtc, stripping the prefix.

        Cloudflare Tunnel ingress cannot rewrite paths, so a rule pointing
        /api/video/* straight at go2rtc would deliver /api/video/api/webrtc to
        a server that serves /api/webrtc. Routing it through here keeps the
        tunnel config to a single rule and puts all path knowledge in one file.

        Only the WebRTC *signalling* crosses this proxy — an SDP offer and an
        answer. The media itself is a peer connection that never touches this
        process. The MSE fallback does stream through it, which is the price of
        working at all on a network where UDP hole punching fails.
        """
        # Video is lab data. The WebSocket has always authenticated itself;
        # this route did not, which meant anyone who knew the tunnel hostname
        # could watch the cameras. The browser reaches these routes through
        # fetch(), so it can carry the same bearer token the socket sends.
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.lower().startswith('bearer ') else ''
        if not token:
            token = request.query.get('access_token', '')
        try:
            self.tokens.verify(token)
        except AuthError as exc:
            log.info('[video] rejected from %s: %s', request.remote, exc)
            return web.json_response({'error': 'unauthorized'}, status=401)

        tail = request.match_info.get('tail', '')
        target = f'{self.config.go2rtc_url}/{tail}'
        if request.query_string:
            target = f'{target}?{request.query_string}'

        # WebSocket upgrade (go2rtc's MSE/player socket).
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self._proxy_websocket(request, target)

        body = await request.read()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.request(
                    request.method,
                    target,
                    data=body or None,
                    headers={k: v for k, v in request.headers.items()
                             if k.lower() in ('content-type', 'accept')},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as upstream:
                    payload = await upstream.read()
                    return web.Response(
                        status=upstream.status,
                        body=payload,
                        content_type=upstream.content_type,
                    )
        except Exception as exc:
            log.warning('[video] proxy failed for %s: %s', target, exc)
            return web.json_response({'error': 'video unavailable'}, status=502)

    async def _proxy_websocket(
        self, request: web.Request, target: str
    ) -> web.WebSocketResponse:
        client = web.WebSocketResponse()
        await client.prepare(request)
        target = target.replace('http://', 'ws://', 1)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(target) as upstream:

                    async def pump(source, sink):
                        async for message in source:
                            if message.type is aiohttp.WSMsgType.TEXT:
                                await sink.send_str(message.data)
                            elif message.type is aiohttp.WSMsgType.BINARY:
                                await sink.send_bytes(message.data)
                            else:
                                break

                    tasks = [
                        asyncio.create_task(pump(client, upstream)),
                        asyncio.create_task(pump(upstream, client)),
                    ]
                    try:
                        await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            log.warning('[video] websocket proxy failed: %s', exc)
        return client

    async def handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated liveness. Exposes no lab data on purpose — an
        operator has to be able to curl it while debugging the tunnel."""
        return web.json_response(
            {
                'ok': True,
                'lab': self.config.lab_slug,
                'viewers': len(self.sessions),
                'leaseVerification': self.leases.enabled,
                'leaseHeld': self.leases.held,
                'bridge': self.config.bridge_url,
            }
        )


def _decode_detail(call: protocol.ServiceCall, name: Optional[str]) -> Any:
    """Best-effort, size-capped view of a service payload for the activity feed.

    The client speaks CDR — the only encoding the bridge accepts — so this has
    to read CDR to say which axis was jogged or what speed was set. Reporting
    ``{'encoding': 'cdr'}`` instead, as this did, left every entry in the feed
    without its detail: "movió un eje", never which one.
    """
    payload = call.payload[:ACTIVITY_DETAIL_LIMIT]
    if call.encoding == 'json':
        try:
            return json.loads(payload.decode('utf-8'))
        except Exception:
            return None
    if call.encoding == 'cdr' and name:
        return cdr.read_request(name, payload)
    return None


# ── Configuration and entry point ───────────────────────────────────────────


@dataclass
class Config:
    supabase_url: str
    project_id: str
    lab_slug: str
    lease_secret: str
    jwt_secret: str
    bridge_url: str
    go2rtc_url: str
    host: str
    port: int
    default_role: str

    @classmethod
    def from_env(cls) -> 'Config':
        missing = [
            name
            for name in ('SUPABASE_URL', 'LAB_PROJECT_ID', 'LAB_SLUG')
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(
                'gateway: missing required environment: ' + ', '.join(missing)
            )
        return cls(
            supabase_url=os.environ['SUPABASE_URL'],
            project_id=os.environ['LAB_PROJECT_ID'],
            lab_slug=os.environ['LAB_SLUG'],
            lease_secret=os.environ.get('LAB_CONTROL_SIGNING_SECRET', ''),
            jwt_secret=os.environ.get('SUPABASE_JWT_SECRET', ''),
            bridge_url=os.environ.get('FOXGLOVE_BRIDGE_URL', 'ws://127.0.0.1:8765'),
            go2rtc_url=os.environ.get(
                'GO2RTC_URL', 'http://127.0.0.1:1984').rstrip('/'),
            host=os.environ.get('GATEWAY_HOST', '127.0.0.1'),
            port=int(os.environ.get('GATEWAY_PORT', '8766')),
            # Matches the platform rule that every authenticated account is a
            # viewer of every project; set it empty to close the lab to
            # everyone who holds no explicit role (see auth.TokenVerifier).
            default_role=os.environ.get('LAB_DEFAULT_ROLE', 'viewer'),
        )


# Typed key rather than a bare string: aiohttp warns about the latter, and the
# tests reach for the gateway instance through it.
GATEWAY_KEY: web.AppKey['Gateway'] = web.AppKey('gateway')


# The UI and the control endpoint are always different origins — in production
# dobot-cr3.primbiolab.org and dobot-cr3-control.primbiolab.org, on a bench
# localhost:3000 and localhost:8766 — so the browser preflights every video
# request. Without these headers the camera wall silently shows nothing while
# the WebSocket, which is not subject to CORS, works fine: a confusing split
# that costs real time to diagnose.
#
# Only the video routes need it. The WebSocket authenticates itself, and
# echoing the caller's Origin here grants nothing that the go2rtc proxy did not
# already expose — which is itself worth closing separately.
@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get('Origin')
    if request.method == 'OPTIONS':
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Vary'] = 'Origin'
    return response


def build_app(config: Config) -> web.Application:
    gateway = Gateway(config)
    app = web.Application(middlewares=[cors_middleware])
    app[GATEWAY_KEY] = gateway
    app.add_routes(
        [
            web.get('/ws', gateway.handle_ws),
            web.get('/health', gateway.handle_health),
            web.post('/api/estop', gateway.handle_estop),
            web.route('*', '/api/video/{tail:.*}', gateway.handle_video),
            web.options('/api/estop', lambda _r: web.Response(status=204)),
        ]
    )

    async def on_start(_: web.Application) -> None:
        if not gateway.leases.enabled:
            log.warning(
                '[gateway] LAB_CONTROL_SIGNING_SECRET is unset — no lease token can '
                'be verified, so every motion command will be refused. The lab is '
                'view-only until it is configured.'
            )
        async with aiohttp.ClientSession() as http:
            reachable = await probe_jwks(http, gateway.tokens.jwks_url)
        log.info(
            '[gateway] lab=%s project=%s bridge=%s jwks=%s',
            config.lab_slug,
            config.project_id,
            config.bridge_url,
            'reachable' if reachable else 'UNREACHABLE (will retry per request)',
        )

    app.on_startup.append(on_start)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get('GATEWAY_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)-7s %(message)s',
    )
    config = Config.from_env()
    web.run_app(build_app(config), host=config.host, port=config.port)


if __name__ == '__main__':
    main()
