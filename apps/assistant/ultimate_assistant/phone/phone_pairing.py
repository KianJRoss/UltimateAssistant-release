"""Authenticated, revocable pairing between the assistant and its phone app."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
import threading
import base64
import ctypes
import os
import ipaddress
import shutil
import subprocess

from . import DEFAULT_DEVICE


PHONE_CAPABILITIES = frozenset(("conversation", "task_capture", "handoff", "mode_intent", "file_share"))
NATIVE_ANDROID_CAPABILITIES = frozenset(("android_app_control", "accessibility", "notification_access"))
ANDROID_GRANT_EVIDENCE = NATIVE_ANDROID_CAPABILITIES | frozenset(("microphone", "notifications", "privileged_control", "overlay"))
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class PairingStore:
    def __init__(self, home, *, clock=None):
        self.home, self.clock = Path(home), clock or time.time
        self.path = self.home / "phone-pairing.json"
        self._lock = _path_lock(self.path)

    def _load(self):
        if not self.path.is_file():
            return {"devices": {}, "challenges": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"devices": {}, "challenges": {}}

    def _save(self, value):
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def begin(self, device_id=DEFAULT_DEVICE, *, ttl=300):
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        device_id = device_id.strip()
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._lock:
            data, challenge = self._load(), secrets.token_urlsafe(24)
            data["challenges"][challenge] = {"device_id": device_id, "expires": self.clock() + ttl, "used": False}
            self._save(data)
            return {"device_id": device_id, "challenge": challenge, "expires_at": data["challenges"][challenge]["expires"]}

    @staticmethod
    def response(secret, challenge):
        if not secret:
            raise PermissionError("secret is required")
        return hmac.new(secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()

    def complete(self, challenge, response, *, secret, advertised_capabilities=()):
        with self._lock:
            data = self._load()
            item = data["challenges"].get(challenge)
            if not item or item["used"] or item["expires"] <= self.clock():
                raise ValueError("pairing challenge is invalid, expired, or already used")
            if not secret:
                raise PermissionError("pairing secret is required")
            if not hmac.compare_digest(self.response(secret, challenge), response):
                raise PermissionError("pairing authentication failed")
            requested = set(advertised_capabilities)
            forbidden = requested & NATIVE_ANDROID_CAPABILITIES
            unknown = requested - PHONE_CAPABILITIES - NATIVE_ANDROID_CAPABILITIES
            if forbidden or unknown:
                raise ValueError("forged or unsupported phone capability advertisement")
            item["used"] = True
            prior = data["devices"].get(item["device_id"], {})
            credential = secrets.token_urlsafe(32)
            device = {
                "device_id": item["device_id"],
                "state": "paired_not_connected",
                "revoked": False,
                "capabilities": sorted(requested),
                "paired_at": self.clock(),
                "last_verified": None,
                "pairing_revision": int(prior.get("pairing_revision", 0)) + 1,
                "credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
                "reported_grants": {},
            }
            data["devices"][item["device_id"]] = device
            self._save(data)
            result = dict(device)
            result.pop("credential_hash", None)
            result["device_credential"] = credential
            return result

    def redeem(self, challenge, *, device_id=DEFAULT_DEVICE):
        """Consume an operator-created invitation; possession is the secret.

        Native redemption reuses complete's atomic single-use/fencing path.
        Platform permissions are never granted by redeeming an invitation.
        """
        if not isinstance(challenge, str) or not challenge or len(challenge) > 256:
            raise ValueError("bounded invitation challenge is required")
        with self._lock:
            item = self._load()["challenges"].get(challenge)
            if not item or item.get("device_id") != device_id:
                raise ValueError("pairing invitation is invalid")
            return self.complete(
                challenge, self.response(challenge, challenge), secret=challenge,
                advertised_capabilities=sorted(PHONE_CAPABILITIES))

    @staticmethod
    def _protected_credential(value, *, decrypt=False):
        """Protect retry credentials with the Windows process user's DPAPI key."""
        if os.name != "nt":
            try:
                import keyring
                backend = keyring.get_keyring()
                if backend.priority <= 0 or "plaintext" in type(backend).__module__.lower():
                    raise PermissionError("a secure OS credential vault is required")
                if decrypt:
                    if not value.startswith("keyring:"):
                        raise PermissionError("credential storage format is unavailable")
                    credential = keyring.get_password("UltimateAssistant.phone", value[8:])
                    if not credential:
                        raise PermissionError("protected credential is unavailable")
                    return credential
                reference = secrets.token_hex(24)
                keyring.set_password("UltimateAssistant.phone", reference, value)
                return "keyring:" + reference
            except (ImportError, RuntimeError) as exc:
                raise PermissionError("automatic pairing requires a secure OS credential vault") from exc
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        raw = base64.b64decode(value) if decrypt else value.encode()
        buffer = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        function = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
                    else ctypes.windll.crypt32.CryptProtectData)
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), None, None, None, None, 1,
                        ctypes.byref(target)):
            raise PermissionError("protected credential storage is unavailable")
        try:
            output = ctypes.string_at(target.data, target.size)
            return output.decode() if decrypt else base64.b64encode(output).decode()
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def auto_pair(self, *, peer, forwarded_for, forwarded_proto, login,
                  installation_id=None, device_id=DEFAULT_DEVICE):
        """Enroll only the configured node through the trusted local Serve proxy.

        The server must disable uvicorn proxy_headers so peer is the actual
        socket address. Serve strips/repopulates identity headers; whois fences
        the client IP against the explicitly configured stable node and owner.
        """
        try:
            if not ipaddress.ip_address(peer).is_loopback or forwarded_proto != "https":
                raise ValueError("untrusted transport")
            address = str(ipaddress.ip_address(forwarded_for))
        except (ValueError, TypeError) as exc:
            raise PermissionError("automatic pairing requires trusted HTTPS Serve transport") from exc
        if not login or not isinstance(login, str):
            raise PermissionError("Tailscale identity is required")
        try:
            config = json.loads((self.home.parent / "tailscale-phone.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise PermissionError("automatic pairing is not configured") from exc
        authorized = [item for item in config.get("allowed_nodes", [])
                      if isinstance(item, dict) and item.get("device_id") == device_id
                      and item.get("stable_id") and item.get("user_id")]
        if config.get("require_serve_identity") is not True or not authorized:
            raise PermissionError("automatic pairing is not authorized")
        executable = config.get("tailscale_executable") or shutil.which("tailscale")
        if not executable and os.name == "nt":
            executable = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) /
                             "Tailscale" / "tailscale.exe")
        try:
            proc = subprocess.run([executable, "whois", "--json", address],
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            identity = json.loads(proc.stdout) if proc.returncode == 0 else {}
            node, user = identity.get("Node") or {}, identity.get("UserProfile") or {}
            if not isinstance(node, dict) or not isinstance(user, dict):
                raise ValueError("invalid Tailscale identity")
        except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
            raise PermissionError("Tailscale identity verification is unavailable") from exc
        allowed = any(node.get("StableID") == item["stable_id"]
                      and str(node.get("User")) == str(item["user_id"])
                      and str(user.get("ID")) == str(item["user_id"])
                      for item in authorized)
        if not allowed or user.get("LoginName") != login:
            raise PermissionError("Tailscale node or owner is not authorized")
        installation_id = installation_id or node["StableID"]
        if (not isinstance(installation_id, str) or not installation_id.strip()
                or len(installation_id) > 256):
            raise ValueError("bounded installation_id is required")
        install_hash = hashlib.sha256(installation_id.encode()).hexdigest()
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if device and device.get("revoked"):
                raise PermissionError("device was revoked; operator authorization is required")
            cached = device.get("auto_pairing", {}) if device else {}
            if (cached.get("installation_hash") == install_hash
                    and cached.get("stable_node_id") == node["StableID"]
                    and cached.get("credential")):
                credential = self._protected_credential(cached["credential"], decrypt=True)
                self.authenticate(credential, device_id,
                                  expected_pairing_revision=device["pairing_revision"],
                                  connection_evidence="tailscale-serve:" + node["StableID"])
                return dict(self._public(self._load()["devices"][device_id]),
                            device_credential=credential)
            # Check protected storage before rotating an existing credential.
            self._protected_credential("storage-check")
            invitation = self.begin(device_id)
            paired = self.redeem(invitation["challenge"], device_id=device_id)
            data = self._load()
            device = data["devices"][device_id]
            device["auto_pairing"] = {
                "installation_hash": install_hash, "stable_node_id": node["StableID"],
                "owner_id": str(user["ID"]), "login": login,
                "credential": self._protected_credential(paired["device_credential"])}
            device.update(state="connected", last_verified=self.clock(),
                          evidence_id="tailscale-serve:" + node["StableID"])
            self._save(data)
            return dict(self._public(device), device_credential=paired["device_credential"])

    def authenticate(self, credential, device_id=DEFAULT_DEVICE, *, expected_pairing_revision=None,
                     connection_evidence=None):
        """Authenticate one device-scoped credential without exposing its hash."""
        if not isinstance(credential, str) or not credential:
            raise PermissionError("device credential is required")
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if not device or device.get("revoked"):
                raise PermissionError("device credential is revoked or unknown")
            if expected_pairing_revision is not None and device.get("pairing_revision") != expected_pairing_revision:
                raise PermissionError("pairing revision is stale")
            supplied = hashlib.sha256(credential.encode()).hexdigest()
            if not hmac.compare_digest(str(device.get("credential_hash", "")), supplied):
                raise PermissionError("device authentication failed")
            if connection_evidence is not None:
                if not isinstance(connection_evidence, str) or not connection_evidence.strip():
                    raise ValueError("connection evidence is required")
                device.update(state="connected", last_verified=self.clock(),
                              evidence_id=connection_evidence.strip())
                self._save(data)
            return self._public(device)

    def report_grant_evidence(self, device_id=DEFAULT_DEVICE, *, pairing_revision,
                              grants, evidence_id):
        """Record permissions observed by Android; never infer platform grants."""
        if not isinstance(grants, dict) or not evidence_id or not str(evidence_id).strip():
            raise ValueError("bounded grant evidence is required")
        unknown = set(grants) - ANDROID_GRANT_EVIDENCE
        if unknown or any(type(value) is not bool for value in grants.values()):
            raise ValueError("unsupported Android grant evidence")
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if not device or device.get("revoked"):
                raise PermissionError("device is not paired")
            if device.get("pairing_revision") != pairing_revision:
                raise PermissionError("pairing revision is stale")
            device["reported_grants"] = dict(grants)
            device["grant_evidence_id"] = str(evidence_id).strip()
            device["grants_reported_at"] = self.clock()
            self._save(data)
            return self._public(device)

    @staticmethod
    def _public(device):
        result = dict(device)
        result.pop("credential_hash", None)
        result.pop("auto_pairing", None)
        result["connected"] = result.get("state") == "connected" and not result.get("revoked", False)
        result["reported_grants"] = dict(result.get("reported_grants", {}))
        result["android_control"] = bool(
            result.get("connected") and
            result["reported_grants"].get("android_app_control") and
            result["reported_grants"].get("accessibility")
        )
        return result

    def record_local_connection_evidence(self, device_id=DEFAULT_DEVICE, *, evidence_id):
        if not evidence_id or not str(evidence_id).strip():
            raise ValueError("local evidence id is required")
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if not device or device.get("revoked"):
                raise PermissionError("device is not paired")
            device.update(state="connected", last_verified=self.clock(), evidence_id=str(evidence_id).strip())
            self._save(data)
            return self._public(device)

    def revoke(self, device_id=DEFAULT_DEVICE):
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            for invitation in data["challenges"].values():
                if invitation.get("device_id") == device_id:
                    invitation["used"] = True
            if device:
                device.pop("auto_pairing", None)
                device.update(revoked=True, state="revoked", capabilities=[],
                              pairing_revision=int(device.get("pairing_revision", 0)) + 1)
            self._save(data)
        return self.status(device_id)

    def authorize(self, capability, device_id=DEFAULT_DEVICE, *, expected_pairing_revision=None):
        """Re-check a scoped grant immediately before execution.

        This performs no device action.  Callers that accepted work earlier can
        carry ``pairing_revision`` and reliably reject it after revocation or
        re-pairing.
        """
        if capability not in PHONE_CAPABILITIES:
            raise PermissionError("capability is not permitted by the phone contract")
        status = self.status(device_id)
        if expected_pairing_revision is not None and status.get("pairing_revision") != expected_pairing_revision:
            raise PermissionError("pairing changed after work was accepted")
        if not status.get("connected") or capability not in status.get("capabilities", ()):
            raise PermissionError("device is not connected with the requested capability")
        return {"device_id": device_id, "capability": capability,
                "pairing_revision": status["pairing_revision"], "authorized": True}

    def status(self, device_id=DEFAULT_DEVICE):
        data = self._load()
        device = data["devices"].get(device_id)
        if not device:
            return {
                "device_id": device_id,
                "state": "not_connected",
                "connected": False,
                "revoked": False,
                "capabilities": [],
                "android_control": False,
            }
        result = self._public(device)
        reported = result.get("grants_reported_at")
        age = max(0.0, self.clock() - reported) if isinstance(reported, (int, float)) else None
        result["grant_evidence_age_seconds"] = age
        result["permission_evidence_state"] = "unobserved" if age is None else ("recent" if age <= 120 else "stale")
        result["paired"] = not result.get("revoked", False)
        result["readiness"] = "unknown_until_native_device_status"
        result["android_control"] = bool(
            result["connected"] and age is not None and age <= 120 and
            result.get("reported_grants", {}).get("android_app_control") and
            result.get("reported_grants", {}).get("accessibility")
        )
        return result
