"""
JARVIS Network Intelligence Center Plugin.
Monitors active adapters, internet connectivity, DNS latency, egress public IP, and network troubleshooting.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._network_utils import NetworkUtils
from plugins._plugin_common import (
    HudVisualGenerator,
    format_bytes,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)

PLUGIN = {
    "name": "network_intelligence",
    "description": (
        "Monitor internet connectivity, network adapters, DNS latency, IP addresses, and troubleshoot network. "
        "Triggers: 'check my network', 'is internet working', 'test ping', 'show my IP address', "
        "'run network diagnostics', 'why is internet slow'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'status', 'ping', 'dns', 'public_ip', 'diagnostics', or 'adapters'",
            },
            "host": {
                "type": "STRING",
                "description": "Optional host/domain to test ping or DNS (defaults to 1.1.1.1 or google.com)",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "network_intelligence",
    "title": "NETWORK INTELLIGENCE CENTER",
    "fields": [
        {
            "key": "ping_target",
            "label": "Primary Ping Target Host",
            "type": "text",
            "default": "1.1.1.1",
            "placeholder": "1.1.1.1 or 8.8.8.8",
        },
        {
            "key": "show_public_ip",
            "label": "Enable Public Egress IP Lookup",
            "type": "toggle",
            "default": True,
        },
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "status").lower().strip()
    custom_host = str(parameters.get("host") or "").strip()

    try:
        # 1. Action: Public IP Lookup
        if action in ("public_ip", "my_ip", "ip"):
            pub_ip = NetworkUtils.get_public_ip()
            if pub_ip:
                safe_player_content(player, "PUBLIC IP ADDRESS", f"Public IP: {pub_ip}\nProvider: api.ipify.org (SSL)")
                return f"Sir, your external public IP address is {pub_ip}."
            return "Sir, unable to resolve public IP address at this time."

        # 2. Action: DNS Test
        if action in ("dns", "resolve"):
            domain = custom_host or "google.com"
            ok, ip_res, lat_ms = NetworkUtils.test_dns_resolution(domain)
            if ok:
                safe_player_content(player, "DNS RESOLUTION", f"Domain: {domain}\nResolved IP: {ip_res}\nLatency: {lat_ms} ms\nStatus: OK")
                return f"DNS resolution for {domain} succeeded in {lat_ms} milliseconds (IP: {ip_res})."
            return f"Sir, DNS resolution for {domain} failed: {ip_res}."

        # 3. Action: Full Network Diagnostics (Default for 'diagnostics' or general status)
        diag = NetworkUtils.run_full_diagnostics()
        io_stats = NetworkUtils.get_io_counters()

        # Try to get public IP if enabled in settings
        pub_ip = NetworkUtils.get_public_ip() if get_plugin_setting("network_intelligence", "show_public_ip", default=True) else "Hidden"

        status_col = "green" if diag["status"] == "ONLINE" else "red"
        metrics = [
            ("STATUS", diag["status"], status_col),
            ("LATENCY", f"{diag['ping_ms']} ms" if diag['ping_ms'] > 0 else "Timeout", "cyan"),
            ("DNS TIME", f"{diag['dns_latency_ms']} ms" if diag['dns_ok'] else "Failed", "green" if diag['dns_ok'] else "red"),
            ("LOCAL IP", diag["primary_ip"], "amber"),
        ]

        table_rows = [
            ["Metric / Interface", "Value"],
            ["Primary Adapter", f"{diag['primary_adapter_name']} ({diag['primary_ip']})"],
            ["Public IP", str(pub_ip or 'Unreachable')],
            ["Total Sent / Recv", f"{format_bytes(io_stats['bytes_sent'])} / {format_bytes(io_stats['bytes_recv'])}"],
            ["DNS Status", f"Resolved ({diag['dns_latency_ms']} ms)" if diag['dns_ok'] else "Failed"],
        ]

        card_path = HudVisualGenerator.create_card_image(
            title="NETWORK INTELLIGENCE CENTER",
            subtitle=f"Adapter: {diag['primary_adapter_name']}",
            metrics=metrics,
            table_rows=table_rows,
            footer=f"Network Status: {diag['status']}",
            output_filename="network_status_card.png"
        )

        safe_player_image(player, card_path, "NETWORK INTELLIGENCE")
        details = (
            f"Network Health: {diag['status']}\n"
            f"Interface: {diag['primary_adapter_name']} [{diag['primary_ip']}]\n"
            f"Public IP: {pub_ip}\n"
            f"Cloudflare Handshake: {diag['ping_ms']} ms\n"
            f"DNS Lookup: {'OK' if diag['dns_ok'] else 'Error'} ({diag['dns_latency_ms']} ms)\n"
            f"Total Traffic: ↑ {format_bytes(io_stats['bytes_sent'])}  ↓ {format_bytes(io_stats['bytes_recv'])}"
        )
        safe_player_content(player, "NETWORK DIAGNOSTICS", details)
        safe_player_log(player, f"Network check complete: {diag['status']} ({diag['ping_ms']} ms).")

        if diag["status"] == "ONLINE":
            return f"Sir, internet connection is active with {diag['ping_ms']} ms ping on {diag['primary_adapter_name']}."
        else:
            return f"Sir, network diagnostics report that your internet connection is currently offline or unreachable."

    except Exception as exc:
        return f"Sir, Network Intelligence encountered an issue: {exc}"
