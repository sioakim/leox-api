import json
import sys
import threading
import unittest
from datetime import datetime
from http.client import HTTPConnection, HTTPResponse
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


PON = """<html><table class='status'>
<tr><th width=40%>Temperature</th><td width=60%>61.828125 C</td></tr>
<tr><th width=40%>Voltage</th><td width=60%>3.335900 V</td></tr>
<tr><th width=40%>Tx Power</th><td width=60%>5.968059  dBm</td>
<tr><th width=40%>Rx Power</th><td width=60%>-17.258418  dBm</td></tr>
<tr><th width=40%>Bias Current</th><td width=60%>44.660000 mA</td></tr>
<tr><th width=40%>ONU State</th><td width=60%>O5</td></tr>
</table></html>"""
DEVICE = """<table>
<tr><th width=40%>Device Name</th><td width=60%>LXE-010X-A</td></tr>
<tr><th width=40%>Uptime</th><td width=60%>33 min</td></tr>
<tr><th width=40%>Firmware Version</th><td width=60%>V4.2.4L6a3</td></tr>
<tr><th width=40%>CPU Usage</th><td width=60%><div class="progress"><div class="progress-bar" aria-valuenow="2"><font>2%</font></div></div></td></tr>
<tr><th width=40%>Memory Usage</th><td width=60%><div class="progress"><div class="progress-bar" aria-valuenow="20"><font>20%</font></div></div></td></tr>
</table>"""
LAN = "<table><tr><td>LAN1</td><td>Up, 10Gb, Full</td></tr></table>"


class ParserTests(unittest.TestCase):
    def assert_matches_schema(self, value, schema, spec):
        if "$ref" in schema:
            name = schema["$ref"].split("/")[-1]
            return self.assert_matches_schema(value, spec["components"]["schemas"][name], spec)
        types = schema.get("type")
        if types is not None:
            types = types if isinstance(types, list) else [types]
            matches = {
                "null": lambda: value is None,
                "object": lambda: isinstance(value, dict),
                "array": lambda: isinstance(value, list),
                "string": lambda: isinstance(value, str),
                "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
                "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
            }
            self.assertTrue(any(matches[kind]() for kind in types), (value, types))
        if "const" in schema:
            self.assertEqual(value, schema["const"])
        if "enum" in schema:
            self.assertIn(value, schema["enum"])
        if "minimum" in schema:
            self.assertGreaterEqual(value, schema["minimum"])
        if schema.get("format") == "date-time":
            self.assertIsNotNone(datetime.fromisoformat(value).tzinfo)
        if isinstance(value, dict):
            self.assertTrue(set(schema.get("required", [])).issubset(value))
            if schema.get("additionalProperties") is False:
                self.assertEqual(set(value), set(schema["properties"]))
            for key, field_value in value.items():
                if key in schema.get("properties", {}):
                    self.assert_matches_schema(field_value, schema["properties"][key], spec)
        elif isinstance(value, list):
            for item in value:
                self.assert_matches_schema(item, schema["items"], spec)

    def test_real_shaped_pages(self):
        pon = server.parse_pon(PON)
        self.assertEqual(pon["onu_state"], "O5")
        self.assertEqual(pon["voltage_mv"], 3336)
        self.assertEqual(pon["rx_power_dbm"], -17.258418)
        self.assertEqual(pon["tx_power_dbm"], 5.968059)
        self.assertEqual(server.parse_device(DEVICE)["software_version"], "V4.2.4L6a3")
        self.assertEqual(server.parse_system(DEVICE)["uptime_seconds"], 1980)
        self.assertEqual(server.parse_system(DEVICE)["cpu_usage"], 2)
        self.assertEqual(server.parse_system(DEVICE)["memory_usage"], 20)
        self.assertEqual(server.parse_lan(LAN)[0]["speed"], 10000)

    def test_status_schema(self):
        pages = {"/status.asp": DEVICE, "/status_pon.asp": PON,
                 "/lan_port_status.asp": LAN}
        with patch.object(server, "fetch", side_effect=pages.__getitem__):
            result = server.status()
        self.assertEqual(set(result), {"name", "ip", "model", "version", "uptime_seconds",
                                       "pon", "system", "lan", "generated"})
        self.assertEqual(result["lan"][0]["link"], "Up")
        self.assertEqual(result["ip"], "192.168.100.1")
        json.dumps(result)

    def test_no_signal_is_null(self):
        self.assertIsNone(server.parse_pon(PON.replace("5.968059  dBm", "No signal"))["tx_power_dbm"])

    def test_login_checksum_matches_device_javascript(self):
        pairs = [("challenge", ""), ("username", "test-user"), ("save", "Login"),
                 ("encodePassword", "ZXhhbXBsZQ=="), ("submit-url", "/admin/login.asp")]
        self.assertEqual(server.security_flag(pairs), "6158")
        pairs[1] = ("username", "a~b")
        pairs[3] = ("encodePassword", "ZXhhbXBsZQ==")
        self.assertEqual(server.security_flag(pairs), "6232")

    def test_zero_minute_uptime(self):
        self.assertEqual(server.uptime_seconds("0 min"), 0)

    def test_device_uptime_switches_to_hours_and_minutes(self):
        self.assertEqual(server.uptime_seconds("49 min"), 2940)
        self.assertEqual(server.uptime_seconds("1:04"), 3840)
        self.assertEqual(server.uptime_seconds("49:00"), 49 * 3600)
        self.assertEqual(server.uptime_seconds("2 days 03:04:05"), 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertEqual(server.uptime_seconds("1 day, 2:03"), 86400 + 2 * 3600 + 3 * 60)
        with self.assertRaises(server.ParseError):
            server.uptime_seconds("1:75")

    def test_proxy_login_502_triggers_retry(self):
        session = server.Session()
        login_page = '<TITLE>Login</TITLE><form action=/boaform/admin/formLogin name="cmlogin">'
        proxy_error = HTTPError("http://proxy/status_pon.asp", 502, "Bad Gateway", {},
                                BytesIO(b"<TITLE>Login</TITLE>You have not logined"))
        requests = [proxy_error, login_page, "<html>OK</html>", PON]
        with patch.object(server, "USERNAME", "example"), patch.object(server, "PASSWORD", "example"), \
             patch.object(session, "request", side_effect=requests) as request:
            self.assertEqual(session.fetch("/status_pon.asp"), PON)
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["/status_pon.asp", "/admin/login.asp", "/boaform/admin/formLogin",
                          "/status_pon.asp"])

    def test_parse_failure(self):
        with self.assertRaisesRegex(server.ParseError, "ONU State"):
            server.parse_pon("<table><tr><td>Temperature</td><td>42 C</td></tr></table>")
        with self.assertRaisesRegex(server.ParseError, "LAN1"):
            server.parse_lan("<html>Login</html>")

    def test_http_parse_failure_is_bad_gateway(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(server, "fetch", return_value="<html>Login</html>"):
                with self.assertRaises(HTTPError) as context:
                    urlopen(f"http://127.0.0.1:{httpd.server_port}/pon")
                with context.exception as response:
                    self.assertEqual(response.code, 502)
                    payload = json.load(response)
                    self.assertIn("ONU State", payload["error"])
                    spec = json.loads(server.OPENAPI_JSON)
                    schema = spec["components"]["responses"]["UpstreamError"]["content"]
                    self.assert_matches_schema(payload, schema["application/json"]["schema"], spec)
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()

    def test_documentation_routes_and_openapi_shapes(self):
        spec = json.loads(server.OPENAPI_JSON)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(server, "fetch", side_effect=AssertionError("docs contacted device")), \
                 patch.object(server, "OPENAPI_PATH", Path("/missing-openapi.json")):
                connection = HTTPConnection("127.0.0.1", httpd.server_port)
                try:
                    canonical_bodies = {}
                    for path, status, content_type in (
                        ("/", 302, None),
                        ("/docs", 200, "text/html"),
                        ("/openapi.json", 200, "application/json"),
                    ):
                        with self.subTest(path=path):
                            connection.request("GET", path)
                            response = connection.getresponse()
                            body = response.read()
                            self.assertEqual(response.status, status)
                            self.assertEqual(response.getheader("Cache-Control"), "no-store")
                            if content_type:
                                self.assertTrue(response.getheader("Content-Type").startswith(content_type))
                            if path == "/":
                                self.assertEqual(response.getheader("Location"), "docs")
                            elif path == "/docs":
                                canonical_bodies[path] = body
                                self.assertIn(b"url: 'openapi.json'", body)
                                self.assertIn(b"swagger-ui-dist@5.33.0", body)
                                self.assertEqual(body.count(b'integrity="sha384-'), 2)
                            else:
                                canonical_bodies[path] = body
                                self.assertEqual(json.loads(body), spec)
                    for alias, location, canonical, content_type in (
                        ("/docs/", "../docs", "/docs", "text/html"),
                        ("/docs/openapi.json", "../openapi.json", "/openapi.json",
                         "application/json"),
                    ):
                        with self.subTest(alias=alias):
                            connection.request("GET", alias)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 302)
                            self.assertEqual(response.getheader("Location"), location)
                            self.assertEqual(response.getheader("Cache-Control"), "no-store")
                            self.assertEqual(response.read(), b"")
                            followed_path = urljoin(alias, location)
                            self.assertEqual(followed_path, canonical)
                            connection.request("GET", followed_path)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.getheader("Cache-Control"), "no-store")
                            self.assertTrue(response.getheader("Content-Type").startswith(content_type))
                            self.assertEqual(response.read(), canonical_bodies[canonical])
                finally:
                    connection.close()
            self.assertEqual(spec["openapi"], "3.1.0")
            self.assertEqual(spec["servers"], [{"url": "./", "description":
                             "Same host and path prefix as this OpenAPI document"}])
            self.assertEqual(urljoin("https://example.test/api/", "docs"),
                             "https://example.test/api/docs")
            self.assertEqual(urljoin("https://example.test/api/docs", "openapi.json"),
                             "https://example.test/api/openapi.json")
            self.assertEqual(urljoin("https://example.test/api/docs/", "../docs"),
                             "https://example.test/api/docs")
            self.assertEqual(urljoin("https://example.test/api/docs/openapi.json",
                                          "../openapi.json"),
                             "https://example.test/api/openapi.json")
            self.assertEqual(urljoin("https://example.test/api/openapi.json",
                                          spec["servers"][0]["url"]), "https://example.test/api/")
            self.assertEqual(set(spec["paths"]),
                             {"/health", "/pon", "/device", "/lan", "/system/stats", "/status"})
            operation_ids = [operation["get"]["operationId"] for operation in spec["paths"].values()]
            self.assertEqual(len(operation_ids), len(set(operation_ids)))
            self.assertEqual(len(operation_ids), 6)
            self.assertEqual(spec["components"]["responses"]["NotFound"]["content"]
                             ["application/json"]["schema"]["$ref"], "#/components/schemas/Error")
            schemas = spec["components"]["schemas"]
            for name, expected in (
                ("Health", {"status"}),
                ("Pon", {"onu_state", "temperature_c", "voltage_mv", "tx_power_dbm",
                         "rx_power_dbm", "current_ma"}),
                ("Device", {"name", "model", "software_version"}),
                ("SystemStats", {"uptime_seconds", "cpu_usage", "memory_usage", "flash_used_percent"}),
                ("LanPort", {"name", "link", "speed", "duplex", "bytes_rx", "bytes_tx"}),
                ("Status", {"name", "ip", "model", "version", "uptime_seconds", "pon", "system",
                            "lan", "generated"}),
            ):
                with self.subTest(schema=name):
                    self.assertEqual(set(schemas[name]["required"]), expected)
                    self.assertEqual(set(schemas[name]["properties"]), expected)
            self.assertEqual(schemas["Pon"]["properties"]["voltage_mv"]["type"], ["integer", "null"])
            self.assertEqual(schemas["LanPort"]["properties"]["speed"]["type"], ["integer", "null"])
            self.assertEqual(schemas["Status"]["properties"]["generated"]["format"], "date-time")
            self.assertIn("COPY server.py openapi.json ./", Path(__file__).resolve().parents[1]
                          .joinpath("Dockerfile").read_text())
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()

    def test_documented_response_fields_match_http_payloads(self):
        pages = {"/status.asp": DEVICE, "/status_pon.asp": PON,
                 "/lan_port_status.asp": LAN}
        spec = json.loads(server.OPENAPI_JSON)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(server, "fetch", side_effect=pages.__getitem__):
                for path in spec["paths"]:
                    with self.subTest(path=path):
                        with urlopen(f"http://127.0.0.1:{httpd.server_port}{path}") as response:
                            self.assertEqual(response.headers["Cache-Control"], "no-store")
                            payload = json.load(response)
                        schema = spec["paths"][path]["get"]["responses"]["200"]["content"]
                        self.assert_matches_schema(payload, schema["application/json"]["schema"], spec)
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()

    def test_undocumented_path_matches_not_found_response(self):
        spec = json.loads(server.OPENAPI_JSON)
        schema = spec["components"]["responses"]["NotFound"]["content"]
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaises(HTTPError) as context:
                urlopen(f"http://127.0.0.1:{httpd.server_port}/missing")
            with context.exception as response:
                self.assertEqual(response.code, 404)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                payload = json.load(response)
            self.assert_matches_schema(payload, schema["application/json"]["schema"], spec)
            self.assertEqual(payload, {"error": "Not found"})
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()

    def test_upstream_http_protocol_failures_are_json_bad_gateway(self):
        class MemorySocket:
            def __init__(self, raw):
                self.raw = raw

            def makefile(self, *args):
                return BytesIO(self.raw)

        for raw, error in (
            (b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\nshort", "IncompleteRead"),
            (b"not an HTTP status line\r\n\r\n", "not an HTTP status line"),
        ):
            with self.subTest(error=error):
                def open_response(*args, **kwargs):
                    response = HTTPResponse(MemorySocket(raw))
                    response.begin()
                    return response

                handler = server.Handler.__new__(server.Handler)
                handler.path = "/status"
                handler.request_version = "HTTP/1.1"
                handler.requestline = "GET /status HTTP/1.1"
                handler.command = "GET"
                handler.wfile = BytesIO()
                session = server.Session()
                with patch.object(server, "SESSION", session), \
                     patch.object(session.opener, "open", side_effect=open_response), \
                     patch.object(handler, "log_request"):
                    handler.do_GET()
                response = HTTPResponse(MemorySocket(handler.wfile.getvalue()))
                response.begin()
                with response:
                    self.assertEqual(response.status, 502)
                    self.assertEqual(response.headers.get_content_type(), "application/json")
                    self.assertIn(error, json.load(response)["error"])

    def test_login_page_triggers_login_and_retry(self):
        session = server.Session()
        login_page = '<TITLE>Login</TITLE><form action=/boaform/admin/formLogin name="cmlogin">'
        requests = [login_page, login_page, "<html>OK</html>", PON]
        with patch.object(server, "USERNAME", "example"), patch.object(server, "PASSWORD", "example"), \
             patch.object(session, "request", side_effect=requests) as request:
            self.assertEqual(session.fetch("/status_pon.asp"), PON)
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["/status_pon.asp", "/admin/login.asp", "/boaform/admin/formLogin",
                          "/status_pon.asp"])

    def test_login_required_without_credentials(self):
        session = server.Session()
        with patch.object(server, "USERNAME", ""), patch.object(server, "PASSWORD", ""), \
             patch.object(session, "request", return_value="<TITLE>Login</TITLE>"):
            with self.assertRaisesRegex(server.ParseError, "LEOX_USERNAME"):
                session.fetch("/status.asp")

    def test_proxy_token_header_on_get_and_login_post(self):
        session = server.Session()
        with patch.object(server, "PROXY_TOKEN", "test-token"), \
             patch.object(session.opener, "open", side_effect=lambda *args, **kwargs: BytesIO(b"OK")) as open_request:
            session.request("/status.asp")
            session.request("/boaform/admin/formLogin", [("username", "example")])
        requests = [call.args[0] for call in open_request.call_args_list]
        self.assertEqual([request.get_header("X-leox-api-key") for request in requests],
                         ["test-token", "test-token"])
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST"])

    def test_proxy_token_header_is_optional(self):
        session = server.Session()
        with patch.object(server, "PROXY_TOKEN", ""), \
             patch.object(session.opener, "open", return_value=BytesIO(b"OK")) as open_request:
            session.request("/status.asp")
        self.assertIsNone(open_request.call_args.args[0].get_header("X-leox-api-key"))


if __name__ == "__main__":
    unittest.main()
