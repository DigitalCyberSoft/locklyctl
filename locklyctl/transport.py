"""Synchronous transport for locklyctl: REST (requests) + MQTT (paho).

Reimplements LocklyHA's async ``api_*`` functions synchronously, keeping their
exact request shapes, and adds automatic transport selection (hub ``senddata``
vs the MQTT device channel) plus the nonce/host-password refresh that
state-changing commands require.

All the fiddly crypto/frame/parse work is in :mod:`protocol` (verbatim from
LocklyHA). This module only moves bytes.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import requests

from . import ble as BLE
from . import features as F
from . import protocol as P
from .capabilities import LockCapabilities, resolve_capabilities
from .const import (
    API_BASE,
    API_USER_AGENT,
    COMMON_BODY,
    EVENT_LOG_BASE,
    HTTP_TIMEOUT,
    MATTER_HUB_PREFIX,
    MQTT_BROKER,
    MQTT_CLIENT_TOPIC_PREFIX,
    MQTT_PORT,
    MQTT_PUBLISH_TOPIC,
    MQTT_RESPONSE_TIMEOUT,
    SENDDATA_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)
_CERT_DIR = Path(__file__).parent / "certs"
# MQTT message names for a firmware upgrade (RequestType / ResponseType, app 3.3.4)
FW_REQUEST, FW_PROGRESS, FW_FAILURE = ("firmwareUpgradeRequest", "firmwareUpgradeProgress",
                                       "firmwareUpgradeFailure")


def _plain(html_text: str) -> str:
    """Lockly's release notes are HTML fragments; keep the words, one line per <br>."""
    import html
    import re
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _int_or_none(value) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def _fw_parts_text(parts: list[dict], on_lock: dict) -> str:
    """'back 1.00.72 (offered 1.00.76), wifi ? (offered 2.02.72)'"""
    return ", ".join(f"{p['part']} {on_lock.get(p['part']) or '?'} (offered {p['version']})" for p in parts)


class LocklyError(Exception):
    """A REST/transport error carrying the Lockly ``cod`` where known."""

    def __init__(self, message: str, cod: str | None = None) -> None:
        super().__init__(message)
        self.cod = cod


# ── MQTT device channel ─────────────────────────────────────────────────────

class MqttChannel:
    """Minimal synchronous client for Lockly's AWS broker (client-cert TLS).

    Publishes ``lockCommandRequest`` frames to ``server`` and matches replies on
    ``client/<client_id>`` by ``requestId``. See docs: mqtt/client_channel.
    """

    def __init__(self, email: str, jwt: str) -> None:
        import paho.mqtt.client as mqtt  # imported lazily so REST-only use needs no paho

        self._email = email
        self._jwt = jwt
        self._client_id = uuid.uuid4().hex
        self._topic = MQTT_CLIENT_TOPIC_PREFIX + self._client_id
        self._connected = threading.Event()
        self._pending: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._listeners: dict[str, tuple[Callable[[dict], bool], Callable[[dict], None]]] = {}
        self._lock = threading.Lock()
        self._tl = threading.local()       # last failure code, per calling thread

        try:
            cli = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=self._client_id,
                protocol=mqtt.MQTTv311,
            )
        except (AttributeError, TypeError):  # paho 1.x
            cli = mqtt.Client(client_id=self._client_id, protocol=mqtt.MQTTv311)
        cli.username_pw_set(email.lower(), jwt)  # username MUST be lowercased
        cli.tls_set(
            ca_certs=str(_CERT_DIR / "ca.crt"),
            certfile=str(_CERT_DIR / "client.crt"),
            keyfile=str(_CERT_DIR / "client_key.key"),
            cert_reqs=ssl.CERT_REQUIRED,
        )
        cli.tls_insecure_set(True)  # broker is an AWS ELB name; chain still verified
        cli.on_connect = self._on_connect
        cli.on_message = self._on_message
        self._client = cli

    def _on_connect(self, client, _userdata, _flags, rc, *_a) -> None:
        if rc == 0:
            client.subscribe(self._topic, qos=0)
            self._connected.set()
        else:
            _LOGGER.error("MQTT connect refused rc=%s (check token/username case)", rc)

    def _on_message(self, _client, _userdata, msg) -> None:
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        rid = (data.get("header") or {}).get("requestId")
        if rid and rid in self._events:
            with self._lock:
                self._pending[rid] = data.get("payload") or {}
                self._events[rid].set()
        with self._lock:
            listeners = list(self._listeners.values())
        for accept, deliver in listeners:          # message streams (firmware progress)
            if accept(data):
                deliver(data)

    def connect(self, timeout: float = 15.0) -> bool:
        self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        self._client.loop_start()
        return self._connected.wait(timeout)

    def close(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    @property
    def last_code(self) -> str | None:
        """Why this thread's last exchange failed: the broker's code (e.g. 3005,
        device offline), "timeout", or None after a reply."""
        return getattr(self._tl, "code", None)

    def exchange_frame(self, device_id: str, frame_hex: str,
                       timeout: float = MQTT_RESPONSE_TIMEOUT) -> str | None:
        """Relay a BLE frame to the lock, return the lock's ACK hex (or None)."""
        import base64
        self._tl.code = None
        rid = str(uuid.uuid4())
        ev = threading.Event()
        self._events[rid] = ev
        envelope = {
            "header": {"namespace": "com.lockly", "name": "lockCommandRequest",
                       "requestId": rid, "timestamp": int(time.time() * 1000)},
            "payload": {"deviceId": device_id, "commandName": "forward",
                        "commandContent": base64.b64encode(bytes.fromhex(frame_hex)).decode()},
        }
        try:
            self._client.publish(MQTT_PUBLISH_TOPIC, json.dumps(envelope, separators=(",", ":")), qos=1)
            if not ev.wait(timeout):
                _LOGGER.warning("MQTT: no reply within %ss for %s", timeout, device_id)
                self._tl.code = "timeout"
                return None
            payload = self._pending.get(rid) or {}
        finally:
            self._events.pop(rid, None)
            self._pending.pop(rid, None)
        code = payload.get("code")
        if code not in (0, "0", None):
            self._tl.code = str(code)
            # 3005 "device is offline" is an expected state for a sleeping
            # battery lock, not a fault — log it quietly; warn on anything else.
            level = logging.DEBUG if str(code) == "3005" else logging.WARNING
            _LOGGER.log(level, "MQTT: %s for %s code=%s %s",
                        "device offline (asleep)" if str(code) == "3005"
                        else "server rejected command",
                        device_id, code, payload.get("errorMessage") or "")
            return None
        content = payload.get("commandContent")
        if not content:
            return None
        try:
            return base64.b64decode(content).hex().upper()
        except Exception:
            return None

    def follow_firmware_upgrade(self, payload: dict, on_progress: Callable[[dict], None],
                                idle_timeout: float = 120.0) -> dict:
        """Publish a firmwareUpgradeRequest and follow it to the lock's "final"
        stage, returning that message's payload. Replies are matched as the
        app's FirmwareUpgradeController matches them: by this request's
        requestId, or by name and the payload's upgradeId. Raises LocklyError
        on a failure message, a refused request, or ``idle_timeout`` seconds
        without a message (the app's timeout, which each message restarts)."""
        upgrade_id = payload["id"]
        inbox: list[dict] = []
        arrived = threading.Condition()

        def accept(data: dict) -> bool:
            header, body = data.get("header") or {}, data.get("payload") or {}
            return header.get("requestId") == upgrade_id or (
                header.get("name") in (FW_PROGRESS, FW_FAILURE) and body.get("upgradeId") == upgrade_id)

        def deliver(data: dict) -> None:
            with arrived:
                inbox.append(data)
                arrived.notify()

        with self._lock:
            self._listeners[upgrade_id] = (accept, deliver)
        envelope = {"header": {"namespace": "com.lockly", "name": FW_REQUEST, "requestId": upgrade_id,
                               "timestamp": int(time.time() * 1000)},
                    "payload": payload}
        try:
            # QoS 2 as the app publishes (PahoManager.createMqttMessage): exactly once.
            info = self._client.publish(MQTT_PUBLISH_TOPIC, json.dumps(envelope, separators=(",", ":")), qos=2)
            if info.rc != 0:
                raise LocklyError(f"Couldn't send the upgrade request to Lockly (MQTT rc {info.rc}).", "publish")
            while True:
                with arrived:
                    if not arrived.wait_for(lambda: inbox, timeout=idle_timeout):
                        raise LocklyError(f"The lock sent no progress for {idle_timeout:.0f} seconds.", "timeout")
                    data = inbox.pop(0)
                name = (data.get("header") or {}).get("name")
                body = data.get("payload") or {}
                if name == FW_FAILURE:
                    stage = F.FW_STAGES.get(str(body.get("stage")), str(body.get("stage") or "")).lower()
                    raise LocklyError(f"The lock reported a failure{' while ' + stage if stage else ''}: "
                                      f"{body.get('errorMessage') or 'no reason given'}", "failure")
                if name == FW_PROGRESS:
                    on_progress(body)
                    if body.get("stage") == "final":
                        return body
                    continue
                code = body.get("code")
                if code not in (0, "0", None):
                    raise LocklyError(f"Lockly refused the upgrade (code {code}"
                                      f"{': ' + body['errorMessage'] if body.get('errorMessage') else ''}).",
                                      str(code))
                _LOGGER.debug("firmware upgrade: %s %s", name, body)
        finally:
            with self._lock:
                self._listeners.pop(upgrade_id, None)


# ── The client ──────────────────────────────────────────────────────────────

class LocklyClient:
    """A logged-in Lockly session: locks, keys, and transport-agnostic commands."""

    def __init__(self, email: str) -> None:
        self.email = email
        self.jwt: str | None = None
        self.pw_hash: str | None = None
        self.des3_key: bytes | None = None
        self.locks: list[dict] = []
        self.status_cache: dict[str, dict] = {}
        self._session = requests.Session()
        self._mqtt: MqttChannel | None = None
        self._mqtt_lock = threading.Lock()
        self._auth_lock = threading.RLock()
        # Per-thread diagnostics: refresh_all_status runs exchanges in parallel.
        self._tl = threading.local()

    # -- low-level REST --------------------------------------------------------

    def _post(self, endpoint: str, body: dict, *, auth: bool = True,
              timeout: int = HTTP_TIMEOUT, base: str = API_BASE) -> dict:
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": API_USER_AGENT,
            "Accept-Encoding": "gzip",
        }
        if auth:
            self._ensure_session()
        if auth and self.jwt:
            headers["Authorization"] = f"Bearer {self.jwt}"
        resp = self._session.post(base + endpoint, json=body, headers=headers, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {}
        return {"_resp": resp, **(data if isinstance(data, dict) else {})}

    def _post_para_rsa(self, endpoint: str, para_obj: dict, **kw) -> dict:
        para = P.rsa_encrypt_para(json.dumps(para_obj, separators=(",", ":")))
        return self._post(endpoint, {**COMMON_BODY, "para": para}, **kw)

    def _post_para_des3(self, endpoint: str, para_obj: dict, **kw) -> dict:
        assert self.des3_key is not None, "des3 key not available — call login() first"
        para = P.des3_encrypt(self.des3_key, json.dumps(para_obj, separators=(",", ":")))
        return self._post(endpoint, {**COMMON_BODY, "para": para}, **kw)

    # -- auth & discovery ------------------------------------------------------

    def login(self, password: str) -> None:
        """Log in with a plaintext password (hashes it, then logs in)."""
        self.login_with_hash(P.sha256_hex(password))

    def login_with_hash(self, pw_hash: str) -> None:
        """Log in with the SHA-256 password hash — the form the API actually
        takes, and all a stored credential needs. This is how the app stays
        logged in: it keeps the credential and silently re-authenticates."""
        self.pw_hash = pw_hash
        body = self._post_para_rsa(
            "login", {"acct": self.email, "pw": pw_hash}, auth=False
        )
        if str(body.get("cod")) != "200":
            raise LocklyError(f"login failed: {P.describe_cod(body.get('cod'))}", str(body.get("cod")))
        jwt = body["_resp"].headers.get("Authorization", "")
        self.jwt = jwt[7:] if jwt.startswith("Bearer ") else jwt
        if not self.jwt:
            raise LocklyError("login succeeded but no token in response header")

    def _token_fresh(self, margin: float = 300) -> bool:
        exp = P.jwt_expiry(self.jwt)
        return exp is None or exp > time.time() + margin

    def _ensure_session(self) -> None:
        """Renew the login before an authenticated call when the token is
        about to expire, so a long-running session keeps working. A token
        whose expiry can't be read is left alone. The renewed login also
        refreshes the DES3 key, and the MQTT channel is rebuilt on next use
        because it authenticates with the token."""
        if not self.pw_hash or self._token_fresh():
            return
        with self._auth_lock:
            if self._token_fresh():          # another thread already renewed it
                return
            _LOGGER.debug("login token expires soon — renewing")
            self.login_with_hash(self.pw_hash)
            self.load_devices()
            with self._mqtt_lock:
                old, self._mqtt = self._mqtt, None
            if old:
                old.close()

    def refresh_token(self) -> None:
        body = self._post("refresh", {**COMMON_BODY})
        jwt = body["_resp"].headers.get("Authorization", "")
        if jwt.startswith("Bearer "):
            self.jwt = jwt[7:]

    def load_devices(self) -> list[dict]:
        body = self._post_para_rsa("qrylknew", {"acct": self.email})
        if str(body.get("cod")) != "200":
            raise LocklyError(f"qrylknew failed: {P.describe_cod(body.get('cod'))}", str(body.get("cod")))
        self.des3_key = P.des3_key_from_server_key(P.rsa_decrypt_key(body["key"]))
        locks = []
        for entry in body.get("dl", []):
            try:
                pt = P.des3_decrypt(self.des3_key, entry)
                locks.append(json.loads(pt.rstrip(b"\x00")))
            except Exception:
                _LOGGER.exception("could not decrypt a lock entry")
        self.locks = locks
        return locks

    # -- transport selection ---------------------------------------------------

    def _is_matter(self, lock: dict) -> bool:
        return str(lock.get("hubid", "")).startswith(MATTER_HUB_PREFIX)

    def _mqtt_channel(self) -> MqttChannel | None:
        # Locked so parallel status refreshes share one channel, not many.
        with self._mqtt_lock:
            if self._mqtt is None:
                assert self.jwt
                try:
                    ch = MqttChannel(self.email, self.jwt)
                    if not ch.connect():
                        _LOGGER.warning("MQTT channel did not connect")
                        ch.close()
                        return None
                    self._mqtt = ch
                except Exception:
                    _LOGGER.exception("MQTT channel setup failed")
                    return None
        return self._mqtt

    def connect_device_channel(self) -> bool:
        """Open the MQTT device channel now, so the first command to a Wi-Fi
        lock doesn't also wait for the broker connection. Thread-safe (the
        channel is built under a lock); returns whether the channel is up."""
        return self._mqtt_channel() is not None

    def _senddata(self, lock: dict, frame_hex: str, directive: str | None) -> str | None:
        """One hub-relayed senddata call. Returns ACK hex, or None (logs cod)."""
        req = {
            "acct": self.email,
            "hubid": str(lock.get("hubid") or ""),
            "dv": lock["ID"],
            "cmd": frame_hex,
            "mdna": lock.get("iotdm", ""),
        }
        if directive:
            req["directive"] = directive
        body = self._post_para_des3("senddata", req, timeout=SENDDATA_TIMEOUT)
        cod = str(body.get("cod"))
        if cod != "200":
            _LOGGER.debug("senddata cod=%s (%s)", cod, P.describe_cod(cod))
            self._tl.last_cod = cod
            return None
        return body.get("ACK")

    @property
    def last_cod(self) -> str | None:
        """The senddata ``cod`` of this thread's last failed exchange."""
        return getattr(self._tl, "last_cod", None)

    def exchange_frame(self, lock: dict, frame_hex: str,
                       directive: str | None = None) -> str | None:
        """Send a BLE frame to a lock over the best transport; return ACK hex.

        Hub-relay (senddata) is tried first when the lock has a classic hub; a
        Wi-Fi-native/Matter lock, or a hub account that answers 930, falls back
        to the MQTT device channel. While this thread holds a Bluetooth link
        (bluetooth_status), the frame goes straight to the lock over it instead.
        """
        self._tl.last_cod = None
        self._tl.mqtt_code = None
        trail: list[str] = []
        self._tl.trail = trail
        link = getattr(self._tl, "ble", None)
        if link is not None:
            ack = link.exchange(frame_hex)
            trail.append("Bluetooth → ACK" if ack else "Bluetooth → no reply")
            return ack
        has_hub = bool(lock.get("hubid")) and not self._is_matter(lock)
        if has_hub:
            ack = self._senddata(lock, frame_hex, directive)
            cod = self.last_cod
            trail.append(
                f"senddata → {'ACK' if ack else f'cod={cod} ({P.describe_cod(cod)})'}")
            if ack:
                return ack
            # 930 / no-ACK → try MQTT as a second transport
        else:
            trail.append(
                "senddata skipped (no classic hub: Wi-Fi-native or Matter lock)")
        ch = self._mqtt_channel()
        if ch is None:
            trail.append("MQTT → could not connect")
            return None
        ack = ch.exchange_frame(lock["ID"], frame_hex)
        self._tl.mqtt_code = ch.last_code
        trail.append("MQTT → ACK" if ack else
                     f"MQTT → no usable reply ({ch.last_code or 'empty'}"
                     f"{', device offline / asleep' if ch.last_code == '3005' else ''})")
        return ack

    def last_transport_trail(self) -> str:
        return " ; ".join(getattr(self._tl, "trail", None) or ["(none)"])

    def _worth_retrying(self) -> bool:
        """Whether this thread's last exchange failed fast in a way that can
        clear up: the broker answering 3005 (the lock is asleep or between
        connections), or a hub relay timeout (408 / 942). A 20 s MQTT timeout or
        a refused hub (930, 943) is not retried."""
        return (getattr(self._tl, "mqtt_code", None) == "3005"
                or P.is_transient_cod(getattr(self._tl, "last_cod", None)))

    # Pauses before the extra status attempts for a lock that seemed asleep.
    # Not tuned on hardware: chosen so a door that stays asleep costs ~10 s more.
    ASLEEP_RETRY_DELAYS = (3.0, 6.0)

    # -- high-level: status, nonce, caps --------------------------------------

    def query_status(self, lock: dict) -> dict | None:
        """Live status query (wakes the lock). Returns parsed dict incl. nonce.
        A lock that answers "offline" (asleep) is asked again, up to twice."""
        mc, uuid_ = str(lock["mc"]), lock["ID"]
        ack = self.exchange_frame(lock, P.build_query_status_cmd(mc, uuid_, F.lock_now(lock)))
        for delay in self.ASLEEP_RETRY_DELAYS:
            if ack or not self._worth_retrying():
                break
            time.sleep(delay)
            ack = self.exchange_frame(lock, P.build_query_status_cmd(mc, uuid_, F.lock_now(lock)))
        if not ack:
            # 3005 is Lockly's server saying "device is offline": a Wi-Fi lock
            # not connected to it, rather than one that didn't answer in time.
            self.status_cache[uuid_] = {"reachable": False, "ts": time.time(),
                                        "offline": getattr(self._tl, "mqtt_code", None) == "3005"}
            return None
        parsed = P.parse_ack(ack, mc, uuid_) or None
        if parsed:
            clock, sent = F.lock_clock(lock, parsed)
            if sent:
                # Seconds the lock's clock is from ours; None when it reports a
                # date that can't be real (no valid time).
                parsed["clock_offset_s"] = round(clock.timestamp() - time.time()) if clock else None
            self.status_cache[uuid_] = {"reachable": True, "ts": time.time(), **parsed}
        return parsed

    # -- Bluetooth, straight from this computer to a lock next to it -------------

    def bluetooth_in_range(self, lock: dict, timeout: float = BLE.SCAN_S) -> bool:
        """Whether this computer hears the lock over Bluetooth (listening only).
        Raises BLE.BluetoothError when Bluetooth can't be used here."""
        why = BLE.available()
        if why:
            raise BLE.BluetoothError(why)
        return BLE.in_range(str(lock.get("blename") or ""), timeout)

    def bluetooth_status(self, lock: dict) -> dict | None:
        """A live status check sent to the lock over Bluetooth from this
        computer. Like every status check it carries the current time, which is
        how the app keeps a lock's clock (QueryLockStatusCmd, isAddTime). Raises
        BLE.BluetoothError when the lock can't be reached that way."""
        why = BLE.available()
        if why:
            raise BLE.BluetoothError(why)
        link = BLE.BleLink(str(lock.get("blename") or ""))
        try:
            link.connect()
            self._tl.ble = link
            return self.query_status(lock)
        finally:
            self._tl.ble = None
            link.close()

    def refresh_all_status(self, locks: list[dict], max_workers: int = 6) -> dict:
        """Query every lock's live status in parallel, caching results. This
        WAKES each lock (they beep). Returns {lock_id: cache_entry}. Sleeping /
        unreachable locks resolve quickly to reachable=False rather than hang."""
        from concurrent.futures import ThreadPoolExecutor

        def one(lk):
            try:
                self.query_status(lk)   # populates status_cache
            except Exception:  # noqa: BLE001 - one lock must not sink the batch
                self.status_cache[lk["ID"]] = {"reachable": False, "ts": time.time()}

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(one, locks))
        return self.status_cache

    def cached_status(self, lock: dict) -> dict | None:
        """Silent status from the hub cache (no beep). Hub firmware >= build 422."""
        req = {"acct": self.email, "dv": lock["ID"], "hubid": lock.get("hubid", "")}
        body = self._post_para_des3("lock/cachedstatus/get", req)
        if str(body.get("cod")) != "200":
            return None
        data = body.get("data") or {}
        if data.get("sts") is None:
            return None
        return {**P.parse_cached_status(int(data["sts"])), "cache_time": data.get("time")}

    def prime_cached_status(self, locks: list[dict], max_workers: int = 6) -> int:
        """Fill status_cache from the hub's cached status, in parallel. Silent:
        nothing is woken. Only hub locks have a cache; live entries already in
        status_cache are kept. Returns how many locks got a cached status."""
        from concurrent.futures import ThreadPoolExecutor

        def one(lk) -> bool:
            if not lk.get("hubid") or lk["ID"] in self.status_cache:
                return False
            try:
                st = self.cached_status(lk)
            except Exception:  # noqa: BLE001 - best-effort, never blocks the list
                return False
            if st:
                self.status_cache.setdefault(
                    lk["ID"], {"reachable": True, "cached": True, "ts": time.time(), **st})
            return bool(st)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            return sum(ex.map(one, locks))

    def capabilities_for(self, lock: dict, lock_type: int | None = None) -> LockCapabilities:
        return resolve_capabilities(lock, lock_type)

    def _nonce_and_caps(self, lock: dict) -> tuple[str | None, LockCapabilities]:
        """Fetch a fresh nonce + resolve caps from the lock's reported type.

        Must use the same transport the command will. ``exchange_frame`` already
        guarantees that, so a status query here primes the nonce correctly.
        """
        status = self.query_status(lock)
        if not status:
            return None, self.capabilities_for(lock)
        nonce = status.get("ble_nonce")
        caps = self.capabilities_for(lock, status.get("lock_type"))
        return nonce, caps

    # -- high-level: lock / unlock --------------------------------------------

    @staticmethod
    def _dry_run_nonce() -> str:
        """Placeholder nonce for dry runs, which must not wake the lock."""
        return "0" * 32

    def _host_password(self, lock: dict) -> str:
        return str(lock.get("hc") or "")

    def unlock(self, lock: dict) -> tuple[bool, str]:
        return self._lock_unlock(lock, P.build_unlock_cmd, "unlock")

    def lock(self, lock: dict) -> tuple[bool, str]:
        return self._lock_unlock(lock, P.build_lock_cmd, "lock")

    def send_built(self, lock: dict, build: Callable[[str | None], str], *,
                   needs_nonce: bool = True) -> tuple[bool, str]:
        """Build a frame with a fresh nonce (one status query, only when the
        command carries one) and send it. Returns (accepted, detail): detail is
        the lock's answer in words (features.check_reply), or the transport
        trail on a delivery failure."""
        nonce = None
        if needs_nonce:
            nonce, _ = self._nonce_and_caps(lock)
            if nonce is None:
                return False, ("could not get a nonce (status query failed) — "
                               f"transport: {self.last_transport_trail()}")
        ack = self.exchange_frame(lock, build(nonce))
        if not ack:
            return False, f"no response from the lock — transport: {self.last_transport_trail()}"
        return F.check_reply(ack, lock)

    def _lock_unlock(self, lock: dict, builder: Callable, directive: str) -> tuple[bool, str]:
        mc, uuid_ = str(lock["mc"]), lock["ID"]
        nonce, caps = self._nonce_and_caps(lock)
        frame = builder(mc, uuid_, self._host_password(lock), nonce, caps=caps)
        ack = self.exchange_frame(lock, frame, directive)
        if not ack:
            return False, f"no response (last cod={self.last_cod})"
        ok, detail = P._ack_reports_success(ack, mc, uuid_)
        return ok, detail

    # -- firmware: what Lockly offers (cloud only; no lock is woken) -----------
    # Request shapes as app 3.3.4 sends them; all three were answered live on
    # 2026-11-12 (a Visage, a PGD7Y and a Vision).

    _FW_PARTS = (("front", "front"), ("back", "back"), ("hub", "wifi"), ("ble", "ble"),
                 ("screen", "screen"))

    def _firmware_reply(self, lock: dict, lock_type: int | None = None) -> dict:
        """Lockly's raw answer to the firmware check this lock's kind uses."""
        kind = F.firmware_check_kind(lock)
        model = str(lock.get("mod") or "")
        if kind == F.FW_SINGLE:
            return self._post("lockly/syspara", {**COMMON_BODY, "para": "firmware_ver", "mod": model,
                                                 "ref": self.email, "did": lock["ID"]})
        if kind == F.FW_MULTI_PART:
            endpoint, req = "fwver/msh/new", {"acct": self.email, "did": lock["ID"],
                                              "model": int(lock_type or F._KNOWN_LOCK_TYPES.get(model.upper(), 0))}
        else:
            endpoint, req = "fwver/new", {"acct": self.email, "dv": lock["ID"]}
        assert self.des3_key is not None, "des3 key not available — call load_devices() first"
        para = P.des3_encrypt(self.des3_key, json.dumps(req, separators=(",", ":")))
        return self._post(endpoint, {**COMMON_BODY, "mod": model, "para": para})

    def firmware_offer(self, lock: dict, lock_type: int | None = None) -> dict:
        """Lockly's newest firmware for this lock: {"kind", "release", "parts":
        [{"part", "version", "size_kb"}], "title", "notes"}; release is None
        when Lockly has nothing newer (cod 920)."""
        return self._offer_from_reply(lock, self._firmware_reply(lock, lock_type))

    def _offer_from_reply(self, lock: dict, body: dict) -> dict:
        kind = F.firmware_check_kind(lock)
        out = {"kind": kind, "release": None, "parts": [], "title": "", "notes": ""}
        cod = str(body.get("cod"))
        if cod == "920":
            return out
        if cod != "200":
            raise LocklyError(f"firmware check failed: {P.describe_cod(cod)}", cod)
        out["title"] = body.get("title") or ""
        out["notes"] = _plain(body.get("content") or "")
        if kind == F.FW_MULTI_PART:
            out["release"] = body.get("verNum") or None
            for prefix, part in self._FW_PARTS:
                ver, url, name, size = (body.get(prefix + k) for k in ("Ver", "Url", "Name", "Size"))
                if ver and url and name and size is not None:      # FirmwareUpgradeCheckerResponse
                    out["parts"].append({"part": part, "version": ver, "size_kb": _int_or_none(size)})
        elif kind == F.FW_SINGLE:
            out["release"] = body.get("value") or None
            if out["release"]:
                out["parts"].append({"part": "main", "version": out["release"], "size_kb": None})
        else:
            out["release"] = body.get("ver") or None
            for key, part in (("ver", "main"), ("lkmver", "front"), ("lksver", "back"),
                              ("camver", "camera"), ("gwver", "gateway")):
                if body.get(key):
                    out["parts"].append({"part": part, "version": body[key], "size_kb": None})
        return out

    # -- firmware: install over Wi-Fi (multi-part locks), as app 3.3.4 does ------

    FW_IDLE_TIMEOUT_S = 120.0     # MultipleFirmwareUpgradeRepository's default; each message restarts it
    FW_SETTLE_S = 10.0            # MultipleUpgradeViewModel waits this long before checking
    FW_VERIFY_S = 300.0           # MultipleFirmwareRebootRepository gives the check 5 minutes
    FW_VERIFY_EVERY_S = 15.0
    FW_BLE_AFTER_S = 60.0         # the app tells the user a restart takes "About 1 minute"

    def firmware_plan(self, lock: dict) -> dict:
        """What installing over Wi-Fi would do now. Asks the lock for its
        versions and battery (wakes it) and Lockly for its offer. Returns
        {"offer", "status", "onLock", "send": the parts the app would install
        over MQTT, "downloadUrl", "blocked": why it can't start, or ""}."""
        st = self.query_status(lock) or {}
        body = self._firmware_reply(lock, st.get("lock_type"))
        offer = self._offer_from_reply(lock, body)
        plan = {"offer": offer, "status": st, "onLock": F.firmware_versions_on_lock(st), "send": [],
                "downloadUrl": str(body.get("downloadUrl") or ""), "blocked": ""}
        if offer["kind"] != F.FW_MULTI_PART:
            plan["blocked"] = "This lock's firmware is installed from a phone over Bluetooth: use the Lockly app."
            return plan
        if not st:
            plan["blocked"] = "The lock didn't answer, so what it runs isn't known."
            return plan
        parts, problems = F.firmware_reply_parts(body) if offer["release"] else ([], [])
        if problems:
            plan["blocked"] = "Lockly's offer looks wrong: " + "; ".join(problems) + "."
            return plan
        plan["send"] = F.firmware_parts_to_install(parts, plan["onLock"], F.bluetooth_chip_needs_phone(lock))
        battery = st.get("battery_percent")
        if not plan["send"]:
            plan["blocked"] = "Nothing to install over Wi-Fi."
        elif not plan["downloadUrl"]:
            plan["blocked"] = "Lockly's offer has no download address."
        elif battery is None or st.get("battery_invalid"):
            plan["blocked"] = "The lock didn't report its battery level."
        elif battery < F.FW_MIN_BATTERY:
            plan["blocked"] = (f"The battery is at {battery} %. The Lockly app won't update a lock at "
                               f"30 % or less either; change the batteries first.")
        return plan

    def firmware_install(self, lock: dict, send: list[dict], download_url: str,
                         on_event: Callable[[dict], None], bluetooth: bool = False) -> dict:
        """Install ``send`` (from firmware_plan) over MQTT, then confirm it: follows
        the lock's progress, then asks the lock for its versions until every part
        reports the offered one (FW_VERIFY_S at most). Calls on_event with
        {"stage", "progress", "text"} while the lock installs, then with "away"
        (seconds since it restarted) while it doesn't answer, and with "versions"
        each time it does. With ``bluetooth`` (this computer can reach the lock
        that way), a lock whose Wi-Fi isn't back after FW_BLE_AFTER_S is asked
        over Bluetooth, which also sets its clock. Returns {"versions"}. Raises
        LocklyError when the lock reports a failure, goes quiet, or doesn't come
        back on the new versions."""
        if not send:
            raise ValueError("Nothing to install.")
        ch = self._mqtt_channel()
        if ch is None:
            raise LocklyError("Couldn't connect to Lockly's device channel.")
        payload = F.build_firmware_upgrade(str(uuid.uuid4()), lock["ID"], download_url, send)

        def progress(body: dict) -> None:
            # The messages name no part, only a stage and a percentage
            # (FirmwareUpgradeProgressResponse, app 3.3.4). Logged whole, to
            # learn from a real install how the lock moves through its parts.
            _LOGGER.info("firmware progress: %s", json.dumps(body, sort_keys=True))
            on_event({"stage": body.get("stage"), "progress": body.get("progress"),
                      "text": F.describe_firmware_progress(body)})

        ch.follow_firmware_upgrade(payload, progress, idle_timeout=self.FW_IDLE_TIMEOUT_S)
        restarted = time.monotonic()
        on_event({"stage": "verify", "progress": None, "away": 0, "text": "The lock is restarting"})
        time.sleep(self.FW_SETTLE_S)
        deadline = restarted + self.FW_VERIFY_S
        seen: dict = {}
        behind = send
        while True:
            st = self.query_status(lock)
            ble_note = ""
            if not st and bluetooth and time.monotonic() - restarted >= self.FW_BLE_AFTER_S:
                # Its Wi-Fi isn't back: ask it over Bluetooth from here. That
                # check carries the time too, so it also sets the lock's clock.
                try:
                    st = self.bluetooth_status(lock)
                    ble_note = "" if st else "; it didn't answer over Bluetooth either"
                except BLE.BluetoothError as e:
                    ble_note = f"; {e}"
            if st:
                seen = F.firmware_versions_on_lock(st)
                behind = [p for p in send if not F.version_at_least(seen.get(p["part"]), p["version"])]
                clock = {"clockOffset": st["clock_offset_s"]} if "clock_offset_s" in st else {}
                if not behind:
                    on_event({"stage": "done", "progress": 100, "versions": seen, **clock,
                              "text": "Installed and confirmed on the lock"})
                    return {"versions": seen, **clock}
                on_event({"stage": "verify", "progress": None, "versions": seen, **clock,
                          "text": "The lock still reports " + _fw_parts_text(behind, seen)})
            elif not seen:
                away = int(time.monotonic() - restarted)
                on_event({"stage": "verify", "progress": None, "away": away,
                          "text": f"Waiting for the lock to come back online ({away // 60} min so far){ble_note}"})
            if time.monotonic() >= deadline:
                break
            time.sleep(self.FW_VERIFY_EVERY_S)
        if not seen:
            raise LocklyError("The lock reported the update finished and restarted, but hasn't come back "
                              f"online in {int(self.FW_VERIFY_S // 60)} minutes, so its new versions aren't "
                              "confirmed.", "verify")
        raise LocklyError("After the upgrade the lock still reports " + _fw_parts_text(behind, seen) + ".",
                          "verify")

    # -- high-level: access log ------------------------------------------------

    def get_history(self, lock: dict, since_ms: int) -> tuple[list[dict], int] | None:
        req = {"acct": self.email, "ID": lock["ID"], "time": str(since_ms)}
        body = self._post_para_des3("getlkhist", req)
        if str(body.get("cod")) != "200":
            return None
        # sul holds secondary users' records; the app shows both lists
        # (UnlockRecordFragment.C4, app 3.3.4).
        events = list(body.get("el") or []) + list(body.get("sul") or [])
        return events, int(body.get("LAST_EVENT_SYNC_TIME") or since_ms)

    # A Wi-Fi Visage uploads its events to Lockly's event log itself, and for
    # those locks the app's history reads that log, not getlkhist
    # (UnlockRecordFragment.i4 -> MqttLockLogRepositoryImpl, app 3.3.4).
    # Read live on 2026-11-13: getlkhist had nothing for most Visages while
    # this log had their week, and its newest events matched one lock's own
    # paged log (0x94).
    EVENT_LOG_PAGE = 100          # the app's page size; offset steps by it
    EVENT_LOG_MAX_PAGES = 50

    def get_event_log(self, lock: dict, since_ms: int, until_ms: int | None = None) -> list[dict]:
        """A lock's events since since_ms from Lockly's event log, shaped like
        getlkhist's (tm in ms, co, na, pid, id). Cloud only: no lock is woken.
        Raises LocklyError when the log doesn't answer as expected."""
        until_ms = until_ms or int(time.time() * 1000)

        def utc(ms: int) -> str:       # DateTimesKt: yyyyMMddHHmmss in UTC
            return time.strftime("%Y%m%d%H%M%S", time.gmtime(ms / 1000))

        events: list[dict] = []
        for page in range(self.EVENT_LOG_MAX_PAGES):
            body = self._post("v1/proto/handler", {
                "header": {"namespace": "com.lockly", "name": "lockEventLogQueryRequest",
                           "requestId": str(uuid.uuid4()), "timestamp": int(time.time() * 1000)},
                "payload": {"deviceId": lock["ID"], "startTime": utc(since_ms), "endTime": utc(until_ms),
                            "offset": page * self.EVENT_LOG_PAGE, "limit": self.EVENT_LOG_PAGE},
            }, base=EVENT_LOG_BASE)
            payload = body.get("payload")
            if (body.get("header") or {}).get("name") != "lockEventLogQueryResponse" \
                    or not isinstance(payload, dict):
                status = getattr(body.get("_resp"), "status_code", "?")
                raise LocklyError(f"Lockly's event log didn't answer (HTTP {status}).", "eventlog")
            items = payload.get("items") or []
            for i in items:
                ts = int(i.get("timestamp") or 0)
                events.append({"tm": ts if len(str(ts)) == 13 else ts * 1000,   # seconds, as seen
                               "co": str(i.get("eventType") or ""), "na": i.get("lockUserName") or "",
                               "pid": i.get("userId"), "id": i.get("eventId")})
            if not payload.get("havingMore") or not items:
                return events
        raise LocklyError(f"Lockly's event log still had more after {len(events)} events.", "eventlog")

    # -- high-level: guests (PIN users) ---------------------------------------

    def list_users(self, lock: dict) -> list[dict]:
        """Everyone with access to this lock, with credential counts (cloud).
        Raises LocklyError when Lockly doesn't return the list."""
        req = {"dv": lock["ID"], "adminAcuId": lock.get("adminAcuId") or 0}
        body = self._post_para_des3("acu/crdntl/list", req)
        if str(body.get("cod")) != "200":
            raise LocklyError(f"acu/crdntl/list failed: {P.describe_cod(body.get('cod'))}",
                              str(body.get("cod")))
        return body.get("acuMediumList") or []

    def list_guests(self, lock: dict) -> list[dict] | None:
        req = {"dv": lock["ID"], "adminAcuId": lock.get("adminAcuId", 0)}
        body = self._post_para_des3("acu/crdntl/list", req)
        if str(body.get("cod")) != "200":
            return None
        return body.get("acuMediumList") or []

    def add_guest_pin(self, lock: dict, name: str, passcode: str,
                      start_ms: int, end_ms: int, *, permanent: bool = False) -> dict | None:
        """Add a PIN user. On access-user locks (Visage, Vision) this runs the
        app's full sequence — cloud draft, write to the lock, activate — so the
        PIN works at the door; saving to the cloud alone does not reach the lock."""
        if F.is_access_user_lock(lock):
            if permanent:
                return self.create_access_user(lock, name=name, pin=passcode)
            from datetime import datetime
            tz = F.lock_tz(lock)
            when = lambda ms: datetime.fromtimestamp(ms / 1000, tz)
            return self.create_access_user(
                lock, name=name, pin=passcode, role="GUEST", time_type="MORE_LIMIT",
                start_ms=start_ms, end_ms=end_ms, user_type=F.UT_GUEST_PERIOD,
                date=F.access_date_period(when(start_ms), when(end_ms)))
        return self._add_guest_pin_cloud(lock, name, passcode, start_ms, end_ms, permanent=permanent)

    def _add_guest_pin_cloud(self, lock: dict, name: str, passcode: str,
                             start_ms: int, end_ms: int, *, permanent: bool = False) -> dict | None:
        """Cloud-only acu/save (slot-based locks; their on-lock activation is unverified)."""
        guest_body = {
            "startTime": start_ms, "endTime": end_ms, "weekData": 0,
            "pw": passcode, "pid": 1, "pwStatus": "Y",
            "subadm": "false", "oacpriv": "false",
        }
        req = {
            "dv": lock["ID"], "name": name,
            "type": "FAMILY" if permanent else "GUEST",
            "userAcuId": 0, "acuStatus": "Y",
            "timeType": "NO_LIMIT" if permanent else "MORE_LIMIT",
            "isRetry": False, "multipleVerific": 0,
            "acuGuest": guest_body,
        }
        body = self._post_para_des3("acu/save", req)
        if str(body.get("cod")) != "200":
            raise LocklyError(f"acu/save failed: {P.describe_cod(body.get('cod'))}", str(body.get("cod")))
        return body

    def delete_guest(self, lock: dict, user_acu_id: int) -> bool:
        req = {"dv": lock["ID"], "userAcuId": user_acu_id,
               "adminAcuId": lock.get("adminAcuId", 0)}
        body = self._post_para_des3("acu/delete", req)
        return str(body.get("cod")) == "200"

    # -- high-level: remote enrolment over BLE (experimental) -----------------
    # The restriction that forces Bluetooth in the app lives only in its
    # enrolment screens; the command layer relays these frames like any other.
    # These send over the chosen transport (senddata / MQTT). A dry_run returns
    # the frame without transmitting, for inspection on a live lock.

    def _send_or_preview(self, lock: dict, label: str, build, dry_run: bool) -> tuple[bool, str]:
        """Dry run: show the frame (placeholder nonce, nothing sent). Otherwise
        send with a fresh nonce and interpret the lock's reply."""
        if dry_run:
            return True, f"[dry-run] {label} frame: {build(self._dry_run_nonce())}"
        return self.send_built(lock, build)

    def set_user_pin_ble(self, lock: dict, pin: str, user_no: int, name: str,
                         *, start: str | None = None, end: str | None = None,
                         dry_run: bool = False) -> tuple[bool, str]:
        """Push a PIN for a user slot directly to the lock (0x4B, slot locks)."""
        return self._send_or_preview(
            lock, "0x4B", lambda n: F.build_set_user_pin_cmd(lock, n, pin, user_no, name,
                                                            start=start, end=end), dry_run)

    def list_users_ble(self, lock: dict, page: int = 1) -> tuple[bool, str]:
        """The lock's user slots, one page (1–3) of QueryUserList 0x50."""
        return self.send_built(lock, lambda n: F.build_list_users_cmd(lock, n, page))

    def delete_user_ble(self, lock: dict, user_no: int, *, kind: str = F.DELETE_ALL,
                        dry_run: bool = False) -> tuple[bool, str]:
        """Delete a user slot (kind DELETE_ALL) or only its PIN (DELETE_PIN), 0x4D."""
        return self._send_or_preview(
            lock, "0x4D", lambda n: F.build_delete_user_cmd(lock, n, user_no, kind), dry_run)

    def fingerprint_capture_ble(self, lock: dict, user_id: int, admin_id: int,
                                sensor_code: int, *, dry_run: bool = False) -> tuple[bool, str]:
        """One fingerprint-capture frame (0x7D, access-user locks). The app
        repeats it per finger press until the reply's status is 03, then
        activates with 0x92 and registers the code with the cloud (not built)."""
        if dry_run:
            return True, ("[dry-run] 0x7D frame: " + F.build_add_fingerprint_cmd(
                lock, self._dry_run_nonce(), user_id, admin_id, sensor_code))
        nonce, _ = self._nonce_and_caps(lock)
        if nonce is None:
            return False, f"could not get a nonce — transport: {self.last_transport_trail()}"
        ack = self.exchange_frame(
            lock, F.build_add_fingerprint_cmd(lock, nonce, user_id, admin_id, sensor_code))
        res = F.parse_face_capture_ack(ack, lock)       # same 0x7D reply layout
        return res["ok"], res["detail"]

    # -- access users (Visage / Vision): one user = slot + PIN + sensors -------
    # The app's own sequences (app 3.3.4): the cloud holds the user record, the
    # lock holds the slot, and a user is only live once both agree. Every lock
    # command here goes over hub/MQTT.

    def exchange_built(self, lock: dict, build) -> tuple[str | None, str | None]:
        """Send ``build(nonce)`` with a fresh nonce; return (ack, decrypted reply)."""
        nonce, _ = self._nonce_and_caps(lock)
        if nonce is None:
            raise LocklyError(f"could not reach the lock — transport: {self.last_transport_trail()}")
        ack = self.exchange_frame(lock, build(nonce))
        if not ack:
            raise LocklyError(f"no reply from the lock — transport: {self.last_transport_trail()}")
        return ack, F.decrypt_ack(ack, lock)

    def lock_user_table(self, lock: dict) -> list[dict]:
        """Every user record held by the lock itself (0x9F, paged; read-only)."""
        users, pos = [], 1
        while pos <= 20:
            ack, d = self.exchange_built(lock, lambda n: F.build_access_user_list(lock, n, pos))
            if d is None:
                raise LocklyError(F.check_reply(ack, lock)[1])
            page = F.parse_access_user_list(d)
            users += page["users"]
            if page["page"] >= page["pages"] or not page["users"]:
                break
            pos += 1
        return users

    def _rest_ok(self, endpoint: str, req: dict) -> dict:
        body = self._post_para_des3(endpoint, req)
        if str(body.get("cod")) != "200":
            raise LocklyError(f"{endpoint} failed: cod={body.get('cod')} "
                              f"{body.get('msg') or P.describe_cod(body.get('cod'))}", str(body.get("cod")))
        return body

    def _write_user_record(self, lock: dict, *, user_type: int, pwd_id: int, user_acu_id: int,
                           date: str, pin: str) -> int:
        """0x73 mode 1; returns the slot the lock confirms."""
        ack, d = self.exchange_built(lock, lambda n: F.build_access_user_set(
            lock, n, user_type=user_type, pwd_id=pwd_id, user_id=user_acu_id & 0xFFFF,
            date=date, pin=pin))
        ok, detail = F.check_reply(ack, lock)
        if not ok:
            raise LocklyError(detail)
        return F.parse_access_user_reply(d).get("pwd_id", pwd_id)

    def create_access_user(self, lock: dict, *, name: str, pin: str = "",
                           role: str = "FAMILY", time_type: str = "NO_LIMIT",
                           start_ms: int = 0, end_ms: int = 0, week: int = 0,
                           user_type: int = F.UT_FAMILY, date: str | None = None) -> dict:
        """Add a person to this lock the way the app does: cloud draft (acu/save,
        status S) → write to the lock (0x73) → activate (crdntl/pwList/active).
        A failed lock write removes the draft again. The server takes the same
        names it returns (FAMILY / GUEST; NO_LIMIT / WEEK / MORE_LIMIT /
        ONE_LIMIT) and rejects LOCKLY 3.3.4's FAMILY_MEMBER / UNLIMITED with
        ERROR_ACU_TYPE_OR_TIME_TYPE."""
        guest = {"startTime": start_ms, "endTime": end_ms, "weekData": week, "pw": pin,
                 "pid": 0, "subadm": False, "oacpriv": False, "pwStatus": "S", "permission": 0}
        req = {"userAcuId": 0, "name": name, "type": role, "dv": lock["ID"],
               "multipleVerific": 1, "acuStatus": "S", "timeType": time_type,
               "isRetry": False, "acuGuest": guest}
        body = self._rest_ok("acu/save", req)
        uid = int(body.get("userAcuId") or 0)
        pid = body.get("pid")
        pwd_id = int(pid) if pid is not None and 0 <= int(pid) <= 100 else F.PWD_ID_ANY
        try:
            slot = self._write_user_record(lock, user_type=user_type, pwd_id=pwd_id,
                                           user_acu_id=uid, date=date or F.access_date_permanent(),
                                           pin=pin)
        except Exception:
            try:
                self._rest_ok("acu/delete", {"adminAcuId": 0, "dv": lock["ID"], "userAcuId": uid})
            except LocklyError:
                _LOGGER.warning("could not remove the draft user %s after a failed lock write", uid)
            raise
        self._rest_ok("crdntl/pwList/active", {
            "dv": lock["ID"], "isAdmin": False,
            "pwList": [{"userAcuId": uid, "acuIndex": slot, "adminAcuId": 0, "tm": 0}]})
        return {"userAcuId": uid, "pwdId": slot, "crdntlId": body.get("crdntlId")}

    def change_access_user_pin(self, lock: dict, user: dict, new_pin: str, *,
                               user_type: int, date: str) -> None:
        """New PIN for an existing user: lock first (0x73), then the cloud."""
        self._write_user_record(lock, user_type=user_type, pwd_id=int(user["acuIndex"]),
                                user_acu_id=int(user["userAcuId"]), date=date, pin=new_pin)
        req = {"crdntlId": user.get("crdntlId"), "userAcuId": user["userAcuId"],
               "name": user.get("name") or "", "pw": new_pin, "dv": lock["ID"],
               "startTime": user.get("startTime") or 0, "endTime": user.get("endTime") or 0,
               "timeType": user.get("timeType"), "weekData": int(user.get("weekData") or 0)}
        if user.get("crdntlId"):
            self._rest_ok("crdntl/pw/update", req)
        else:
            self._rest_ok("crdntl/pw/add", {"dv": lock["ID"], "userAcuId": user["userAcuId"],
                                            "pw": new_pin, "pid": int(user["acuIndex"]),
                                            "subadm": False, "oacpriv": "N", "pwStatus": "Y"})

    def delete_access_user(self, lock: dict, *, user_acu_id: int, pwd_id: int,
                           user_type: int) -> None:
        """Remove a person from this lock: 0x73 mode 2, then acu/delete. The
        lock answering 'already gone' (FF / E2 / 01) counts as removed."""
        ack, d = self.exchange_built(lock, lambda n: F.build_access_user_delete(
            lock, n, user_type=user_type, pwd_id=pwd_id, user_id=user_acu_id & 0xFFFF))
        status = d[0:2] if d else ack.upper()[16:18]
        if status not in ("00", "01", "FF", "E2"):
            raise LocklyError(F.check_reply(ack, lock)[1])
        self._rest_ok("acu/delete", {"adminAcuId": 0, "dv": lock["ID"], "userAcuId": user_acu_id})

    def user_detail(self, lock: dict, user_acu_id: int) -> dict:
        """One user's PIN, fingerprints, faces and cards with names and ids
        (acu/getAuInfoByDid — cloud, no lock wake)."""
        body = self._rest_ok("acu/getAuInfoByDid", {"deviceId": lock["ID"], "userAcuId": user_acu_id})
        return body.get("obj") or {}

    def delete_sensor(self, lock: dict, *, user_acu_id: int, pwd_id: int, sensor_type: int,
                      code: int, crdntl_id: int | None) -> None:
        """Remove one fingerprint / card / face: lock (0x74) then cloud (crdntl/delete)."""
        ack, _d = self.exchange_built(lock, lambda n: F.build_delete_sensor(
            lock, n, pwd_id=pwd_id, user_id=user_acu_id & 0xFFFF, sensor_type=sensor_type,
            sensor_code=code))
        ok, detail = F.check_reply(ack, lock)
        if not ok:
            raise LocklyError(detail)
        if crdntl_id:
            kind = {F.SENSOR_FINGERPRINT: "FINGERPRINT", F.SENSOR_FACE: "FACE_ID",
                    F.SENSOR_RFID: "RFID"}[sensor_type]
            self._rest_ok("crdntl/delete", {"dv": lock["ID"], "crdntlId": crdntl_id,
                                            "crdntlType": kind})

    # -- face-enrolment relay probe (experimental) ----------------------------
    # Tests the "forceBle is app-only" hypothesis without enrolling a real face:
    # does the lock accept a relayed face-enrolment command and enter capture?
    # The stop (0x17) is exact and safe; the start (0x7D) is best-effort.

    def stop_face_enrolment(self, lock: dict, *, dry_run: bool = False) -> tuple[bool, str]:
        """Relay StopSensorCmd 0x17 (face). Exact frame; safe anchor/exit."""
        nonce = self._dry_run_nonce() if dry_run else self._nonce_and_caps(lock)[0]
        if nonce is None:
            return False, "could not get a nonce (status query failed)"
        frame = F.build_stop_sensor_cmd(lock, nonce)
        if dry_run:
            return True, f"[dry-run] would relay 0x17 stop: {frame}"
        ack = self.exchange_frame(lock, frame)
        return F.check_reply(ack, lock)

    def start_face_enrolment(self, lock: dict, *, face_code: int = 0,
                             dry_run: bool = False) -> tuple[bool, str]:
        """Relay a FRONT face-capture start (0x7D). EXPERIMENTAL / best-effort.

        Success here is not the point — watch the lock for a camera light / voice
        prompt. Always follow with stop_face_enrolment() to exit.
        """
        nonce = self._dry_run_nonce() if dry_run else self._nonce_and_caps(lock)[0]
        if nonce is None:
            return False, "could not get a nonce (status query failed)"
        frame = F.build_face_start_cmd(lock, nonce, face_code=face_code)
        if dry_run:
            return True, f"[dry-run] would relay 0x7D face-start: {frame}"
        ack = self.exchange_frame(lock, frame)
        if not ack:
            return False, f"no response (last cod={self.last_cod})"
        # Decode the lock's reply (the client has the master code): a proper
        # 0A7D with status 03 = accepted; a short 0C frame = rejection.
        res = F.parse_face_capture_ack(ack, lock)
        return res["ok"], f"lock replied — {res['detail']}  (raw {ack[:24]}…)"

    # -- full face enrolment --------------------------------------------------
    # Verified sequence: create/select user → getFaceCode → per-direction 0x7D
    # captures → crdntl/face/add. Face-capable models only.

    def get_face_code(self, lock: dict) -> int | None:
        import random, string, time as _t
        nonce = "".join(random.choice(string.ascii_lowercase) for _ in range(16))
        req = {"deviceId": lock["ID"], "timestamp": int(_t.time() * 1000), "nonce": nonce}
        body = self._post_para_des3("getFaceCode", req)
        if str(body.get("cod")) != "200":
            return None
        fc = body.get("faceCode")
        return int(fc) if fc is not None else None

    def face_add(self, lock: dict, user_acu_id: int, face_id: int,
                 face_name: str, face_code: int) -> dict | None:
        req = {"userAcuId": user_acu_id, "deviceId": lock["ID"],
               "pid": face_id, "faceName": face_name, "faceCode": face_code}
        body = self._post_para_des3("crdntl/face/add", req)
        if str(body.get("cod")) != "200":
            raise LocklyError(f"crdntl/face/add failed: {P.describe_cod(body.get('cod'))}",
                              str(body.get("cod")))
        return body

    def enrol_face(self, lock: dict, *, user_acu_id: int = 0, admin_id: int = 0,
                   user_type: int | None = None, face_name: str = "Face",
                   on_prompt=None, dry_run: bool = False) -> tuple[bool, str]:
        """Full face enrolment for a user (owner if user_acu_id==0).

        on_prompt(direction_value, direction_text) is called before each capture
        so the CLI can tell the person which way to look. Returns (ok, detail).
        """
        if not F.supports_face(lock):
            return False, (f"{lock.get('mod') or 'this lock'} has no facial "
                           "recognition — face enrolment does not apply.")
        if user_type is None:
            user_type = F.USER_TYPE_OWNER if user_acu_id == 0 else F.USER_TYPE_FAMILY
        directions = F.face_directions_for(lock)

        face_code = 0 if dry_run else self.get_face_code(lock)
        if face_code is None:
            return False, "getFaceCode failed (could not reserve a face slot)"

        last: dict = {}
        for d in directions:
            text = F.FACE_DIR_NAME.get(d, "position your face")
            if on_prompt:
                on_prompt(d, text)
            if dry_run:
                # A dry run must not touch the lock: build with a placeholder nonce.
                frame = F.build_face_capture_cmd(
                    lock, "0" * 32, d, user_acu_id=user_acu_id, admin_id=admin_id,
                    face_code=face_code, user_type=user_type)
                if on_prompt:
                    on_prompt(d, f"[dry-run] 0x7D {text}: {frame}")
                continue
            nonce, _caps = self._nonce_and_caps(lock)
            if nonce is None:
                return False, "could not get a nonce (status query failed)"
            frame = F.build_face_capture_cmd(
                lock, nonce, d, user_acu_id=user_acu_id,
                admin_id=admin_id, face_code=face_code, user_type=user_type)
            ack = self.exchange_frame(lock, frame)
            res = F.parse_face_capture_ack(ack, lock)
            if on_prompt:
                on_prompt(d, f"→ {res['detail']}")
            if not res["ok"]:
                return False, f"capture ({text}) failed: {res['detail']}  " \
                              f"[transport: {self.last_transport_trail()}]"
            last = res
        if dry_run:
            return True, "[dry-run] would now activate (0x92) and call crdntl/face/add"
        if not last.get("done"):
            return False, f"the lock did not report the face as captured ({last['detail']})"
        # The last direction (DOWN / ONLY_ONCE) is the activating one: 0x92 with
        # the sensor id the lock assigned, which is also the cloud's "pid".
        sensor_id = last.get("sensor_id") or 0
        ok, detail = self.send_built(lock, lambda n: F.build_activate_sensor_cmd(
            lock, n, user_acu_id, admin_id, sensor_id, face_code,
            sensor_type=F._SENSOR_TYPE_FACE_ID))
        if not ok:
            return False, f"face captured but activation failed: {detail}"
        self.face_add(lock, user_acu_id, sensor_id, face_name, face_code)
        return True, (f"face enrolled for userAcuId={user_acu_id} "
                      f"(sensor {sensor_id}, faceCode={face_code})")

    def close(self) -> None:
        if self._mqtt:
            self._mqtt.close()
