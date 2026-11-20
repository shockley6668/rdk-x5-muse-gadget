#!/usr/bin/env python3
"""Send a typed message or push-to-talk command to the local Muse UI."""
import argparse
import json
import socket

p = argparse.ArgumentParser()
p.add_argument("message", nargs="?")
p.add_argument("--action", choices=["start", "stop", "mute", "call", "hangup"])
a = p.parse_args()
if not a.action and not a.message:
    p.error("provide a message or --action")
command = {"action": a.action} if a.action else {"action": "text", "message": a.message}
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
    s.sendto(json.dumps(command).encode(), "/run/muse-ui/input.sock")
