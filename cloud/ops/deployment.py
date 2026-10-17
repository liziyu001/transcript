"""ROS lifecycle orchestration with automatic stack discovery and safe data retention."""

import hashlib
import json
import os
import time
import uuid

from alibabacloud_ros20190910 import models
from alibabacloud_ros20190910.client import Client
from alibabacloud_tea_openapi.models import Config as SDKConfig

from .config import ROOT


def redact(message):
    message = str(message)
    for name, value in os.environ.items():
        if value and any(part in name for part in ("SECRET", "TOKEN", "KEY", "PASSWORD")):
            message = message.replace(value, "[REDACTED]")
    if any(marker in message for marker in ("TemplateBody", "PanelToken", "DashScopeKey")):
        message = "Request rejected; inspect ROS events using the request ID."
    return message


def failure_message(exc):
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    message = redact(getattr(exc, "message", "") or str(exc))
    data = getattr(exc, "data", None) or {}
    request_id = data.get("RequestId", "") if isinstance(data, dict) else ""
    return f"{code}: {message[:1500]}\nRequest ID: {request_id or 'unavailable'}"


class Deployment:
    def __init__(self, config, client=None, sleep=time.sleep, monotonic=time.monotonic):
        self.config, self.sleep, self.monotonic = config, sleep, monotonic
        values = config.values
        self.client = client or Client(
            SDKConfig(
                region_id=config.region,
                access_key_id=values["ALIBABA_CLOUD_ACCESS_KEY_ID"],
                access_key_secret=values["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
                security_token=values.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
            )
        )

    def find(self, kind):
        name, page, matches = f"{self.config.prefix}-{kind}", 1, []
        while True:
            result = self.client.list_stacks(
                models.ListStacksRequest(
                    region_id=self.config.region, stack_name=[name], page_number=page, page_size=50
                )
            ).body.to_map()
            rows = result.get("Stacks", [])
            matches.extend(
                s for s in rows if s.get("StackName") == name and s["Status"] != "DELETE_COMPLETE"
            )
            if page * 50 >= result.get("TotalCount", len(rows)):
                break
            page += 1
        if len(matches) > 1:
            raise ValueError(f"Multiple active stacks named {name}; resolve this in ROS first.")
        return self.get(matches[0]["StackId"]) if matches else None

    def get(self, stack_id):
        return self.client.get_stack(
            models.GetStackRequest(region_id=self.config.region, stack_id=stack_id)
        ).body.to_map()

    def wait(self, stack_id, deleting=False, timeout=1800):
        deadline, previous = self.monotonic() + timeout, None
        while self.monotonic() < deadline:
            try:
                result = self.get(stack_id)
            except Exception as exc:
                if deleting and getattr(exc, "code", "") in {"StackNotFound", "StackNotExists"}:
                    return {"Status": "DELETE_COMPLETE"}
                raise
            status = result["Status"]
            if status != previous:
                print(f"{stack_id}: {status}", flush=True)
                previous = status
            if status in (
                {"DELETE_COMPLETE"} if deleting else {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            ):
                return result
            if not status.endswith("_IN_PROGRESS"):
                raise ValueError(
                    f"Stack stopped at {status}. Run python cloudctl.py events; fix the cause and rerun up."
                )
            self.sleep(5)
        raise TimeoutError(
            "ROS is still working. Run status; rerun up after it finishes. No stack was rolled back locally."
        )

    def identity(self, stack):
        params = {p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])}
        for name, expected in [
            ("BucketName", self.config.bucket),
            ("InstanceName", self.config.instance),
        ]:
            if params.get(name) != expected:
                raise ValueError(
                    f"Existing stack {name} differs from configuration. Refusing to replace storage; restore the matching config or choose a new TC_PREFIX."
                )

    def template(self, kind):
        name = "TranscriptStorage" if kind == "storage" else "TranscriptApp"
        value = json.loads((ROOT / "ros.out" / f"{name}.json").read_text(encoding="utf-8"))
        if kind == "storage" and self.config.existing_bucket:
            value["Resources"].pop("Bucket", None)
        return value

    def parameters(self, kind, manifest=None):
        c = self.config
        values = {"BucketName": c.bucket, "InstanceName": c.instance}
        if kind == "app":
            values.update(
                Prefix=c.prefix,
                CodeKey=manifest["object_key"],
                PanelToken=c.values["TC_PANEL_TOKEN"],
                DashScopeKey=c.values["TC_DASHSCOPE_KEY"],
            )
        return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]

    def apply(self, kind, manifest=None):
        existing = self.find(kind)
        if existing:
            self.identity(existing)
            if existing["Status"].endswith("_IN_PROGRESS"):
                existing = self.wait(existing["StackId"])
            if kind == "storage" and existing["Status"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                print("Storage stack already ready; preserving its resources and ownership.")
                return existing
        if kind == "storage" and self.config.existing_bucket:
            info = self.bucket().get_bucket_info()
            if info.location not in {self.config.region, "oss-" + self.config.region}:
                raise ValueError("Existing OSS bucket is in a different region.")
        request = {
            "RegionId": self.config.region,
            "TemplateBody": json.dumps(self.template(kind)),
            "Parameters": self.parameters(kind, manifest),
            "TimeoutInMinutes": 30,
            "ClientToken": uuid.uuid4().hex,
        }
        print(f"Applying {self.config.prefix}-{kind} in {self.config.region}", flush=True)
        if existing:
            request["StackId"] = existing["StackId"]
            if existing["Status"] == "CREATE_FAILED":
                request.pop("ClientToken")
                request.pop("TimeoutInMinutes")
                self.client.continue_create_stack(
                    models.ContinueCreateStackRequest().from_map(request)
                )
            else:
                try:
                    self.client.update_stack(models.UpdateStackRequest().from_map(request))
                except Exception as exc:
                    if getattr(exc, "code", "") not in {
                        "StackNotChanged",
                        "NotSupported.UpdateOperation",
                    }:
                        raise
                    # Only a documented no-op can be accepted; unsupported changes fail.
                    if getattr(exc, "code", "") != "StackNotChanged":
                        raise
            stack_id = existing["StackId"]
        else:
            request.update(StackName=f"{self.config.prefix}-{kind}", DisableRollback=True)
            stack_id = self.client.create_stack(
                models.CreateStackRequest().from_map(request)
            ).body.stack_id
        return self.wait(stack_id)

    def bucket(self):
        import oss2

        c = self.config
        token = c.values.get("ALIBABA_CLOUD_SECURITY_TOKEN")
        auth = (
            oss2.StsAuth(
                c.values["ALIBABA_CLOUD_ACCESS_KEY_ID"],
                c.values["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
                token,
            )
            if token
            else oss2.Auth(
                c.values["ALIBABA_CLOUD_ACCESS_KEY_ID"], c.values["ALIBABA_CLOUD_ACCESS_KEY_SECRET"]
            )
        )
        return oss2.Bucket(auth, f"https://oss-{c.region}.aliyuncs.com", c.bucket)

    def manifest(self):
        manifest = json.loads((ROOT / ".artifacts" / "build.json").read_text(encoding="utf-8"))
        path = ROOT / ".artifacts" / "function.zip"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("ZIP checksum mismatch; run build again.")
        from scripts.build import source_hash

        if manifest.get("source_sha256") != source_hash():
            raise ValueError(
                "Source or dependencies changed since the last build; run build again."
            )
        return manifest

    def upload(self, manifest):
        path = ROOT / ".artifacts" / "function.zip"
        print(f"Uploading immutable package: {manifest['object_key']}", flush=True)
        self.bucket().put_object_from_file(manifest["object_key"], str(path))
        return manifest

    def up(self):
        manifest = self.manifest()
        self.apply("storage")
        self.upload(manifest)
        result = self.apply("app", manifest)
        outputs = {v["OutputKey"]: v["OutputValue"] for v in result.get("Outputs", [])}
        self.config.panel_env(outputs["ApiUrl"])
        print("Cloud ready. Configuration and panel env updated. Run docker compose up -d --build.")

    def down(self):
        app = self.find("app")
        if not app:
            print("Cloud compute is already down. Storage is retained.")
            return
        self.identity(app)
        if app["Status"].endswith("_IN_PROGRESS"):
            if app["Status"] == "DELETE_IN_PROGRESS":
                self.wait(app["StackId"], deleting=True)
                return
            self.wait(app["StackId"])
        self.client.delete_stack(
            models.DeleteStackRequest(region_id=self.config.region, stack_id=app["StackId"])
        )
        self.wait(app["StackId"], deleting=True)
        print("Cloud compute removed. Storage stack, OSS files and Tablestore records retained.")

    def status(self, events=False):
        for kind in ("storage", "app"):
            item = self.find(kind)
            print(f"{self.config.prefix}-{kind}: {item['Status'] if item else 'ABSENT'}")
            if item and events:
                data = self.client.list_stack_events(
                    models.ListStackEventsRequest(
                        region_id=self.config.region, stack_id=item["StackId"]
                    )
                ).body.to_map()
                for entry in data.get("Events", []):
                    print(
                        entry.get("LogicalResourceId"),
                        entry.get("Status"),
                        redact(entry.get("StatusReason", "")),
                    )
