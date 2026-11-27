"""Read the six Google sources and write an encrypted comparison for review."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from director_sources import ACTIVE_TABS, compare_states, normalize
from tracker import decrypt, encrypt, load, password, save


DRIVE = "https://www.googleapis.com/drive/v3/files"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
NATIVE = "application/vnd.google-apps.spreadsheet"
WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)
LAST_COLUMN = {
    "ami": ("I",), "krd": ("G", "G", "H", "J"), "msk": ("I", "H"),
    "loft": ("E", "G"), "lig": ("E",),
}
GRID_FIELDS = (
    "spreadsheetId,spreadsheetUrl,sheets(properties(title,sheetId),"
    "data(startRow,rowData(values(formattedValue,effectiveValue,userEnteredValue,"
    "effectiveFormat(backgroundColor,textFormat(bold,strikethrough,foregroundColor)),"
    "dataValidation(condition),note))))"
)
FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,size"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def get(session, url, params):
    response = session.get(url, params=params, timeout=60)
    if not response.ok:
        raise ValueError(f"Google API HTTP {response.status_code}")
    return response


def collect(session, previous, directory):
    metadata = {}
    for source, titles in ACTIVE_TABS.items():
        source_id = previous["sources"][source]["file"]["id"]
        if not source_id or not all(c.isascii() and (c.isalnum() or c in "_-") for c in source_id):
            raise ValueError("Invalid Google file ID in source state.")
        file_url = f"{DRIVE}/{source_id}"
        before = get(session, file_url, {"fields": FILE_FIELDS, "supportsAllDrives": "true"}).json()
        expected_mime = WORKBOOK if source == "vas" else NATIVE
        if before["id"] != source_id or before["mimeType"] != expected_mime or not before.get("modifiedTime"):
            raise ValueError(f"Unexpected source metadata: {source}")
        metadata[source] = {"file": {
            "id": source_id, "name": before["name"], "mime_type": before["mimeType"],
            "modified_time": before["modifiedTime"], "web_view_link": before.get("webViewLink"),
            "size": before.get("size"),
        }}
        if source == "vas":
            workbook = get(session, file_url, {"alt": "media", "supportsAllDrives": "true"})
            (directory / "vas.xlsx").write_bytes(workbook.content)
        else:
            spreadsheet_url = f"{SHEETS}/{source_id}"
            tabs = get(session, spreadsheet_url, {
                "fields": "spreadsheetId,spreadsheetUrl,sheets(properties(title,sheetId,hidden,gridProperties))",
            }).json()
            metadata[source]["sheets"] = tabs
            properties = {tab["properties"]["title"]: tab["properties"] for tab in tabs["sheets"]}
            ranges = []
            for title, column in zip(titles, LAST_COLUMN[source], strict=True):
                grid = properties[title]["gridProperties"]
                if grid["columnCount"] < ord(column) - ord("A") + 1:
                    raise ValueError(f"Source has fewer columns than expected: {source}")
                quoted = "'" + title.replace("'", "''") + "'"
                values = get(session, f"{spreadsheet_url}/values:batchGet", {
                    "ranges": f"{quoted}!A1:{column}{grid['rowCount']}",
                    "valueRenderOption": "UNFORMATTED_VALUE",
                }).json()
                rows = values["valueRanges"][0].get("values", [])
                if not rows:
                    raise ValueError(f"Source tab is empty: {source}")
                ranges.append(f"{quoted}!A1:{column}{len(rows)}")
            cells = get(session, spreadsheet_url, {"ranges": ranges, "fields": GRID_FIELDS}).json()
            if {tab["properties"]["title"] for tab in cells["sheets"]} != set(titles):
                raise ValueError(f"Incomplete cell response: {source}")
            write_json(directory / f"{source}_cells.json", cells)

        after = get(session, file_url, {"fields": FILE_FIELDS, "supportsAllDrives": "true"}).json()
        if before["modifiedTime"] != after.get("modifiedTime"):
            raise ValueError(f"Source changed during the read: {source}")
    write_json(directory / "metadata.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Published source-state.enc")
    parser.add_argument("--output", type=Path, required=True, help="Encrypted review file outside the repository")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(root):
        parser.error("Keep the review file outside the repository.")
    try:
        from google.auth import default, load_credentials_from_dict
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        parser.exit(1, "Install the dependencies from requirements.txt.\n")

    try:
        secret = password()
        previous = decrypt(load(args.state), secret)
        credential_json = os.environ.get("MIA_GOOGLE_CREDENTIALS")
        if credential_json:
            credentials, _ = load_credentials_from_dict(json.loads(credential_json), scopes=SCOPES)
        else:
            credentials, _ = default(scopes=SCOPES)
        with AuthorizedSession(credentials) as session, TemporaryDirectory(prefix="mia-source-") as temporary:
            directory = Path(temporary)
            collect(session, previous, directory)
            current = normalize(directory)
            result = compare_states(previous, current)
        review = {
            "comparison": result, "current": current,
            "previous_pending_changes": previous.get("pending_changes", []),
        }
        save(args.output, encrypt(review, secret, compress=True), plaintext=False)
    except Exception as exc:
        # Google errors can contain private spreadsheet links or values.
        parser.exit(1, f"Source check failed ({type(exc).__name__}).\n")
    print(json.dumps({
        "sources": len(current["sources"]),
        "changes": result["summary"],
        "structure_changes": len(result["structure_changes"]),
        "review_required": result["requires_review"] or any(result["summary"].values())
        or bool(review["previous_pending_changes"]),
    }))


if __name__ == "__main__":
    main()
