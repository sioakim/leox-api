"""Read-only HTTP adapter for the LEOX status pages."""

import base64
import http.client
import http.cookiejar
import json
import os
import re
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = os.environ.get("LEOX_BASE_URL", "http://10.0.0.1:8100").rstrip("/")
DEVICE_IP = os.environ.get("LEOX_DEVICE_IP", "192.168.100.1")
PORT = int(os.environ.get("PORT", "8000"))
TIMEOUT = float(os.environ.get("LEOX_TIMEOUT", "5"))
USERNAME = os.environ.get("LEOX_USERNAME", "")
PASSWORD = os.environ.get("LEOX_PASSWORD", "")
PROXY_TOKEN = os.environ.get("LEOX_PROXY_TOKEN", "")


class StatusTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self.row is not None and len(self.row) >= 2:
                self.rows.append(self.row)
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.row) >= 2:
                self.rows.append(self.row)
            self.row = None


class ParseError(ValueError):
    pass


def fields(html):
    table = StatusTable()
    table.feed(html)
    return {re.sub(r"[^a-z0-9]", "", row[0].lower()): row[1] for row in table.rows}


def required(data, label):
    value = data.get(re.sub(r"[^a-z0-9]", "", label.lower()))
    if not value:
        raise ParseError(f"Missing {label}")
    return value


def number(value):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def uptime_seconds(value):
    total = 0
    matched = False
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for count, unit in re.findall(r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[dhms])\b", value, re.I):
        matched = True
        total += int(count) * units[unit[0].lower()]
    # The device uses "49 min" below one hour, then H:MM (e.g. "1:04").
    clock = re.search(r"\b\d+:\d{2}(?::\d{2})?\b", value)
    parts = clock.group().split(":") if clock else []
    if len(parts) in (2, 3) and all(part.isdigit() for part in parts):
        hours, minutes = map(int, parts[:2])
        seconds = int(parts[2]) if len(parts) == 3 else 0
        if minutes >= 60 or seconds >= 60:
            raise ParseError(f"Unrecognized uptime: {value}")
        return total + hours * 3600 + minutes * 60 + seconds
    if matched:
        return total
    raise ParseError(f"Unrecognized uptime: {value}")


def parse_pon(html):
    data = fields(html)
    state = required(data, "ONU State")
    result = {"onu_state": state}
    for label, key in (("Temperature", "temperature_c"), ("Voltage", "voltage_mv"),
                       ("Tx Power", "tx_power_dbm"), ("Rx Power", "rx_power_dbm"),
                       ("Bias Current", "current_ma")):
        value = number(required(data, label))
        if key == "voltage_mv" and value is not None:
            raw = required(data, label).lower()
            value = round(value * 1000) if re.search(r"\bvolt|\bv\b", raw) and "mv" not in raw else round(value)
        result[key] = value
    return result


def parse_device(html):
    data = fields(html)
    name = required(data, "Device Name")
    return {"name": name, "model": name, "software_version": required(data, "Firmware Version")}


def parse_system(html):
    data = fields(html)
    return {
        "uptime_seconds": uptime_seconds(required(data, "Uptime")),
        "cpu_usage": number(required(data, "CPU Usage")),
        "memory_usage": number(required(data, "Memory Usage")),
        "flash_used_percent": None,
    }


def parse_lan(html):
    data = fields(html)
    value = required(data, "LAN1")
    parts = [part.strip() for part in value.split(",")]
    link = "Up" if parts[0].lower() == "up" else "Down" if parts[0].lower() == "down" else None
    if link is None:
        raise ParseError(f"Unrecognized LAN1 link: {value}")
    speed = None
    if len(parts) > 1:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(g|m)(?:b|bps|bit)?", parts[1], re.I)
        if match:
            speed = round(float(match.group(1)) * (1000 if match.group(2).lower() == "g" else 1))
    return [{"name": "LAN1", "link": link, "speed": speed,
             "duplex": parts[2] if len(parts) > 2 else None,
             "bytes_rx": None, "bytes_tx": None}]


def is_login_page(html):
    lower = html.lower()
    return ("<title>login</title>" in lower or "you have not logined" in lower
            or "name=\"cmlogin\"" in lower or "name='cmlogin'" in lower)


def security_flag(fields):
    """Compute the Realtek form checksum used by postTableEncrypt in common.js."""
    # JS postTableEncrypt explicitly encodes ~ after encodeURIComponent.
    encoded = urlencode(fields, safe="-._*").replace("~", "%7E") + "&"
    values = encoded.encode("ascii")
    checksum = 0
    for offset in range(0, len(values), 4):
        chunk = values[offset:offset + 4]
        word = int.from_bytes(chunk.ljust(4, b"\0"), "big", signed=True)
        checksum += word
    signed = ((checksum + 2**31) % 2**32) - 2**31
    checksum = ((checksum & 0xffff) + (signed >> 16)) & 0xffff
    return str((~checksum) & 0xffff)


class Session:
    def __init__(self):
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.lock = threading.RLock()

    def request(self, path, data=None):
        body = urlencode(data).encode() if data is not None else None
        request = Request(BASE_URL + path, data=body)
        if PROXY_TOKEN:
            request.add_header("X-Leox-API-Key", PROXY_TOKEN)
        with self.opener.open(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")

    def login(self):
        if not USERNAME or not PASSWORD:
            raise ParseError("LEOX login required; set LEOX_USERNAME and LEOX_PASSWORD")
        page = self.request("/admin/login.asp")
        if "formLogin" not in page:
            raise ParseError("LEOX login form unavailable")
        encoded_password = base64.b64encode(PASSWORD.encode()).decode()
        form = [("challenge", ""), ("username", USERNAME), ("save", "Login"),
                ("encodePassword", encoded_password), ("submit-url", "/admin/login.asp")]
        form.append(("postSecurityFlag", security_flag(form)))
        response = self.request("/boaform/admin/formLogin", form)
        if "you have logined" in response.lower():
            raise ParseError("LEOX already has another web session")

    def fetch(self, path):
        with self.lock:
            try:
                page = self.request(path)
            except HTTPError as exc:
                # The management proxy can turn a bare login response into 502.
                if exc.code not in (401, 403, 502):
                    raise
                exc.close()
                self.login()
                page = self.request(path)
            else:
                if not is_login_page(page):
                    return page
                self.login()
                page = self.request(path)
            if is_login_page(page):
                raise ParseError("LEOX login did not grant access")
            return page


SESSION = Session()


def fetch(path):
    return SESSION.fetch(path)


def status():
    device_page = fetch("/status.asp")
    device = parse_device(device_page)
    system = parse_system(device_page)
    return {"name": device["name"], "ip": DEVICE_IP, "model": device["model"],
            "version": device["software_version"], "uptime_seconds": system["uptime_seconds"],
            "pon": parse_pon(fetch("/status_pon.asp")), "system": system,
            "lan": parse_lan(fetch("/lan_port_status.asp")),
            "generated": datetime.now(timezone.utc).isoformat()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/health":
                data = {"status": "healthy"}
            elif path == "/pon":
                data = parse_pon(fetch("/status_pon.asp"))
            elif path in ("/device", "/system/stats"):
                page = fetch("/status.asp")
                data = parse_device(page) if path == "/device" else parse_system(page)
            elif path == "/lan":
                data = parse_lan(fetch("/lan_port_status.asp"))
            elif path == "/status":
                data = status()
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, data)
        except (OSError, http.client.HTTPException, ParseError, UnicodeError) as exc:
            self.respond(502, {"error": str(exc)})

    def respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
