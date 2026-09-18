#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2Ray SNI Updater & Connectivity Tester — sing-box edition
==========================================================
يستخدم محرك sing-box (نفس محرك Hiddify-core) لفحص آلاف النودز
من عملية واحدة عبر Clash API بدون فتح أي processes إضافية.

Usage:
  python v2ray_sni_updater.py <sni> <url1,url2,... | sources_file.txt> [flags]

Flags:
  --no-upload      لا ترفع النتائج إلى GitHub
  --retry-upload   أعد المحاولة 3 مرات عند الرفع (للنت الضعيف)
  --upload-only    ارفع الملفات المحلية الموجودة فقط بدون فحص
  --append         أضف النودز الجديدة للملف المحلي القديم
  --merge          ادمج مع ملف GitHub الحالي أيضاً (يتطلب نت شغال)
  --batch N        حجم الدفعة الواحدة (default 500)
  --timeout MS     مهلة كل نود بالمللي ثانية (default 8000)
"""
import sys
import os
import re
import json
import html
import base64
import subprocess
import socket
import time
import urllib.request
import urllib.parse
import tempfile
import random

# Default settings
# Pre-tested endpoint (Xray-verified upstream) -> far fewer dead nodes to test ourselves
DEFAULT_SOURCE_URL = "https://raw.githubusercontent.com/4n0nymou3/multi-proxy-config-fetcher/refs/heads/main/configs/proxy_configs_tested.txt"
RAW_SOURCE_URL = "https://raw.githubusercontent.com/4n0nymou3/multi-proxy-config-fetcher/refs/heads/main/configs/proxy_configs.txt"
TEST_URL = "http://cp.cloudflare.com/generate_204"
TIMEOUT_MS = 8000          # per-node test timeout (ms)
BATCH_SIZE = 200           # nodes per sing-box instance
API_PORT_BASE = 19090      # clash api ports: base + batch*2
MAX_BATCHES_PORTS = 40     # rotate ports after this many batches
FETCH_RETRIES = 3          # download attempts with exponential backoff

# --- GITHUB CONFIGURATION ---
# Token read from environment variable GITHUB_TOKEN (never hardcode secrets).
# Needed only for --upload mode; Actions workflow commits via git instead.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Kirolos124/multi-proxy")

SINGBOX_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "sing-box", "sing-box-1.13.19-windows-amd64", "sing-box.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sing-box", "sing-box.exe"),
    r"C:\sing-box\sing-box.exe",
]


def print_banner():
    banner = """
=========================================================
     V2Ray SNI Updater & Tester  [sing-box engine]
=========================================================
 السكريبت يقوم بـ:
 1. تحميل قوائم الخوادم (VMess, VLess, Trojan) من عدة روابط.
 2. تعديل الـ SNI لخوادم TLS وتفعيل insecure.
 3. فحص كل النودز بالتوازي من عملية sing-box واحدة (Clash API).
 4. ترتيب النودز الشغالة حسب الاستجابة وحفظها.
 5. دعم الروابط المتعددة ووضع الإضافة/الدمج.
=========================================================
"""
    print(banner)


class Color:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            GREEN = ''
            RED = ''
            YELLOW = ''
            RESET = ''


def detect_singbox():
    for path in SINGBOX_PATHS:
        if os.path.exists(path):
            return path
    try:
        subprocess.run(["sing-box", "version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "sing-box"
    except FileNotFoundError:
        pass
    return None


def _read_streaming(response):
    """Read response body in chunks with progress display. Returns decoded text."""
    total = int(response.headers.get('Content-Length') or 0)
    chunks = []
    downloaded = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        downloaded += len(chunk)
        if total:
            print(f"\r[+] Downloaded {downloaded/1048576:.1f} / {total/1048576:.1f} MB", end="", flush=True)
    print()
    return b"".join(chunks).decode('utf-8', errors='ignore')


def _fetch_with_retry(req, retries=FETCH_RETRIES):
    """Fetch a urllib Request with exponential backoff (3s, 6s, 12s...)."""
    delay = 3
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=3600) as response:
                return _read_streaming(response)
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"[*] Attempt {attempt}/{retries} failed ({e}) — retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
    raise last_err


def fetch_links(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        return _fetch_with_retry(req)
    except Exception as e:
        print(f"Error fetching list: {e}")
        return None


# Telegram public channel pages embed message HTML; proxy links appear as text.
PROXY_LINK_RE = re.compile(r'(?:vmess|vless|trojan)://[^\s<>"\']+')


def is_telegram_url(url):
    return 't.me/s/' in url


def fetch_telegram_channel(url):
    """Fetch a t.me/s/<channel> page and extract all proxy links from messages."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        page = _fetch_with_retry(req)
    except Exception as e:
        print(f"Error fetching telegram channel: {e}")
        return []
    # Unescape HTML entities (&amp; -> &) then regex out every proxy link
    text = html.unescape(page)
    links = PROXY_LINK_RE.findall(text)
    # Strip trailing punctuation that may have been glued to the link in prose
    cleaned = [l.rstrip('.,;)»"\'') for l in links]
    return [l for l in cleaned if len(l) > 15]


def parse_content_to_links(content_str):
    """Decode content (raw or base64 subscription) and return list of supported links."""
    content_str = content_str.strip()

    decoded = None
    try:
        b64_str = content_str
        padding = len(b64_str) % 4
        if padding:
            b64_str += '=' * (4 - padding)
        decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
        if not any(proto in decoded for proto in ('vmess://', 'vless://', 'trojan://')):
            decoded = None
    except Exception:
        decoded = None

    text = decoded if decoded else content_str
    links = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('vmess://', 'vless://', 'trojan://')):
            links.append(line)
    return links


# sing-box supported uTLS fingerprints (anything else crashes the whole batch)
SINGBOX_FINGERPRINTS = {
    "chrome", "firefox", "safari", "ios", "android", "edge", "360",
    "qq", "random", "randomized",
}
# sing-box supported vless flows
SINGBOX_FLOWS = {"", "xtls-rprx-vision"}


def _sanitize_fp(fp):
    fp = (fp or "").strip().lower()
    return fp if fp in SINGBOX_FINGERPRINTS else None


def _sanitize_flow(flow):
    flow = (flow or "").strip()
    return flow if flow in SINGBOX_FLOWS else None


def _b64_json_vmess(url):
    parsed = urllib.parse.urlparse(url)
    b64_str = parsed.netloc
    padding = len(b64_str) % 4
    if padding:
        b64_str += '=' * (4 - padding)
    return json.loads(base64.b64decode(b64_str).decode('utf-8', errors='ignore'))


def _make_tls(server_name, insecure=True, fp=None, alpn=None, reality=None):
    tls = {"enabled": True, "server_name": server_name, "insecure": insecure}
    if alpn:
        tls["alpn"] = alpn
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if reality:
        tls["reality"] = reality
        if not fp:
            tls["utls"] = {"enabled": True, "fingerprint": "chrome"}
    return tls


def _make_transport(net, path="", host="", service_name="", header_type=""):
    """Build sing-box transport dict from common link parameters."""
    if net == "ws":
        t = {"type": "ws"}
        if path:
            t["path"] = path
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        t = {"type": "grpc"}
        if service_name:
            t["service_name"] = service_name
        return t
    if net in ("h2", "http") and header_type != "http":
        t = {"type": "http"}
        if path:
            t["path"] = path
        if host:
            t["host"] = [host]
        return t
    if net == "tcp" and header_type == "http":
        t = {"type": "http", "method": "GET"}
        t["path"] = [path] if path else ["/"]
        t["headers"] = {"Host": [host]} if host else {}
        return t
    return None  # plain tcp -> no transport


def build_outbound(link, custom_sni, idx, force_sni=False):
    """Convert one link to a sing-box outbound.

    Returns (tag, outbound, modified_link, sni_is_custom) or None.
    sni_is_custom=True means the node was tested WITH custom_sni
    (qualifies for the dedicated speedtest.net file).

    SNI policy:
      - TLS nodes: server_name overwritten with custom_sni (the tool's purpose).
      - REALITY nodes: original sni kept by default (overwriting breaks the
        reality handshake on servers with an SNI allowlist).
        With force_sni=True, reality nodes get custom_sni too — tested and
        saved accordingly; only servers accepting arbitrary SNI survive.
    """
    tag = f"n{idx}"
    try:
        scheme = urllib.parse.urlparse(link).scheme.lower()

        if scheme == "vmess":
            data = _b64_json_vmess(link)
            if str(data.get("tls", "")).lower() != "tls":
                return None
            net = data.get("net", "tcp")
            outbound = {
                "type": "vmess",
                "tag": tag,
                "server": data.get("add"),
                "server_port": int(data.get("port")),
                "uuid": data.get("id"),
                "security": data.get("scy") or "auto",
                "alter_id": int(data.get("aid", 0) or 0),
                "tls": _make_tls(
                    custom_sni,
                    fp=_sanitize_fp(data.get("fp")),
                    alpn=[a.strip() for a in data["altn"].split(",")] if data.get("altn") else None,
                ),
            }
            tr = _make_transport(net, data.get("path", ""), data.get("host", ""),
                                 data.get("path", ""), data.get("type", ""))
            if tr:
                outbound["transport"] = tr
            if not outbound["server"] or not outbound["uuid"]:
                return None
            # Modified vmess link with new SNI + insecure
            mod = dict(data)
            mod["sni"] = custom_sni
            mod["tls"] = "tls"
            mod["skip-cert-verify"] = True
            mod_b64 = base64.b64encode(json.dumps(mod).encode()).decode()
            return tag, outbound, f"vmess://{mod_b64}", True

        if scheme in ("vless", "trojan"):
            parsed = urllib.parse.urlparse(link)
            if '@' not in parsed.netloc:
                return None
            user_info, server_info = parsed.netloc.rsplit('@', 1)
            if ':' not in server_info:
                return None
            server_addr, server_port = server_info.rsplit(':', 1)
            server_port = int(server_port)
            q = dict(urllib.parse.parse_qsl(parsed.query))

            # Scrub '#'-polluted params: source authors sometimes embed the
            # remark as an encoded %23 INSIDE a param value
            # (e.g. type=tcp%23name) — Hiddify's parser then reads transport
            # "tcp#name" and the whole background core fails to start.
            recovered_fragments = []
            cleaned_q = {}
            for k, v in q.items():
                if "#" in v:
                    v, _, tail = v.partition("#")
                    if tail:
                        recovered_fragments.append(tail)
                    v = v.strip()
                k = k.replace("#", "").strip()
                if not k:
                    continue
                cleaned_q[k] = v
            q = cleaned_q

            security = q.get("security", "").lower()
            if scheme == "trojan" and not security:
                security = "tls"
            if security not in ("tls", "reality"):
                return None

            # SNI: reality keeps its own unless force_sni, tls gets the custom one
            sni = custom_sni if (security == "tls" or force_sni) else q.get("sni", "")
            reality = None
            if security == "reality":
                reality = {
                    "enabled": True,
                    "public_key": q.get("pbk", ""),
                    "short_id": q.get("sid", ""),
                }
                if not reality["public_key"]:
                    return None

            outbound = {
                "type": scheme,
                "tag": tag,
                "server": server_addr,
                "server_port": server_port,
                "tls": _make_tls(
                    sni,
                    fp=_sanitize_fp(q.get("fp")),
                    alpn=[a.strip() for a in q["alpn"].split(",")] if q.get("alpn") else None,
                    reality=reality,
                ),
            }
            if scheme == "vless":
                outbound["uuid"] = user_info
                flow = _sanitize_flow(q.get("flow"))
                if flow:
                    outbound["flow"] = flow
            else:
                outbound["password"] = urllib.parse.unquote(user_info)

            # Transport whitelist — anything unknown falls back to tcp so the
            # saved link never carries a type that crashes client parsers.
            KNOWN_NETS = {"tcp", "raw", "ws", "grpc", "h2", "http"}
            raw_net = (q.get("type") or "tcp").strip().lower()
            net = raw_net if raw_net in KNOWN_NETS else "tcp"
            q["type"] = net
            tr = _make_transport(net, q.get("path", ""), q.get("host", ""),
                                 q.get("serviceName", ""), q.get("headerType", ""))
            if tr:
                outbound["transport"] = tr
            # Modified link: new SNI (tls always; reality only when forced)
            # + explicit insecure flags. Both 'allowInsecure' and 'insecure' are
            # set to '1' so clients reading either parameter see consistent
            # values (a leftover insecure=0 makes Hiddify verify TLS ->
            # cert mismatch -> dead node). Query values are pre-scrubbed of
            # '#'-pollution and the type is whitelisted, so Hiddify's parser
            # can never choke on them again.
            mod_q = dict(q)
            if security == "tls" or (security == "reality" and force_sni):
                mod_q["sni"] = custom_sni
                if security == "tls":
                    mod_q["allowInsecure"] = "1"
                    mod_q["insecure"] = "1"
            new_query = urllib.parse.urlencode(mod_q)
            modified_link = urllib.parse.ParseResult(
                scheme=parsed.scheme,
                netloc=parsed.netloc,
                path=parsed.path,
                params=parsed.params,
                query=new_query,
                fragment=parsed.fragment,
            ).geturl()
            sni_is_custom = (security == "tls") or (security == "reality" and force_sni)
            return tag, outbound, modified_link, sni_is_custom

        return None
    except Exception:
        return None


def dedup_links(links):
    """Remove duplicate nodes by protocol|server|port|identity.
    Never discards a link on parse failure — falls back to raw-string identity
    so validation stays the job of build_outbound."""
    seen = set()
    unique = []
    for link in links:
        try:
            scheme = urllib.parse.urlparse(link).scheme.lower()
            if scheme == "vmess":
                d = _b64_json_vmess(link)
                key = f"vmess|{d.get('add')}|{d.get('port')}|{d.get('id')}"
            else:
                parsed = urllib.parse.urlparse(link)
                key = f"{scheme}|{parsed.netloc.lower()}|{sorted(urllib.parse.parse_qsl(parsed.query).items())}"
        except Exception:
            key = ("raw", link)
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def build_batch_config(node_outbounds, api_port):
    """Build a single sing-box config: clash api + selector group with all nodes."""
    tags = [o["tag"] for o in node_outbounds]
    outbounds = [{
        "type": "selector",
        "tag": "select",
        "outbounds": tags + ["direct"],
        "interrupt_exist_connections": False,
    }] + node_outbounds + [{"type": "direct", "tag": "direct"}]

    return {
        "log": {"level": "fatal"},
        "experimental": {
            "clash_api": {
                "external_controller": f"127.0.0.1:{api_port}",
                "default_mode": "rule",
            }
        },
        "outbounds": outbounds,
        "route": {"rules": [], "final": "select"},
    }


def _wait_api(api_port, proc, deadline_s=8.0):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            s = socket.create_connection(("127.0.0.1", api_port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def run_batch(singbox_path, node_outbounds, batch_idx, test_url, timeout_ms, _depth=0):
    """Start one sing-box instance and group-test all nodes. Returns ({tag: delay}, direct_ok).
    If sing-box fails to start (one bad outbound can kill the whole config), the batch
    is split in half and each half is retried recursively (up to depth 3)."""
    api_port = API_PORT_BASE + (batch_idx % MAX_BATCHES_PORTS) * 2
    config = build_batch_config(node_outbounds, api_port)

    fd, cfg_path = tempfile.mkstemp(prefix=f"sbox_{batch_idx}_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Use temp files instead of PIPEs to avoid pipe-buffer deadlocks on Windows
    out_path = cfg_path + ".out"
    err_path = cfg_path + ".err"
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        proc = subprocess.Popen(
            [singbox_path, "run", "-c", cfg_path],
            stdout=out_f, stderr=err_f,
            creationflags=creationflags,
        )
        delays = {}
        direct_ok = None
        try:
            if not _wait_api(api_port, proc):
                # sing-box crashed — capture stderr for diagnosis
                err_text = ""
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                    with open(err_path, "r", errors="ignore") as f:
                        err_text = f.read()[:300]
                except Exception:
                    pass
                # One poisoned outbound kills the whole config: split & retry
                if len(node_outbounds) > 1 and _depth < 3:
                    mid = len(node_outbounds) // 2
                    d1, ok1 = run_batch(singbox_path, node_outbounds[:mid], batch_idx,
                                        test_url, timeout_ms, _depth + 1)
                    d2, ok2 = run_batch(singbox_path, node_outbounds[mid:], batch_idx + 1000 * (_depth + 1),
                                        test_url, timeout_ms, _depth + 1)
                    return {**d1, **d2}, (ok1 if ok1 is not None else ok2)
                print(f"    {Color.RED}[!] sing-box failed (batch {batch_idx}):{Color.RESET} {err_text}")
                return delays, direct_ok

            encoded = urllib.parse.quote(test_url, safe="")
            url = (f"http://127.0.0.1:{api_port}/group/select/delay"
                   f"?url={encoded}&timeout={timeout_ms}")
            try:
                with urllib.request.urlopen(url, timeout=timeout_ms / 1000.0 + 30) as r:
                    delays = json.loads(r.read().decode())
            except Exception:
                delays = {}

            # sanity: direct connectivity inside this batch
            try:
                durl = (f"http://127.0.0.1:{api_port}/proxies/direct/delay"
                        f"?url={encoded}&timeout={timeout_ms}")
                with urllib.request.urlopen(durl, timeout=timeout_ms / 1000.0 + 10) as r:
                    direct_ok = bool(json.loads(r.read().decode()).get("delay"))
            except Exception:
                direct_ok = False
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    for p in (cfg_path, out_path, err_path):
        try:
            os.remove(p)
        except Exception:
            pass
    return delays, direct_ok


# ------------------------- GitHub upload -------------------------

def _clash_proxy_from_outbound(o):
    """Convert a sing-box outbound dict to a Clash/Mihomo proxy dict.
    Unlike upstream's converter, this keeps REALITY (reality-opts),
    vless flow, and grpc transport intact."""
    ptype = o.get("type")
    server = o.get("server")
    port = o.get("server_port")
    if not ptype or not server or not port or ptype in ("direct", "selector", "urltest"):
        return None
    transport = o.get("transport") or {}
    tls = o.get("tls") or {}

    proxy = {"name": o.get("tag", ""), "type": ptype, "server": server,
             "port": int(port)}

    net = transport.get("type", "tcp")
    if net in ("h2", "http"):
        proxy["network"] = "h2"
        h2 = {}
        if transport.get("path"):
            h2["path"] = transport["path"]
        if transport.get("host"):
            h2["host"] = transport["host"]
        if h2:
            proxy["h2-opts"] = h2
    elif net == "tcp" and transport.get("method"):
        # tcp with http obfs header
        proxy["network"] = "http"
        proxy["http-opts"] = {
            "method": transport.get("method", "GET"),
            "path": transport.get("path", ["/"]),
            "headers": transport.get("headers", {}),
        }
    else:
        proxy["network"] = net
        if net == "ws":
            inner = {"path": transport.get("path", "/")}
            host = (transport.get("headers") or {}).get("Host")
            if host:
                inner["headers"] = {"Host": host}
            proxy["ws-opts"] = inner
        elif net == "grpc":
            proxy["grpc-opts"] = {"grpc-service-name": transport.get("service_name", "")}

    if tls.get("enabled"):
        proxy["tls"] = True
        proxy["servername"] = tls.get("server_name") or server
        proxy["skip-cert-verify"] = bool(tls.get("insecure", False))
        utls = tls.get("utls") or {}
        proxy["client-fingerprint"] = utls.get("fingerprint", "chrome")
        if tls.get("alpn"):
            proxy["alpn"] = tls["alpn"]
        reality = tls.get("reality") or {}
        if reality.get("enabled"):
            proxy["reality-opts"] = {
                "public-key": reality.get("public_key", ""),
                "short-id": reality.get("short_id", ""),
            }

    if ptype == "vmess":
        proxy["uuid"] = o.get("uuid", "")
        proxy["alterId"] = int(o.get("alter_id", 0))
        proxy["cipher"] = o.get("security", "auto")
    elif ptype == "vless":
        proxy["uuid"] = o.get("uuid", "")
        if o.get("flow"):
            proxy["flow"] = o["flow"]
    elif ptype == "trojan":
        proxy["password"] = o.get("password", "")
    else:
        return None
    return proxy


def build_clash_config(node_outbounds):
    """Build a complete Clash/Mihomo config from working sing-box outbounds."""
    proxies = []
    names = []
    seen_tags = set()
    for o in node_outbounds:
        p = _clash_proxy_from_outbound(o)
        if not p:
            continue
        tag = p["name"]
        n = 0
        while tag in seen_tags:  # clash requires unique names
            n += 1
            tag = f"{p['name']}#{n}"
        p["name"] = tag
        seen_tags.add(tag)
        proxies.append(p)
        names.append(tag)

    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True,
            "listen": "127.0.0.1:1053",
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["https://8.8.8.8/dns-query", "https://1.1.1.1/dns-query"],
        },
        "proxies": proxies,
        "proxy-groups": [
            {"name": "PROXY", "type": "select",
             "proxies": ["Auto-Best-Ping"] + names + ["DIRECT"]},
            {"name": "Auto-Best-Ping", "type": "url-test",
             "proxies": names, "url": TEST_URL, "interval": 180, "tolerance": 50},
        ],
        "rules": ["GEOIP,lan,DIRECT,no-resolve", "MATCH,PROXY"],
    }


def get_github_file_sha(repo, path, token):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Python-urllib")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
            return data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def upload_to_github(repo, path, content_bytes, token, message):
    sha = get_github_file_sha(repo, path, token)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Python-urllib")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.getcode() in (200, 201):
                print(f"[+] Successfully uploaded '{path}' to GitHub repository '{repo}'.")
                return True
    except Exception as e:
        print(f"[-] Failed to upload '{path}': {e}")
    return False


def upload_files_with_retry(files, retry_upload):
    max_attempts = 3 if retry_upload else 1
    for path, content, message in files:
        ok = False
        for attempt in range(1, max_attempts + 1):
            if upload_to_github(GITHUB_REPO, path, content, GITHUB_TOKEN, message):
                ok = True
                break
            if attempt < max_attempts:
                print(f"[*] Retrying upload of '{path}' (attempt {attempt + 1}/{max_attempts})...")
                time.sleep(10)
        if not ok:
            print(f"[-] Failed to upload '{path}' after {max_attempts} attempt(s).")


def upload_results(retry_upload):
    print("\n[*] Upload-only mode: re-uploading existing local files...")
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print("[-] GitHub upload not configured.")
        return
    files = []
    for name in ("working_proxies.txt", "working_subscription.txt"):
        if os.path.exists(name):
            with open(name, "rb") as f:
                files.append((name, f.read(), f"Update {name} (manual re-upload)"))
            print(f"[+] Loaded '{name}' ({os.path.getsize(name)} bytes).")
        else:
            print(f"[-] '{name}' not found locally. Skipping.")
    if files:
        upload_files_with_retry(files, retry_upload)
    else:
        print("[-] No files to upload.")


# ------------------------------ main ------------------------------

def load_existing_nodes(txt_filename):
    existing = []
    if os.path.exists(txt_filename):
        try:
            with open(txt_filename, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        existing.append(line)
            if existing:
                print(f"[+] Loaded {len(existing)} existing nodes from '{txt_filename}'.")
        except Exception as e:
            print(f"[-] Error loading existing nodes: {e}")
    return existing


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    print_banner()

    singbox_path = detect_singbox()
    if not singbox_path:
        print("[-] Error: sing-box.exe not found!")
        print("Expected locations:")
        for p in SINGBOX_PATHS:
            print(f"  - {p}")
        sys.exit(1)
    print(f"[+] Found sing-box at: {singbox_path}")

    flags = [a.lower() for a in sys.argv[1:] if a.lower().startswith('--')]
    positionals = [a for a in sys.argv[1:] if not a.lower().startswith('--')]
    no_upload = '--no-upload' in flags
    retry_upload = '--retry-upload' in flags
    append_mode = '--append' in flags
    merge_mode = '--merge' in flags

    batch_size = BATCH_SIZE
    timeout_ms = TIMEOUT_MS
    retest_count = 50  # 0 disables the second verification pass
    force_sni = '--force-sni' in flags
    for flag in flags:
        if flag.startswith('--batch='):
            try:
                batch_size = max(10, int(flag.split('=', 1)[1]))
            except ValueError:
                pass
        if flag.startswith('--timeout='):
            try:
                timeout_ms = max(1000, int(flag.split('=', 1)[1]))
            except ValueError:
                pass
        if flag.startswith('--retest'):
            try:
                retest_count = max(0, int(flag.split('=', 1)[1]))
            except (ValueError, IndexError):
                pass

    if '--upload-only' in flags:
        upload_results(retry_upload)
        return

    # --- SNI + source URLs ---
    if positionals:
        custom_sni = positionals[0].strip()
        input_urls = []
        if len(positionals) > 1:
            url_arg = positionals[1].strip()
            if os.path.isfile(url_arg):
                with open(url_arg, 'r', encoding='utf-8') as f:
                    input_urls = [l.strip() for l in f if l.strip() and not l.startswith('#')]
                print(f"[+] Loaded {len(input_urls)} URLs from file: {url_arg}")
            elif ',' in url_arg:
                input_urls = [u.strip() for u in url_arg.split(',') if u.strip()]
                print(f"[+] Using {len(input_urls)} URLs from comma-separated list.")
            else:
                input_urls = [url_arg]
        else:
            input_urls = [DEFAULT_SOURCE_URL]
        print(f"[+] Using SNI: {custom_sni}")
    else:
        custom_sni = input("[?] Enter target SNI (e.g. speedtest.net): ").strip()
        while not custom_sni:
            custom_sni = input("[!] SNI cannot be empty. Enter target SNI: ").strip()
        url_input = input(f"[?] Enter URL(s), comma-separated, or a .txt file [Default: {DEFAULT_SOURCE_URL}]: ").strip()
        if not url_input:
            input_urls = [DEFAULT_SOURCE_URL]
        elif os.path.isfile(url_input):
            with open(url_input, 'r', encoding='utf-8') as f:
                input_urls = [l.strip() for l in f if l.strip() and not l.startswith('#')]
        elif ',' in url_input:
            input_urls = [u.strip() for u in url_input.split(',') if u.strip()]
        else:
            input_urls = [url_input]

    # --- Fetch ---
    all_links = []
    for url in input_urls:
        if is_telegram_url(url):
            print(f"\n[*] Fetching telegram channel: {url} ...")
            links = fetch_telegram_channel(url)
            print(f"[+] Extracted {len(links)} supported links from channel.")
            all_links.extend(links)
            continue
        print(f"\n[*] Fetching list from: {url} ...")
        raw = fetch_links(url)
        if not raw:
            print(f"[-] Failed to download from {url}. Skipping.")
            continue
        links = parse_content_to_links(raw)
        print(f"[+] Extracted {len(links)} supported links (vmess/vless/trojan).")
        all_links.extend(links)

    all_links = dedup_links(all_links)
    print(f"\n[+] Total unique candidate links: {len(all_links)}")

    # --- Convert to outbounds ---
    print("[*] Converting links to sing-box outbounds (TLS filter + SNI override)...")
    outbounds = []       # list of outbound dicts
    tag_to_link = {}     # tag -> modified-style display link (original)
    tag_to_sni_custom = {}  # tag -> True if node tested WITH custom_sni
    idx = 0
    discarded = 0
    for link in all_links:
        result = build_outbound(link, custom_sni, idx, force_sni=force_sni)
        if result:
            tag, ob, modified_link, sni_is_custom = result
            outbounds.append(ob)
            tag_to_link[tag] = modified_link
            tag_to_sni_custom[tag] = sni_is_custom
            idx += 1
        else:
            discarded += 1
    print(f"[+] {len(outbounds)} TLS/Reality configurations ready.")
    print(f"[-] Discarded {discarded} non-TLS or invalid configurations.")

    if not outbounds:
        print("[-] No compatible configurations found. Exiting.")
        sys.exit(0)

    # --- Test in batches ---
    total_batches = (len(outbounds) + batch_size - 1) // batch_size
    print(f"\n[*] Testing {len(outbounds)} nodes in {total_batches} batch(es) "
          f"of up to {batch_size} nodes each (timeout {timeout_ms}ms/node)...")

    results = {}  # tag -> delay
    any_direct_fail = False
    for i in range(total_batches):
        chunk = outbounds[i * batch_size:(i + 1) * batch_size]
        t0 = time.time()
        delays, direct_ok = run_batch(singbox_path, chunk, i, TEST_URL, timeout_ms)
        # 'direct' is included in the group test for sanity; keep it out of node results
        node_delays = {t: d for t, d in delays.items() if t != "direct"}
        results.update(node_delays)
        found = len(node_delays)
        status = ""
        if direct_ok is False:
            any_direct_fail = True
            status = f"  {Color.YELLOW}[!] WARNING: direct internet check FAILED in this batch{Color.RESET}"
        print(f"[Batch {i+1}/{total_batches}] {found}/{len(chunk)} alive "
              f"in {time.time()-t0:.1f}s | running total: {len(results)}{status}")

    print(f"\n[+] Testing completed. {len(results)} / {len(outbounds)} nodes are working.")

    # --- Retest pass: verify top N nodes survived by chance (two-pass accuracy) ---
    if retest_count > 0 and results:
        top = sorted(results.items(), key=lambda x: x[1])[:retest_count]
        tag_index = {o["tag"]: o for o in outbounds}
        retest_outbounds = [tag_index[t] for t, _ in top if t in tag_index]
        if retest_outbounds:
            print(f"\n[*] Retest pass: verifying top {len(retest_outbounds)} nodes a second time...")
            confirmed = {}
            rb_total = (len(retest_outbounds) + batch_size - 1) // batch_size
            for i in range(rb_total):
                chunk = retest_outbounds[i * batch_size:(i + 1) * batch_size]
                delays, _ = run_batch(singbox_path, chunk, 1000 + i, TEST_URL, timeout_ms)
                confirmed.update({t: d for t, d in delays.items() if t != "direct"})
            before = len(top)
            # Remove only the retested nodes that FAILED the second pass.
            # All other working nodes (outside top-N) are kept untouched.
            flukes = {t for t, _ in top if t not in confirmed}
            results = {t: d for t, d in results.items() if t not in flukes}
            # Refresh latencies of retest survivors with their fresh measurements
            results.update(confirmed)
            print(f"[+] Retest: {len(confirmed)}/{before} confirmed stable "
                  f"(flukes removed: {before - len(confirmed)}). "
                  f"Total kept: {len(results)}.")

    # --- Split results: nodes tested WITH custom_sni vs reality-on-own-SNI ---
    sni_matched = []   # [(link, delay)] already verified using custom_sni
    other_group = []   # [(tag, link, delay)] reality nodes running on original SNI
    for tag, delay in results.items():
        if tag_to_sni_custom.get(tag):
            sni_matched.append((tag_to_link[tag], delay))
        else:
            other_group.append((tag, tag_to_link[tag], delay))
    print(f"\n[+] {len(sni_matched)} nodes already on '{custom_sni}' | "
          f"{len(other_group)} reality nodes on their original SNI.")

    # --- Probe pass: offer the forced SNI to reality survivors too ---
    probe_variant_by_orig = {}   # orig_tag -> outbound variant using custom_sni
    accepted_orig = {}
    if not force_sni and other_group:
        print(f"[*] Probing {len(other_group)} reality nodes with forced SNI '{custom_sni}'...")
        alt_outbounds = []
        alt_pairs = []     # (probe_tag, orig_tag)
        base_idx = len(outbounds) + 10
        for i, (_t, link, _d) in enumerate(other_group):
            r = build_outbound(link, custom_sni, base_idx + i, force_sni=True)
            if r:
                ft, fo, _flink, _s = r
                alt_outbounds.append(fo)
                alt_pairs.append((ft, _t))
                probe_variant_by_orig[_t] = fo
        ab_total = (len(alt_outbounds) + batch_size - 1) // batch_size
        for i in range(ab_total):
            chunk = alt_outbounds[i * batch_size:(i + 1) * batch_size]
            delays, _ = run_batch(singbox_path, chunk, 2000 + i, TEST_URL, timeout_ms)
            for pt in (t for t in delays if t != "direct"):
                orig = next((ot for ptag, ot in alt_pairs if ptag == pt), None)
                if orig:
                    accepted_orig[orig] = delays[pt]
        print(f"[+] Forced-SNI probe: {len(accepted_orig)}/{len(alt_outbounds)} "
              f"reality nodes also accept '{custom_sni}'.")
        for orig_tag in accepted_orig:
            sni_matched.append((tag_to_link[orig_tag], results[orig_tag]))

    # Forced-SNI variants for probe survivors (used by SNI-matched configs)
    matched_variant_outbounds = [
        probe_variant_by_orig[ot] for ot in accepted_orig if ot in probe_variant_by_orig
    ]

    if not results:
        if any_direct_fail:
            print(f"{Color.YELLOW}[!] Your internet connection was failing during the test "
                  f"(direct checks failed). Fix your connection and try again.{Color.RESET}")
        else:
            print(f"{Color.YELLOW}[!] Internet is fine but ALL nodes are dead. Free lists like this "
                  f"rotate every few minutes — just run again to fetch a fresh snapshot.{Color.RESET}")
        print("[-] No working nodes found. No files will be generated.")
        sys.exit(0)

    # --- Sort by latency ---
    working = sorted(((tag_to_link[tag], delay) for tag, delay in results.items()),
                     key=lambda x: x[1])

    txt_filename = "working_proxies.txt"
    sub_filename = "working_subscription.txt"

    # --- Append / Merge with previous local results ---
    if append_mode or merge_mode:
        existing = load_existing_nodes(txt_filename)
        existing_set = set(existing)
        new_links = [link for link, _ in working]
        added = [l for l in new_links if l not in existing_set]
        combined = existing + added
        print(f"[+] {'Merge' if merge_mode else 'Append'}: {len(combined)} total "
              f"({len(existing)} previous + {len(added)} new).")
        final_links = combined
    else:
        final_links = [link for link, _ in working]

    # --- Save ---
    with open(txt_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(final_links) + "\n")
    print(f"[+] Saved {len(final_links)} nodes to '{txt_filename}'.")

    sub_content = "\n".join(final_links)
    encoded_sub = base64.b64encode(sub_content.encode('utf-8')).decode('utf-8')
    with open(sub_filename, "w", encoding="utf-8") as f:
        f.write(encoded_sub)
    print(f"[+] Saved base64 subscription to '{sub_filename}'.")

    # --- Dedicated file: nodes confirmed working with the custom SNI ---
    try:
        sni_txt = "working_sni_matched.txt"
        sni_sub = "working_sni_matched_subscription.txt"
        sni_links_sorted = [link for link, _ in sorted(sni_matched, key=lambda x: x[1])]
        with open(sni_txt, "w", encoding="utf-8") as f:
            f.write("\n".join(sni_links_sorted) + "\n")
        enc2 = base64.b64encode("\n".join(sni_links_sorted).encode('utf-8')).decode('utf-8')
        with open(sni_sub, "w", encoding="utf-8") as f:
            f.write(enc2)
        print(f"[+] Saved {len(sni_links_sorted)} nodes verified on '{custom_sni}' "
              f"to '{sni_txt}' (+ base64).")
    except Exception as e:
        print(f"[-] Failed to write SNI-matched files: {e}")

    # --- Ready-to-import sing-box config with urltest auto-pick ---
    # Capture SNI-matched members BEFORE the tag-renaming mutation below.
    matched_custom_tags = {t for t in results if tag_to_sni_custom.get(t)}
    matched_custom_copies = [dict(o) for o in outbounds if o["tag"] in matched_custom_tags]
    try:
        sb_config_filename = "singbox_config.json"
        node_outbounds_live = [o for o in outbounds if o["tag"] in results]
        for o in node_outbounds_live:
            o["tag"] = "p" + o["tag"][1:]  # fresh tags to avoid clashes
        sb_cfg = {
            "log": {"level": "warn"},
            "experimental": {
                "clash_api": {"external_controller": "127.0.0.1:9090", "default_mode": "rule"}
            },
            "inbounds": [{
                "type": "mixed", "tag": "in",
                "listen": "127.0.0.1", "listen_port": 2080
            }],
            "outbounds": (
                [{
                    "type": "urltest", "tag": "auto",
                    "outbounds": [o["tag"] for o in node_outbounds_live],
                    "url": TEST_URL, "interval": "10m", "tolerance": 50
                }] +
                node_outbounds_live +
                [{"type": "direct", "tag": "direct"}]
            ),
            "route": {"rules": [], "final": "auto"},
        }
        with open(sb_config_filename, "w", encoding="utf-8") as f:
            json.dump(sb_cfg, f, indent=2, ensure_ascii=False)
        print(f"[+] Saved ready-to-import sing-box config to '{sb_config_filename}' "
              f"({len(node_outbounds_live)} nodes, urltest auto-select).")
    except Exception as e:
        print(f"[-] Failed to write singbox_config.json: {e}")

    # --- Ready-to-import Clash/Mihomo config (same nodes, reality/flow/grpc preserved) ---
    try:
        clash_filename = "clash_config.yaml"
        # node_outbounds_live already filtered by results (tags renamed but content intact)
        clash_cfg = build_clash_config(node_outbounds_live)
        # JSON is valid YAML — no external yaml dependency needed
        with open(clash_filename, "w", encoding="utf-8") as f:
            json.dump(clash_cfg, f, indent=2, ensure_ascii=False)
        n_proxies = len(clash_cfg.get("proxies", []))
        print(f"[+] Saved ready-to-import Clash config to '{clash_filename}' "
              f"({n_proxies} proxies, PROXY select + Auto-Best-Ping urltest).")
    except Exception as e:
        print(f"[-] Failed to write clash_config.yaml: {e}")

    # --- SNI-matched-only configs (nodes verified on the custom SNI) ---
    try:
        sbm_filename = "singbox_sni_matched.json"
        ccm_filename = "clash_sni_matched.yaml"
        mb = [dict(o) for o in matched_custom_copies]
        mb += [dict(o) for o in matched_variant_outbounds]
        if not mb:
            print(f"[-] No nodes matched custom SNI — skipping {sbm_filename}/{ccm_filename}.")
        else:
            for i, o in enumerate(mb):
                o["tag"] = f"m{i}"
            sbm_cfg = {
                "log": {"level": "warn"},
                "experimental": {
                    "clash_api": {"external_controller": "127.0.0.1:9090", "default_mode": "rule"}
                },
                "inbounds": [{
                    "type": "mixed", "tag": "in",
                    "listen": "127.0.0.1", "listen_port": 2080
                }],
                "outbounds": (
                    [{
                        "type": "urltest", "tag": "auto",
                        "outbounds": [o["tag"] for o in mb],
                        "url": TEST_URL, "interval": "10m", "tolerance": 50
                    }] +
                    mb +
                    [{"type": "direct", "tag": "direct"}]
                ),
                "route": {"rules": [], "final": "auto"},
            }
            with open(sbm_filename, "w", encoding="utf-8") as f:
                json.dump(sbm_cfg, f, indent=2, ensure_ascii=False)
            clash_m = build_clash_config([dict(o) for o in mb])
            with open(ccm_filename, "w", encoding="utf-8") as f:
                json.dump(clash_m, f, indent=2, ensure_ascii=False)
            print(f"[+] Saved SNI-matched configs: '{sbm_filename}' + '{ccm_filename}' "
                  f"({len(mb)} nodes on '{custom_sni}').")
    except Exception as e:
        print(f"[-] Failed to write SNI-matched configs: {e}")

    # --- Upload ---
    if not no_upload and GITHUB_TOKEN and GITHUB_REPO:
        print("\n[*] Uploading results to GitHub...")
        try:
            with open(txt_filename, "rb") as f:
                txt_content = f.read()
            with open(sub_filename, "rb") as f:
                sub_content = f.read()
            upload_entries = [
                (txt_filename, txt_content, f"Update working proxies (found {len(results)} live / {len(final_links)} total)"),
                (sub_filename, sub_content, f"Update working subscription (found {len(results)} live / {len(final_links)} total)"),
                (sni_txt, open(sni_txt, "rb").read(), f"Update SNI-matched proxies ({len(sni_matched)} nodes on {custom_sni})"),
                (sni_sub, open(sni_sub, "rb").read(), f"Update SNI-matched subscription ({len(sni_matched)} nodes on {custom_sni})"),
            ]
            for extra_name, extra_msg in [
                ("singbox_config.json", f"Update sing-box ready config ({len(results)} working nodes)"),
                ("clash_config.yaml", f"Update Clash ready config ({len(results)} working nodes)"),
                ("singbox_sni_matched.json", f"Update sing-box config ({custom_sni} SNI nodes only)"),
                ("clash_sni_matched.yaml", f"Update Clash config ({custom_sni} SNI nodes only)"),
            ]:
                if os.path.exists(extra_name):
                    with open(extra_name, "rb") as f:
                        upload_entries.append((extra_name, f.read(), extra_msg))
            upload_files_with_retry(upload_entries, retry_upload)
        except Exception as e:
            print(f"[-] Error reading files for GitHub upload: {e}")
    elif no_upload:
        print("[-] GitHub upload skipped (--no-upload).")

    # Top 5 preview
    print("\n[Top 5 fastest nodes]")
    for link, delay in working[:5]:
        print(f"  {Color.GREEN}{delay:>5}ms{Color.RESET}  {link[:80]}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        # Skip the interactive pause in CI environments or non-interactive shells
        try:
            is_interactive = sys.stdin.isatty() and os.environ.get("CI") != "true"
        except Exception:
            is_interactive = False
        if is_interactive:
            try:
                input("\n[Press Enter to exit...]")
            except Exception:
                pass
