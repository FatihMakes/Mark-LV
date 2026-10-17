"""
Cannabis (Cannanas) plugin for Mark-LV (JARVIS).

Drop into Mark-LV/plugins/ as cannabis_status.py.
Routes queries to Cannanas MCP server or HA entities.
"""

PLUGIN = {
    "name": "cannabis_status",
    "description": (
        "Abfrage von Cannanas-Pflanzendaten. "
        "Nutze es für: 'Wie geht es den Pflanzen?', 'Status vom Phenom Hand', "
        "'Wie viele Strains haben wir?', 'Batch X läuft'. "
        "Nicht nutzen für Haussensoren – dafür nutze 'home_assistant'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Was soll abgefragt werden? Z.B. 'alle Pflanzen', 'Phenom Hand Sorten', 'Batch Status', 'Wachstumsstadien'"
            }
        },
        "required": ["query"]
    },
}

import os
import subprocess
import json

CANNABAS_MCP_PATH = "/home/lacasa15/services/cannanas-mcp/"


def _run_cannabis_cli(args: list) -> str:
    """Run a Cannanas CLI command and return JSON result."""
    try:
        result = subprocess.run(
            ["python3", "-m", "cannanas"] + args,
            cwd=CANNABAS_MCP_PATH,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return f"Cannanas CLI error: {result.stderr}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "Cannanas CLI nicht gefunden. Pfad prüfen: " + CANNABAS_MCP_PATH
    except subprocess.TimeoutExpired:
        return "Cannanas API-Zeitüberschreitung (15s)"
    except Exception as e:
        return f"Cannanas-Fehler: {e}"


def _search_plants(query: str) -> str:
    """Search plants by name or keyword using Cannanas find_plants."""
    return _run_cannabis_cli(["plants", "find", "--query", query])


def _list_strains() -> str:
    """List all strains from Cannanas."""
    return _run_cannabis_cli(["strains", "list"])


def _get_overview() -> str:
    """Get Cannanas overview (rooms, plants, batches)."""
    return _run_cannanas_cli(["overview"])


def _run_cannanas_cli(args: list) -> str:
    """Run Cannanas MCP tool via subprocess."""
    try:
        result = subprocess.run(
            ["python3", "-m", "cannanas"] + args,
            cwd="/home/lacasa15/services/cannanas-mcp/",
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return f"Cannanas CLI error: {result.stderr}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "Cannanas CLI nicht gefunden."
    except subprocess.TimeoutExpired:
        return "Cannanas API-Zeitüberschreitung (15s)"
    except Exception as e:
        return f"Cannanas-Fehler: {e}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    query = parameters.get("query", "").lower()

    try:
        if any(kw in query for kw in ["alle pflanzen", "pflanzen", "list"]):
            return _run_cannabis_cli(["plants", "list"])

        elif any(kw in query for kw in ["phenom", "hand", "sorten", "strains"]):
            result = _run_cannabis_cli(["strains", "list"])
            if "Phenom" in result:
                return f"Phenom Hand Sammlung:\n{result}"
            return result

        elif any(kw in query for kw in ["batch", "batches", "lauf"]):
            return _run_cannabis_cli(["batches", "list"])

        elif any(kw in query for kw in ["status", "wachstum", "gesundheit"]):
            return _get_overview()

        elif query:
            return _search_plants(query)

        else:
            return "Was möchtest du über die Pflanzen wissen? Z.B. 'alle Pflanzen', 'Phenom Hand', 'Batch Status'"

    except Exception as e:
        return f"Cannanas-Plugin Fehler: {e}"
