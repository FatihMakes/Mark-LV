"""
JARVIS Email Intelligence Desk Plugin.
Fetches unread emails, summarizes threads, searches messages, and drafts replies via Gmail API.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from memory.config_manager import get_plugin_config, get_plugin_setting
from plugins._google_auth import GoogleAuthManager, SCOPES_EMAIL
from plugins._google_email import GoogleEmailClient
from plugins._plugin_common import (
    HudVisualGenerator,
    safe_player_content,
    safe_player_image,
    safe_player_log,
)


def _test_email_connection(values: dict) -> tuple[bool, str]:
    """Test action connected through PLUGIN_SETTINGS."""
    cid = values.get("client_id", "")
    sec = values.get("client_secret", "")
    service, msg = GoogleEmailClient.get_service(cid, sec)
    if service:
        return True, "Gmail API connection verified and active."
    return False, f"Connection failed: {msg}"


PLUGIN = {
    "name": "email_intelligence",
    "description": (
        "Check inbox, read unread emails, search email messages, and create reply drafts. "
        "Triggers: 'show my inbox', 'check unread emails', 'find emails from John', "
        "'draft an email', 'search my messages'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action: 'unread', 'inbox', 'search', 'draft', 'connect', or 'status'",
            },
            "query": {
                "type": "STRING",
                "description": "Search keyword, sender name, or subject filter",
            },
            "to_address": {
                "type": "STRING",
                "description": "Recipient email address for draft creation",
            },
            "subject": {
                "type": "STRING",
                "description": "Subject line for draft creation",
            },
            "body": {
                "type": "STRING",
                "description": "Message body for draft creation",
            },
        },
        "required": ["action"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "core",
}

PLUGIN_SETTINGS = {
    "namespace": "email_intelligence",
    "title": "EMAIL INTELLIGENCE DESK",
    "fields": [
        {
            "key": "client_id",
            "label": "Google OAuth Client ID",
            "type": "text",
            "default": "",
            "placeholder": "Enter client ID from Google Cloud Console",
        },
        {
            "key": "client_secret",
            "label": "Google OAuth Client Secret",
            "type": "password",
            "default": "",
            "placeholder": "Enter client secret",
        },
        {
            "key": "max_inbox_fetch",
            "label": "Max Unread Messages to Display",
            "type": "choice",
            "options": ["5", "10", "15"],
            "default": "5",
        },
    ],
    "action": {
        "label": "TEST CONNECTION",
        "run": _test_email_connection,
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "unread").lower().strip()
    query = str(parameters.get("query") or "").strip()
    to_addr = str(parameters.get("to_address") or "").strip()
    subject = str(parameters.get("subject") or "").strip()
    body = str(parameters.get("body") or "").strip()

    cid = get_plugin_setting("email_intelligence", "client_id", default="")
    sec = get_plugin_setting("email_intelligence", "client_secret", default="")

    try:
        # 1. Action: Connect OAuth
        if action in ("connect", "auth", "login"):
            ok, msg = GoogleAuthManager.run_local_auth_flow("email", cid, sec, SCOPES_EMAIL)
            return msg

        # Try to acquire service
        service, err_msg = GoogleEmailClient.get_service(cid, sec)

        # 2. Action: Create Draft
        if action in ("draft", "create_draft", "compose"):
            if not service:
                return f"Sir, Gmail is not connected: {err_msg}"
            if not to_addr or not subject:
                return "Sir, please specify recipient and subject for the draft."
            ok, res_msg = GoogleEmailClient.create_draft(service, to_addr, subject, body or "Draft message created by JARVIS.")
            safe_player_log(player, f"Email: {res_msg}")
            safe_player_content(player, "EMAIL DRAFT CREATED", f"To: {to_addr}\nSubject: {subject}\nStatus: {res_msg}")
            return f"Draft to {to_addr} created in Gmail. I will never send an email without your explicit review."

        # 3. Action: Search Emails
        if action in ("search", "find"):
            if not service:
                return f"Sir, Gmail is not connected: {err_msg}"
            results = GoogleEmailClient.search_emails(service, query=query or "is:inbox", max_results=8)
            if not results:
                return f"Sir, no emails matched your query: '{query}'."

            details = "\n\n".join([f"From: {r['from']}\nSubject: {r['subject']}\nSnippet: {r['snippet']}" for r in results[:5]])
            safe_player_content(player, f"EMAIL SEARCH ({len(results)})", details)
            return f"Sir, found {len(results)} emails matching '{query}'. Top message from {results[0]['from']}."

        # 4. Action: Unread / Inbox Summary (Default)
        if not service:
            metrics = [
                ("STATUS", "UNCONFIGURED", "amber"),
                ("UNREAD", "0", "cyan"),
                ("PROVIDER", "Gmail API", "cyan"),
            ]
            card_path = HudVisualGenerator.create_card_image(
                title="EMAIL INTELLIGENCE DESK",
                subtitle="Gmail Integration Status",
                metrics=metrics,
                table_rows=[["Setup Step", "Instructions"], ["OAuth Credentials", "Configure Client ID in ⚙ → Plugin Settings"]],
                footer="Awaiting Google Cloud Credentials",
                output_filename="email_status_card.png"
            )
            safe_player_image(player, card_path, "EMAIL INTELLIGENCE")
            safe_player_content(player, "EMAIL INTELLIGENCE", f"Gmail is not currently connected.\n\nTo view unread messages and compose drafts, configure your Google OAuth Client ID and Secret in ⚙ → PLUGIN SETTINGS.")
            return "Sir, your Gmail account is not yet connected. You can configure credentials under Plugin Settings."

        limit = int(get_plugin_setting("email_intelligence", "max_inbox_fetch", default="5"))
        summary = GoogleEmailClient.get_unread_summary(service, max_results=limit)
        count = summary["unread_count"]
        latest = summary["latest_emails"]

        metrics = [
            ("STATUS", "CONNECTED", "green"),
            ("UNREAD", f"{count} Messages", "amber" if count > 0 else "green"),
            ("PROVIDER", "Gmail", "cyan"),
        ]

        table_rows = [["From", "Subject", "Snippet"]]
        for em in latest:
            sender_clean = em["from"].split("<")[0].strip()[:18]
            table_rows.append([sender_clean, em["subject"][:22], em["snippet"][:30]])

        card_path = HudVisualGenerator.create_card_image(
            title="EMAIL INTELLIGENCE - INBOX",
            subtitle=f"{count} Unread Messages in Inbox",
            metrics=metrics,
            table_rows=table_rows,
            footer="Gmail sync active",
            output_filename="email_inbox_card.png"
        )
        safe_player_image(player, card_path, "EMAIL INTELLIGENCE")

        details = "\n\n".join([f"• From: {e['from']}\n  Subject: {e['subject']}\n  {e['snippet']}" for e in latest])
        safe_player_content(player, f"UNREAD INBOX ({count})", details or "No unread messages.")

        if count > 0:
            top_sender = latest[0]['from'].split('<')[0].strip() if latest else "Unknown"
            return f"Sir, you have {count} unread emails in your inbox. Latest is from {top_sender} regarding '{latest[0]['subject'] if latest else ''}'."
        return "Sir, your inbox is clean with zero unread emails."

    except Exception as exc:
        return f"Sir, Email Intelligence encountered an issue: {exc}"
