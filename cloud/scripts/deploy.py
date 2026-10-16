"""Explicit, opt-in ROS deployment with the official Python SDK.

Without --execute this prints a local plan only; even validation requires --execute.
"""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ros_client(region):
    from alibabacloud_ros20190910.client import Client
    from alibabacloud_tea_openapi.models import Config
    from transcript_cloud.storage import credentials

    key, secret, token = credentials()
    return Client(
        Config(region_id=region, access_key_id=key, access_key_secret=secret, security_token=token)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["storage", "upload", "app", "domain", "status"])
    parser.add_argument("--config", type=Path, default=ROOT / "deploy.local.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--update-stack-id", help="Update an existing stack rather than creating one"
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    region = config.get("region", "cn-beijing")
    prefix = config.get("prefix", "transcript")
    if args.stage == "upload":
        manifest = json.loads((ROOT / ".artifacts" / "build.json").read_text())
        print(f"Upload code ZIP to oss://{config['bucket']}/{manifest['object_key']}")
        if args.execute:
            import hashlib

            import oss2
            from transcript_cloud.storage import credentials

            path = Path(manifest["file"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
                raise SystemExit("Build ZIP checksum changed; rebuild before uploading")
            key, secret, token = credentials()
            auth = oss2.StsAuth(key, secret, token) if token else oss2.Auth(key, secret)
            oss2.Bucket(
                auth, f"https://oss-{region}.aliyuncs.com", config["bucket"]
            ).put_object_from_file(manifest["object_key"], str(path))
        return
    if args.stage == "status":
        if not args.update_stack_id:
            parser.error("status requires --update-stack-id")
        if args.execute:
            from alibabacloud_ros20190910.models import GetStackRequest

            result = (
                ros_client(region)
                .get_stack(GetStackRequest(region_id=region, stack_id=args.update_stack_id))
                .body.to_map()
            )
            print(
                json.dumps({k: result.get(k) for k in ("StackId", "Status", "Outputs")}, indent=2)
            )
        else:
            print("Would query ROS stack status:", args.update_stack_id)
        return
    name = {"storage": "TranscriptStorage", "app": "TranscriptApp", "domain": "TranscriptDomain"}[
        args.stage
    ]
    template = ROOT / "ros.out" / f"{name}.json"
    if not template.exists():
        raise SystemExit("Synthesize ROS templates first")
    params = {"BucketName": config["bucket"], "InstanceName": config["ots_instance"]}
    if args.stage == "app":
        manifest = json.loads((ROOT / ".artifacts" / "build.json").read_text())
        params.update(
            Prefix=prefix,
            CodeKey=manifest["object_key"],
            AllowedOrigin=config.get("allowed_origin", "http://localhost:8787"),
            MaxActive=str(config.get("max_active", 4)),
        )
        if args.execute:
            params.update(
                PanelToken=os.environ["TC_PANEL_TOKEN"], DashScopeKey=os.environ["TC_DASHSCOPE_KEY"]
            )
    elif args.stage == "domain":
        params = {"DomainName": config["domain"], "FunctionName": prefix + "-api"}
        if args.execute:
            params.update(
                Certificate=Path(config["certificate_file"]).read_text(),
                PrivateKey=Path(config["private_key_file"]).read_text(),
            )
    action = "UpdateStack" if args.update_stack_id else "CreateStack"
    print(f"{action}: {prefix}-{args.stage} in {region}; template {template}")
    if not args.execute:
        print(
            "Plan only. No credentials read and no cloud requests made. Add --execute when ready."
        )
        return
    request = {
        "RegionId": region,
        "TemplateBody": template.read_text(encoding="utf-8"),
        "Parameters": [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()],
        "TimeoutInMinutes": 30,
    }
    if args.update_stack_id:
        request["StackId"] = args.update_stack_id
    else:
        request["StackName"] = f"{prefix}-{args.stage}"
    # Structured SDK requests keep secrets out of command-line arguments and local files.
    import hashlib

    from alibabacloud_ros20190910 import models

    request["ClientToken"] = hashlib.sha256(
        json.dumps(request, sort_keys=True).encode()
    ).hexdigest()
    client = ros_client(region)
    if args.update_stack_id:
        result = client.update_stack(models.UpdateStackRequest().from_map(request))
    else:
        result = client.create_stack(models.CreateStackRequest().from_map(request))
    print(json.dumps({"StackId": result.body.stack_id}, indent=2))
    print(
        "Save the returned StackId. Wait for CREATE_COMPLETE/UPDATE_COMPLETE before the next stage."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(
            f"Deployment command failed ({type(exc).__name__}). "
            "Check configuration/credentials and ROS stack events."
        ) from None
