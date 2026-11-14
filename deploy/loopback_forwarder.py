#!/usr/bin/env python3
"""Loopback forwarders for the Bambuddy cloud bridge.

Each Bambu printer gets its own loopback address (127.0.1.x). For every
Bambu service port, this process listens on the loopback address and
relays the connection through the tailscaled userspace SOCKS5 proxy
(localhost:1055) to the printer's real LAN address, which the tailnet
reaches through the on-site subnet-route advertisers.

The relay is byte-transparent: MQTT/TLS, FTP and camera protocols pass
through untouched. SOCKS5 CONNECT is done with the target IPv4 address.

Known limit (phase 2): FTP passive data channels are negotiated in-band
with ephemeral ports on the printer side; the control channel relays
fine, but a passive data connection to an unmapped ephemeral port will
not follow. Live status/control (MQTT 8883) is unaffected.
"""

import asyncio
import struct
import sys
import time

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 1055

# loopback -> real printer LAN address (fleet of record, PX-13 instance)
PRINTERS = {
    "127.0.1.2": "192.168.68.54",    # 2 Terminator (home)
    "127.0.1.3": "192.168.68.52",    # 3 X1-Carbon (home)
    "127.0.1.4": "192.168.68.64",    # 4 H2D (home)
    "127.0.1.5": "192.168.68.53",    # 5 Caveman (home)
    "127.0.1.6": "192.168.12.164",   # 6 Vampire (shop)
    "127.0.1.7": "192.168.12.41",    # 7 Titan-1 (shop)
    "127.0.1.8": "192.168.12.227",   # 8 Werewolf (shop)
    "127.0.1.9": "192.168.12.83",    # 9 Titan-2 (shop)
}

# NOTE: 8080 is deliberately NOT relayed. Railway injects PORT=8080 and
# uvicorn wildcard-binds 0.0.0.0:8080; a forwarder bind on 127.0.1.x:8080
# wins the race and uvicorn dies with EADDRINUSE (crash loop, deployment
# c26b3358, 2026-11-14). A1/X1C camera streams (printer port 8080) are a
# phase-2 gap: they cannot be relayed while the app wildcard-binds 8080.
PORTS = [8883, 6000, 990, 322]

_last_err_log = {}


def log(msg):
    print(f"[forwarder] {msg}", flush=True)


def log_err(key, msg):
    now = time.time()
    if now - _last_err_log.get(key, 0) > 60:
        _last_err_log[key] = now
        log(f"relay error {key}: {msg}")


async def socks5_connect(target_ip, target_port):
    reader, writer = await asyncio.open_connection(SOCKS_HOST, SOCKS_PORT)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    resp = await reader.readexactly(2)
    if resp != b"\x05\x00":
        raise ConnectionError(f"socks auth rejected: {resp!r}")
    writer.write(
        b"\x05\x01\x00\x01"
        + bytes(int(x) for x in target_ip.split("."))
        + struct.pack("!H", target_port)
    )
    await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0:
        raise ConnectionError(f"socks connect failed, code {head[1]}")
    atyp = head[3]
    if atyp == 1:
        await reader.readexactly(6)
    elif atyp == 3:
        ln = (await reader.readexactly(1))[0]
        await reader.readexactly(ln + 2)
    elif atyp == 4:
        await reader.readexactly(18)
    return reader, writer


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(client_reader, client_writer, target_ip, target_port):
    key = f"{target_ip}:{target_port}"
    try:
        up_reader, up_writer = await socks5_connect(target_ip, target_port)
    except Exception as exc:
        log_err(key, str(exc))
        try:
            client_writer.close()
        except Exception:
            pass
        return
    await asyncio.gather(
        pipe(client_reader, up_writer),
        pipe(up_reader, client_writer),
    )


async def main():
    servers = []
    for loop_ip, real_ip in PRINTERS.items():
        for port in PORTS:
            srv = await asyncio.start_server(
                lambda r, w, ip=real_ip, p=port: handle(r, w, ip, p),
                host=loop_ip,
                port=port,
            )
            servers.append(srv)
    log(f"{len(servers)} listeners up ({len(PRINTERS)} printers x {len(PORTS)} ports)")
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
