import json
import sys
import threading
import unittest
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
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
                    self.assertIn("ONU State", json.load(response)["error"])
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
