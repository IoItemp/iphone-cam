#!/usr/bin/env python3
"""
iphonecam.py - use an iPhone (running the iPhoneCam app) as a webcam over USB.

Pipeline:
  iPhone camera -> iPhoneCam app (JPEG frames over TCP :5005)
  -> usbmuxd USB tunnel -> receiver thread (decode) -> output loop (steady clock)
  -> virtual camera (OBS / v4l2loopback) -> Zoom, Streamlabs, OBS, browsers, etc.

Usage:
  python iphonecam.py                 # auto-detect first iPhone
  python iphonecam.py --preview       # also show a preview window
  python iphonecam.py --flip          # mirror horizontally
  python iphonecam.py --rotate 90     # rotate 0/90/180/270
  python iphonecam.py --list          # list connected iPhones

While running it prints, every 5 seconds:
  in  = new frames per second arriving from the phone (the real camera rate)
  out = frames per second delivered to the virtual camera (steady clock)
  decode = average time to decode one frame on this PC
"""
import argparse
import plistlib
import socket
import struct
import sys
import threading
import time

import cv2
import numpy as np
import pyvirtualcam
from pyvirtualcam import PixelFormat


# ---------------------------------------------------------------- usbmuxd ---

def _usbmux_socket() -> socket.socket:
    """Open a connection to the local usbmuxd service."""
    if sys.platform == "win32":
        # Apple Mobile Device Service (installed with iTunes / Apple Devices app)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", 27015))
    else:
        # macOS and Linux (Linux needs the `usbmuxd` package running)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("/var/run/usbmuxd")
    return s


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


_tag = 0


def _send_plist(sock: socket.socket, payload: dict) -> None:
    global _tag
    _tag += 1
    body = plistlib.dumps(payload)
    # header: length, version (1 = plist), request type (8 = plist), tag
    sock.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, _tag) + body)


def _recv_plist(sock: socket.socket) -> dict:
    length, _version, _request, _tag_in = struct.unpack("<IIII", _recv_exact(sock, 16))
    return plistlib.loads(_recv_exact(sock, length - 16))


def list_devices() -> list:
    s = _usbmux_socket()
    try:
        _send_plist(s, {
            "MessageType": "ListDevices",
            "ClientVersionString": "iphonecam",
            "ProgName": "iphonecam",
        })
        reply = _recv_plist(s)
    finally:
        s.close()
    devices = []
    for d in reply.get("DeviceList", []):
        props = d.get("Properties", {})
        if props.get("ConnectionType") == "USB":
            devices.append(props)
    return devices


def connect_to_device(device_id: int, port: int) -> socket.socket:
    """Open a TCP tunnel to `port` on the iPhone over USB."""
    s = _usbmux_socket()
    _send_plist(s, {
        "MessageType": "Connect",
        "ClientVersionString": "iphonecam",
        "ProgName": "iphonecam",
        "DeviceID": device_id,
        "PortNumber": socket.htons(port),  # usbmuxd wants network byte order
    })
    reply = _recv_plist(s)
    if reply.get("Number") != 0:
        s.close()
        raise ConnectionError(
            f"usbmuxd refused connection (code {reply.get('Number')}). "
            "Is the iPhoneCam app open and in the foreground?")
    return s  # from here on the socket is a raw tunnel to the phone


# ----------------------------------------------------------------- stream ---

def read_frames(sock: socket.socket):
    """Yield (decoded BGR frame, decode time in seconds) from the JPEG stream."""
    while True:
        (length,) = struct.unpack(">I", _recv_exact(sock, 4))
        if length == 0 or length > 20_000_000:
            raise ValueError(f"bad frame length {length}")
        jpeg = _recv_exact(sock, length)
        t0 = time.perf_counter()
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        dt = time.perf_counter() - t0
        if frame is not None:
            yield frame, dt


def transform(frame, rotate: int, flip: bool):
    if rotate == 90:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif rotate == 180:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
    elif rotate == 270:
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if flip:
        frame = cv2.flip(frame, 1)
    return frame


class LatestFrame:
    """Shared slot holding the newest decoded frame plus simple counters."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.received = 0        # frames received since start
        self.decode_time = 0.0   # summed decode seconds since last stats read
        self.decode_count = 0
        self.error = None        # exception raised in the receiver thread


def receiver_loop(sock, state: LatestFrame, rotate: int, flip: bool) -> None:
    """Runs in its own thread: read, decode and orient frames as fast as they arrive."""
    try:
        for frame, dt in read_frames(sock):
            frame = transform(frame, rotate, flip)
            with state.lock:
                state.frame = frame
                state.received += 1
                state.decode_time += dt
                state.decode_count += 1
    except Exception as e:  # reported to the main thread, which decides to retry
        state.error = e


def run_session(args) -> None:
    devices = list_devices()
    if not devices:
        raise ConnectionError(
            "No iPhone found over USB. Plug it in, unlock it, and tap 'Trust This Computer'.")
    if args.device >= len(devices):
        raise ConnectionError(f"--device {args.device} out of range ({len(devices)} found)")
    device = devices[args.device]
    print(f"Found iPhone (serial {device.get('SerialNumber', '?')}). Connecting...")

    sock = connect_to_device(device["DeviceID"], args.port)
    # Ask the phone app for the requested capture format (needs the updated app).
    sock.sendall(f"CONFIG {args.width} {args.height} {args.fps}\n".encode())
    print(f"Connected. Requested {args.width}x{args.height} at {args.fps} fps.")

    state = LatestFrame()
    threading.Thread(
        target=receiver_loop, args=(sock, state, args.rotate, args.flip), daemon=True
    ).start()

    cam = None
    out_frames = 0
    last_received = 0
    last_t = time.time()
    try:
        while True:
            if state.error is not None:
                raise state.error

            with state.lock:
                frame = state.frame
            if frame is None:  # still waiting for the first frame
                time.sleep(0.005)
                continue

            h, w = frame.shape[:2]
            if cam is None:
                try:
                    cam = pyvirtualcam.Camera(
                        width=w, height=h, fps=args.fps,
                        fmt=PixelFormat.BGR, backend=args.backend)
                except Exception as e:
                    raise SystemExit(
                        f"Could not start virtual camera: {e}\n"
                        "Windows/macOS: install OBS Studio, open it once, and click "
                        "'Start Virtual Camera' then stop it (this registers the device).\n"
                        "Linux: sudo modprobe v4l2loopback exclusive_caps=1")
                print(f"Virtual camera active: '{cam.device}' at {w}x{h}")

            if (w, h) != (cam.width, cam.height):
                frame = cv2.resize(frame, (cam.width, cam.height))

            # Deliver on a steady clock. If the phone is a little slow, the last
            # frame is repeated instead of the output rate wobbling.
            cam.send(frame)
            cam.sleep_until_next_frame()

            if args.preview:
                cv2.imshow("iPhoneCam preview (q to quit)", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    raise KeyboardInterrupt

            out_frames += 1
            now = time.time()
            if now - last_t >= 5:
                with state.lock:
                    received = state.received
                    dec_ms = (state.decode_time / state.decode_count * 1000
                              if state.decode_count else 0.0)
                    state.decode_time, state.decode_count = 0.0, 0
                dt = now - last_t
                print(f"  in {(received - last_received) / dt:5.1f} fps | "
                      f"out {out_frames / dt:5.1f} fps | decode {dec_ms:4.1f} ms")
                last_received, out_frames, last_t = received, 0, now
    finally:
        sock.close()
        if cam is not None:
            cam.close()
        if args.preview:
            cv2.destroyAllWindows()


def main() -> None:
    p = argparse.ArgumentParser(description="Use an iPhone as a USB webcam.")
    p.add_argument("--port", type=int, default=5005, help="port the iPhone app listens on")
    p.add_argument("--width", type=int, default=1280, help="capture width requested from the phone")
    p.add_argument("--height", type=int, default=720, help="capture height requested from the phone")
    p.add_argument("--fps", type=int, default=30, help="capture and virtual camera frame rate")
    p.add_argument("--device", type=int, default=0, help="which iPhone, if several are plugged in")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0)
    p.add_argument("--flip", action="store_true", help="mirror horizontally")
    p.add_argument("--preview", action="store_true", help="show a local preview window")
    p.add_argument("--backend", default=None,
                   help="pyvirtualcam backend (obs, v4l2loopback, unitycapture); default auto")
    p.add_argument("--list", action="store_true", help="list connected iPhones and exit")
    args = p.parse_args()

    if args.list:
        try:
            devs = list_devices()
        except OSError as e:
            sys.exit(f"Cannot reach usbmuxd: {e}")
        if not devs:
            print("No iPhones found.")
        for i, d in enumerate(devs):
            print(f"[{i}] serial {d.get('SerialNumber')}  id {d.get('DeviceID')}")
        return

    while True:
        try:
            run_session(args)
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except (ConnectionError, ValueError, OSError) as e:
            print(f"{e}\nRetrying in 3s... (Ctrl+C to quit)")
            time.sleep(3)


if __name__ == "__main__":
    main()
