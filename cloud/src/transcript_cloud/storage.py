"""OSS (S3) artifacts and conditional Tablestore (DynamoDB) job records."""

import json
from functools import lru_cache


@lru_cache(maxsize=1)
def credential_client():
    """Reuse the SDK provider and its credential refresh scheduler per process."""
    from alibabacloud_credentials.client import Client

    return Client()


class Conflict(RuntimeError):
    pass


def credentials(context=None):
    if context is not None and hasattr(context, "credentials"):
        c = context.credentials
        return c.access_key_id, c.access_key_secret, c.security_token
    c = credential_client().get_credential()
    return c.access_key_id, c.access_key_secret, c.security_token


def oss_auth(context=None):
    """Refresh SDK credentials between multipart requests during long local uploads."""
    import oss2
    from oss2.credentials import Credentials, CredentialsProvider

    if context is not None:
        # Python 3.12 exposes invocation credentials through environment variables.
        # Resolve them on each invocation; older runtimes expose context.credentials.
        key, secret, token = credentials(context)
        return oss2.StsAuth(key, secret, token)

    class Provider(CredentialsProvider):
        def __init__(self):
            self.client = credential_client()

        def get_credentials(self):
            c = self.client.get_credential()
            return Credentials(c.access_key_id, c.access_key_secret, c.security_token)

    return oss2.ProviderAuth(Provider())


class Records:
    def __init__(self, settings, context=None):
        self.settings, self.context = settings, context

    def client(self):
        import tablestore as ots

        key, secret, token = credentials(self.context)
        s = self.settings
        return ots.OTSClient(
            f"https://{s.ots_instance}.{s.region}.ots.aliyuncs.com",
            key,
            secret,
            s.ots_instance,
            sts_token=token or None,
        )

    @staticmethod
    def decode(row):
        if row is None:
            return None
        cols = {c[0]: c[1] for c in row.attribute_columns}
        value = json.loads(cols["document"])
        value["revision"] = cols["revision"]
        return value

    def get(self, kind, key):
        _, row, _ = self.client().get_row(
            self.settings.table, [("kind", kind), ("id", key)], max_version=1
        )
        return self.decode(row)

    def put(self, kind, key, value, revision=None):
        import tablestore as ots

        new = {**value, "id": key, "revision": (revision or 0) + 1}
        condition = (
            ots.Condition(ots.RowExistenceExpectation.EXPECT_NOT_EXIST)
            if revision is None
            else ots.Condition(
                ots.RowExistenceExpectation.EXPECT_EXIST,
                ots.SingleColumnCondition(
                    "revision", revision, ots.ComparatorType.EQUAL, pass_if_missing=False
                ),
            )
        )
        row = ots.Row(
            [("kind", kind), ("id", key)],
            [("document", json.dumps(new, ensure_ascii=False)), ("revision", new["revision"])],
        )
        try:
            self.client().put_row(self.settings.table, row, condition)
        except ots.OTSServiceError as exc:
            if exc.get_error_code() == "OTSConditionCheckFail":
                raise Conflict("Record changed; refresh and retry") from None
            raise
        return new

    def list(self, kind, cursor=None, limit=100):
        import tablestore as ots

        start = [("kind", kind), ("id", cursor if cursor else ots.INF_MIN)]
        end = [("kind", kind), ("id", ots.INF_MAX)]
        _, next_key, rows, _ = self.client().get_range(
            self.settings.table, ots.Direction.FORWARD, start, end, limit=limit, max_version=1
        )
        return {
            "items": [self.decode(row) for row in rows],
            "cursor": dict(next_key)["id"] if next_key else None,
        }


class Artifacts:
    def __init__(self, settings, context=None):
        self.settings, self.context = settings, context

    def bucket(self):
        import oss2

        return oss2.Bucket(
            oss_auth(self.context),
            f"https://oss-{self.settings.region}.aliyuncs.com",
            self.settings.bucket,
            connect_timeout=30,
        )

    def verify(self, audio):
        if audio.key.split("/")[1] != audio.sha256:
            raise ValueError("Object key and audio hash do not match")
        info = self.bucket().head_object(audio.key)
        if info.content_length != audio.size:
            raise ValueError("Uploaded object size does not match registration")
        if info.headers.get("x-oss-meta-sha256") != audio.sha256:
            raise ValueError("Uploaded object is missing matching SHA-256 metadata")

    def url(self, key, expires=3600, download=False):
        params = (
            {"response-content-disposition": f'attachment; filename="{key.rsplit("/", 1)[-1]}"'}
            if download
            else None
        )
        return self.bucket().sign_url("GET", key, expires, slash_safe=True, params=params)

    def put(self, key, data, content_type="application/json"):
        self.bucket().put_object(key, data.encode("utf-8"), headers={"Content-Type": content_type})

    def put_json(self, key, data):
        self.put(key, json.dumps(data, ensure_ascii=False))

    def get_json(self, key):
        return json.loads(self.bucket().get_object(key).read())


class Flows:
    def __init__(self, settings, context=None):
        self.settings, self.context = settings, context

    def start(self, job):
        from alibabacloud_fnf20190315.client import Client
        from alibabacloud_fnf20190315.models import StartExecutionRequest
        from alibabacloud_tea_openapi.models import Config

        key, secret, token = credentials(self.context)
        s = self.settings
        client = Client(
            Config(
                access_key_id=key,
                access_key_secret=secret,
                security_token=token,
                endpoint=f"{s.region}.fnf.aliyuncs.com",
            )
        )
        name = f"job-{job['id']}-{job['attempt']}"
        try:
            client.start_execution(
                StartExecutionRequest(
                    flow_name=s.flow, execution_name=name, input=json.dumps({"job_id": job["id"]})
                )
            )
        except Exception as exc:
            # Deterministic execution name makes retrying a lost start response safe.
            if getattr(exc, "code", "") not in ("ExecutionAlreadyExists", "ExecutionAlreadyExist"):
                raise
        return name
