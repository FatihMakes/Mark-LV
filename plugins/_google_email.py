"""
Gmail API client wrapper for Email Intelligence Desk.
Fetches message summaries, unread counts, searches threads, and creates drafts safely.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from plugins._google_auth import GoogleAuthManager, SCOPES_EMAIL


class GoogleEmailClient:
    """Safely executes read, search, and draft operations on user's Gmail account."""

    @classmethod
    def get_service(cls, client_id: str, client_secret: str) -> Tuple[Optional[Any], str]:
        creds, msg = GoogleAuthManager.get_credentials("email", client_id, client_secret, SCOPES_EMAIL)
        if not creds:
            return None, msg

        try:
            from googleapiclient.discovery import build
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return service, "Connected"
        except Exception as exc:
            return None, f"Failed to build Gmail service: {exc}"

    @classmethod
    def get_unread_summary(cls, service: Any, max_results: int = 5) -> Dict[str, Any]:
        """Fetch unread count and latest unread message snippets."""
        try:
            # Query unread messages in INBOX
            res = service.users().messages().list(userId="me", q="is:unread label:INBOX", maxResults=max_results).execute()
            messages = res.get("messages", [])
            total_estimate = res.get("resultSizeEstimate", len(messages))

            summaries = []
            for msg_meta in messages:
                m_id = msg_meta["id"]
                full_msg = service.users().messages().get(userId="me", id=m_id, format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in full_msg.get("payload", {}).get("headers", [])}
                summaries.append({
                    "id": m_id,
                    "from": headers.get("From", "Unknown"),
                    "subject": headers.get("Subject", "(No Subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": full_msg.get("snippet", "")
                })

            return {
                "unread_count": total_estimate,
                "latest_emails": summaries,
                "status": "OK"
            }
        except Exception as exc:
            return {"unread_count": 0, "latest_emails": [], "status": f"Error: {exc}"}

    @classmethod
    def search_emails(cls, service: Any, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """Search emails by sender, subject, or keywords."""
        try:
            res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = res.get("messages", [])
            out = []
            for m in messages:
                full_msg = service.users().messages().get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in full_msg.get("payload", {}).get("headers", [])}
                out.append({
                    "id": m["id"],
                    "from": headers.get("From", "Unknown"),
                    "subject": headers.get("Subject", "(No Subject)"),
                    "date": headers.get("Date", ""),
                    "snippet": full_msg.get("snippet", "")
                })
            return out
        except Exception:
            return []

    @classmethod
    def create_draft(cls, service: Any, to_addr: str, subject: str, body_text: str) -> Tuple[bool, str]:
        """Create a draft email without sending it."""
        try:
            msg = MIMEText(body_text)
            msg["to"] = to_addr
            msg["subject"] = subject
            raw_encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()

            draft_body = {"message": {"raw": raw_encoded}}
            res = service.users().drafts().create(userId="me", body=draft_body).execute()
            draft_id = res.get("id", "Unknown")
            return True, f"Draft created successfully (ID: {draft_id})."
        except Exception as exc:
            return False, f"Failed to create draft: {exc}"
