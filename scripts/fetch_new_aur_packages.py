#!/usr/bin/env python3
"""Fetch packages newly created on the Arch User Repository (AUR).

New packages are read from the AUR's own RSS feed of newest packages at
``https://aur.archlinux.org/rss/``, which holds the 100 most recent
additions. Package details (version, votes, first submission time) are
enriched through the [AUR RPC interface](
https://aur.archlinux.org/rpc/). The feed is capped at 100 entries, so the
manifest records ``source_truncated`` whenever the feed does not reach back
past the requested window.

The end of the last list is stored in the manifest so the next run resumes
where the previous one stopped.
"""

import argparse
import csv
import datetime as dt
import email.utils
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

RSS_URL = "https://aur.archlinux.org/rss/"
RPC_URL = "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]={name}"
DEFAULT_USER_AGENT = (
    "new-aur-packages/1.0 (https://github.com/GHLists/new-aur-packages)"
)

MAX_BATCH = 100
DESCRIPTION_LIMIT = 300
CSV_HEADER = (
    "created_at",
    "package",
    "version",
    "votes",
    "description",
)

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


class NotFound(Exception):
    pass


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc822(value):
    moment = email.utils.parsedate_to_datetime(value)
    if moment is None:
        raise ValueError(f"unparseable pubDate: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def fetch_bytes(url, user_agent, accept, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent, "Accept": accept}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(url) from error
            last_error = error
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def clean_text(value, limit=DESCRIPTION_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def fetch_feed(user_agent, retries):
    """Return the list of (name, published, description) newest-package items."""
    text = fetch_bytes(
        RSS_URL, user_agent, "application/rss+xml", retries=retries
    ).decode("utf-8")
    root = ET.fromstring(text)
    items = []
    for item in root.findall(".//item"):
        name = (item.findtext("title") or "").strip()
        if not name:
            continue
        try:
            published = parse_rfc822(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        items.append(
            {
                "name": name,
                "published": published,
                "description": item.findtext("description") or "",
            }
        )
    return items


def fetch_rpc_details(names, user_agent, retries, api_delay):
    """Return AUR RPC info records for the given package names."""
    details = {}
    for start in range(0, len(names), MAX_BATCH):
        batch = names[start : start + MAX_BATCH]
        url = RPC_URL.format(
            name=("&arg[]=".join(urllib.parse.quote(name, safe="") for name in batch))
        )
        try:
            data = json.loads(
                fetch_bytes(
                    url, user_agent, "application/json", retries=retries
                )
            )
        except (NotFound, RuntimeError) as error:
            print(f"RPC batch failed: {error}", file=sys.stderr)
            continue
        results = data.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict) and result.get("Name"):
                    details[result["Name"]] = result
        if start + MAX_BATCH < len(names):
            time.sleep(api_delay)
    return details


def build_row(item, details):
    version = votes = first = ""
    if details:
        version = clean_text(details.get("Version"), 20)
        votes = details.get("NumVotes")
        votes = votes if isinstance(votes, int) else ""
        submitted = details.get("FirstSubmitted")
        if isinstance(submitted, (int, float)):
            first = dt.datetime.fromtimestamp(submitted, dt.timezone.utc)
    created = first if first is not None and first != "" else item["published"]
    return {
        "created_at": iso(created),
        "package": item["name"],
        "version": version,
        "votes": votes,
        "description": clean_text(item["description"]),
    }


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    parser.add_argument(
        "--api-delay",
        type=float,
        default=1.0,
        help="seconds between AUR RPC requests (default: 1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    items = fetch_feed(args.user_agent, args.retries)
    truncated = bool(items) and not any(
        item["published"] <= since for item in items
    )
    if truncated:
        print(
            "the newest-packages feed does not reach past the window start; "
            "older entries within this window are missing",
            file=sys.stderr,
        )

    window_items = [
        item for item in items if since < item["published"] <= until
    ]
    details = fetch_rpc_details(
        [item["name"] for item in window_items],
        args.user_agent,
        args.retries,
        args.api_delay,
    )
    rows = [build_row(item, details.get(item["name"])) for item in window_items]
    rows.sort(key=lambda row: row["created_at"])

    manifest["window"] = iso(until)
    manifest["source_truncated"] = bool(truncated)
    if rows:
        output = (
            Path(args.output_dir)
            / f"new-aur-packages-{timestamp_filename(until)}.csv"
        )
        write_csv(output, rows)
        manifest["list"] = {
            "path": output.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} packages created between {iso(since)} "
            f"and {iso(until)} to {output}"
        )
    else:
        print(f"no new packages between {iso(since)} and {iso(until)}")
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
