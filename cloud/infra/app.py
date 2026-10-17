"""ROS CDK (CloudFormation/CDK): synthesize locally; never deploy on import/run."""

import argparse
import json
from pathlib import Path

import jsii
import ros_cdk_core as ros
import ros_cdk_cxapi as cxapi


@jsii.implements(ros.ISynthesisSession)
class TemplateSession:
    """Emit ROS templates only; deployment uses ROS CLI, not the CDK asset manifest."""

    def __init__(self, outdir):
        self._outdir = str(outdir)
        self._assembly = cxapi.CloudAssemblyBuilder(self._outdir)

    @property
    def outdir(self):
        return self._outdir

    @property
    def assembly(self):
        return self._assembly

    @property
    def validate_on_synth(self):
        return True


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
    r = ros.RosResource(stack, name, type=type_, properties=properties)
    r.override_logical_id(name)
    if retain:
        r.add_override("DeletionPolicy", "Retain")
    return r


def param(stack, name, **kwargs):
    kwargs.setdefault("type", ros.RosParameterType.STRING)
    p = ros.RosParameter(stack, name, **kwargs)
    p.override_logical_id(name)
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
    app = ros.App(outdir=str(outdir / "assembly"), auto_synth=False)
    storage = ros.Stack(app, "TranscriptStorage")
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
    ros.RosOutput(storage, "BucketOutput", value=bucket)
    ros.RosOutput(storage, "InstanceOutput", value=instance)

    stack = ros.Stack(app, "TranscriptApp")
    param(stack, "Prefix", default_value="transcript", allowed_pattern="[a-z][a-z0-9-]{2,24}")
    bucket = param(stack, "BucketName")
    instance = param(stack, "InstanceName")
    code_key = param(
        stack, "CodeKey", description="Immutable code ZIP object key from build manifest"
    )
    token = param(stack, "PanelToken", no_echo=True, min_length=24)
    model_key = param(stack, "DashScopeKey", no_echo=True, min_length=1)
    origin = param(stack, "AllowedOrigin", default_value="http://localhost:8787")
    maximum = param(
        stack,
        "MaxActive",
        type=ros.RosParameterType.NUMBER,
        default_value=4,
        min_value=1,
        max_value=16,
    )
    project = resource(
        stack,
        "Logs",
        "ALIYUN::SLS::Project",
        {
            "Name": sub("${Prefix}-${ALIYUN::AccountId}-${ALIYUN::Region}"),
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
                    "acs:ots:${ALIYUN::Region}:${ALIYUN::AccountId}:instance/${InstanceName}/table/transcript_records"
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
                    "acs:log:${ALIYUN::Region}:${ALIYUN::AccountId}:project/${Prefix}-${ALIYUN::AccountId}-${ALIYUN::Region}/logstore/functions"
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
                [sub("acs:fc:${ALIYUN::Region}:${ALIYUN::AccountId}:functions/${Prefix}-worker*")],
            ),
        ],
    )
    environment = {
        "TC_REGION": {"Ref": "ALIYUN::Region"},
        "TC_BUCKET": bucket,
        "TC_OTS_INSTANCE": instance,
        "TC_FLOW": sub("${Prefix}-parse"),
        "TC_PANEL_TOKEN": token,
        "TC_ALLOWED_ORIGIN": origin,
        "TC_MAX_ACTIVE": {"Fn::Join": ["", [maximum]]},
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
            "TriggerConfig": {"authType": "anonymous", "methods": ["GET", "POST", "OPTIONS"]},
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
                        "acs:fc:${ALIYUN::Region}:${ALIYUN::AccountId}:functions/${Prefix}-worker/LATEST",
                        "${Prefix}-parse",
                    )
                )
            ),
        },
    )
    flow.add_override("DependsOn", ["Worker", "FlowRole"])
    ros.RosOutput(stack, "ApiUrl", value={"Fn::GetAtt": [trigger.logical_id, "UrlInternet"]})
    ros.RosOutput(stack, "FlowName", value=sub("${Prefix}-parse"))

    # Optional hosted panel. A custom HTTPS domain avoids attachment-only HTML on FC default URLs.
    domain_stack = ros.Stack(app, "TranscriptDomain")
    domain = param(domain_stack, "DomainName")
    function = param(domain_stack, "FunctionName")
    certificate = param(domain_stack, "Certificate", no_echo=True)
    private_key = param(domain_stack, "PrivateKey", no_echo=True)
    resource(
        domain_stack,
        "Domain",
        "ALIYUN::FC3::CustomDomain",
        {
            "DomainName": domain,
            "Protocol": "HTTPS",
            "AuthConfig": {"AuthType": "anonymous"},
            "CertConfig": {
                "CertName": "transcript",
                "Certificate": certificate,
                "PrivateKey": private_key,
            },
            "RouteConfig": {
                "Routes": [
                    {
                        "Path": "/*",
                        "FunctionName": function,
                        "Qualifier": "LATEST",
                        "Methods": ["GET", "POST", "OPTIONS"],
                    }
                ]
            },
        },
    )
    outdir.mkdir(parents=True, exist_ok=True)
    session = TemplateSession(outdir / "assembly")
    for item in (storage, stack, domain_stack):
        item.synthesize(session)
        name = item.stack_name
        template = json.loads((outdir / "assembly" / item.template_file).read_text())
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
