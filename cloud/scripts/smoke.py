"""Read-only HTTP checks for a deployed control-panel API (no model calls)."""

import argparse
import os

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("TC_API_URL"))
    args = parser.parse_args()
    if not args.url or not args.url.startswith("https://"):
        parser.error("Set TC_API_URL or --url to the deployed HTTPS API URL")
    token = os.environ["TC_PANEL_TOKEN"]
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=120) as client:
        for path, auth, expected in (
            ("/api/config", False, 401),
            ("/api/config", True, 200),
            ("/api/audio", True, 200),
            ("/api/jobs", True, 200),
            ("/", False, 200),
        ):
            response = client.get(
                path, headers={"Authorization": "Bearer " + token} if auth else {}
            )
            print(path, "authenticated" if auth else "anonymous", response.status_code)
            if response.status_code != expected:
                print(response.text.replace(token, "[REDACTED]")[:2000])
                raise SystemExit(f"Expected HTTP {expected}")
        response = client.options(
            "/api/config",
            headers={
                "Origin": "http://localhost:8787",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:8787"
        response = client.get(
            "/api/config",
            headers={
                "Origin": "http://localhost:8787",
                "Authorization": "Bearer " + token,
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:8787"
        print("Local panel CORS: OK")
    print("Smoke checks passed. No transcription jobs submitted.")


if __name__ == "__main__":
    main()
