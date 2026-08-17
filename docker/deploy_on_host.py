#!/usr/bin/env python3
"""Stage + build the LAN air-quality image ON the .225 host (curl-based).

Container 'aqbuilder' (docker:cli, host socket bind) is created if missing,
started with the -d '{}' idiom the proxy accepts, then:
  exec: write source files from base64 env vars -> /tmp/ctx
  exec: docker build -t tazz/air-quality:amd64 /tmp/ctx
Image lands directly in the host daemon (Portainer's load cap never involved).
"""
import base64
import json
import subprocess
import sys
import time

KEY = subprocess.run(
    "sed -n 's/^PORTAINER_API_KEY=//p' /home/gaurav/.config/portainer-mcp/env | tr -d '\\r\\n '",
    shell=True, capture_output=True, text=True).stdout.strip()
BASE = "https://porty.in.tazz.in/api/endpoints/3/docker"
SRC = "/home/gaurav/opt/tinytuya/docker"
FILES = ["Dockerfile", "tuyainfluxdbv2.py", "config.yaml", "requirements.txt"]
NAME = "aqbuilder"


def curl(method, path, body=None, timeout=600):
    """body: None = no payload; dict/str = JSON body. Returns parsed response."""
    cmd = ["curl", "-sk", "-X", method, "-H", f"X-API-KEY: {KEY}",
           "--max-time", str(timeout), f"{BASE}{path}"]
    import shlex
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body) if not isinstance(body, str) else body]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        print(f"CURL FAIL {method} {path}: {p.stderr[:300]}"); sys.exit(1)
    out = p.stdout
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return out


def get_container(name):
    for c in curl("GET", "/containers/json?all=1") or []:
        if name in [n.lstrip("/") for n in c.get("Names", [])]:
            return c
    return None


# 1. Ensure builder container exists and is running
c = get_container(NAME)
cid = c["Id"] if c else None
if not c:
    r = curl("POST", f"/containers/create?name={NAME}", {
        "Image": "docker:cli",
        "Entrypoint": ["tail"],
        "Cmd": ["-f", "/dev/null"],
        "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
    })
    cid = r["Id"]
    print("created:", cid[:12])
if c and c.get("State") != "running":
    curl("POST", f"/containers/{cid}/start", {})   # -d '{}' idiom, works
    print("started (was:", c.get("State"), ")")
elif c and c.get("State") == "running":
    print("already running:", cid[:12])
cid = cid or c["Id"]

# 2. Stage source files via base64 env vars
env = {}
for f in FILES:
    with open(f"{SRC}/{f}", "rb") as fh:
        env[f"F_{f.replace('.', '_')}"] = base64.b64encode(fh.read()).decode()
decode = " && ".join(f'echo "$F_{f.replace(chr(46), chr(95))}" | base64 -d > /tmp/ctx/{f}' for f in FILES)
ex = curl("POST", f"/containers/{cid}/exec", {
    "AttachStdout": True, "AttachStderr": True, "Tty": True,
    "Env": [f"{k}={v}" for k, v in env.items()],
    "Cmd": ["sh", "-c", f"mkdir -p /tmp/ctx && {decode} && ls -la /tmp/ctx"],
})
out = curl("POST", f"/exec/{ex['Id']}/start", {})
print("staged:\n", out if isinstance(out, str) else json.dumps(out))

# 3. Build in the host daemon
ex = curl("POST", f"/containers/{cid}/exec", {
    "AttachStdout": True, "AttachStderr": True, "Tty": True,
    "Cmd": ["docker", "build", "-t", "tazz/air-quality:amd64", "/tmp/ctx"],
}, timeout=30)
out = curl("POST", f"/exec/{ex['Id']}/start", {}, timeout=600)
text = out if isinstance(out, str) else json.dumps(out)
print("=== build tail ===")
print(text[-1500:])

# 4. Verify image in host daemon
for i in curl("GET", "/images/json?all=1") or []:
    if "tazz/air-quality" in " ".join(i.get("RepoTags", [])):
        print("image:", i["RepoTags"], "created:", time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(i["Created"])), "id:", i["Id"][:12])

# 5. Cleanup
curl("DELETE", f"/containers/{NAME}?force=1", None)
print("builder removed")