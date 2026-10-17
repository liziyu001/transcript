"""Explicit, opt-in ROS deployment with the official Python SDK.

Without --execute this prints a local plan only; even validation requires --execute.
"""

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def failure_message(exc):
    """Report useful SDK diagnostics without dumping requests or credentials."""
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    message = str(getattr(exc, "message", "") or str(exc))
    secrets = [
        os.getenv(name)
        for name in (
            "ALIBABA_CLOUD_ACCESS_KEY_ID",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
            "ALIBABA_CLOUD_SECURITY_TOKEN",
            "TC_PANEL_TOKEN",
            "TC_DASHSCOPE_KEY",
        )
    ]
    try:
        from transcript_cloud.storage import credentials

        secrets.extend(credentials())
    except Exception:
        pass
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    # SDK messages may embed whole request bodies; never emit those.
    if any(marker in message for marker in ("TemplateBody", "PanelToken", "DashScopeKey")):
        message = "Request rejected; inspect ROS stack events using the request ID."
    data = getattr(exc, "data", None) or {}
    request_id = data.get("RequestId", "") if isinstance(data, dict) else ""
    return (
        f"Deployment failed [{code}]: {message[:1500]}\nRequest ID: {request_id or 'unavailable'}"
    )


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
    parser.add_argument(
        "stage",
        choices=["check", "events", "resources", "storage", "upload", "app", "domain", "status"],
    )
    parser.add_argument("--config", type=Path, default=ROOT / "deploy.local.json")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--update-stack-id", help="Update an existing stack rather than creating one"
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    region = config.get("region", "cn-beijing")
    prefix = config.get("prefix", "transcript")
    if args.stage == "resources":
        if not args.update_stack_id:
            parser.error("resources requires --update-stack-id")
        if args.execute:
            from alibabacloud_ros20190910.models import ListStackResourcesRequest

            result = (
                ros_client(region)
                .list_stack_resources(
                    ListStackResourcesRequest(region_id=region, stack_id=args.update_stack_id)
                )
                .body.to_map()
            )
            for item in result.get("Resources", []):
                print(
                    json.dumps(
                        {
                            k: item.get(k)
                            for k in (
                                "LogicalResourceId",
                                "PhysicalResourceId",
                                "ResourceType",
                                "Status",
                            )
                        }
                    )
                )
        else:
            print("Would read ROS stack resources:", args.update_stack_id)
        return
    if args.stage == "events":
        if not args.update_stack_id:
            parser.error("events requires --update-stack-id")
        if args.execute:
            from alibabacloud_ros20190910.models import ListStackEventsRequest

            result = (
                ros_client(region)
                .list_stack_events(
                    ListStackEventsRequest(region_id=region, stack_id=args.update_stack_id)
                )
                .body.to_map()
            )
            for event in result.get("Events", []):
                print(
                    json.dumps(
                        {k: event.get(k) for k in ("LogicalResourceId", "Status", "StatusReason")}
                    )
                )
        else:
            print("Would read ROS stack events:", args.update_stack_id)
        return
    if args.stage == "check":
        instance = config.get("ots_instance", "")
        print(
            "Tablestore name length:",
            "OK" if 3 <= len(instance) <= 16 else "INVALID (must be 3-16)",
        )
        for name in ("TC_PANEL_TOKEN", "TC_DASHSCOPE_KEY"):
            print(name + ":", "set" if os.getenv(name) else "missing in this process")
        if args.execute:
            from alibabacloud_ros20190910.models import ListStacksRequest

            result = (
                ros_client(region)
                .list_stacks(
                    ListStacksRequest(
                        region_id=region, stack_name=[prefix + "-storage", prefix + "-app"]
                    )
                )
                .body.to_map()
            )
            print(json.dumps({"Stacks": result.get("Stacks", [])}, indent=2))
        else:
            print("Local check only; add --execute for a read-only ROS credential/stack check.")
        return
    if args.stage in ("storage", "app") and not re.fullmatch(
        r"[a-zA-Z][a-zA-Z0-9-]{1,14}[a-zA-Z0-9]", config.get("ots_instance", "")
    ):
        raise SystemExit(
            "ots_instance must be 3-16 characters, start with a letter, and end "
            "with a letter or digit; only letters, digits and hyphens are allowed."
        )
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
                json.dumps(
                    {k: result.get(k) for k in ("StackId", "Status", "StatusReason", "Outputs")},
                    indent=2,
                )
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
    template_body = json.loads(template.read_text(encoding="utf-8"))
    if args.stage == "storage" and config.get("existing_bucket", False):
        # The user-owned bucket remains outside this stack and is never modified by ROS.
        import oss2

        from transcript_cloud.storage import oss_auth

        info = oss2.Bucket(
            oss_auth(), f"https://oss-{region}.aliyuncs.com", config["bucket"]
        ).get_bucket_info()
        if info.location not in (region, "oss-" + region):
            raise ValueError(f"Existing bucket is in {info.location}, expected oss-{region}")
        template_body["Resources"].pop("Bucket", None)
        print("Reusing existing bucket; its settings and lifecycle remain externally managed.")
    request = {
        "RegionId": region,
        "TemplateBody": json.dumps(template_body),
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
        raise SystemExit(failure_message(exc)) from None
