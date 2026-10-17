"""Project lifecycle. Normally invoked through python cloudctl.py."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ops.config import ENV_FILE, ROOT, Config, initialize, read_env, write_values
from ops.deployment import Deployment, failure_message


def build(offline=False):
    from infra.app import synth
    from scripts.build import main

    synth(ROOT / "ros.out")
    if offline:
        os.environ["UV_OFFLINE"] = "true"
    main()


def migrate(path):
    values = read_env(path)
    old = ROOT / "deploy.local.json"
    if not old.exists():
        raise ValueError("No legacy deploy.local.json found; use init for new installations.")
    data = json.loads(old.read_text(encoding="utf-8"))
    for key, oldkey in [
        ("TC_REGION", "region"),
        ("TC_BUCKET", "bucket"),
        ("TC_OTS_INSTANCE", "ots_instance"),
    ]:
        if key in values and oldkey in data and values[key] != data[oldkey]:
            raise ValueError(f"Legacy config disagrees on {key}; reconcile it before migration.")
    backup = Path(str(path) + ".before-cleanup")
    if not backup.exists():
        backup.write_bytes(Path(path).read_bytes())
        backup.chmod(0o600)
    updates = {
        "TC_PREFIX": data.get("prefix", "transcript"),
        "TC_EXISTING_BUCKET": str(data.get("existing_bucket", False)).lower(),
    }
    for key, oldkey in [
        ("TC_REGION", "region"),
        ("TC_BUCKET", "bucket"),
        ("TC_OTS_INSTANCE", "ots_instance"),
    ]:
        if oldkey in data:
            updates[key] = data[oldkey]
    write_values(path, updates)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    retired = {"TC_ALLOWED_ORIGIN", "TC_MAX_ACTIVE", "TC_FLOW"}
    Path(path).write_text(
        "\n".join(line for line in lines if line.split("=", 1)[0].strip() not in retired) + "\n",
        encoding="utf-8",
    )
    print("Migrated configuration without rotating credentials. Legacy JSON is no longer read.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "migrate",
            "build",
            "up",
            "deploy",
            "redeploy",
            "down",
            "destroy",
            "status",
            "events",
            "check",
            "plan",
            "test",
        ],
    )
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument(
        "--no-build", action="store_true", help="Use the last built ZIP for up/redeploy"
    )
    parser.add_argument("--offline", action="store_true", help="Build using the uv cache only")
    args = parser.parse_args(argv)
    if args.command == "init":
        return initialize(args.env_file)
    if args.command == "migrate":
        return migrate(args.env_file)
    if args.command == "build":
        return build(args.offline)
    if args.command == "test":
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(ROOT / "tests"),
                str(ROOT.parent / "audio-scraper" / "tests"),
                "-q",
            ],
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(ROOT), str(ROOT.parent / "cloudctl.py")],
            check=True,
        )
        return
    config = Config.load(args.env_file)
    if args.command == "plan":
        print(
            f"Region: {config.region}\nStack prefix: {config.prefix}\nBucket: {config.bucket}\nTablestore: {config.instance}\nExisting bucket: {config.existing_bucket}"
        )
        print(
            "up: ensure storage, upload package, create/update app; down: delete app only, retain all data. No cloud requests made."
        )
        return
    deployment = Deployment(config)
    if args.command in {"up", "deploy", "redeploy"}:
        if not args.no_build:
            build(args.offline)
        return deployment.up()
    if args.command in {"down", "destroy"}:
        return deployment.down()
    if args.command in {"status", "events"}:
        return deployment.status(args.command == "events")
    deployment.status()
    app = deployment.find("app")
    if not app or app["Status"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise ValueError("App stack is not ready; run up first.")
    outputs = {v["OutputKey"]: v["OutputValue"] for v in app.get("Outputs", [])}
    import httpx

    with httpx.Client(base_url=outputs["ApiUrl"], timeout=60) as client:
        if client.get("/api/config").status_code != 401:
            raise ValueError("Cloud API did not reject an unauthenticated request.")
        for path in ("/api/config", "/api/studio/library", "/api/studio/settings"):
            response = client.get(
                path, headers={"Authorization": "Bearer " + config.values["TC_PANEL_TOKEN"]}
            )
            if response.status_code != 200:
                raise ValueError(f"{path}: HTTP {response.status_code}")
            print(path, "OK")
    config.panel_env(outputs["ApiUrl"])
    print("Read-only checks passed; panel env refreshed. No model calls made.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(failure_message(exc)) from None
