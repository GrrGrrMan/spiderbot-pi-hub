#!/usr/bin/env python3
"""Find ESPs on local LANs and link them to this Pi's MQTT broker."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
HOST_PRIORITIES = {
    "spider-j": 100,
    "spider-w": 80,
}
HUB_ALIASES = {
    "spiderbot-j": "spider-j",
    "spiderj": "spider-j",
    "spiderbot-w": "spider-w",
    "spiderw": "spider-w",
}


def load_config(path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    if not path.exists():
        return config

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if "=(" in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue

        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        config[key] = parts[0] if parts else ""

    return config


def getenv_config(config: dict[str, str], name: str, default: str = "") -> str:
    return os.environ.get(name, config.get(name, default))


def bool_config(config: dict[str, str], name: str, default: bool) -> bool:
    value = getenv_config(config, name, "1" if default else "0").strip().lower()
    return value not in {"0", "false", "no", "off", ""}


def int_config(config: dict[str, str], name: str, default: int) -> int:
    value = getenv_config(config, name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def run_text(command: list[str], timeout: float = 3.0) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def parse_ipv4(value: str) -> ipaddress.IPv4Address | None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip if isinstance(ip, ipaddress.IPv4Address) else None


def local_interfaces() -> list[ipaddress.IPv4Interface]:
    interfaces: list[ipaddress.IPv4Interface] = []
    output = run_text(["ip", "-o", "-4", "addr", "show", "scope", "global"])
    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        addr = parts[parts.index("inet") + 1]
        try:
            iface = ipaddress.ip_interface(addr)
        except ValueError:
            continue
        if isinstance(iface, ipaddress.IPv4Interface) and iface.ip not in TAILSCALE_NET:
            interfaces.append(iface)
    return interfaces


def same_network(left: ipaddress.IPv4Address, iface: ipaddress.IPv4Interface) -> bool:
    return left in iface.network


def choose_link_host(
    config: dict[str, str],
    esp_ip: ipaddress.IPv4Address,
    interfaces: list[ipaddress.IPv4Interface],
) -> str:
    configured = getenv_config(config, "PI_HUB_LINK_HOST").strip()
    if configured:
        return configured

    for iface in interfaces:
        if same_network(esp_ip, iface):
            return str(iface.ip)
    for iface in interfaces:
        if iface.ip.is_private:
            return str(iface.ip)
    return str(interfaces[0].ip) if interfaces else ""


def neighbor_candidates(interfaces: list[ipaddress.IPv4Interface]) -> set[str]:
    networks = [iface.network for iface in interfaces]
    out: set[str] = set()
    for line in run_text(["ip", "-4", "neigh", "show"]).splitlines():
        ip = parse_ipv4(line.split(maxsplit=1)[0])
        if ip and any(ip in network for network in networks):
            out.add(str(ip))
    return out


def sweep_candidates(interfaces: list[ipaddress.IPv4Interface]) -> set[str]:
    out: set[str] = set()
    for iface in interfaces:
        network = iface.network
        if network.num_addresses > 512:
            continue
        for ip in network.hosts():
            if ip != iface.ip:
                out.add(str(ip))
    return out


def allowed_roots(config: dict[str, str]) -> set[str]:
    raw = getenv_config(
        config,
        "PI_HUB_ESP_ALLOWED_ROOTS",
        getenv_config(config, "PI_HUB_AUTO_LINK_ALLOWED_ROOTS", ""),
    ).strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def canonical_hub_id(raw: str) -> str:
    key = raw.strip().lower().replace("_", "-")
    return HUB_ALIASES.get(
        key.replace("-", ""), HUB_ALIASES.get(key, key or "spider-w")
    )


def canonical_offer(
    root: str,
    nonce: str,
    hub: str,
    host: str,
    port: int,
    priority: int,
    ttl: int,
) -> str:
    return f"v1|{root}|{nonce}|{hub}|{host}|{port}|{priority}|{ttl}"


def sign_offer(token: str, canonical: str) -> str:
    return hmac.new(
        token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def recv_json_line(sock: socket.socket, timeout: float) -> dict[str, Any] | None:
    sock.settimeout(timeout)
    data = bytearray()
    while len(data) < 1024:
        chunk = sock.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            break
        if chunk != b"\r":
            data.extend(chunk)
    if not data:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def probe_esp(ip: str, port: int, timeout_s: float) -> dict[str, Any] | None:
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as sock:
            hello = recv_json_line(sock, timeout_s)
            if not hello:
                sock.sendall(b"hello\n")
                hello = recv_json_line(sock, timeout_s)
            if not hello or hello.get("type") != "hello":
                return None
            hello["ip"] = str(hello.get("ip") or ip)
            hello["_probe_ip"] = ip
            return hello
    except OSError:
        return None


def send_link(
    hello: dict[str, Any],
    *,
    config: dict[str, str],
    interfaces: list[ipaddress.IPv4Interface],
    token: str,
    hub: str,
    priority: int,
    mqtt_port: int,
    control_port: int,
    ttl: int,
    timeout_s: float,
    dry_run: bool,
) -> bool:
    probe_ip = str(hello.get("_probe_ip", "")).strip()
    stale_root = str(hello.get("root", "")).strip()
    if not probe_ip:
        return False

    if dry_run:
        root = str(hello.get("root", "")).strip()
        esp_ip = parse_ipv4(str(hello.get("ip") or probe_ip))
        if not root or not esp_ip:
            return False
        host = choose_link_host(config, esp_ip, interfaces)
        if not host:
            print(f"[pi-esp-link] skipped {root}@{probe_ip}: no local Pi link host")
            return False
        print(f"[pi-esp-link] dry-run: would offer {host}:{mqtt_port} to {root}@{probe_ip}")
        return True

    try:
        with socket.create_connection((probe_ip, control_port), timeout=timeout_s) as sock:
            fresh = recv_json_line(sock, timeout_s)
            if not fresh:
                sock.sendall(b"hello\n")
                fresh = recv_json_line(sock, timeout_s)
            if not fresh or fresh.get("type") != "hello":
                print(f"[pi-esp-link] link failed {probe_ip}: no ESP hello")
                return False

            root = str(fresh.get("root", "")).strip()
            nonce = str(fresh.get("nonce", "")).strip()
            esp_ip = parse_ipv4(str(fresh.get("ip") or probe_ip))
            if not root or not nonce or not esp_ip:
                print(f"[pi-esp-link] link failed {probe_ip}: incomplete ESP hello")
                return False

            host = choose_link_host(config, esp_ip, interfaces)
            if not host:
                print(f"[pi-esp-link] skipped {root}@{probe_ip}: no local Pi link host")
                return False

            canonical = canonical_offer(root, nonce, hub, host, mqtt_port, priority, ttl)
            offer = {
                "v": 1,
                "root": root,
                "nonce": nonce,
                "hub": hub,
                "host": host,
                "port": mqtt_port,
                "priority": priority,
                "ttl": ttl,
                "sig": sign_offer(token, canonical),
            }
            line = "link " + json.dumps(offer, separators=(",", ":")) + "\n"
            sock.sendall(line.encode("utf-8"))
            sock.settimeout(timeout_s)
            response = sock.recv(160).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        label = f"{stale_root}@{probe_ip}" if stale_root else probe_ip
        print(f"[pi-esp-link] link failed {label}: {exc}")
        return False

    if response.startswith("OK"):
        print(f"[pi-esp-link] linked {root}@{probe_ip} -> {host}:{mqtt_port}")
        return True

    print(f"[pi-esp-link] link rejected {root}@{probe_ip}: {response or '<no response>'}")
    return False


def discover(
    candidates: set[str],
    *,
    port: int,
    timeout_s: float,
    concurrency: int,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not candidates:
        return found

    workers = max(1, min(concurrency, len(candidates)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(probe_esp, ip, port, timeout_s): ip
            for ip in sorted(candidates, key=lambda value: tuple(map(int, value.split("."))))
        }
        for future in concurrent.futures.as_completed(future_map):
            hello = future.result()
            if hello:
                found.append(hello)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "conf" / "pi_hub.conf"),
        help="Path to pi_hub.conf",
    )
    parser.add_argument("--once", action="store_true", help="Run one discovery pass")
    parser.add_argument("--host", action="append", default=[], help="Probe one ESP IP")
    parser.add_argument("--target", action="append", default=[], help="Only link this MQTT root")
    parser.add_argument("--ttl", type=int, default=None, help="Override link TTL seconds")
    parser.add_argument("--dry-run", action="store_true", help="Show offers without sending")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if not bool_config(config, "PI_HUB_ESP_LINK_ENABLE", True):
        print("[pi-esp-link] disabled by PI_HUB_ESP_LINK_ENABLE=0")
        return 0

    token = getenv_config(config, "PI_HUB_LINK_TOKEN").strip()
    if not token:
        print("[pi-esp-link] PI_HUB_LINK_TOKEN is required", file=sys.stderr)
        return 1

    hub = getenv_config(config, "PI_HUB_ESP_LINK_ID", "").strip()
    if not hub:
        hub = getenv_config(config, "PI_HUB_AUTO_LINK_ID", "").strip()
    if not hub:
        hub = socket.gethostname().split(".", 1)[0]
    hub = canonical_hub_id(hub)

    priority = int_config(
        config,
        "PI_HUB_ESP_LINK_PRIORITY",
        int_config(config, "PI_HUB_AUTO_LINK_PRIORITY", HOST_PRIORITIES.get(hub, 50)),
    )
    mqtt_port = int_config(config, "PI_HUB_MQTT_PORT", 1883)
    control_port = int_config(config, "PI_HUB_ESP_CONTROL_PORT", 7777)
    interval_s = max(3, int_config(config, "PI_HUB_ESP_SCAN_INTERVAL_SEC", 10))
    full_interval_s = max(
        interval_s, int_config(config, "PI_HUB_ESP_FULL_SCAN_INTERVAL_SEC", 60)
    )
    relink_s = max(10, int_config(config, "PI_HUB_ESP_RELINK_SEC", 60))
    timeout_s = max(
        0.05, int_config(config, "PI_HUB_ESP_SCAN_TIMEOUT_MS", 250) / 1000.0
    )
    concurrency = max(1, int_config(config, "PI_HUB_ESP_SCAN_CONCURRENCY", 32))
    ttl = args.ttl if args.ttl is not None else int_config(config, "PI_HUB_LINK_TTL_SEC", 0)
    if mqtt_port < 1 or mqtt_port > 65535 or control_port < 1 or control_port > 65535:
        print("[pi-esp-link] invalid MQTT/control port", file=sys.stderr)
        return 1
    if ttl < 0:
        print("[pi-esp-link] invalid link TTL", file=sys.stderr)
        return 1

    roots = allowed_roots(config)
    roots.update(root.strip() for root in args.target if root.strip())
    last_link: dict[tuple[str, str], float] = {}
    last_full_scan = 0.0

    print(
        f"[pi-esp-link] hub={hub} priority={priority} mqtt_port={mqtt_port} "
        f"control_port={control_port} interval={interval_s}s full_scan={full_interval_s}s"
    )

    while True:
        now = time.monotonic()
        interfaces = local_interfaces()
        candidates = {host for host in args.host if parse_ipv4(host)}
        candidates.update(neighbor_candidates(interfaces))
        if args.once or (now - last_full_scan) >= full_interval_s:
            candidates.update(sweep_candidates(interfaces))
            last_full_scan = now

        hellos = discover(
            candidates,
            port=control_port,
            timeout_s=timeout_s,
            concurrency=concurrency,
        )

        linked = 0
        for hello in hellos:
            root = str(hello.get("root", "")).strip()
            probe_ip = str(hello.get("_probe_ip", "")).strip()
            if roots and root not in roots:
                continue
            if bool(hello.get("hotspot")) and bool(hello.get("mqtt")):
                if args.once:
                    print(f"[pi-esp-link] skipped {root}@{probe_ip}: already on hotspot broker")
                continue
            key = (root, probe_ip)
            if not args.once and now - last_link.get(key, 0.0) < relink_s:
                continue
            if send_link(
                hello,
                config=config,
                interfaces=interfaces,
                token=token,
                hub=hub,
                priority=priority,
                mqtt_port=mqtt_port,
                control_port=control_port,
                ttl=ttl,
                timeout_s=timeout_s,
                dry_run=args.dry_run,
            ):
                last_link[key] = now
                linked += 1

        if args.once:
            if linked == 0:
                print("[pi-esp-link] no unlinked matching ESPs found")
            return 0

        time.sleep(interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
