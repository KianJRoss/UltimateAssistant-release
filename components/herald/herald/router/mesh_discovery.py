"""Zero-config LAN discovery for Herald nodes, via mDNS (zeroconf).

Advertises this node's identity (node_id, short_code, display_name -- no
secrets) as a `_herald._tcp.local.` service, and listens for other Herald
nodes doing the same. Discovered peers are recorded in mesh_trust as
'pending' until a human approves them from an already-trusted node.

Follows the same start()/stop() + background asyncio.Task shape as
quota_watcher.py, idle_unload.py, and scheduler.py so it wires into
server.py's startup/shutdown hooks the same way those do.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from herald.router.mesh_trust import MeshTrust

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_herald._tcp.local."
POLL_INTERVAL_SECONDS = 30

_zeroconf = None  # type: ignore[assignment]
_browser = None  # type: ignore[assignment]
_service_info = None  # type: ignore[assignment]
_task: asyncio.Task | None = None


def _mesh_trust(db_path: str | None = None) -> MeshTrust:
    return MeshTrust(db_path) if db_path else MeshTrust()


async def _advertise(port: int, trust: MeshTrust) -> None:
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceInfo

    global _zeroconf, _service_info

    identity = trust.self_identity()
    try:
        local_ip = socket.inet_aton(socket.gethostbyname(socket.gethostname()))
    except OSError:
        local_ip = socket.inet_aton("127.0.0.1")

    _service_info = AsyncServiceInfo(
        SERVICE_TYPE,
        f"{identity.node_id}.{SERVICE_TYPE}",
        addresses=[local_ip],
        port=port,
        properties={
            "node_id": identity.node_id,
            "short_code": identity.short_code,
            "display_name": identity.display_name,
            "public_key": identity.public_key_pem,
        },
    )
    _zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
    await _zeroconf.async_register_service(_service_info)


def _on_peer(trust: MeshTrust, info) -> None:
    try:
        props = {
            k.decode(): v.decode() for k, v in (info.properties or {}).items() if v is not None
        }
        node_id = props.get("node_id")
        public_key = props.get("public_key")
        if not node_id or not public_key:
            return
        self_id = trust.self_identity()
        if node_id == self_id.node_id:
            return
        addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        trust.observe(
            node_id=node_id,
            display_name=props.get("display_name", node_id),
            hostname=props.get("display_name", node_id),
            public_key_pem=public_key,
            address=addresses[0] if addresses else None,
            port=info.port,
        )
    except Exception:  # noqa: BLE001 - a malformed peer advert must never crash discovery
        logger.exception("mesh_discovery: failed to process a discovered peer")


async def _browse(trust: MeshTrust) -> None:
    from zeroconf.asyncio import AsyncServiceBrowser

    global _browser

    class _Listener:
        def add_service(self, zc, service_type, name):
            asyncio.ensure_future(self._resolve(zc, service_type, name))

        def update_service(self, zc, service_type, name):
            asyncio.ensure_future(self._resolve(zc, service_type, name))

        def remove_service(self, zc, service_type, name):
            pass

        async def _resolve(self, zc, service_type, name):
            info = await zc.async_get_service_info(service_type, name)
            if info is not None:
                _on_peer(trust, info)

    _browser = AsyncServiceBrowser(_zeroconf.zeroconf, SERVICE_TYPE, listener=_Listener())


async def _worker(port: int) -> None:
    trust = _mesh_trust()
    try:
        await _advertise(port, trust)
        await _browse(trust)
    except Exception:  # noqa: BLE001 - discovery is best-effort, never block router startup
        logger.exception("mesh_discovery: failed to start mDNS advertise/browse")
        return
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


def start(port: int) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_worker(port))


async def stop() -> None:
    global _task, _zeroconf, _browser, _service_info
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    if _zeroconf is not None:
        try:
            if _service_info is not None:
                await _zeroconf.async_unregister_service(_service_info)
            await _zeroconf.async_close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.exception("mesh_discovery: error during shutdown")
        _zeroconf = None
        _service_info = None
    _browser = None
