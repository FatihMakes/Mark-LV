"""
Network diagnostic, latency measurement, adapter inspection, and speed testing utilities
for Network Intelligence Center. Strictly non-intrusive and never logs private browsing.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import urllib.request
import psutil
from typing import Any, Dict, List, Optional, Tuple


class NetworkUtils:
    """Non-intrusive network health checking, DNS verification, and adapter status."""

    @classmethod
    def get_adapter_summary(cls) -> List[Dict[str, Any]]:
        """Return status and IP addresses of active network interfaces."""
        adapters: List[Dict[str, Any]] = []
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()

        for name, stat in stats.items():
            if not stat.isup:
                continue
            # Ignore purely virtual or loopback interfaces if physical exist
            ip_v4 = None
            for a in addrs.get(name, []):
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    ip_v4 = a.address
                    break

            if ip_v4:
                adapters.append({
                    "name": name,
                    "ip": ip_v4,
                    "speed_mbps": stat.speed,
                    "is_up": stat.isup,
                })
        return adapters

    @classmethod
    def check_internet_connectivity(cls, host: str = "1.1.1.1", port: int = 53, timeout: float = 2.0) -> Tuple[bool, float]:
        """Verify reachability to external root resolver and measure TCP handshake latency."""
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latency_ms = (time.perf_counter() - start) * 1000.0
                return True, round(latency_ms, 1)
        except (socket.timeout, OSError):
            return False, -1.0

    @classmethod
    def test_dns_resolution(cls, domain: str = "google.com") -> Tuple[bool, str, float]:
        """Test standard DNS resolution time."""
        start = time.perf_counter()
        try:
            ip = socket.gethostbyname(domain)
            latency_ms = (time.perf_counter() - start) * 1000.0
            return True, ip, round(latency_ms, 1)
        except socket.gaierror as exc:
            return False, str(exc), -1.0

    @classmethod
    def get_io_counters(cls) -> Dict[str, Any]:
        """Return total bytes sent and received since boot."""
        io = psutil.net_io_counters()
        return {
            "bytes_sent": io.bytes_sent,
            "bytes_recv": io.bytes_recv,
            "packets_sent": io.packets_sent,
            "packets_recv": io.packets_recv,
            "errin": io.errin,
            "errout": io.errout,
            "dropin": io.dropin,
            "dropout": io.dropout
        }

    @classmethod
    def get_public_ip(cls, timeout: float = 2.5) -> Optional[str]:
        """Retrieve public egress IP address safely from api.ipify.org."""
        try:
            req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "JARVIS-MarkLVII"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8").strip()
        except Exception:
            pass
        return None

    @classmethod
    def run_full_diagnostics(cls) -> Dict[str, Any]:
        """Perform comprehensive network health diagnostics."""
        adapters = cls.get_adapter_summary()
        has_adapter = len(adapters) > 0
        primary_adapter = adapters[0] if adapters else None

        # 1. External reachability (Cloudflare 1.1.1.1)
        connected_cf, ping_cf = cls.check_internet_connectivity("1.1.1.1", 53)
        # 2. External reachability (Google 8.8.8.8)
        connected_google, ping_google = cls.check_internet_connectivity("8.8.8.8", 53)
        # 3. DNS resolution
        dns_ok, resolved_ip, dns_latency = cls.test_dns_resolution("google.com")

        overall_status = "ONLINE" if (connected_cf or connected_google) and dns_ok else "DISCONNECTED"

        return {
            "status": overall_status,
            "adapters": adapters,
            "primary_ip": primary_adapter["ip"] if primary_adapter else "Unavailable",
            "primary_adapter_name": primary_adapter["name"] if primary_adapter else "None",
            "ping_ms": ping_cf if ping_cf > 0 else ping_google,
            "dns_ok": dns_ok,
            "dns_latency_ms": dns_latency,
            "timestamp": time.time()
        }
