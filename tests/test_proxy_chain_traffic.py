# -*- coding: utf-8 -*-
import socket
import threading
import unittest
from unittest.mock import patch

from core.proxy_chain import ProxyChainRelay, open_proxy_pool_proxy
from core.browser_traffic import merge_proxy_transport_traffic


class ProxyChainTrafficTests(unittest.TestCase):
    def test_counting_relay_does_not_require_upstream_or_pysocks(self):
        with patch("core.proxy_chain.socks", None), patch("core.proxy_chain.socket.create_connection") as connect:
            relay = ProxyChainRelay("http://127.0.0.1:8888")
            relay._connect_target_through_upstream("127.0.0.1", 8888)
        connect.assert_called_once_with(("127.0.0.1", 8888), timeout=15.0)

    def test_pool_counting_is_opt_in_and_preserves_target(self):
        with patch("config.proxy.PROXY_POOL_UPSTREAM_PROXY", ""), patch("core.proxy_chain.ProxyChainRelay") as factory:
            self.assertEqual(open_proxy_pool_proxy("http://proxy.test:8888"), ("http://proxy.test:8888", None))
            factory.assert_not_called()
            relay = factory.return_value.start.return_value
            relay.proxy_url = "http://127.0.0.1:12345"
            self.assertEqual(open_proxy_pool_proxy("http://proxy.test:8888", measure_transport=True), (relay.proxy_url, relay))
            factory.assert_called_once_with("http://proxy.test:8888", "", timeout=15.0)

    def test_transport_replaces_total_without_adding_page_bytes_twice(self):
        observed = {"upload_bytes": 1, "download_bytes": 9, "total_bytes": 10, "http_download_bytes": 9}
        transport = {"available": True, "upload_bytes": 20, "download_bytes": 180, "total_bytes": 200}
        result = merge_proxy_transport_traffic(observed, transport, label="Cloak")
        self.assertEqual(result["total_bytes"], 200)
        self.assertEqual(result["browser_observed_total_bytes"], 10)
        self.assertEqual(result["http_download_bytes"], 9)
        self.assertEqual(merge_proxy_transport_traffic(result, transport, label="Cloak"), result)
        self.assertEqual(observed["total_bytes"], 10)
        self.assertIs(merge_proxy_transport_traffic(observed, None, label="Roxy"), observed)

    def test_plain_http_keeps_body_and_counts_wire_bytes(self):
        relay = ProxyChainRelay("socks5://127.0.0.1:8888")
        browser, client = socket.socketpair()
        remote, server = socket.socketpair()
        server.settimeout(2)
        browser.settimeout(2)
        request = b"POST http://example.test/test?q=1 HTTP/1.1\r\nHost: example.test\r\nContent-Length: 4\r\nProxy-Authorization: secret\r\n\r\nbody"
        expected = b"POST /test?q=1 HTTP/1.1\r\nHost: example.test\r\nContent-Length: 4\r\n\r\nbody"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
        with patch.object(relay, "_connect_target_through_upstream", return_value=remote), patch.object(relay, "_target_socks_connect") as tunnel:
            thread = threading.Thread(target=relay._handle_client, args=(client,))
            thread.start()
            try:
                browser.sendall(request)
                self.assertEqual(server.recv(4096), expected)
                server.sendall(response)
                self.assertEqual(browser.recv(4096), response)
                tunnel.assert_called_once_with(remote, "example.test", 80)
            finally:
                relay._stop.set()
                browser.close()
                thread.join(timeout=3)
                server.close()
        self.assertEqual(relay.traffic_snapshot()["total_bytes"], len(expected) + len(response))

    def test_relay_counts_both_directions(self):
        relay = object.__new__(ProxyChainRelay)
        relay._stop = threading.Event()
        relay._traffic_lock = threading.Lock()
        relay._upload_bytes = 0
        relay._download_bytes = 0

        browser, relay_client = socket.socketpair()
        relay_remote, upstream = socket.socketpair()
        thread = threading.Thread(target=relay._relay, args=(relay_client, relay_remote))
        thread.start()
        try:
            browser.sendall(b"upload")
            self.assertEqual(upstream.recv(6), b"upload")
            upstream.sendall(b"download")
            self.assertEqual(browser.recv(8), b"download")
        finally:
            relay._stop.set()
            browser.close()
            upstream.close()
            thread.join(timeout=2)
            relay_client.close()
            relay_remote.close()

        self.assertEqual(relay.traffic_snapshot(), {
            "available": True,
            "upload_bytes": 6,
            "download_bytes": 8,
            "total_bytes": 14,
        })


if __name__ == "__main__":
    unittest.main()
