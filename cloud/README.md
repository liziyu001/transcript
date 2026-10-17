# Transcript Cloud

Personal transcription with **local scraping/upload** and **Alibaba Cloud processing**.
The original local studio on `master` remains independent. This package extracts the
DashScope result contract and subtitle exporters; it does not install or import Whisper.

Deployment is explicit through `--execute`. The Beijing deployment has been verified
with authenticated API requests, browser CORS, a synthetic CloudFlow export, and signed
downloads of JSON/TXT/SRT/VTT. No paid recognition was used for those checks; verify the
model submission/polling path with one short recording before a large batch.

## Architecture

| Responsibility | Service | AWS equivalent |
| --- | --- | --- |
| Audio, raw results, exports, function ZIP | OSS | S3 |
| Short worker invocations and HTTP API | Function Compute 3 | Lambda |
| Durable wait/check loop | CloudFlow Standard | Step Functions Standard |
| Audio metadata, jobs, concurrency slots | Tablestore | DynamoDB |
| Seven-day function logs | Simple Log Service | CloudWatch Logs |
| Roles and temporary credentials | RAM / STS | IAM / STS |
| Infrastructure | ROS / Python ROS CDK | CloudFormation / CDK |

The local CLI downloads one video at a time, uploads its parts, and registers each
completed upload. With `--start`, recognition begins while subsequent videos are
downloaded. Scraping and uploading are sequential in this first version; each file
uses three concurrent upload parts. No scraping runs in Alibaba's datacenter.

The panel lists the cloud library, starts selected recordings, shows job status,
downloads TXT/SRT/VTT/JSON/raw results, retries saved work, and re-exports without
another model request. Closing the panel or turning off the computer has no effect
on registered cloud jobs. The panel does not remotely start the local scraper.

CloudFlow invokes a short worker, waits 60 seconds, and invokes it again. Workers
persist task IDs before waiting. Tablestore conditional writes prevent two workers
from advancing the same job concurrently. Four durable slots limit **remote model
jobs**, independently of function invocation concurrency. The default wait limit is
24 hours per job, including time queued for a slot.

## Local setup and checks

Run from the repository root:

```powershell
uv sync --project cloud --extra dev --extra ingest --extra infra
uv run --project cloud --no-sync python -m pytest cloud/tests -q
uv run --project cloud --no-sync ruff check --config cloud/pyproject.toml cloud
npm run cloud:check
```

`cloud/uv.lock` is separate from the working local application's lockfile. `ingest`
adds the existing standalone scraper; `infra` adds ROS CDK and the deployment SDK.
Use `--no-sync` for the commands below after installing those extras. `uv sync`
without the extras removes them from the environment.

Python 3.12 and Node are used for packaging and ROS synthesis. FFmpeg/ffprobe must
be on PATH for local scraping and audio validation. There is no npm install step:
the panel uses browser APIs and the preview server uses Node built-ins.

Preview the panel without credentials or cloud resources:

```powershell
npm run cloud:panel
# http://localhost:8787 — displays the disconnected panel
```

After deployment, enter the API URL and personal token. Only the URL is saved in
browser storage; the token remains in page memory. The API also serves these static
assets for the optional hosted-panel configuration below.

## Build and review infrastructure locally

```powershell
uv run --project cloud --no-sync python cloud/infra/app.py
uv run --project cloud --no-sync python cloud/scripts/build.py
uv run --project cloud --no-sync python cloud/scripts/deploy.py storage --config cloud/deploy.example.json
uv run --project cloud --no-sync python cloud/scripts/deploy.py app --config cloud/deploy.example.json
```

These commands **do not deploy**. Build downloads Python packages if needed. They produce:

```text
cloud/ros.out/TranscriptStorage.json   OSS bucket + Tablestore instance/table
cloud/ros.out/TranscriptApp.json       roles, FC API/worker, HTTP trigger, CloudFlow, logs
cloud/ros.out/TranscriptDomain.json    optional HTTPS domain for the hosted panel
cloud/ros.out/workflow.example.json    readable workflow definition
cloud/.artifacts/function.zip         Linux x86_64 Python 3.12 code/dependencies
cloud/.artifacts/build.json           SHA-256 and immutable code object key
```

ROS code uses the public template-only CDK synthesis API; deployment uses the ROS
Python SDK. This avoids the CDK asset-manifest path, which failed with the bundled
schema validator on the installed Node 24. CDK may print an untested-Node warning.
No CDK bootstrap bucket or global CDK CLI is required.

The ZIP contains Linux wheels, excludes all local scraper/model packages, and
rejects Windows binaries. It uses `oss2`/`crcmod` supplied by FC's Python 3.12
runtime; all other runtime dependencies are packaged from the lockfile. Local
packaging does not execute Linux native modules on Windows. Verify imports during
the first cloud smoke test.

## Deploy later

Install/configure the Alibaba CLI when ready if you want it for account inspection.
The provided deployment script uses official Python SDKs, so the CLI is optional.
The portable credential configuration for both the scripts and local uploader is:

If you saved credentials in ignored `cloud/.env.local`, load it explicitly on
every deployment command: `uv run --env-file cloud/.env.local --project cloud --no-sync python ...`.
Plain `uv run` does not load that file. This also loads `TC_PANEL_TOKEN` and
`TC_DASHSCOPE_KEY` for the application deployment.

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = 'your-RAM-access-key-id'
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = 'your-RAM-access-key-secret'
# If these are temporary credentials:
$env:ALIBABA_CLOUD_SECURITY_TOKEN = 'your-session-token'
```

Use Beijing for this first release and a Beijing DashScope key. Enable OSS, FC,
CloudFlow, Tablestore, SLS, and Model Studio in the account before deploying. The
deployer needs permission to create these resources and their RAM execution roles.
Runtime roles are generated by ROS and scoped to the bucket paths/table/logstore.
`fnf:StartExecution` uses `Resource: "*"` because its current API authorization
reference specifies All Resource; that role receives only this one CloudFlow action.

1. Copy `cloud/deploy.example.json` to ignored `cloud/deploy.local.json`. Set a globally
   unique bucket, a 3–16-character lowercase Tablestore instance name, and a prefix.
2. Set `TC_PANEL_TOKEN` to a random secret of at least 24 characters and
   `TC_DASHSCOPE_KEY` to the Beijing model key. Generate a token with
   `uv run --project cloud --no-sync python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Synthesize and build with the commands above.
   If you already created the bucket, set `"existing_bucket": true` in
   `deploy.local.json`. The script verifies access and region, then leaves that
   bucket outside ROS management; its existing settings and lifecycle are retained.
   Keep this setting unchanged on later storage stack updates.
   Use an instance name such as `transcript-home` (at most 16 characters).

   Run `uv run --project cloud --no-sync python cloud/scripts/deploy.py check --execute`
   to check local secret presence and perform a read-only ROS stack/credential check.
   Variables set in another terminal are not inherited by this terminal or Codex.
   Deployment failures now include the SDK error code and request ID with known
   credentials redacted.
4. Deploy storage, wait for completion, upload the code ZIP, then deploy the application:

```powershell
# These commands intentionally make cloud changes only when --execute is present.
uv run --project cloud --no-sync python cloud/scripts/deploy.py storage --execute
# Save StackId, then check until CREATE_COMPLETE:
uv run --project cloud --no-sync python cloud/scripts/deploy.py status --update-stack-id STORAGE_STACK_ID --execute

uv run --project cloud --no-sync python cloud/scripts/deploy.py upload --execute
uv run --project cloud --no-sync python cloud/scripts/deploy.py app --execute
uv run --project cloud --no-sync python cloud/scripts/deploy.py status --update-stack-id APP_STACK_ID --execute
```

The application's `ApiUrl` output is used by the CLI and panel. For later releases,
rebuild, upload, then use `app --update-stack-id APP_STACK_ID --execute`. The SHA-256
code key ensures that a changed ZIP changes the function resource. Keep the stack
IDs; don't create a second stack over the same named resources. Repeating an
identical deployment request uses the same idempotency token.

New stacks preserve resources on creation failure. After fixing the cause, use
`app --continue-stack-id STACK_ID --execute` to resume a `CREATE_FAILED` stack.
For completed stacks, continue using `--update-stack-id`. Inspect `events` and
`resources` with `--update-stack-id STACK_ID --execute` before recovery. A timed-out
FC create request can leave an orphan function even when ROS recorded no resource ID;
check the FC console before recreating a stack with the same function names.

After deployment, check the API without invoking a model:

```powershell
uv run --env-file cloud/.env.local --project cloud --no-sync python cloud/scripts/smoke.py --url YOUR_API_URL
```

The runtime package includes compatible PyOpenSSL alongside cryptography to avoid
mixing with FC's older bundled OpenSSL library. Python 3.12 credentials are resolved
through the SDK provider rather than assuming `context.credentials` exists.

The storage resources have ROS `DeletionPolicy: Retain`. Replacing or deleting the
application stack does not delete recordings/results. There is no teardown command
in this project. Old audio, exports, and code ZIPs remain until explicitly removed;
incomplete audio multipart uploads expire after seven days.

### Hosted control panel

FC's generated HTTP domain may force HTML responses to download. Its API still
works with `npm run cloud:panel`. To serve the panel entirely from Alibaba, use the
optional `TranscriptDomain` stack with your HTTPS domain and certificate:

```powershell
uv run --project cloud --no-sync python cloud/scripts/deploy.py domain --execute
```

Set `domain`, `certificate_file`, and `private_key_file` in your local deployment
config first, then configure the domain's DNS CNAME to FC's prescribed target.
A mainland-hosted custom domain must satisfy Alibaba's domain/filing prerequisites.
Use the custom HTTPS URL as the panel API endpoint. Same-origin API calls need no
additional CORS entry. `allowed_origin` controls the one separate local-panel origin.

## Local ingestion

Use a separate uploader RAM identity with only the audio-prefix OSS permissions
shown in `infra/uploader-policy.example.json`. It does not need model, Tablestore,
or workflow permissions. Library registration uses the authenticated API.

```powershell
$env:TC_API_URL = 'https://your-api-endpoint'
$env:TC_PANEL_TOKEN = 'the-same-personal-token'

# Existing audio files; register only, then start from the panel:
uv run --project cloud --no-sync transcript-ingest --files 'D:\audio\part1.m4a' 'D:\audio\part2.m4a'

# Scrape locally from a text file containing one video URL per line:
uv run --project cloud --no-sync transcript-ingest --urls-file urls.txt --start

# Or a Bilibili collection:
uv run --project cloud --no-sync transcript-ingest --collection 'https://space.bilibili.com/USER/lists/ID?type=series' --start --model paraformer-v2
```

Uploads use SHA-256 object keys, multipart checkpoints, and per-file manifests in
`data/cloud-ingest`. Rerun the same command to resume. Already completed remote
uploads are verified and reused even if the local manifest is lost. `--start`
persists a model/settings-specific request ID **before** calling the API, so a lost
response can be replayed without creating a second job. Removing manifests removes
that submission identity; use the panel to inspect existing jobs first.

The SDK refreshes renewable credentials between multipart requests. Static STS
environment credentials cannot renew themselves; if they expire, provide fresh
credentials and rerun. Each file must be at most 2 GB / 12 hours. ffprobe checks
duration before upload. Automatic splitting is not included.

## Recovery and operational boundaries

- Transient polling/export errors get up to three consecutive attempts. Afterwards,
  **Retry saved work** continues with the saved task ID or raw result.
- A failed workflow invocation outside application code can be restarted using
  **Resume stalled workflow** after the five-minute worker lease expires.
- A crash or timeout during submission has an uncertain outcome. It becomes
  **needs attention** and never automatically resubmits. Recover the task ID from
  DashScope, or verify the remote state and explicitly **Dismiss / release slot**.
- Dismissing does not cancel or refund a provider task. Unresolved jobs retain a
  concurrency slot until recovered or dismissed. A new panel submission is explicitly
  new paid work; retrying a provider-declared failed task does not create a replacement.
- Raw provider results are copied to your OSS bucket promptly. Export revisions
  are immutable by attempt number. Failed re-export leaves the old output keys usable.
- The signed audio URL requests a 24-hour lifetime, but its effective lifetime is
  capped by the FC role's STS credentials. Validate URL fetching/queue delay in the
  first smoke test. Download links are regenerated on demand and request 15 minutes.
- Model settings and UI token are deployment configuration. Keys are NoEcho ROS
  parameters and FC environment variables, never browser responses or job snapshots.
  This personal version does not add a paid KMS secret service or multi-user accounts.
- The app uses no VPC/NAT gateway or provisioned FC instances. OSS public URL reads
  may incur egress fees even when the model runs on Alibaba. Logs retain seven days.

For local backend debugging against already-created cloud resources, populate the
`TC_*` values in `.env.example` and run `uv run --project cloud --no-sync transcript-cloud`.
It binds to `127.0.0.1:8787`; do not run it alongside the Node preview on that port.

## First deployment smoke test

Use one short audio file: upload/register it, start one job, close the panel, and
return to verify raw JSON plus TXT/SRT/VTT/JSON. Re-export and confirm the provider
task ID stays unchanged. Then test a second recording with concurrency set to one.
This small test checks live RAM policies, FC imports, CloudFlow FDL, OSS URL access,
and the real provider response. Offline tests cannot establish those account-level
contracts. No large end-to-end suite is required.

## Implementation map

```text
src/transcript_cloud/models.py      API/config contracts
src/transcript_cloud/provider.py    API-only recognition and result normalization
src/transcript_cloud/exporter.py    Pure TXT/SRT/VTT/JSON rendering
src/transcript_cloud/storage.py     OSS, Tablestore, CloudFlow SDK adapters
src/transcript_cloud/service.py     Job state, leases, concurrency and recovery
src/transcript_cloud/handlers.py    FC worker and HTTP-to-ASGI adapter
src/transcript_cloud/api.py         Authenticated API and static panel
src/transcript_cloud/ingest.py      Local downloader/uploader CLI
infra/app.py                       Python ROS CDK source
scripts/build.py                   Cross-platform Linux function packaging
scripts/deploy.py                  Plan/create/update/status using the ROS SDK
tests/                            Focused offline contracts and recovery tests
```

References: [ROS CDK](https://www.alibabacloud.com/help/en/ros/developer-reference/overview-8),
[CloudFlow polling](https://help.aliyun.com/zh/document_detail/2862582.html),
[FC HTTP event contract](https://help.aliyun.com/zh/functioncompute/http-trigger-invoking-function),
[FC Python runtimes](https://help.aliyun.com/zh/functioncompute/python3/),
[FC custom domains](https://help.aliyun.com/zh/ros/developer-reference/aliyun-fc3-customdomain),
[StartExecution authorization](https://help.aliyun.com/en/document_detail/2590640.html).
