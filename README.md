# Transcript Studio

A Chinese Web UI running locally in Docker Compose, controlling an Alibaba Cloud serverless parser. Collections and records come from cloud metadata; videos are downloaded and uploaded locally; parsing runs in the cloud. Audio and results live in OSS. No hosted website or local Whisper installation is needed.

## New developer setup

Install **Python 3.12**, **uv**, and **Docker with Compose v2**. Run commands from the repository root in PowerShell or bash. The launcher installs locked dependencies into `cloud/.venv`. Node.js and Alibaba CLI/CDK are not required for deployment.

```console
python cloudctl.py init
```

Edit **`cloud/.env.local`**. Supply five values:

| Key | Value |
| --- | --- |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | Alibaba account/RAM access key ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | Matching access key secret |
| `TC_DASHSCOPE_KEY` | DashScope API key for transcription |
| `TC_BUCKET` | Globally unique OSS bucket name |
| `TC_OTS_INSTANCE` | New Tablestore instance name |

`init` generates `TC_PANEL_TOKEN`. Defaults: `TC_REGION=cn-beijing`, `TC_PREFIX=transcript`. To use an existing bucket in this region, set `TC_EXISTING_BUCKET=true`; otherwise deployment creates it. Existing Tablestore instances cannot be adopted automatically. For temporary STS credentials, also fill `ALIBABA_CLOUD_SECURITY_TOKEN` and refresh it before expiry.

Enable OSS, Tablestore, Function Compute, CloudFlow, SLS, ROS and DashScope in your Alibaba account. The deployment identity needs permission to manage the resources in `cloud/infra/app.py`, including RAM roles/policies and role assignment, and upload code/audio to OSS. The shipped model/endpoints target Beijing. Changing the region alone does not migrate data or make a model available elsewhere.

```console
python cloudctl.py up
docker compose up -d --build
```

Open **http://localhost:8787**. `up` builds the Linux function ZIP, discovers/creates the two ROS stacks, uploads code, deploys compute, waits for completion, and writes the API URL back to the env file. It generates `cloud/.panel.env` for Compose; do not edit that derived file.

Credentials come directly from the env file: no temporary shell variables, CLI login, stack IDs or manually copied URLs. Quote values containing spaces or `#`; `$` is literal. Keep both env files private and out of Git. The panel receives OSS credentials and its API token, but not the DashScope key.

## Everyday commands

| Command | Effect |
| --- | --- |
| `python cloudctl.py up` | Build and create/update compute; ensure storage exists |
| `python cloudctl.py redeploy` | Same as `up`; update code/config in place |
| `python cloudctl.py build` | Build templates and Linux ZIP, without cloud calls |
| `python cloudctl.py up --no-build` | Deploy last build; reject changed source/dependencies |
| `python cloudctl.py status` | Show discovered stack status |
| `python cloudctl.py events` | Show recent ROS deployment events |
| `python cloudctl.py check` | Check API auth/metadata and refresh panel env; no model call |
| `python cloudctl.py plan` | Validate config and summarize targets offline; not a ROS change set |
| `python cloudctl.py down` | Remove app stack: functions, workflow, roles and logs |
| `python cloudctl.py destroy` | Alias of `down`, with the same data retention |
| `docker compose down` | Stop local panel; preserve its volume |

**`down`/`destroy` retain the storage stack, OSS objects and Tablestore records. Storage charges can continue.** `up` reuses storage and recreates compute. Let active jobs finish before taking cloud compute down. Permanent erasure requires explicit deletion of the retained bucket/Tablestore resources through Alibaba Cloud; there is no automated data-purge command. Existing buckets are never taken over or deleted.

After changing code or cloud secrets:

```console
python cloudctl.py redeploy
docker compose up -d --build
python cloudctl.py check
```

For separate build/deploy steps, run `build` then `up --no-build`. `--offline` lets the package build use a populated uv cache; deployment still needs network access. Commands accept `--env-file PATH`. Compose uses the most recently generated panel env; use one target at a time per checkout. Run only one lifecycle operation at a time.

Keep `TC_PREFIX`, `TC_BUCKET`, `TC_OTS_INSTANCE` and `TC_REGION` stable. Changing them is not a rename/migration. The command refuses to update a discovered stack pointing at different storage.

## Development and troubleshooting

```console
python cloudctl.py test
docker compose logs -f cloud-studio
python cloudctl.py events
```

`test` runs Python regression suites and Ruff. With Node installed, `npm run check` checks browser JavaScript syntax. Function logs are in SLS project `<prefix>-<account-id>-<region>`, logstore `functions`. Panel task details show local stage logs and cloud request IDs. Failed ROS creates can be resumed by fixing the cause and rerunning `up`; inspect events before retrying other failed states.

The Compose volume `transcript_cloud-studio-data` stores the local queue, logs, settings and retry state. Avoid `docker compose down -v` unless intentionally resetting these. Successfully uploaded and registered audio is removed locally; incomplete uploads may retain files for retry. Use panel OSS cleanup actions to reclaim cloud audio space after parsing. Batch result ZIPs preserve collection/video/part names and include a manifest; split selections over 512 MB.

To upgrade an older checkout using `cloud/deploy.local.json`, run `python cloudctl.py migrate` once, then `redeploy` and the default Compose command. Migration backs up the env file and preserves credentials. Legacy JSON and `compose.cloud.yaml` are no longer used. The previous implementation remains on `codex/aliyun-cloud`; this checkpoint is `codex/serverless-compose-cleanup`.

See [Architecture](docs/architecture.md) for components, storage, scheduling and recovery.
