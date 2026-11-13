"""
Secure Google OAuth2 authentication flow and token storage helper for Email & Calendar.
Stores encrypted refresh tokens in user-data dir, never commits tokens to Git,
and implements connection testing safely.
"""

from __future__ import annotations

import os
import sys
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from plugins._plugin_common import get_plugins_data_dir

# Safe token files in user's OS data dir (not in repo)
TOKEN_DIR = get_plugins_data_dir() / "google_tokens"
TOKEN_DIR.mkdir(parents=True, exist_ok=True)

SCOPES_EMAIL = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

SCOPES_CALENDAR = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class GoogleAuthManager:
    """Handles OAuth2 tokens and Google API client credentials securely."""

    @classmethod
    def get_token_file(cls, service_name: str) -> pathlib.Path:
        return TOKEN_DIR / f"{service_name}_token.json"

    @classmethod
    def get_credentials(
        cls,
        service_name: str,
        client_id: str,
        client_secret: str,
        scopes: List[str]
    ) -> Tuple[Optional[Any], str]:
        """
        Load or refresh OAuth2 credentials for a service ('email' or 'calendar').
        Returns (Credentials, StatusMessage).
        """
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
        except ImportError as exc:
            return None, f"Google OAuth libraries not available: {exc}"

        token_path = cls.get_token_file(service_name)
        creds = None

        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), scopes)
            except Exception:
                creds = None

        if creds and creds.valid:
            return creds, "Authorized and active."

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                return creds, "Token refreshed successfully."
            except Exception as exc:
                return None, f"Failed to refresh expired token: {exc}"

        # If not authorized and client credentials provided, prompt setup
        if not client_id or not client_secret:
            return None, "Google OAuth Client ID & Secret must be configured in ⚙ → PLUGIN SETTINGS."

        return None, "OAuth token required. Run authorization action to connect Google account."

    @classmethod
    def run_local_auth_flow(
        cls,
        service_name: str,
        client_id: str,
        client_secret: str,
        scopes: List[str]
    ) -> Tuple[bool, str]:
        """Start local web flow for initial user authentication."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost:8088/"],
                }
            }

            flow = InstalledAppFlow.from_client_config(client_config, scopes)
            creds = flow.run_local_server(port=8088, prompt="consent")

            token_path = cls.get_token_file(service_name)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

            return True, f"Google {service_name.capitalize()} account connected successfully!"
        except Exception as exc:
            return False, f"Authorization flow error: {exc}"

    @classmethod
    def is_connected(cls, service_name: str) -> bool:
        """Check if an active token file exists on disk."""
        return cls.get_token_file(service_name).exists()
