#!/usr/bin/env python3
"""
SharePoint upload module (via Microsoft Graph).

This file supports two usages:
1) As a module (recommended for other Python scripts):
     from sharepoint_upload_files import (
         upload_fixed_target_file,
         upload_fixed_target_folder,
         list_remote_folder_files,
     )
     upload_fixed_target_file("/path/to/file")
     upload_fixed_target_folder("/path/to/folder")
     list_remote_folder_files("SCETS/test_auto", recursive=False)

2) As a standalone script (for debugging):
     python sharepoint_upload_files.py --file ./xx.json
     python sharepoint_upload_files.py --folder ./data_dir
     python sharepoint_upload_files.py --file ./xx.json --delete-local-after-upload
     python sharepoint_upload_files.py --folder ./data_dir --delete-local-after-upload

     # List files and total count under a specific SharePoint folder
     python sharepoint_upload_files.py --list-remote-files --remote-folder SCETS/test_auto

     # List files and total count under a specific SharePoint folder (including subfolders recursively)
     python sharepoint_upload_files.py --list-remote-files --remote-folder SCETS/test_auto --recursive

Token acquisition strategy (best-effort):
1) Use SHAREPOINT_ACCESS_TOKEN env var if provided.
2) Try azure-identity AzureCliCredential.
3) Fallback to Azure CLI:
   az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request


GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# ===== Fixed SharePoint target config (macros) =====
HOSTNAME = os.getenv("SHAREPOINT_HOST_NAME", "")
SITE_PATH = "/sites/SCPI"
DRIVE_NAME = "Documents"
#REMOTE_FOLDER = "SCETS/SCET_export_minified"
REMOTE_FOLDER = "SCETS/test_auto"
FAILED_UPLOAD_LOG_FILE = "sharepoint_failed_uploads.log"
SINGLE_FILE_FAILED_UPLOAD_LOG_FILE = "log/sharepoint_upload_failed.log"


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def run_az_get_token() -> Optional[str]:
    """Fallback: get Graph access token from Azure CLI."""
    cmd = [
        "az",
        "account",
        "get-access-token",
        "--resource",
        "https://graph.microsoft.com",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        token = result.stdout.strip()
        return token or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_access_token() -> str:
    """
    Resolve access token in this order:
    1) SHAREPOINT_ACCESS_TOKEN env
    2) AzureCliCredential from azure-identity
    3) az CLI fallback
    """
    env_token = os.getenv("SHAREPOINT_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        from azure.identity import AzureCliCredential  # type: ignore

        credential = AzureCliCredential()
        token = credential.get_token("https://graph.microsoft.com/.default").token
        if token:
            return token
    except Exception:
        pass

    token = run_az_get_token()
    if token:
        return token

    raise RuntimeError(
        "Failed to get access token. Please ensure one of the following:\n"
        "1) Set SHAREPOINT_ACCESS_TOKEN env var, or\n"
        "2) Install azure-identity and ensure Azure CLI is logged in, or\n"
        "3) Azure CLI is installed and 'az account get-access-token' works."
    )


def graph_request(
    method: str,
    endpoint: str,
    token: str,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
) -> Tuple[int, Any]:
    """Send HTTP request to Microsoft Graph."""
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        url = endpoint
    else:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = GRAPH_BASE + endpoint

    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type

    req = request.Request(url=url, method=method.upper(), headers=headers, data=body)
    try:
        with request.urlopen(req) as resp:
            raw = resp.read()
            status = resp.status
            if not raw:
                return status, None
            try:
                return status, json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return status, raw
    except error.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"Graph API error {http_err.code} {http_err.reason} at {url}\n{err_body}"
        ) from http_err
    except error.URLError as url_err:
        raise RuntimeError(f"Network error when calling Graph API at {url}: {url_err}") from url_err


def normalize_site_path(site_path: str) -> str:
    """Ensure site path starts with '/'."""
    if not site_path.startswith("/"):
        return "/" + site_path
    return site_path


def resolve_site_id(token: str, hostname: str, site_path: str) -> str:
    """Resolve site-id from hostname + server-relative site path."""
    if not hostname.strip():
        raise RuntimeError("Missing required environment variable: SHAREPOINT_HOST_NAME")
    site_path = normalize_site_path(site_path)
    endpoint = f"/sites/{hostname}:{site_path}?$select=id,displayName,webUrl"
    _, data = graph_request("GET", endpoint, token)
    site_id = data.get("id") if isinstance(data, dict) else None
    if not site_id:
        raise RuntimeError(f"Cannot resolve site-id from hostname={hostname}, site_path={site_path}")
    return site_id


def resolve_drive_id(token: str, site_id: str, drive_name: str) -> str:
    """Resolve drive-id by drive name under a site."""
    endpoint = f"/sites/{site_id}/drives?$select=id,name"
    _, data = graph_request("GET", endpoint, token)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected response when listing drives.")
    drives = data.get("value", [])
    for d in drives:
        if str(d.get("name", "")).strip().lower() == drive_name.strip().lower():
            return d["id"]
    available = ", ".join(d.get("name", "<unnamed>") for d in drives) or "<none>"
    raise RuntimeError(
        f"Drive '{drive_name}' not found in site '{site_id}'. Available drives: {available}"
    )


def build_remote_item_path(local_file: Path, remote_folder: Optional[str]) -> str:
    """Build library-relative file path for single file upload."""
    filename = local_file.name
    if not remote_folder:
        return filename
    remote_folder = remote_folder.strip().strip("/")
    return f"{remote_folder}/{filename}" if remote_folder else filename


def build_remote_item_path_from_relative(relative_file: Path, remote_folder: Optional[str]) -> str:
    """Build library-relative file path using a path relative to a local folder."""
    relative_posix = relative_file.as_posix().lstrip("/")
    if not remote_folder:
        return relative_posix
    remote_folder = remote_folder.strip().strip("/")
    return f"{remote_folder}/{relative_posix}" if remote_folder else relative_posix


def write_failed_uploads_log(
    failed_items: List[Tuple[Path, str]],
    log_file: str = FAILED_UPLOAD_LOG_FILE,
) -> Path:
    """Write failed upload file list to a local log file."""
    log_path = Path(log_file).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# SharePoint upload failed files", ""]
    for file_path, err_msg in failed_items:
        lines.append(f"- {file_path}")
        lines.append(f"  error: {err_msg}")
    lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def upload_file_content(token: str, drive_id: str, local_file: Path, remote_item_path: str) -> Dict[str, Any]:
    """
    Upload file via simple upload API:
      PUT /drives/{drive-id}/root:/{path/to/file.ext}:/content
    """
    if not local_file.exists() or not local_file.is_file():
        raise RuntimeError(f"Local file does not exist or is not a file: {local_file}")

    encoded_path = parse.quote(remote_item_path, safe="/")
    endpoint = f"/drives/{drive_id}/root:/{encoded_path}:/content"

    with local_file.open("rb") as f:
        content = f.read()

    _, data = graph_request(
        method="PUT",
        endpoint=endpoint,
        token=token,
        body=content,
        content_type="application/octet-stream",
    )

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected upload response from Graph.")
    return data


def upload_fixed_target_file(file_path: str, delete_local_after_upload: bool = False) -> Dict[str, Any]:
    """
    Upload a local file to fixed SharePoint target configured by constants:
    HOSTNAME / SITE_PATH / DRIVE_NAME / REMOTE_FOLDER.

    Retry policy for single-file upload:
    - If an upload attempt fails, refresh token and retry that same file.
    - If the same file fails 3 times, skip this file and write failure log to:
      log/sharepoint_upload_failed.log

    Args:
        file_path: Local file path to upload.
        delete_local_after_upload: Whether to delete local file after successful upload.
            Default is False (keep local file).

    Returns Graph response JSON (dict). Returns empty dict when file upload
    finally fails after all retries.
    """
    start_ts = time.perf_counter()
    local_file = Path(file_path).expanduser().resolve()

    token = get_access_token()
    site_id = resolve_site_id(token, HOSTNAME, SITE_PATH)
    print(f"[info] Resolved site-id: {site_id}")

    drive_id = resolve_drive_id(token, site_id, DRIVE_NAME)
    print(f"[info] Resolved drive-id: {drive_id}")

    remote_item_path = build_remote_item_path(local_file, REMOTE_FOLDER)
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            result = upload_file_content(token, drive_id, local_file, remote_item_path)

            elapsed = time.perf_counter() - start_ts
            print("[ok] Upload succeeded.")
            print(f"[ok] Item ID: {result.get('id')}")
            print(f"[ok] Size: {result.get('size')}")
            print(f"[ok] Web URL: {result.get('webUrl')}")

            if delete_local_after_upload:
                try:
                    if local_file.exists():
                        local_file.unlink()
                        print(f"[info] Local file deleted after upload: {local_file}")
                except Exception as del_exc:
                    print(
                        f"[warn] Uploaded but failed to delete local file {local_file}: {del_exc}",
                        file=sys.stderr,
                    )

            print(f"[ok] Total elapsed: {elapsed:.2f}s")
            return result
        except Exception as exc:
            err_msg = str(exc)
            print(
                f"[error] Upload failed (attempt {attempt}/{max_retries}): "
                f"{local_file} -> {remote_item_path}"
            )
            print(f"[error] Reason: {err_msg}")

            if attempt >= max_retries:
                log_path = write_failed_uploads_log(
                    failed_items=[(local_file, err_msg)],
                    log_file=SINGLE_FILE_FAILED_UPLOAD_LOG_FILE,
                )
                print(
                    f"[error] Upload skipped after {max_retries} failed attempts: {local_file}. "
                    f"Failed log written to: {log_path}",
                    file=sys.stderr,
                )
                return {}

            try:
                token = get_access_token()
                print("[info] Token refreshed for retry.")
            except Exception as token_exc:
                combined_err = f"{err_msg} | token refresh failed: {token_exc}"
                log_path = write_failed_uploads_log(
                    failed_items=[(local_file, combined_err)],
                    log_file=SINGLE_FILE_FAILED_UPLOAD_LOG_FILE,
                )
                print(
                    f"[error] Upload skipped because token refresh failed: {local_file}. "
                    f"Failed log written to: {log_path}",
                    file=sys.stderr,
                )
                return {}

    return {}


def upload_fixed_target_folder(
    folder_path: str,
    delete_local_after_upload: bool = False,
) -> List[Dict[str, Any]]:
    """
    Upload all files under a local folder (recursive) to fixed SharePoint target.
    Remote path keeps the folder-relative structure under REMOTE_FOLDER.

    Retry policy for folder upload:
    - If an upload attempt fails, refresh token and retry that same file.
    - If the same file fails 3 times, abort the whole folder upload process.

    Returns a list of Graph response JSON dicts.
    """
    start_ts = time.perf_counter()
    local_folder = Path(folder_path).expanduser().resolve()
    if not local_folder.exists() or not local_folder.is_dir():
        raise RuntimeError(f"Local folder does not exist or is not a directory: {local_folder}")

    files = sorted(p for p in local_folder.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError(f"No files found under folder: {local_folder}")

    token = get_access_token()

    site_id = resolve_site_id(token, HOSTNAME, SITE_PATH)
    print(f"[info] Resolved site-id: {site_id}")

    drive_id = resolve_drive_id(token, site_id, DRIVE_NAME)
    print(f"[info] Resolved drive-id: {drive_id}")

    print(f"[info] Found {len(files)} files under: {local_folder}")

    results: List[Dict[str, Any]] = []
    failed_items: List[Tuple[Path, str]] = []

    max_retries_per_file = 3

    for idx, local_file in enumerate(files, start=1):
        relative_file = local_file.relative_to(local_folder)
        remote_item_path = build_remote_item_path_from_relative(relative_file, REMOTE_FOLDER)

        print(f"[info] ({idx}/{len(files)}) Uploading: {relative_file}")

        attempt = 0
        while attempt < max_retries_per_file:
            attempt += 1
            try:
                result = upload_file_content(token, drive_id, local_file, remote_item_path)
                print(f"[ok] Uploaded: {relative_file} -> {remote_item_path}")

                if delete_local_after_upload:
                    try:
                        if local_file.exists():
                            local_file.unlink()
                            print(f"[info] Local file deleted after upload: {local_file}")
                    except Exception as del_exc:
                        print(
                            f"[warn] Uploaded but failed to delete local file {local_file}: {del_exc}",
                            file=sys.stderr,
                        )

                results.append(result)
                break
            except Exception as exc:
                err_msg = str(exc)
                print(
                    f"[error] Upload failed (attempt {attempt}/{max_retries_per_file}): "
                    f"{relative_file} -> {remote_item_path}"
                )
                print(f"[error] Reason: {err_msg}")

                if attempt >= max_retries_per_file:
                    failed_items.append((relative_file, err_msg))
                    log_path = write_failed_uploads_log(failed_items)
                    raise RuntimeError(
                        f"Upload aborted: file {relative_file} failed "
                        f"{max_retries_per_file} times. Failed list logged at: {log_path}"
                    ) from exc

                # Refresh token before retrying the same file
                try:
                    token = get_access_token()
                    print("[info] Token refreshed for retry.")
                except Exception as token_exc:
                    failed_items.append((relative_file, f"{err_msg} | token refresh failed: {token_exc}"))
                    log_path = write_failed_uploads_log(failed_items)
                    raise RuntimeError(
                        f"Upload aborted: failed to refresh token after upload failure "
                        f"for file {relative_file}. Failed list logged at: {log_path}"
                    ) from token_exc

    elapsed = time.perf_counter() - start_ts
    print(f"[ok] Folder upload finished. Success: {len(results)}, Failed: {len(failed_items)}")
    print(f"[ok] Total elapsed: {elapsed:.2f}s")

    return results


def list_remote_folder_files(remote_folder: str = REMOTE_FOLDER, recursive: bool = False) -> List[str]:
    """
    List file names under a remote SharePoint folder.

    Args:
        remote_folder: Library-relative remote folder path, e.g. "SCETS/test_auto".
        recursive: Whether to recursively list files in subfolders.

    Returns:
        A list of file paths relative to the given remote_folder.
    """
    token = get_access_token()
    site_id = resolve_site_id(token, HOSTNAME, SITE_PATH)
    print(f"[info] Resolved site-id: {site_id}")

    drive_id = resolve_drive_id(token, site_id, DRIVE_NAME)
    print(f"[info] Resolved drive-id: {drive_id}")

    normalized_remote_folder = remote_folder.strip().strip("/")
    files: List[str] = []

    def _iter_children(endpoint: str) -> List[Dict[str, Any]]:
        children: List[Dict[str, Any]] = []
        next_endpoint: Optional[str] = endpoint
        while next_endpoint:
            _, data = graph_request("GET", next_endpoint, token)
            if not isinstance(data, dict):
                raise RuntimeError("Unexpected response when listing remote folder children.")
            children.extend(data.get("value", []))
            next_endpoint = data.get("@odata.nextLink")
        return children

    def _walk_folder_by_item_id(folder_item_id: str, prefix: str) -> None:
        endpoint = f"/drives/{drive_id}/items/{folder_item_id}/children"
        children = _iter_children(endpoint)

        for item in children:
            name = str(item.get("name", "")).strip()
            if not name:
                continue

            relative_path = f"{prefix}/{name}" if prefix else name
            if "file" in item:
                files.append(relative_path)
            elif "folder" in item and recursive:
                child_id = str(item.get("id", "")).strip()
                if child_id:
                    _walk_folder_by_item_id(child_id, relative_path)

    if normalized_remote_folder:
        encoded_folder = parse.quote(normalized_remote_folder, safe="/")
        root_children_endpoint = f"/drives/{drive_id}/root:/{encoded_folder}:/children"
    else:
        root_children_endpoint = f"/drives/{drive_id}/root/children"

    children = _iter_children(root_children_endpoint)
    for item in children:
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        if "file" in item:
            files.append(name)
        elif "folder" in item and recursive:
            child_id = str(item.get("id", "")).strip()
            if child_id:
                _walk_folder_by_item_id(child_id, name)

    return files


def write_remote_list_outputs(
    files: List[str],
    list_file_path: str,
    count_file_path: str,
) -> Tuple[Path, Path]:
    """Write remote file list and file count to two separate local files."""
    list_path = Path(list_file_path).expanduser().resolve()
    count_path = Path(count_file_path).expanduser().resolve()

    list_path.parent.mkdir(parents=True, exist_ok=True)
    count_path.parent.mkdir(parents=True, exist_ok=True)

    list_path.write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8")
    count_path.write_text(f"{len(files)}\n", encoding="utf-8")

    return list_path, count_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload local file/folder to SharePoint, or list remote folder file names for debugging."
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--file", help="Local file path to upload")
    group.add_argument("--folder", help="Local folder path; upload all files recursively")
    group.add_argument(
        "--list-remote-files",
        action="store_true",
        help="List file names and file count under a SharePoint remote folder (debug mode).",
    )

    parser.add_argument(
        "--delete-local-after-upload",
        action="store_true",
        help="Used with --file/--folder: delete local file after successful upload (default: keep local file).",
    )
    parser.add_argument(
        "--remote-folder",
        default=REMOTE_FOLDER,
        help=f"Remote folder path for --list-remote-files (default: {REMOTE_FOLDER})",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Used with --list-remote-files: recursively include files in subfolders.",
    )
    parser.add_argument(
        "--list-output-file",
        default="sharepoint_remote_file_list.txt",
        help="Used with --list-remote-files: local output file for remote file name list.",
    )
    parser.add_argument(
        "--count-output-file",
        default="sharepoint_remote_file_count.txt",
        help="Used with --list-remote-files: local output file for remote file count.",
    )

    args = parser.parse_args()

    if not (args.file or args.folder or args.list_remote_files):
        parser.error("one of --file, --folder, or --list-remote-files is required")

    return args


def main() -> int:
    try:
        args = parse_args()
        if args.file:
            upload_fixed_target_file(
                args.file,
                delete_local_after_upload=args.delete_local_after_upload,
            )
        elif args.folder:
            upload_fixed_target_folder(
                args.folder,
                delete_local_after_upload=args.delete_local_after_upload,
            )
        else:
            files = list_remote_folder_files(
                remote_folder=args.remote_folder,
                recursive=args.recursive,
            )
            print(f"[ok] Remote folder: {args.remote_folder}")
            print(f"[ok] File count: {len(files)}")
            for idx, name in enumerate(files, start=1):
                print(f"{idx:04d}. {name}")

            list_path, count_path = write_remote_list_outputs(
                files=files,
                list_file_path=args.list_output_file,
                count_file_path=args.count_output_file,
            )
            print(f"[ok] File list written to: {list_path}")
            print(f"[ok] File count written to: {count_path}")
        return 0
    except Exception as exc:
        eprint(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
