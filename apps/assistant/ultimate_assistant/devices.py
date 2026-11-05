"""One operator-facing inventory, retaining phone and Router trust boundaries."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from .phone import DEFAULT_DEVICE


class DeviceAction(BaseModel):
    kind: str
    device_id: str = Field(min_length=1, max_length=200)
    code: str = Field(default="", max_length=20)
    expected_pairing_revision: int | None = None


class PhoneInvitation(BaseModel):
    replace_existing: StrictBool = False
    expected_pairing_revision: int | None = None


_PUBLIC_NODE_FIELDS = ("node_id", "display_name", "hostname", "status", "short_code", "last_seen")
_PUBLIC_PHONE_FIELDS = ("device_id", "paired", "revoked", "pairing_revision", "last_verified")


def _node(row):
    return {key: row.get(key) for key in _PUBLIC_NODE_FIELDS}


def register(app, *, require_operator, router_factory, pairing_factory, invitation):
    dependencies = [Depends(require_operator)]

    @app.get('/api/devices', dependencies=dependencies)
    def inventory():
        pairing = pairing_factory().status(DEFAULT_DEVICE)
        result = {"phone": {key: pairing.get(key) for key in _PUBLIC_PHONE_FIELDS},
                  "computers": [], "router_available": False,
                  "identity_approval": False, "self": None}
        try:
            router = router_factory()
            identity = router.device_identity()
            # The SDK returns an empty identity after a transport error; it
            # does not always raise. Do not present that as a reachable Router.
            if not identity.get('node_id'):
                raise ValueError('Router identity unavailable')
            result.update(self=_node(identity), router_available=True,
                          identity_approval=(identity.get('approval_binding') == 'node_id_and_code_v1' and identity.get('grant_protocol') == 'sealed_v2'))
            result['computers'] = [_node(row) for row in router.pending_devices() + router.trusted_devices()]
        except Exception:
            # Keep phone management available during Router outages. Never echo
            # SDK exception strings, URLs, or credentials into the browser.
            result.update(router_available=False, identity_approval=False, computers=[])
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/api/devices/phone-invitation', dependencies=dependencies)
    def phone_invitation(body: PhoneInvitation):
        store = pairing_factory()
        # Serialize confirmation and invitation creation with native redemption.
        with store._lock:
            current = store.status(DEFAULT_DEVICE)
            if current.get('paired') and (
                not body.replace_existing or
                body.expected_pairing_revision != current.get('pairing_revision')
            ):
                raise HTTPException(409, 'A phone is already paired. Confirm replacing that pairing after refreshing Devices.')
            result = invitation()
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/api/devices/approve', dependencies=dependencies)
    def approve(body: DeviceAction):
        if body.kind != 'computer' or not body.code.strip():
            raise HTTPException(400, 'Select a discovered computer and compare its pairing code.')
        try:
            router = router_factory()
            identity = router.device_identity()
            if identity.get('approval_binding') != 'node_id_and_code_v1' or identity.get('grant_protocol') != 'sealed_v2':
                raise HTTPException(409, 'Update Herald before approving a computer from Devices.')
            result = router.approve_device(body.code, node_id=body.device_id)
            return JSONResponse({'device': _node(result['device']),
                                 'delivery': 'unconfirmed' if result.get('push_warning') else 'sent'},
                                headers={'Cache-Control': 'no-store'})
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, 'Approval did not complete. Refresh Devices and compare the code again.') from None

    @app.post('/api/devices/revoke', dependencies=dependencies)
    def revoke(body: DeviceAction):
        if body.kind == 'phone' and body.device_id == DEFAULT_DEVICE:
            store = pairing_factory()
            with store._lock:
                if store.status(DEFAULT_DEVICE).get('pairing_revision') != body.expected_pairing_revision:
                    raise HTTPException(409, 'Phone pairing changed. Refresh Devices before revoking it.')
                store.revoke(DEFAULT_DEVICE)
        elif body.kind == 'computer':
            try:
                router_factory().revoke_device(body.device_id)
            except Exception:
                raise HTTPException(409, 'Revocation did not complete. Refresh Devices and retry.') from None
        else:
            raise HTTPException(400, 'Unknown device type.')
        return JSONResponse({'revoked': True}, headers={'Cache-Control': 'no-store'})
