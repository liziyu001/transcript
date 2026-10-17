"""One env file, explicit credentials, no ambient shell configuration required."""

import os
import re
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.local"


def read_env(path):
    values = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ValueError(f"Invalid env syntax on line {number}; expected KEY=value.")
        try:
            parts = shlex.split(match[2], comments=True, posix=True)
        except ValueError:
            raise ValueError(f"Invalid quoting on env line {number}.") from None
        if len(parts) > 1:
            raise ValueError(f"Quote values containing spaces on env line {number}.")
        if match[1] in values:
            raise ValueError(f"Duplicate env key: {match[1]}")
        values[match[1]] = parts[0] if parts else ""
    return values


def write_values(path, updates):
    """Preserve comments and all credentials; replace only explicit keys atomically."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    remaining = dict(updates)
    result = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in updates:
            key = match[1]
            result.append(f"{key}={shlex.quote(updates[key])}")
            remaining.pop(key, None)
        else:
            result.append(line)
    result.extend(f"{key}={shlex.quote(value)}" for key, value in remaining.items())
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def initialize(path=ENV_FILE):
    path = Path(path)
    if path.exists():
        print(f"Keeping existing configuration: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    write_values(path, {"TC_PANEL_TOKEN": secrets.token_urlsafe(32)})
    print(f"Created {path}; fill the five required values before deploying.")


@dataclass(frozen=True)
class Config:
    region: str
    prefix: str
    bucket: str
    instance: str
    existing_bucket: bool
    values: dict
    path: Path

    @classmethod
    def load(cls, path=ENV_FILE):
        values = read_env(path)
        required = (
            "ALIBABA_CLOUD_ACCESS_KEY_ID",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
            "TC_DASHSCOPE_KEY",
            "TC_PANEL_TOKEN",
            "TC_BUCKET",
            "TC_OTS_INSTANCE",
        )
        missing = [
            key for key in required if not values.get(key) or values[key].startswith("replace-")
        ]
        if missing:
            raise ValueError("Fill these keys in the env file: " + ", ".join(missing))
        if len(values["TC_PANEL_TOKEN"]) < 24:
            raise ValueError("TC_PANEL_TOKEN must contain at least 24 characters.")
        prefix, region = (
            values.get("TC_PREFIX", "transcript"),
            values.get("TC_REGION", "cn-beijing"),
        )
        patterns = {
            "TC_PREFIX": (prefix, r"[a-z][a-z0-9-]{2,24}"),
            "TC_REGION": (region, r"[a-z]+-[a-z]+(?:-[a-z]+)?-?\d*"),
            "TC_BUCKET": (values["TC_BUCKET"], r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"),
            "TC_OTS_INSTANCE": (
                values["TC_OTS_INSTANCE"],
                r"[a-zA-Z][a-zA-Z0-9-]{1,14}[a-zA-Z0-9]",
            ),
        }
        for name, (value, pattern) in patterns.items():
            if not re.fullmatch(pattern, value):
                raise ValueError(f"Invalid {name}; see cloud/.env.example.")
        existing = values.get("TC_EXISTING_BUCKET", "false").lower()
        if existing not in {"true", "false"}:
            raise ValueError("TC_EXISTING_BUCKET must be true or false.")
        # Prevent a stale shell token/profile from selecting a different account.
        for name in (
            "ALIBABA_CLOUD_ACCESS_KEY_ID",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
            "ALIBABA_CLOUD_SECURITY_TOKEN",
        ):
            os.environ[name] = values.get(name, "")
        for name in ("TC_PANEL_TOKEN", "TC_DASHSCOPE_KEY"):
            os.environ[name] = values[name]
        return cls(
            region,
            prefix,
            values["TC_BUCKET"],
            values["TC_OTS_INSTANCE"],
            existing == "true",
            values,
            Path(path),
        )

    def panel_env(self, api_url):
        from urllib.parse import urlparse

        parsed = urlparse(api_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Cloud deployment did not return a valid HTTPS API URL.")
        write_values(self.path, {"TC_API_URL": api_url})
        # Generated runtime subset: never inject the model key into the local panel.
        selected = {
            k: self.values[k]
            for k in (
                "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "TC_PANEL_TOKEN",
            )
        }
        selected["ALIBABA_CLOUD_SECURITY_TOKEN"] = self.values.get(
            "ALIBABA_CLOUD_SECURITY_TOKEN", ""
        )
        selected["TC_API_URL"] = api_url
        path = ROOT / ".panel.env"
        text = "# Generated by cloudctl; edit .env.local instead.\n"
        for key, value in selected.items():
            if "\n" in value or "\r" in value:
                raise ValueError("Env values must be single-line.")
            text += key + "='" + value.replace("'", "\\'") + "'\n"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        path.chmod(0o600)
