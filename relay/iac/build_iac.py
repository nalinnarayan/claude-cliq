#!/usr/bin/env python3
"""Build a Catalyst IaC package from relay/schema.json.

Produces project-template-1.0.0.json plus the function zip, in the layout
`catalyst iac:import` expects. The component shapes here were taken from a
real `catalyst iac:export`, not guessed.

    python3 build_iac.py            # writes dist/claude-cliq-relay.zip
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAY = HERE.parent
DIST = HERE / "dist"

ROLES = ["App Administrator", "App User"]
FUNCTION = "relay"
# 128MB rather than the 256 default: this function is I/O-bound, and memory
# is a straight multiplier on the GB-second bill.
MEMORY = 128

# Set as placeholders so the project imports cleanly; fill them in with real
# values before the relay can talk to Cliq.
ENV_PLACEHOLDERS = {
    "CLIQ_BOT_NAME": "",
    "CLIQ_ZAPIKEY": "",
    "CLIQ_BASE_URL": "https://cliq.zoho.com",
    "RELAY_INBOUND_SECRET": "",
    "DEBUG_USERS": "",
    "POLL_TICK_MS": "3000",
    "STRATUS_BUCKET": "",
}


def datastore_components(schema: dict) -> list[dict]:
    out: list[dict] = []
    for table in schema["tables"]:
        name = table["table_name"]
        dep = [f"Datastore.table.{name}"]
        out.append({"type": "table", "name": name,
                    "properties": {"table_name": name}, "dependsOn": []})
        for col in table["columns"]:
            props = {k: v for k, v in col.items() if not k.startswith("_")}
            # IaC uses real booleans where the REST API takes "true"/"false".
            for flag in ("is_unique", "is_mandatory", "search_index_enabled",
                         "audit_consent"):
                if flag in props:
                    props[flag] = str(props[flag]).lower() == "true"
            props["table_name"] = name
            props["table_id"] = name
            out.append({"type": "column",
                        "name": f"{name}-{col['column_name']}",
                        "properties": props, "dependsOn": dep})
        for role in ROLES:
            out.append({"type": "tableScope", "name": f"{name}-{role}",
                        "properties": {"role_name": role, "type": role,
                                       "table_scope": table.get("table_scope", "GLOBAL"),
                                       "table_name": name},
                        "dependsOn": dep})
            out.append({"type": "tablePermission", "name": f"{name}-{role}",
                        "properties": {"role_name": role, "type": role,
                                       "table_permissions": ["SELECT", "UPDATE",
                                                             "INSERT", "DELETE"],
                                       "table_name": name},
                        "dependsOn": dep})
    return out


def build_function_zip(dest: Path) -> None:
    src = RELAY / "functions" / FUNCTION
    if not (src / "node_modules").exists():
        print("  ! node_modules missing — run `npm install` in "
              f"{src} first, or the deployed function will not start")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(src.rglob("*")):
            if path.is_file() and ".git" not in path.parts:
                z.write(path, path.relative_to(src))


def main() -> int:
    schema = json.loads((RELAY / "schema.json").read_text())
    template = {
        "name": "project-template",
        "version": "1.0.0",
        "parameters": {},
        "components": {
            "Datastore": datastore_components(schema),
            "Functions": [{
                "type": "function", "name": FUNCTION,
                "properties": {
                    "stack": "node20",
                    "code": {"path": f"functions/{FUNCTION}.zip"},
                    "configuration": {
                        "environment": {"variables": dict(ENV_PLACEHOLDERS)},
                        "memory": MEMORY,
                    },
                    "type": "applogic", "name": FUNCTION,
                },
                "dependsOn": [],
            }],
            # Advanced I/O is public by default; this makes that explicit so
            # Cliq can POST in. Our own checks are the bearer token and the
            # shared secret, enforced in code.
            "SecurityRules": [{
                "type": "rules", "name": "security-rules",
                "properties": {"advancedio": {FUNCTION: [
                    {".*": {"methods": ["GET", "POST"],
                            "authentication": "optional"}}
                ]}},
                "dependsOn": [],
            }],
        },
    }

    if DIST.exists():
        shutil.rmtree(DIST)
    staging = DIST / "pkg"
    staging.mkdir(parents=True)
    (staging / "project-template-1.0.0.json").write_text(
        json.dumps(template, indent=2) + "\n")
    build_function_zip(staging / "functions" / f"{FUNCTION}.zip")

    out = DIST / "claude-cliq-relay.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(staging))

    counts: dict[str, int] = {}
    for c in template["components"]["Datastore"]:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    print(f"built {out}")
    print("  Datastore: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"  Functions: {FUNCTION} (node20, {MEMORY}MB, "
          f"{len(ENV_PLACEHOLDERS)} env vars)")
    print("\nCreate a NEW project with everything in one step:")
    print(f"  catalyst iac:import -n claude-cliq-relay {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
