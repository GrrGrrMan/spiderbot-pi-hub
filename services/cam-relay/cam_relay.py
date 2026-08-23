#!/usr/bin/env python3
"""
V2 Hexapod - Dynamic MQTT Auto-Discovering Camera Relay
Auto-discovers the ESP32-CAM IP from the MQTT control plane on localhost,
adapting automatically across Home Wi-Fi, Hotspots, and Field networks.
"""

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import os
import signal
import time
from typing import Optional, Set
from aiohttp import ClientSession, ClientTimeout, web
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("cam.relay")

MAX_BUFFER_BYTES = 512 * 1024  # 512 KB safety threshold


@dataclass
class RelayConfig:
    upstream_url: str
    host: str
    port: int
    max_clients: int
    mqtt_host: str
    mqtt_port: int
    device_id: str
    boundary: str = "123456789000000000000987654321"

    @classmethod
    def load(cls) -> "RelayConfig":
        env_candidates = [
            os.environ.get("CAM_ENV_FILE", ""),
            "/etc/hexapod-cam-relay/cam_relay.env",
            os.path.join(os.path.dirname(__file__), ".env"),
            os.path.join(os.path.dirname(__file__), "../../conf/cam_relay.env"),
        ]

        for env_path in env_candidates:
            if env_path and os.path.exists(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
                    log.info("Loaded config from: %s", env_path)
                    break
                except Exception as e:
                    log.warning("Could not read %s: %s", env_path, e)

        parser = argparse.ArgumentParser(description="V2 Hexapod Dynamic Camera Relay")
        parser.add_argument("--upstream", default=os.environ.get("CAM_UPSTREAM_URL", "auto"),
                            help="Upstream URL ('auto' enables MQTT auto-discovery)")
        parser.add_argument("--host", default=os.environ.get("CAM_RELAY_HOST", "127.0.0.1"))
        parser.add_argument("--port", type=int, default=int(os.environ.get("CAM_RELAY_PORT", "8088")))
        parser.add_argument("--max-clients", type=int, default=int(os.environ.get("CAM_MAX_CLIENTS", "100")))
        parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
        parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
        parser.add_argument("--device", default=os.environ.get("CAM_DEVICE_ID", "hexapod-cam-01"))

        args, _ = parser.parse_known_args()

        return cls(
            upstream_url=args.upstream,
            host=args.host,
            port=args.port,
            max_clients=args.max_clients,
            mqtt_host=args.mqtt_host,
            mqtt_port=args.mqtt_port,
            device_id=args.device,
        )


class FrameHub:
    def __init__(self, config: RelayConfig):
        self.config = config
        self.clients: Set[asyncio.Queue] = set()
        self.latest_frame: Optional[bytes] = None
        self.last_frame_ts: float = 0.0
        self.upstream_connected: bool = False
        self.current_upstream_url: str = config.upstream_url
        self.fps_counter: int = 0
        self.current_fps: float = 0.0
        self._fps_timer: float = time.time()

        self.part_header = (
            f"--{config.boundary}\r\n"
            "Content-Type: image/jpeg\r\n"
            "Content-Length: {length}\r\n\r\n"
        ).encode("ascii")

    def register_client(self) -> Optional[asyncio.Queue]:
        if len(self.clients) >= self.config.max_clients:
            log.warning("Max client limit reached (%d)", self.config.max_clients)
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.clients.add(q)
        log.info("Client connected. Active viewers: %d", len(self.clients))
        return q

    def unregister_client(self, q: asyncio.Queue):
        self.clients.discard(q)
        log.info("Client disconnected. Active viewers: %d", len(self.clients))

    def broadcast(self, frame_bytes: bytes):
        self.latest_frame = frame_bytes
        self.last_frame_ts = time.time()
        self.fps_counter += 1

        now = time.time()
        if now - self._fps_timer >= 2.0:
            self.current_fps = round(self.fps_counter / (now - self._fps_timer), 1)
            self.fps_counter = 0
            self._fps_timer = now

        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(frame_bytes)
            except asyncio.QueueFull:
                pass


class UpstreamConsumer:
    def __init__(self, config: RelayConfig, hub: FrameHub, loop: asyncio.AbstractEventLoop):
        self.config = config
        self.hub = hub
        self.loop = loop
        self._running = True
        self._url_changed = asyncio.Event()
        self._discovered_url = None if config.upstream_url == "auto" else config.upstream_url

        if config.upstream_url == "auto":
            self._setup_mqtt()

    def _setup_mqtt(self):
        self.mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"cam-relay-discovery-{os.getpid()}"
        )
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        try:
            self.mqtt_client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=30)
            self.mqtt_client.loop_start()
            log.info("MQTT Auto-Discovery listening on %s:%d", self.config.mqtt_host, self.config.mqtt_port)
        except Exception as e:
            log.warning("MQTT discovery connect failed: %s", e)

    def _on_mqtt_connect(self, client, userdata, flags, rc, props=None):
        topic_cfg = f"hexapod/{self.config.device_id}/config"
        topic_tel = f"hexapod/{self.config.device_id}/telemetry"
        client.subscribe(topic_cfg)
        client.subscribe(topic_tel)
        log.info("Subscribed for Camera IP auto-discovery: %s, %s", topic_cfg, topic_tel)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            stream_url = data.get("stream_url") or (f"http://{data['ip']}:81/stream" if "ip" in data else None)

            if stream_url and stream_url != self._discovered_url:
                log.info("MQTT Discovery -> Camera announced new location: %s", stream_url)
                self._discovered_url = stream_url
                self.hub.current_upstream_url = stream_url
                self.loop.call_soon_threadsafe(self._url_changed.set)
        except Exception as e:
            log.debug("MQTT parse error: %s", e)

    def stop(self):
        self._running = False
        if hasattr(self, "mqtt_client"):
            self.mqtt_client.loop_stop()

    async def run(self):
        while self._running:
            target_url = self._discovered_url

            if not target_url or target_url == "auto":
                log.info("Waiting for ESP32-CAM to announce its IP via MQTT...")
                self._url_changed.clear()
                try:
                    await asyncio.wait_for(self._url_changed.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                target_url = self._discovered_url

            self._url_changed.clear()
            log.info("Connecting to active camera at %s...", target_url)

            try:
                timeout = ClientTimeout(total=None, connect=4.0, sock_read=8.0)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(target_url) as resp:
                        if resp.status != 200:
                            log.warning("Camera upstream returned HTTP %d", resp.status)
                            await asyncio.sleep(2.0)
                            continue

                        log.info("Camera stream connected and streaming.")
                        self.hub.upstream_connected = True
                        buffer = bytearray()

                        while self._running and not self._url_changed.is_set():
                            chunk = await resp.content.read(4096)
                            if not chunk:
                                break

                            buffer.extend(chunk)

                            # Safety Guard: Clear buffer if corrupted
                            if len(buffer) > MAX_BUFFER_BYTES:
                                log.warning("Buffer overflow detected (%d bytes) - clearing buffer", len(buffer))
                                buffer.clear()
                                continue

                            while True:
                                start = buffer.find(b"\xff\xd8")
                                if start == -1:
                                    buffer.clear()
                                    break
                                end = buffer.find(b"\xff\xd9", start + 2)
                                if end == -1:
                                    if start > 0:
                                        buffer = buffer[start:]
                                    break

                                frame = bytes(buffer[start : end + 2])
                                buffer = buffer[end + 2 :]
                                self.hub.broadcast(frame)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Camera stream disconnected (%s). Retrying in 2s...", e)

            self.hub.upstream_connected = False
            self.hub.current_fps = 0.0
            await asyncio.sleep(2.0)


class RelayHttpServer:
    def __init__(self, config: RelayConfig, hub: FrameHub):
        self.config = config
        self.hub = hub
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/stream", self.handle_stream)
        self.app.router.add_get("/snapshot", self.handle_snapshot)
        self.app.router.add_get("/status", self.handle_status)

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        q = self.hub.register_client()
        if q is None:
            return web.Response(status=503, text="Max viewers reached")

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": f"multipart/x-mixed-replace;boundary={self.config.boundary}",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Access-Control-Allow-Origin": "*",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        try:
            if self.hub.latest_frame:
                hdr = self.hub.part_header.replace(
                    b"{length}", str(len(self.hub.latest_frame)).encode("ascii")
                )
                await response.write(hdr + self.hub.latest_frame + b"\r\n")

            while True:
                frame = await q.get()
                hdr = self.hub.part_header.replace(
                    b"{length}", str(len(frame)).encode("ascii")
                )
                await response.write(hdr + frame + b"\r\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.hub.unregister_client(q)

        return response

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        if not self.hub.latest_frame:
            return web.Response(status=503, text="Camera frame not ready")

        return web.Response(
            body=self.hub.latest_frame,
            content_type="image/jpeg",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store",
            },
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "upstream_url": self.hub.current_upstream_url,
            "upstream_connected": self.hub.upstream_connected,
            "active_viewers": len(self.hub.clients),
            "fps": self.hub.current_fps,
            "last_frame_age_s": (
                round(time.time() - self.hub.last_frame_ts, 2)
                if self.hub.last_frame_ts
                else None
            ),
        })


async def main():
    config = RelayConfig.load()
    loop = asyncio.get_running_loop()
    hub = FrameHub(config)
    consumer = UpstreamConsumer(config, hub, loop)
    server = RelayHttpServer(config, hub)

    runner = web.AppRunner(server.app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    log.info("Relay active on http://%s:%d/stream [Upstream Mode: %s]", config.host, config.port, config.upstream_url)

    consumer_task = asyncio.create_task(consumer.run())

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    consumer.stop()
    consumer_task.cancel()
    await runner.cleanup()
    log.info("Relay stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())