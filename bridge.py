"""tapo-bridge — put battery-powered Tapo cameras into Frigate (or anything else).

Battery and solar Tapo cameras (C400, C420, C425, C660 solar, …) expose no RTSP and no
ONVIF. They speak only TP-Link's encrypted protocol on port 8800, and the "Camera
Account" screen that enables third-party access on mains-powered models does not exist
on them.

go2rtc has a `tapo://` source that connects to that protocol, but it reports the video
as H264 when these cameras actually send **HEVC**. Parsing H.265 as H.264 means the
frame geometry is never recovered, so ffmpeg fails with:

    Could not find codec parameters for stream 0 (Video: h264, none): unspecified size

No probe window fixes that, because nothing in the stream is being read correctly.

This bridge uses pytapo, which decrypts the stream properly and hands ffmpeg an MPEG-TS
with `-c:v copy`. MPEG-TS carries codec parameters inline at every keyframe and needs no
upfront header, so any consumer learns the real geometry from the stream itself. The
result is served over plain HTTP as `video/mp2t`.

A camera session is opened only while a client is actually pulling, so nothing holds the
camera awake when your NVR is stopped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field

from aiohttp import web

try:
    from pytapo import Tapo
    from pytapo.media_stream.streamer import Streamer
except ImportError:  # pragma: no cover
    sys.exit("pytapo is required:  pip install pytapo")

log = logging.getLogger("tapo-bridge")


# --------------------------------------------------------------------------- config


@dataclass
class Camera:
    name: str
    host: str
    password: str
    user: str = "admin"
    quality: str = "HD"
    include_audio: bool = False
    # Passed straight through to pytapo's Streamer as ff_args, for anything this
    # config does not model (e.g. {"-c:v": "libx264"} to transcode at the source).
    ff_args: dict = field(default_factory=dict)

    def __post_init__(self):
        # The camera's live preview only knows two resolution names, "HD" and
        # "VGA" (same enum in the Tapo app, and what go2rtc sends for
        # subtype=1). Anything else, "SD" included, is silently ignored by the
        # camera: the session never starts and no data arrives.
        q = self.quality.strip().upper()
        self.quality = {"SD": "VGA", "LOW": "VGA", "MAIN": "HD"}.get(q, q)
        if self.quality not in ("HD", "VGA"):
            sys.exit(f"Camera {self.name}: quality must be HD or SD (VGA), "
                     f"got {self.quality!r}")

    @property
    def path(self) -> str:
        return f"/{self.name}.ts"


def _bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_cameras() -> list[Camera]:
    """Cameras from TAPO_CAMERAS (JSON), else the single-camera TAPO_* variables.

    JSON form, for more than one camera:

        TAPO_CAMERAS='[{"name":"front","host":"10.0.0.5","password":"…"},
                       {"name":"yard","host":"10.0.0.6","password":"…","quality":"SD"}]'
    """
    raw = os.environ.get("TAPO_CAMERAS", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"TAPO_CAMERAS is not valid JSON: {exc}")
        if not isinstance(entries, list) or not entries:
            sys.exit("TAPO_CAMERAS must be a non-empty JSON list")
        cameras = []
        for i, e in enumerate(entries):
            missing = [k for k in ("name", "host", "password") if not e.get(k)]
            if missing:
                sys.exit(f"TAPO_CAMERAS[{i}] is missing: {', '.join(missing)}")
            cameras.append(Camera(
                name=str(e["name"]),
                host=str(e["host"]),
                password=str(e["password"]),
                user=str(e.get("user", "admin")),
                quality=str(e.get("quality", "HD")),
                include_audio=bool(e.get("include_audio", False)),
                ff_args=dict(e.get("ff_args") or {}),
            ))
        return cameras

    host = os.environ.get("TAPO_HOST", "").strip()
    password = os.environ.get("TAPO_PASSWORD", "")
    if not host or not password:
        sys.exit("Set TAPO_HOST and TAPO_PASSWORD, or TAPO_CAMERAS as JSON. "
                 "See .env.example.")
    ff_args_raw = os.environ.get("TAPO_FF_ARGS", "").strip()
    try:
        ff_args = json.loads(ff_args_raw) if ff_args_raw else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"TAPO_FF_ARGS is not valid JSON: {exc}")

    return [Camera(
        name=os.environ.get("TAPO_NAME", "stream"),
        host=host,
        password=password,
        user=os.environ.get("TAPO_USER", "admin"),
        quality=os.environ.get("TAPO_QUALITY", "HD"),
        include_audio=_bool(os.environ.get("TAPO_INCLUDE_AUDIO"), False),
        ff_args=ff_args,
    )]


# --------------------------------------------------------------------------- serving


CHUNK = 64 * 1024

# These cameras allow very few simultaneous streams, and every one this bridge holds
# is one the Tapo app cannot have - the app reports "You've reached the live stream
# limit" and refuses to change resolution. go2rtc reconnects without always closing
# cleanly first, so without this the sessions pile up until nothing else can connect.
# One session per camera, and a new client takes over from the old one.
_ACTIVE: dict[str, object] = {}
_ACTIVE_LOCK = asyncio.Lock()


async def _release(name: str, streamer=None) -> None:
    async with _ACTIVE_LOCK:
        current = _ACTIVE.get(name)
        target = streamer if streamer is not None else current
        if target is None:
            return
        if streamer is None or current is streamer:
            _ACTIVE.pop(name, None)
    try:
        await target.stop()
    except Exception:
        proc = getattr(target, "streamProcess", None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


async def serve_camera(cam: Camera, request: web.Request) -> web.StreamResponse:
    peer = request.remote
    log.info("[%s] client %s connected; opening camera session", cam.name, peer)

    loop = asyncio.get_running_loop()
    try:
        # Tapo() does blocking HTTP in its constructor.
        tapo = await loop.run_in_executor(
            None, lambda: Tapo(cam.host, cam.user, cam.password, cam.password))
    except Exception as exc:
        # The overwhelmingly common cause, and it looks exactly like a bad password.
        log.error("[%s] login failed: %s", cam.name, exc)
        return web.Response(
            status=502,
            text=(f"Could not log in to {cam.host}: {exc}\n\n"
                  "If this is an authentication error, check that Third-Party\n"
                  "Compatibility is ON in the Tapo app:\n"
                  "  Me > Tapo Lab / Third-Party Services > Third-Party Compatibility\n"
                  "With it off, correct credentials still return 401.\n"))

    streamer = Streamer(
        tapo,
        mode="pipe",
        includeAudio=cam.include_audio,
        quality=cam.quality,
        ff_args=cam.ff_args or None,
    )

    # Take over from any session still held for this camera.
    await _release(cam.name)
    async with _ACTIVE_LOCK:
        _ACTIVE[cam.name] = streamer

    try:
        await streamer.start()
    except Exception as exc:
        log.error("[%s] could not start stream: %s", cam.name, exc)
        await _release(cam.name, streamer)
        return web.Response(status=502, text=f"Could not start stream: {exc}\n")

    proc = getattr(streamer, "streamProcess", None)
    if proc is None or proc.stdout is None:
        log.error("[%s] streamer produced no process", cam.name)
        return web.Response(status=503, text="Camera stream unavailable\n")

    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"},
    )
    await resp.prepare(request)

    sent = 0
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                log.info("[%s] camera stream ended after %d bytes", cam.name, sent)
                break
            sent += len(chunk)
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        log.info("[%s] client %s disconnected after %d bytes", cam.name, peer, sent)
    except Exception as exc:
        log.warning("[%s] stream aborted after %d bytes: %s", cam.name, sent, exc)
    finally:
        # Always release, so the camera's stream slot goes back to the app.
        await _release(cam.name, streamer)
    return resp


def build_app(cameras: list[Camera]) -> web.Application:
    app = web.Application()

    async def healthz(_request):
        return web.Response(text="ok\n")

    async def index(_request):
        lines = ["tapo-bridge", ""]
        lines += [f"  {c.name:<16} {c.path:<20} {c.host}  quality={c.quality}"
                  for c in cameras]
        lines += ["", "Point your NVR at one of the paths above."]
        return web.Response(text="\n".join(lines) + "\n")

    app.add_routes([web.get("/", index), web.get("/healthz", healthz)])
    for cam in cameras:
        app.add_routes([web.get(cam.path, lambda r, c=cam: serve_camera(c, r))])
        # Convenience alias so single-camera setups can hardcode /stream.ts. Skipped
        # when the camera is already named "stream", or aiohttp rejects the duplicate.
        if len(cameras) == 1 and cam.path != "/stream.ts":
            app.add_routes([web.get("/stream.ts", lambda r, c=cam: serve_camera(c, r))])
    return app


def attach_onvif(app: web.Application, cameras: list[Camera]) -> None:
    """Expose the first camera's motor over ONVIF, for NVRs whose PTZ needs it.

    Frigate's PTZ controls speak ONVIF and nothing else, and battery Tapos have none.
    One camera only - ONVIF models a device, not a fleet.
    """
    if not _bool(os.environ.get("ONVIF_ENABLED"), False):
        return
    try:
        from onvif_service import OnvifService, PtzController, attach
    except ImportError as exc:
        log.warning("ONVIF requested but unavailable: %s", exc)
        return

    cam = cameras[0]
    advertise = os.environ.get("ONVIF_ADVERTISE_HOST", "127.0.0.1")
    # The port Frigate must dial, which is the PUBLISHED one - not BRIDGE_PORT, the
    # port inside the container. Getting this wrong advertises a URL nothing can reach.
    port = int(os.environ.get("ONVIF_ADVERTISE_PORT",
                              os.environ.get("BRIDGE_PORT", "8080")))
    stream_uri = os.environ.get(
        "ONVIF_STREAM_URI", f"http://{advertise}:{port}{cam.path}")

    ptz = PtzController(lambda: Tapo(cam.host, cam.user, cam.password, cam.password))
    attach(app, OnvifService(ptz, advertise, port, stream_uri, name=cam.name))
    log.info("ONVIF enabled for %s at http://%s:%d/onvif/device_service",
             cam.name, advertise, port)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cameras = load_cameras()
    port = int(os.environ.get("BRIDGE_PORT", "8080"))
    host = os.environ.get("BRIDGE_BIND", "0.0.0.0")

    for c in cameras:
        log.info("serving %s (%s, quality=%s, audio=%s) at %s",
                 c.name, c.host, c.quality, c.include_audio, c.path)

    app = build_app(cameras)
    attach_onvif(app, cameras)
    web.run_app(app, host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()
