"""
AMD Competency Selector – Python Flask Server
=============================================
Production-ready backend:
  • GET  /             → serves index.html
  • POST /api/ai       → proxies to internal AI endpoint (credentials stay server-side)
  • POST /api/fetch-ticket → fetches SCET ticket via JIRA API or SharePoint (Graph API)
  • POST /api/feedback → appends user correction to feedback_logs.json

Deployment host: 10.95.37.121
User access URL: http://10.95.37.121:5000

Run (development):
    python server.py

Run (production - Windows, requires waitress):
    waitress-serve --port=5000 --host=0.0.0.0 server:app
"""

import base64
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse as urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

# -- Load .env ----------------------------------------------------------------
load_dotenv()

# -- App ----------------------------------------------------------------------
app = Flask(__name__, static_folder=".")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# -- Config -------------------------------------------------------------------
AI_ENDPOINT    = os.getenv("AI_ENDPOINT", "")
SUB_KEY        = os.getenv("SUB_KEY", "")
API_USER       = os.getenv("API_USER", "competency-selector-user")
API_VERSION    = os.getenv("API_VERSION", "")
LLM_MODEL      = os.getenv("LLM_MODEL", "")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))
AI_TIMEOUT     = int(os.getenv("AI_TIMEOUT", "90"))
FEEDBACK_LOG   = Path(os.getenv("FEEDBACK_LOG", "feedback_logs.json"))
HOST           = os.getenv("HOST", "0.0.0.0")
PORT           = int(os.getenv("PORT", 5000))
DEPLOY_HOST    = os.getenv("DEPLOY_HOST", "10.95.37.121")

# ── JIRA REST API ─────────────────────────────────────────────────────────────
# Fetch-ticket priority:
#   Method 1: Bearer PAT  (JIRA_TOKEN)
#   Method 2: Service Account Basic Auth  (JIRA_SERVICE_ACCOUNT_ID + PASSWORD)
#   Method 3: SharePoint Graph API  (Azure CLI / SHAREPOINT_ACCESS_TOKEN)
JIRA_BASE_URL                 = os.getenv("JIRA_BASE_URL", "https://ontrack.amd.com")
JIRA_TOKEN                    = os.getenv("JIRA_TOKEN", "")                    # Bearer PAT (personal)
JIRA_SERVICE_ACCOUNT_ID       = os.getenv("JIRA_SERVICE_ACCOUNT_ID", "")      # Service account ID
JIRA_SERVICE_ACCOUNT_PASSWORD = os.getenv("JIRA_SERVICE_ACCOUNT_PASSWORD", "") # Service account password
JIRA_TIMEOUT                  = int(os.getenv("JIRA_TIMEOUT", "30"))

# ── SharePoint via Microsoft Graph (fallback) ──────────────────────────────────
# Token priority: SHAREPOINT_ACCESS_TOKEN env → az account get-access-token (Azure CLI)
SHAREPOINT_HOST_NAME     = os.getenv("SHAREPOINT_HOST_NAME", "amdcloud.sharepoint.com")
SHAREPOINT_SITE_PATH     = os.getenv("SHAREPOINT_SITE_PATH", "/sites/SCPI")
SHAREPOINT_DRIVE_NAME    = os.getenv("SHAREPOINT_DRIVE_NAME", "Documents")
SHAREPOINT_REMOTE_FOLDER = os.getenv("SHAREPOINT_REMOTE_FOLDER", "SCETS/SCET_export_minified")
SHAREPOINT_ACCESS_TOKEN  = os.getenv("SHAREPOINT_ACCESS_TOKEN", "")  # Manual override
SHAREPOINT_TIMEOUT       = int(os.getenv("SHAREPOINT_TIMEOUT", "30"))

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Simple in-process cache for Graph site/drive IDs (reset on restart)
_graph_cache: dict[str, str] = {}


# -- Valid options (mirrors index.html) ---------------------------------------
Q1_LABELS: dict[str, str] = {
    "a": "[3rd Party] Issue caused by a third-party component.",
    "b": "[Firmware] Firmware coding errors or functional defects (NOT where the fix is implemented).",
    "c": "[CPU] Silicon design defects.",
    "d": "[Platform Hardware] Platform-level hardware design defects.",
    "e": "[Tool] Design errors originating from tools.",
    "f": "[SI] Simulation model errors or defects.",
    "g": "[OS] OS-related issues (e.g., kernel, Windows drivers).",
    "h": "[Electrical Validation] Errors in electrical validation guidance.",
    "i": "[Design Collateral] Defects or gaps in documentation or design collateral.",
    "j": "[Design Review] Applicable to design review tickets (not issue-related).",
    "k": "[Others] Items not covered above (e.g., CND, works as designed, customer education).",
}

Q2_OPTIONS_MAP: dict[str, list[str]] = {
    "a": ["[3rd party] Compliance test tool","[3rd Party] CXL","[3rd party] GPU card","[3rd Party] IBV Tools","[3rd party] Memory","[3rd party] NIC card","[3rd party] Others","[3rd Party] Redriver","[3rd Party] Retimer","[3rd party] Storage (NVMe, SATA, M.2)"],
    "b": ["[Firmware] Agesa/ ABL","[Firmware] AGESA/ CPM","[Firmware] Agesa/ DXIO","[Firmware] Agesa/ FCH","[Firmware] Agesa/ Hotplug","[Firmware] Agesa/ MPIO","[Firmware] Agesa/ Others","[Firmware] Agesa/ PSP","[Firmware] Agesa/ PSP /ASP 2.0","[Firmware] Agesa/ RAS","[Firmware] Agesa/ Security","[Firmware] Agesa/ SMU","[Firmware] Agesa/ UEFI","[Firmware] AMD EDK","[Firmware] APML","[Firmware] BIOS Building Issue","[Firmware] BIOS/CPLD/FPGA","[Firmware] Core Boot","[Firmware] Customer Platform","[Firmware] IBV firmware BIOS","[Firmware] IBV firmware BMC","[Firmware] Open BMC","[Firmware] Open SIL"],
    "c": ["[CPU] Core","[CPU] CXL","[CPU] DDR","[CPU] DF","[CPU] FCH","[CPU] I2C/I3C/SMBUS","[CPU] JTAG","[CPU] Others","[CPU] PCIe","[CPU] Power","[CPU] SATA","[CPU] SPI/eSPI","[CPU] SVI Bus","[CPU] USB","[CPU] XGMI"],
    "d": ["[Platform Hardware] CRB","[Platform Hardware] DDR","[Platform Hardware] FPGA","[Platform Hardware] I2C/I3C/SMBUS","[Platform Hardware] JTAG","[Platform Hardware] Mechanical","[Platform Hardware] Others","[Platform Hardware] PCIe","[Platform Hardware] Power","[Platform Hardware] SATA","[Platform Hardware] SPI/eSPI","[Platform Hardware] SVI Bus","[Platform Hardware] Thermal","[Platform Hardware] USB","[Platform Hardware] XGMI"],
    "e": ["[Tool] AMD Checker","[Tool] AMD CPR","[Tool] AMD CXL Validation Tool (CVT)","[Tool] AMD Debug Tool (Go-Pi, ADDC, etc)","[Tool] AMD Debug Tool (HDT/ HDS/ Wombat/ Glider)","[Tool] AMD Marginning Tool (Memeye/ AMDXIO)","[Tool] AMD Net Tool","[Tool] AMD Performance Tool (uPerf/Multievent)","[Tool] AMD Power Tool (SDLE)","[Tool] AMD RAS Tool (Error Injection/ MEDT)","[Tool] AMD Server Schematic Checker","[Tool] AMD Stardust","[Tool] AMD Stress Tool (ASST)","[Tool] AMD Thermal Tool (AMPTTK/ AVT)","[Tool] Non-AMD tool","[Tool] Others","[Tool] Power Test Kit - LoadSlammer","[Tool] ProGrAnalog LoadSlammer"],
    "f": ["[SI] Impedance/loss test report review","[SI] Others","[SI] S2eye tool","[SI] SATA","[SI] Seasim tool","[SI] Simulation report review","[SI] USB"],
    "g": ["[OS] Driver","[OS] fix","[OS] kernel","[OS] patch"],
    "h": ["[Electrical Validation] CXL","[Electrical Validation] DDR","[Electrical Validation] I2C/I3C/SMBUS","[Electrical Validation] Others","[Electrical Validation] PCIe","[Electrical Validation] SATA","[Electrical Validation] SPI/eSPI","[Electrical Validation] SVI Bus","[Electrical Validation] USB","[Electrical Validation] XGMI"],
    "i": ["[Design Collateral] Flotherm Thermal Model","[Design Collateral] Debug handbook","[Design Collateral] Electrical datasheet","[Design Collateral] Functional datasheet","[Design Collateral] Infrastructure Roadmap","[Design Collateral] Interlock","[Design Collateral] Layout checklist","[Design Collateral] MBDG","[Design Collateral] Mechanical Specification and User Guidance","[Design Collateral] Others","[Design Collateral] Power and Thermal Data Sheet","[Design Collateral] PPOG","[Design Collateral] PPR","[Design Collateral] Schematic checklist","[Design Collateral] SI Checklist","[Design Collateral] SI model user guide","[Design Collateral] SVM","[Design Collateral] Technical Advisory","[Design Collateral] Thermal Design Guide"],
    "j": ["[Design Review] BIOS review","[Design Review] Block diagram review","[Design Review] E2E topology review","[Design Review] Firmware bring-up check list review","[Design Review] HW bring-up check list review","[Design Review] PCB Layout review","[Design Review] PCB stack-up review","[Design Review] Platform Config. review","[Design Review] Power test result review","[Design Review] Schematic review"],
    "k": ["[Others]","[Others] Can Not Duplicate","[Others] Customer Education Awareness/Engagement","[Others] Customer Training Content Enhancement","[Others] Industry (CXL Consortium, PCI-SIG)","[Others] Issue Unrelated to AMD","[Others] New Feature Enhancement","[Others] No-hit (Validation step error or something should not happen)","[Others] System/Board level/CPU Single Case","[Others] Work as designed (Customer normal behavior)"],
}


# -- HTML helpers -------------------------------------------------------------

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_HTML_ENTITIES = {
    '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>',
    '&quot;': '"', '&#39;': "'", '&zwnj;': '', '&ndash;': '-', '&mdash;': '—',
}


def strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    text = _HTML_TAG_RE.sub(' ', str(html_text))
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    return re.sub(r'\s+', ' ', text).strip()


# -- Microsoft Graph helpers (same pattern as upload_to_sharepoint.py) --------

def get_graph_token() -> str:
    """
    Obtain a Microsoft Graph access token.
    Priority (same as upload_to_sharepoint.py):
      1. SHAREPOINT_ACCESS_TOKEN env var
      2. az account get-access-token (Azure CLI) – tries multiple paths for Windows
    """
    env_token = SHAREPOINT_ACCESS_TOKEN.strip()
    if env_token:
        return env_token

    az_base_args = [
        "account", "get-access-token",
        "--resource", "https://graph.microsoft.com",
        "--query", "accessToken", "-o", "tsv",
    ]

    candidate_cmds: list[list[str]] = []

    if os.name == "nt":
        candidate_cmds.append(["az"] + az_base_args)
        win_paths = [
            r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
            r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        ]
        for p in win_paths:
            if os.path.exists(p):
                candidate_cmds.append([p] + az_base_args)
    else:
        candidate_cmds.append(["az"] + az_base_args)

    for cmd in candidate_cmds:
        try:
            use_shell = (os.name == "nt" and cmd[0] == "az")
            result = subprocess.run(
                cmd, check=True, capture_output=True, text=True,
                timeout=20, shell=use_shell,
            )
            token = result.stdout.strip()
            if token:
                app.logger.info("[Graph] Got token via Azure CLI (%s)", cmd[0])
                return token
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue

    raise RuntimeError(
        "Cannot obtain a SharePoint/Graph access token.\n"
        "Please run 'az login --use-device-code --allow-no-subscriptions' on the server once,\n"
        "or set SHAREPOINT_ACCESS_TOKEN in .env."
    )


def _graph_get(endpoint: str, token: str, timeout: int = 30) -> dict:
    url = endpoint if endpoint.startswith("http") else GRAPH_BASE + endpoint
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _resolve_drive_id(token: str) -> str:
    cache_key = f"drive:{SHAREPOINT_HOST_NAME}:{SHAREPOINT_SITE_PATH}:{SHAREPOINT_DRIVE_NAME}"
    if cache_key in _graph_cache:
        return _graph_cache[cache_key]

    site_path = SHAREPOINT_SITE_PATH if SHAREPOINT_SITE_PATH.startswith("/") else f"/{SHAREPOINT_SITE_PATH}"
    site_data = _graph_get(f"/sites/{SHAREPOINT_HOST_NAME}:{site_path}?$select=id", token)
    site_id = site_data.get("id", "")
    if not site_id:
        raise RuntimeError("Cannot resolve SharePoint site ID.")

    drives_data = _graph_get(f"/sites/{site_id}/drives?$select=id,name", token)
    drive_id = ""
    for d in drives_data.get("value", []):
        if d.get("name", "").strip().lower() == SHAREPOINT_DRIVE_NAME.strip().lower():
            drive_id = d["id"]
            break
    if not drive_id:
        available = ", ".join(d.get("name", "") for d in drives_data.get("value", []))
        raise RuntimeError(f"Drive '{SHAREPOINT_DRIVE_NAME}' not found. Available: {available}")

    _graph_cache[cache_key] = drive_id
    app.logger.info("[Graph] Resolved drive_id for '%s': %s", SHAREPOINT_DRIVE_NAME, drive_id)
    return drive_id


def download_json_from_sharepoint(ticket_id: str) -> dict:
    token    = get_graph_token()
    drive_id = _resolve_drive_id(token)
    remote_path   = f"{SHAREPOINT_REMOTE_FOLDER.strip('/')}/{ticket_id}.json"
    encoded_path  = urlparse.quote(remote_path, safe="/")
    download_url  = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded_path}:/content"
    app.logger.info("[Graph] Downloading %s from drive %s", remote_path, drive_id)
    headers = {"Authorization": f"Bearer {token}"}
    resp    = requests.get(download_url, headers=headers, timeout=SHAREPOINT_TIMEOUT,
                           allow_redirects=True)
    resp.raise_for_status()
    return resp.json()


# -- Ticket text extraction ---------------------------------------------------

def extract_ticket_text_from_jira_api(fields: dict, max_chars: int = 4000) -> str:
    """
    Extract classification text from JIRA REST API /issue/{key} fields dict.
    Comments appear first (newest → oldest) with descending character budgets.
    """
    parts: list[str] = []

    summary = fields.get("summary") or ""
    if summary:
        parts.append(f"[Summary] {strip_html(str(summary))}")

    comment_data = fields.get("comment") or {}
    all_comments = [
        c for c in (comment_data.get("comments", []) if isinstance(comment_data, dict) else [])
        if c.get("body")
    ]
    if all_comments:
        char_limits = [500, 400, 300, 200, 150, 100]
        for i, c in enumerate(reversed(all_comments[:len(char_limits) * 2])):
            if i >= len(char_limits):
                break
            label = "LATEST COMMENT" if i == 0 else f"COMMENT-{i + 1}"
            text = strip_html(c.get("body", ""))[:char_limits[i]]
            if text.strip():
                parts.append(f"[{label}] {text}")

    root_cause = fields.get("customfield_19420") or ""
    if root_cause:
        parts.append(f"[Root Cause] {strip_html(str(root_cause))[:600]}")

    root_cause2 = fields.get("customfield_11616") or ""
    if root_cause2 and root_cause2 != root_cause:
        parts.append(f"[Root Cause (Analysis)] {strip_html(str(root_cause2))[:600]}")

    description = fields.get("description") or ""
    if description:
        parts.append(f"[Description] {strip_html(str(description))[:800]}")

    workaround = fields.get("customfield_19421") or ""
    if workaround:
        parts.append(f"[Workaround] {strip_html(str(workaround))[:400]}")

    symptom = fields.get("customfield_11605") or {}
    if isinstance(symptom, dict):
        symptom = symptom.get("value", "")
    if symptom:
        parts.append(f"[Problem Symptom] {strip_html(str(symptom))}")

    steps = fields.get("customfield_11607") or ""
    if steps:
        parts.append(f"[Steps] {strip_html(str(steps))[:300]}")

    return "\n".join(parts)[:max_chars]


def extract_ticket_text_from_sharepoint_json(ticket_data: dict, max_chars: int = 4000) -> str:
    """Extract classification text from a SCET JSON export (SharePoint file format)."""
    vr = ticket_data.get("versionedRepresentations", {})
    rf = ticket_data.get("renderedFields", {})
    ex = ticket_data.get("extras", {})

    def pick(vr_key: str, rf_key: str | None = None) -> str:
        val = (vr.get(vr_key) or {}).get("1") or ""
        if not val and rf_key:
            val = rf.get(rf_key) or ""
        if isinstance(val, dict):
            val = val.get("value", "")
        return strip_html(str(val)) if val else ""

    parts: list[str] = []

    summary = pick("summary")
    if summary:
        parts.append(f"[Summary] {summary}")

    all_comments = [
        c for c in ex.get("comments", {}).get("items", [])
        if c.get("body")
    ]
    if all_comments:
        char_limits = [500, 400, 300, 200, 150, 100]
        for i, c in enumerate(reversed(all_comments)):
            if i >= len(char_limits):
                break
            label = "LATEST COMMENT" if i == 0 else f"COMMENT-{i + 1}"
            text = strip_html(c.get("body", ""))[:char_limits[i]]
            if text.strip():
                parts.append(f"[{label}] {text}")

    root_cause = pick("customfield_19420", "customfield_19420")
    if root_cause:
        parts.append(f"[Root Cause] {root_cause[:600]}")

    root_cause2 = pick("customfield_11616", "customfield_11616")
    if root_cause2 and root_cause2 != root_cause:
        parts.append(f"[Root Cause (Analysis)] {root_cause2[:600]}")

    description = pick("description", "description")
    if description:
        parts.append(f"[Description] {description[:800]}")

    workaround = pick("customfield_19421", "customfield_19421")
    if workaround:
        parts.append(f"[Workaround] {workaround[:400]}")

    symptom = (vr.get("customfield_11605") or {}).get("1") or {}
    if isinstance(symptom, dict):
        symptom = symptom.get("value", "")
    if symptom:
        parts.append(f"[Problem Symptom] {symptom}")

    steps = pick("customfield_11607", "customfield_11607")
    if steps:
        parts.append(f"[Steps] {steps[:300]}")

    return "\n".join(parts)[:max_chars]


# -- Shared JIRA fetch helper -------------------------------------------------

# Ordered list: try full fields first, fall back to minimal if server returns 500
_JIRA_FIELDS_FULL    = "summary,description,customfield_19420,customfield_19421,customfield_11616,customfield_11605,customfield_11607,comment"
_JIRA_FIELDS_MINIMAL = "summary,description,comment"


def _call_jira_api(ticket_id: str, auth_headers: dict, label: str):
    """
    Call JIRA REST API with the given auth headers.
    Tries full fields first; falls back to minimal fields if the server returns 500
    (which can happen when custom field IDs don't exist on the JIRA instance).
    """
    merged_headers = {**auth_headers, "Accept": "application/json"}

    for fields_param in (_JIRA_FIELDS_FULL, _JIRA_FIELDS_MINIMAL):
        jira_api_url = (
            f"{JIRA_BASE_URL.rstrip('/')}/rest/api/2/issue/{ticket_id}"
            f"?fields={fields_param}"
        )
        app.logger.info("[%s] Fetching %s (fields=%s...)", label, ticket_id, fields_param[:30])
        resp = requests.get(jira_api_url, headers=merged_headers, timeout=JIRA_TIMEOUT)
        if resp.status_code != 500:
            return resp   # success OR non-500 error → let caller handle
        app.logger.warning("[%s] HTTP 500 with fields=%s, retrying with minimal fields",
                           label, fields_param[:30])

    return resp   # return last response (minimal-fields 500)


# -- q2_index resolver --------------------------------------------------------

def resolve_q2(q1_id: str, q2_index) -> str:
    valid = Q2_OPTIONS_MAP.get(q1_id, [])
    if not valid:
        return ""
    try:
        idx = int(q2_index)
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(idx, len(valid) - 1))
    return valid[idx]


# -- Few-shot learning helpers ------------------------------------------------

def load_feedback_examples(query: str, max_examples: int = 5) -> list[dict]:
    if not FEEDBACK_LOG.exists():
        return []
    try:
        logs = json.loads(FEEDBACK_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    query_words = set(query.lower().split())
    scored: list[tuple[int, dict]] = []
    for log in logs:
        if not log.get("correction_reason", "").strip():
            continue
        log_words = set(log.get("user_input", "").lower().split())
        overlap = len(query_words & log_words)
        if overlap > 0:
            scored.append((overlap, log))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [log for _, log in scored[:max_examples]]


def build_system_prompt(examples: list[dict]) -> str:
    q2_block = ""
    for k, opts in Q2_OPTIONS_MAP.items():
        numbered = "\n".join(f"    {i}: {o}" for i, o in enumerate(opts))
        q2_block += f"\n  Q1={k}:\n{numbered}"

    base = (
        "You are an AMD expert classifier. Analyze the SCET ticket and classify it.\n\n"
        "══ PRIORITY RULE ═══════════════════════════════════════════════════════\n"
        "  [LATEST COMMENT] and recent comments carry the HIGHEST WEIGHT.\n"
        "  They often contain the CONFIRMED final root cause and override the\n"
        "  original description. Always read them FIRST before classifying.\n"
        "  The original [Description] is background context only.\n"
        "═══════════════════════════════════════════════════════════════════════\n\n"
        "CLASSIFICATION PROCESS – follow these two steps:\n\n"
        "  STEP 1 – Q1 Category  (ASK: WHO CAUSED THIS PROBLEM?)\n\n"
        "    Work through the following priority order and stop at the FIRST match:\n\n"
        "    ① Is this the CUSTOMER's fault?\n"
        "       (Customer design error, misunderstanding of AMD docs, or the product\n"
        "        actually works as designed / cannot be duplicated / customer needs\n"
        "        training / issue unrelated to AMD)\n"
        "       → k  (Others)\n\n"
        "    ② Is the root cause from a NON-AMD 3rd-party component or tool?\n"
        "       (DRAM/memory, NIC card, GPU add-in card, CXL device, redriver,\n"
        "        retimer, NVMe/SATA storage, IBV BIOS/BMC tool)\n"
        "       → a  (3rd Party)\n"
        "       ★ KEY: Even if AMD added a firmware workaround, the root cause is\n"
        "         still 3rd Party if the original defect is in a 3rd-party component.\n\n"
        "    ③ Is the root cause in AMD FIRMWARE CODE itself (not just a workaround)?\n"
        "       (AGESA sub-components: DXIO/MPIO/PSP/SMU/FCH/UEFI/ABL/RAS/Security\n"
        "        /Hotplug; Open SIL; Open BMC; AMD EDK; APML; BIOS/CPLD/FPGA;\n"
        "        IBV firmware BIOS/BMC; Customer Platform firmware; Core Boot)\n"
        "       → b  (Firmware)\n"
        "       ★ Do NOT choose b if firmware only patched around a CPU silicon bug.\n"
        "         In that case the root cause is CPU silicon → go to ④.\n\n"
        "    ④ Is the root cause a defect in AMD CPU SILICON hardware?\n"
        "       (Core, PCIe, DDR, CXL, USB, SATA, xGMI, SVI Bus, DF, FCH silicon,\n"
        "        I2C/I3C/SMBUS, JTAG, Power, SPI/eSPI)\n"
        "       → c  (CPU)\n"
        "       ★ Even if AGESA patched it: the root cause is still CPU silicon → c.\n\n"
        "    ⑤ Is the root cause a defect in SERVER BOARD / PLATFORM hardware design?\n"
        "       (PCB schematic, DDR trace routing, power delivery design on board,\n"
        "        FPGA/CPLD on the board, mechanical or thermal design of the platform;\n"
        "        NOT the CPU die itself)\n"
        "       → d  (Platform Hardware)\n\n"
        "    ⑥ Is the root cause a BUG IN AN ENGINEERING TOOL?\n"
        "       (AMD: HDT/HDS/Wombat/Glider, Memeye/AMDXIO, ASST, AMPTTK/AVT,\n"
        "        CVT, CPR, SDLE, Stardust, Net Tool, Performance Tool,\n"
        "        Server Schematic Checker, Power Test Kit, ProGrAnalog LoadSlammer;\n"
        "        also Non-AMD tools used in validation)\n"
        "       → e  (Tool)\n\n"
        "    ⑦ Is the root cause a SIGNAL INTEGRITY simulation model or SI tool error?\n"
        "       (Seasim, S2Eye; SI simulation/impedance report reviews for\n"
        "        SATA, USB, PCIe, DDR, xGMI)\n"
        "       → f  (SI)\n\n"
        "    ⑧ Is the root cause an OS KERNEL or DRIVER bug?\n"
        "       (Linux kernel defect, Windows driver bug, OS patch required)\n"
        "       → g  (OS)\n\n"
        "    ⑨ Is the root cause WRONG or MISSING AMD ELECTRICAL VALIDATION GUIDANCE?\n"
        "       (Incorrect AMD EV test procedures or guidelines for\n"
        "        CXL, DDR, PCIe, USB, SATA, SPI/eSPI, SVI Bus, xGMI)\n"
        "       → h  (Electrical Validation)\n\n"
        "    ⑩ Is the root cause an ERROR or GAP in AMD DOCUMENTATION?\n"
        "       (PPR, datasheet, layout checklist, schematic checklist,\n"
        "        debug handbook, MBDG, thermal design guide, PPOG, SVM,\n"
        "        Technical Advisory, SI model user guide, Interlock)\n"
        "       → i  (Design Collateral)\n\n"
        "    ⑪ Is this ticket a DESIGN REVIEW REQUEST (not a bug report)?\n"
        "       (Customer asking AMD to review their schematic, PCB layout,\n"
        "        firmware bring-up checklist, platform config, stack-up, BOM)\n"
        "       → j  (Design Review)\n\n"
        "  STEP 2 – Q2 Sub-Category:\n"
        "    Look ONLY at the Q2 options listed under your chosen Q1 below.\n"
        "    Select the most specific sub-category that matches the issue.\n"
        "    Do NOT pick Q2 from a different Q1 group.\n\n"
        "Return ONLY valid JSON with these three fields:\n"
        "  {\"q1_id\": \"<letter a-k>\", \"q2_index\": <integer>, \"reasoning\": \"<explanation>\"}\n\n"
        "Constraints:\n"
        "  1. q1_id must be exactly one letter from: a b c d e f g h i j k\n"
        "  2. q2_index must be the integer index from the numbered list under your chosen q1_id.\n"
        "     Do NOT output q2_text – output only the integer index.\n"
        "  3. The index must be within the valid range for the chosen q1_id.\n\n"
        f"Numbered Q2 options per Q1:{q2_block}"
    )
    if not examples:
        return base

    corrections = "\n\nLearn from these past user corrections (apply the same logic):"
    for i, ex in enumerate(examples, 1):
        corrections += (
            f"\n\n[Past correction {i}]"
            f"\n  Description : {ex.get('user_input', '')}"
            f"\n  AI chose    : {ex.get('ai_output', '')} (Q1={ex.get('ai_q1', '')})"
            f"\n  User said   : {ex.get('correction_reason', '')}"
        )
    return base + corrections


# -- Routes -------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/health")
def health():
    """Configuration health check – does NOT expose secret values."""
    az_logged_in = False
    try:
        r = subprocess.run(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        az_logged_in = bool(r.stdout.strip())
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "ai_endpoint_set": bool(AI_ENDPOINT),
        "sub_key_set": bool(SUB_KEY),
        "api_user": API_USER,
        "api_version": API_VERSION,
        "llm_model": LLM_MODEL,
        "llm_max_tokens": LLM_MAX_TOKENS,
        "ai_timeout_s": AI_TIMEOUT,
        "feedback_log": str(FEEDBACK_LOG),
        "jira_base_url": JIRA_BASE_URL,
        "jira_token_set": bool(JIRA_TOKEN),
        "jira_service_account_set": bool(JIRA_SERVICE_ACCOUNT_ID and JIRA_SERVICE_ACCOUNT_PASSWORD),
        "sharepoint_host": SHAREPOINT_HOST_NAME,
        "sharepoint_access_token_set": bool(SHAREPOINT_ACCESS_TOKEN),
        "az_cli_logged_in": az_logged_in,
        "server_url": f"http://{DEPLOY_HOST}:{PORT}",
    })


@app.route("/api/fetch-ticket", methods=["POST"])
def fetch_ticket():
    """
    Fetch a SCET ticket and return extracted text for AI classification.

    Priority:
      1. JIRA Bearer PAT        – if JIRA_TOKEN is set
      2. JIRA Service Account   – if JIRA_SERVICE_ACCOUNT_ID + PASSWORD are set (Basic Auth)
      3. SharePoint Graph API   – via Azure CLI / SHAREPOINT_ACCESS_TOKEN (fallback)

    Request : { "ticket_id": "SCET-1234" or "https://ontrack.amd.com/browse/SCET-1234" }
    Response: { "ticket_id", "ticket_url", "extracted_text", "source" }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    ticket_input = data.get("ticket_id", "").strip()
    if not ticket_input:
        return jsonify({"error": "ticket_id is required."}), 400

    match = re.search(r'(SCET-\d+)', ticket_input, re.IGNORECASE)
    if not match:
        return jsonify({"error": f"Cannot find SCET-XXXX in: '{ticket_input}'"}), 400

    ticket_id  = match.group(1).upper()
    ticket_url = f"{JIRA_BASE_URL}/browse/{ticket_id}"

    def _process_jira_response(resp, source_label: str):
        """Parse a successful JIRA API response and return Flask response tuple."""
        fields    = resp.json().get("fields", {})
        extracted = extract_ticket_text_from_jira_api(fields)
        if not extracted.strip():
            return jsonify({"error": f"{ticket_id} fetched but no text extracted."}), 422
        app.logger.info("[%s] Extracted %d chars from %s", source_label, len(extracted), ticket_id)
        return jsonify({"ticket_id": ticket_id, "ticket_url": ticket_url,
                        "extracted_text": extracted, "source": source_label})

    # ── Method 1: JIRA Bearer PAT ─────────────────────────────────────────────
    if JIRA_TOKEN:
        try:
            resp = _call_jira_api(ticket_id, {"Authorization": f"Bearer {JIRA_TOKEN}"}, "JIRA-PAT")
            if not resp.ok:
                return jsonify({
                    "error": f"JIRA API returned HTTP {resp.status_code} for {ticket_id}.",
                    "detail": "If 401/403: JIRA_TOKEN may be expired. Regenerate at ontrack.amd.com → Profile → Personal Access Tokens.",
                }), resp.status_code
            return _process_jira_response(resp, "jira_pat")
        except requests.exceptions.Timeout:
            return jsonify({"error": f"Timeout fetching {ticket_id} from JIRA."}), 504
        except requests.exceptions.ConnectionError as exc:
            return jsonify({"error": f"Cannot connect to JIRA: {exc}"}), 502
        except Exception as exc:
            app.logger.error("[JIRA-PAT] Error for %s: %s", ticket_id, exc)
            return jsonify({"error": f"JIRA PAT error: {exc}"}), 500

    # ── Method 2: JIRA Service Account (Basic Auth) ───────────────────────────
    if JIRA_SERVICE_ACCOUNT_ID and JIRA_SERVICE_ACCOUNT_PASSWORD:
        raw_cred = f"{JIRA_SERVICE_ACCOUNT_ID}:{JIRA_SERVICE_ACCOUNT_PASSWORD}"
        b64_cred = base64.b64encode(raw_cred.encode()).decode()
        try:
            resp = _call_jira_api(ticket_id, {"Authorization": f"Basic {b64_cred}"}, "JIRA-SA")
            if not resp.ok:
                return jsonify({
                    "error": f"JIRA API returned HTTP {resp.status_code} for {ticket_id}.",
                    "detail": (
                        "If 401/403: check JIRA_SERVICE_ACCOUNT_ID / JIRA_SERVICE_ACCOUNT_PASSWORD in .env. "
                        "Ensure the service account has read access to the SCET project on ontrack.amd.com."
                    ),
                }), resp.status_code
            return _process_jira_response(resp, "jira_service_account")
        except requests.exceptions.Timeout:
            return jsonify({"error": f"Timeout fetching {ticket_id} from JIRA."}), 504
        except requests.exceptions.ConnectionError as exc:
            return jsonify({"error": f"Cannot connect to JIRA: {exc}"}), 502
        except Exception as exc:
            app.logger.error("[JIRA-SA] Error for %s: %s", ticket_id, exc)
            return jsonify({"error": f"JIRA Service Account error: {exc}"}), 500

    # ── Method 3: SharePoint via Microsoft Graph (Azure CLI auth) ─────────────
    try:
        ticket_data = download_json_from_sharepoint(ticket_id)
        extracted   = extract_ticket_text_from_sharepoint_json(ticket_data)
        if not extracted.strip():
            return jsonify({"error": f"{ticket_id} fetched from SharePoint but no text extracted."}), 422
        app.logger.info("[Graph] Extracted %d chars from %s", len(extracted), ticket_id)
        return jsonify({"ticket_id": ticket_id, "ticket_url": ticket_url,
                        "extracted_text": extracted, "source": "sharepoint"})

    except RuntimeError as exc:
        err_msg = str(exc)
        app.logger.error("[Graph] %s", err_msg)
        return jsonify({
            "error": "SharePoint access failed.",
            "detail": (
                err_msg + "\n\n"
                "How to fix:\n"
                "  Option A (recommended): Set JIRA_SERVICE_ACCOUNT_ID + JIRA_SERVICE_ACCOUNT_PASSWORD in .env\n"
                "  Option B: Set JIRA_TOKEN in .env\n"
                "  Option C: Run 'az login --use-device-code --allow-no-subscriptions' on the server"
            ),
        }), 503

    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        return jsonify({
            "error": f"SharePoint Graph returned HTTP {status} for {ticket_id}.json.",
            "detail": "Try 'az login --use-device-code' on the server, or set SHAREPOINT_ACCESS_TOKEN in .env.",
        }), status or 502

    except Exception as exc:
        app.logger.error("[Graph] Error for %s: %s", ticket_id, exc)
        return jsonify({"error": f"SharePoint error: {exc}"}), 500


@app.route("/api/ai", methods=["POST"])
def proxy_ai():
    """Proxy AI classification requests. Credentials stay server-side."""
    if not AI_ENDPOINT or not SUB_KEY:
        return jsonify({"error": "AI_ENDPOINT or SUB_KEY not configured in .env."}), 503

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"error": "Invalid JSON body."}), 400

    if LLM_MODEL:
        payload.setdefault("model", LLM_MODEL)
    payload.setdefault("max_completion_tokens", LLM_MAX_TOKENS)

    user_content = next(
        (m.get("content", "") for m in payload.get("messages", []) if m.get("role") == "user"), ""
    )
    examples = load_feedback_examples(user_content)
    enriched_system = build_system_prompt(examples)
    for msg in payload.get("messages", []):
        if msg.get("role") == "system":
            msg["content"] = enriched_system
            break
    if examples:
        app.logger.info("Injected %d few-shot correction(s).", len(examples))

    headers = {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": SUB_KEY,
        "user": API_USER,
    }
    endpoint_url = f"{AI_ENDPOINT}?api-version={API_VERSION}" if API_VERSION else AI_ENDPOINT

    try:
        app.logger.info("Calling AI endpoint (timeout=%ds) ...", AI_TIMEOUT)
        upstream = requests.post(endpoint_url, json=payload, headers=headers, timeout=AI_TIMEOUT)
        app.logger.info("AI upstream responded: HTTP %d", upstream.status_code)

        if not upstream.ok:
            return jsonify({
                "error": f"AI upstream returned HTTP {upstream.status_code}.",
                "detail": upstream.text[:500],
            }), upstream.status_code

        upstream_data = upstream.json()
        raw_content   = upstream_data["choices"][0]["message"]["content"]
        raw_content   = raw_content.replace("```json", "").replace("```", "").strip()
        ai_result     = json.loads(raw_content)

        q1_id    = ai_result.get("q1_id", "k")
        q2_index = ai_result.get("q2_index", 0)
        q2_text  = resolve_q2(q1_id, q2_index)
        app.logger.info("AI: q1=%s q2_index=%s -> '%s'", q1_id, q2_index, q2_text)

        ai_result.pop("q2_index", None)
        ai_result["q2_text"] = q2_text
        upstream_data["choices"][0]["message"]["content"] = json.dumps(ai_result)
        return jsonify(upstream_data)

    except Exception as parse_err:
        if isinstance(parse_err, requests.exceptions.Timeout):
            return jsonify({"error": f"AI timed out after {AI_TIMEOUT}s. Please retry."}), 504
        if isinstance(parse_err, requests.exceptions.ConnectionError):
            return jsonify({"error": f"Cannot reach AI endpoint: {AI_ENDPOINT}"}), 502
        app.logger.error("AI proxy error: %s", parse_err)
        return jsonify({"error": f"AI error: {parse_err}"}), 502


@app.route("/api/feedback", methods=["POST", "OPTIONS"])
def feedback():
    """Collect user corrections → feedback_logs.json."""
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    entry = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "user_input":        data.get("original_input", ""),
        "ai_q1":             data.get("ai_q1", ""),
        "ai_output":         data.get("ai_output", ""),
        "correction_reason": data.get("correction_reason", ""),
    }

    logs: list = []
    if FEEDBACK_LOG.exists():
        try:
            logs = json.loads(FEEDBACK_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logs = []

    logs.append(entry)
    FEEDBACK_LOG.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
    app.logger.info("Feedback recorded. Total: %d", len(logs))
    return jsonify({"status": "success", "count": len(logs)})


@app.route("/api/export-feedback")
def export_feedback():
    """Download feedback_logs.json as a fine-tuning dataset."""
    if not FEEDBACK_LOG.exists():
        return jsonify([])
    try:
        logs = json.loads(FEEDBACK_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logs = []
    return (
        json.dumps(logs, ensure_ascii=False, indent=2),
        200,
        {"Content-Type": "application/json",
         "Content-Disposition": "attachment; filename=feedback_logs.json"},
    )


# -- Entry point --------------------------------------------------------------

if __name__ == "__main__":
    app.logger.info("Starting Competency Selector on http://%s:%d  (AI timeout=%ds)",
                    HOST, PORT, AI_TIMEOUT)
    app.logger.info("User access URL: http://%s:%d", DEPLOY_HOST, PORT)
    debug = os.getenv("DEBUG", "false").lower() == "true"
    app.run(host=HOST, port=PORT, debug=debug)
