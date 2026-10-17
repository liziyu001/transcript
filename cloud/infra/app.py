"""Pure Python ROS templates. No cloud access, JavaScript runtime or CDK required."""

import argparse
import json
from pathlib import Path


class Resource:
    def __init__(self, stack, name, value):
        self.logical_id = name
        self.value = value
        stack["Resources"][name] = value

    def add_override(self, key, value):
        self.value[key] = value


def new_stack():
    return {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Parameters": {},
        "Resources": {},
        "Outputs": {},
    }


def output(target, name, value):
    target["Outputs"][name] = {"Value": value}


def workflow(worker_arn, name="transcript-parse"):
    return {
        "Type": "StateMachine",
        "Name": name,
        "SpecVersion": "v1",
        "StartAt": "Step",
        "States": [
            {
                "Name": "Step",
                "Type": "Task",
                "Action": "FC:InvokeFunction",
                "TaskMode": "RequestComplete",
                "Timeout": 200,
                "Parameters": {
                    "resourceArn": worker_arn,
                    "invocationType": "Sync",
                    "body": {"job_id.$": "$Input.job_id"},
                },
                "OutputConstructor": {"$": "$Output.Body"},
                "Next": "Finished",
            },
            {
                "Name": "Finished",
                "Type": "Choice",
                "Branches": [{"Condition": "$Input.done == true", "Next": "Done"}],
                "Default": "Wait",
            },
            {"Name": "Wait", "Type": "Wait", "Seconds": 60, "Next": "Step"},
            {"Name": "Done", "Type": "Succeed", "End": True},
        ],
    }


def resource(stack, name, type_, properties, retain=False):
    r = Resource(stack, name, {"Type": type_, "Properties": properties})
    if retain:
        r.add_override("DeletionPolicy", "Retain")
    return r


def param(stack, name, **kwargs):
    names = {
        "default_value": "Default",
        "allowed_pattern": "AllowedPattern",
        "description": "Description",
        "no_echo": "NoEcho",
        "min_length": "MinLength",
    }
    stack["Parameters"][name] = {"Type": "String", **{names[k]: v for k, v in kwargs.items()}}
    return {"Ref": name}


def sub(text):
    return {"Fn::Sub": text}


def role(stack, name, principal, statements):
    return resource(
        stack,
        name,
        "ALIYUN::RAM::Role",
        {
            "RoleName": sub("${Prefix}-" + name.lower()),
            "AssumeRolePolicyDocument": {
                "Version": "1",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"Service": [principal]},
                    }
                ],
            },
            "Policies": [
                {
                    "PolicyName": sub("${Prefix}-" + name.lower() + "-policy"),
                    "PolicyDocument": {"Version": "1", "Statement": statements},
                }
            ],
        },
    )


def statement(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def synth(outdir):
    storage = new_stack()
    bucket = param(storage, "BucketName", allowed_pattern="[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
    instance = param(
        storage, "InstanceName", allowed_pattern="[a-zA-Z][a-zA-Z0-9-]{1,14}[a-zA-Z0-9]"
    )
    resource(
        storage,
        "Bucket",
        "ALIYUN::OSS::Bucket",
        {
            "BucketName": bucket,
            "AccessControl": "private",
            "StorageClass": "Standard",
            "RedundancyType": "LRS",
            "LifecycleConfiguration": {
                "Rule": [
                    {
                        "ID": "abort-incomplete-uploads",
                        "Prefix": "audio/",
                        "Status": "Enabled",
                        "AbortMultipartUpload": {"Days": 7},
                    }
                ]
            },
        },
        retain=True,
    )
    resource(
        storage,
        "Instance",
        "ALIYUN::OTS::Instance",
        {
            "InstanceName": instance,
            "ClusterType": "HYBRID",
            "Network": "NORMAL",
        },
        retain=True,
    )
    table = resource(
        storage,
        "Records",
        "ALIYUN::OTS::Table",
        {
            "InstanceName": {"Fn::GetAtt": ["Instance", "InstanceName"]},
            "TableName": "transcript_records",
            "TimeToLive": -1,
            "MaxVersions": 1,
            "PrimaryKey": [{"Name": "kind", "Type": "STRING"}, {"Name": "id", "Type": "STRING"}],
            "ReservedThroughput": {"Read": 0, "Write": 0},
        },
        retain=True,
    )
    table.add_override("DependsOn", ["Instance"])
    output(storage, "BucketOutput", bucket)
    output(storage, "InstanceOutput", instance)

    stack = new_stack()
    param(stack, "Prefix", default_value="transcript", allowed_pattern="[a-z][a-z0-9-]{2,24}")
    bucket = param(stack, "BucketName")
    instance = param(stack, "InstanceName")
    code_key = param(
        stack, "CodeKey", description="Immutable code ZIP object key from build manifest"
    )
    token = param(stack, "PanelToken", no_echo=True, min_length=24)
    model_key = param(stack, "DashScopeKey", no_echo=True, min_length=1)
    project = resource(
        stack,
        "Logs",
        "ALIYUN::SLS::Project",
        {
            "Name": sub("${Prefix}-${ALIYUN::TenantId}-${ALIYUN::Region}"),
            "Description": "Transcript function logs",
        },
    )
    resource(
        stack,
        "Logstore",
        "ALIYUN::SLS::Logstore",
        {
            "ProjectName": {"Fn::GetAtt": [project.logical_id, "Name"]},
            "LogstoreName": "functions",
            "TTL": 7,
            "ShardCount": 1,
            "AutoSplit": False,
        },
    )
    data_policy = [
        statement(
            ["ots:GetRow", "ots:PutRow", "ots:GetRange"],
            [
                sub(
                    "acs:ots:${ALIYUN::Region}:${ALIYUN::TenantId}:instance/${InstanceName}/table/transcript_records"
                )
            ],
        ),
        statement(
            ["oss:GetObject"],
            [sub("acs:oss:*:*:${BucketName}/audio/*"), sub("acs:oss:*:*:${BucketName}/results/*")],
        ),
        statement(["oss:PutObject"], [sub("acs:oss:*:*:${BucketName}/results/*")]),
        statement(
            ["log:PostLogStoreLogs"],
            [
                sub(
                    "acs:log:${ALIYUN::Region}:${ALIYUN::TenantId}:project/${Prefix}-${ALIYUN::TenantId}-${ALIYUN::Region}/logstore/functions"
                )
            ],
        ),
    ]
    worker_role = role(stack, "WorkerRole", "fc.aliyuncs.com", data_policy)
    api_role = role(
        stack,
        "ApiRole",
        "fc.aliyuncs.com",
        data_policy
        + [
            statement(["oss:GetBucketVersioning"], [sub("acs:oss:*:*:${BucketName}")]),
            statement(
                ["oss:DeleteObject"],
                [
                    sub("acs:oss:*:*:${BucketName}/audio/*"),
                    sub("acs:oss:*:*:${BucketName}/results/*"),
                ],
            ),
            statement(
                ["fnf:StartExecution"],
                # Current StartExecution API documents only All Resource authorization.
                ["*"],
            ),
        ],
    )
    flow_role = role(
        stack,
        "FlowRole",
        "fnf.aliyuncs.com",
        [
            statement(
                ["fc:InvokeFunction"],
                [sub("acs:fc:${ALIYUN::Region}:${ALIYUN::TenantId}:functions/${Prefix}-worker*")],
            ),
        ],
    )
    environment = {
        "TC_REGION": {"Ref": "ALIYUN::Region"},
        "TC_BUCKET": bucket,
        "TC_OTS_INSTANCE": instance,
        "TC_FLOW": sub("${Prefix}-parse"),
        "TC_PANEL_TOKEN": token,
    }
    for name, role_resource, handler in [
        ("Api", api_role, "api"),
        ("Worker", worker_role, "worker"),
    ]:
        env = dict(environment)
        if name == "Worker":
            env["TC_DASHSCOPE_KEY"] = model_key
        function_resource = resource(
            stack,
            name,
            "ALIYUN::FC3::Function",
            {
                "FunctionName": sub("${Prefix}-" + name.lower()),
                "Runtime": "python3.12",
                "Handler": "transcript_cloud.handlers." + handler,
                "Code": {"OssBucketName": bucket, "OssObjectName": code_key},
                "Role": {"Fn::GetAtt": [role_resource.logical_id, "Arn"]},
                "MemorySize": 1024,
                "Cpu": 1,
                "DiskSize": 512,
                "Timeout": 180,
                "InstanceConcurrency": 1,
                "InternetAccess": True,
                "EnvironmentVariables": env,
                "LogConfig": {
                    "Project": {"Fn::GetAtt": [project.logical_id, "Name"]},
                    "Logstore": "functions",
                    "EnableRequestMetrics": False,
                    "EnableInstanceMetrics": False,
                },
            },
        )
        function_resource.add_override("DependsOn", ["Logstore"])
    trigger = resource(
        stack,
        "Http",
        "ALIYUN::FC3::Trigger",
        {
            "FunctionName": {"Fn::GetAtt": ["Api", "FunctionName"]},
            "TriggerName": "http",
            "TriggerType": "http",
            "TriggerConfig": {
                "authType": "anonymous",
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            },
        },
    )
    flow = resource(
        stack,
        "Flow",
        "ALIYUN::FNF::Flow",
        {
            "Name": sub("${Prefix}-parse"),
            "Description": "Durable personal audio transcription and export",
            "ExecutionMode": "Standard",
            "RoleArn": {"Fn::GetAtt": [flow_role.logical_id, "Arn"]},
            "Definition": sub(
                json.dumps(
                    workflow(
                        "acs:fc:${ALIYUN::Region}:${ALIYUN::TenantId}:functions/${Prefix}-worker/LATEST",
                        "${Prefix}-parse",
                    )
                )
            ),
        },
    )
    flow.add_override("DependsOn", ["Worker", "FlowRole"])
    output(stack, "ApiUrl", {"Fn::GetAtt": [trigger.logical_id, "UrlInternet"]})
    output(stack, "FlowName", sub("${Prefix}-parse"))

    outdir.mkdir(parents=True, exist_ok=True)
    for name, template in (("TranscriptStorage", storage), ("TranscriptApp", stack)):
        (outdir / f"{name}.json").write_text(json.dumps(template, indent=2), encoding="utf-8")
    (outdir / "workflow.example.json").write_text(
        json.dumps(
            workflow("acs:fc:cn-beijing:ACCOUNT:functions/transcript-worker/LATEST"), indent=2
        ),
        encoding="utf-8",
    )
    print(f"Synthesized ROS templates into {outdir}. No cloud calls made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "ros.out")
    synth(parser.parse_args().out.resolve())
