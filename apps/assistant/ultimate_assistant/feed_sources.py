"""Bounded data sources for canvas feeds. Validation never performs I/O."""
from __future__ import annotations

import json
import math
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

MAX_BYTES = 1_048_576


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a number between {low} and {high}")
    return value


def _path(value):
    if value is not None and not isinstance(value, str):
        raise ValueError("select must be a dotted path string")
    return value


def _url(value):
    if not isinstance(value, str):
        raise ValueError("url must be an HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("url must be an HTTP(S) URL without embedded credentials")
    return value


def _absolute(value, name):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return value


def validate_source(source: dict) -> dict:
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    kind = source.get("type")
    allowed = {
        "weather": {"type", "latitude", "longitude", "units", "title"},
        "json": {"type", "url", "headers_env", "params", "select"},
        "html": {"type", "url", "headers_env", "params", "fields"},
        "file": {"type", "path", "select"},
        "script": {"type", "argv", "cwd", "timeout"},
    }
    if not isinstance(kind, str) or kind not in allowed:
        raise ValueError("source type must be weather, json, html, file, or script")
    if set(source) - allowed[kind]:
        raise ValueError("source contains unsupported fields")
    result = dict(source)
    if kind == "weather":
        _number(source.get("latitude"), "latitude", -90, 90)
        _number(source.get("longitude"), "longitude", -180, 180)
        if source.get("units", "fahrenheit") not in {"fahrenheit", "celsius"}:
            raise ValueError("units must be fahrenheit or celsius")
        if "title" in source and (not isinstance(source["title"], str) or len(source["title"]) > 200):
            raise ValueError("title must be a string of at most 200 characters")
        result["units"] = source.get("units", "fahrenheit")
    if kind in {"json", "html"}:
        _url(source.get("url"))
        headers = source.get("headers_env", {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v or not v.replace("_", "a").isalnum() or v[0].isdigit() or any(c in k for c in "\r\n:") for k, v in headers.items()):
            raise ValueError("headers_env must map header names to environment variable names")
        params = source.get("params", {})
        if not isinstance(params, dict) or any(not isinstance(k, str) or not isinstance(v, (str, int, float, bool)) or isinstance(v, float) and not math.isfinite(v) for k, v in params.items()):
            raise ValueError("params must map strings to scalar values")
        result["headers_env"] = dict(headers)
        result["params"] = dict(params)
    if kind in {"json", "file"}:
        _path(source.get("select"))
    if kind == "html":
        fields = source.get("fields")
        if not isinstance(fields, dict) or not fields or len(fields) > 100:
            raise ValueError("fields must be a nonempty mapping of at most 100 fields")
        for name, spec in fields.items():
            if not isinstance(name, str) or not name or not isinstance(spec, dict) or set(spec) - {"selector", "attribute"} or not isinstance(spec.get("selector"), str) or not spec["selector"].strip() or ("attribute" in spec and (not isinstance(spec["attribute"], str) or not spec["attribute"])):
                raise ValueError("each HTML field needs a selector and optional attribute")
        result["fields"] = {k: dict(v) for k, v in fields.items()}
    if kind == "file":
        _absolute(source.get("path"), "path")
    if kind == "script":
        argv = source.get("argv")
        if not isinstance(argv, list) or not argv or len(argv) > 100 or any(not isinstance(v, str) or "\0" in v for v in argv):
            raise ValueError("argv must be a nonempty list of strings")
        _absolute(argv[0], "script executable")
        if "cwd" in source:
            _absolute(source["cwd"], "cwd")
        result["argv"] = list(argv)
        result["timeout"] = _number(source.get("timeout", 15), "timeout", 1, 30)
    return result


def select_path(value, path):
    """Select dictionary fields/list indexes; an empty path selects the root."""
    _path(path)
    if not path:
        return value
    for part in path.split("."):
        try:
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list) and part.isdecimal():
                value = value[int(part)]
            else:
                raise KeyError(part)
        except (KeyError, IndexError):
            raise ValueError("selected data path does not exist") from None
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("source result must be a JSON object (use select to choose one)")
    return value


def _decode(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("source returned invalid JSON") from None


def _http(source):
    import httpx
    headers = {}
    for header, variable in source.get("headers_env", {}).items():
        value = os.environ.get(variable)
        if value is None:
            raise ValueError("a required source credential environment variable is missing")
        headers[header] = value
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            with client.stream("GET", source["url"], params=source.get("params", {}), headers=headers) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ValueError(f"source HTTP request returned status {response.status_code}")
                chunks, size = [], 0
                deadline = time.monotonic() + 15
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("source response exceeds 1 MiB")
                    if time.monotonic() > deadline:
                        raise ValueError("source HTTP request exceeded 15 seconds")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError:
        raise ValueError("source HTTP request failed") from None
    except (UnicodeError, TypeError):
        raise ValueError("source HTTP configuration is invalid") from None


def _script(source):
    # Files avoid communicate() accumulating unbounded output in RAM. Poll their
    # sizes and kill promptly if either stream exceeds the source budget.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(source["argv"], cwd=source.get("cwd"), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
                                       start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, ValueError):
            raise ValueError("source script could not be started") from None
        deadline = time.monotonic() + source["timeout"]
        try:
            while True:
                finished = process.poll() is not None
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_BYTES:
                    raise ValueError("source script output exceeds 1 MiB")
                if finished:
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("source script timed out")
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # Include descendants of a timed-out producer on Windows.
                    try:
                        subprocess.run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    if process.poll() is None:
                        process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.wait()
        if process.returncode:
            raise ValueError(f"source script exited with status {process.returncode}")
        stdout.seek(0)
        return _object(_decode(stdout.read(MAX_BYTES + 1)))


def fetch_source(source: dict) -> dict:
    source = validate_source(source)
    kind = source["type"]
    if kind == "file":
        try:
            with open(source["path"], "rb") as handle:
                data = handle.read(MAX_BYTES + 1)
        except OSError:
            raise ValueError("source file could not be read") from None
        if len(data) > MAX_BYTES:
            raise ValueError("source file exceeds 1 MiB")
        return _object(select_path(_decode(data), source.get("select")))
    if kind == "script":
        return _script(source)
    if kind == "weather":
        data = _object(_decode(_http({"url": "https://api.open-meteo.com/v1/forecast", "params": {
            "latitude": source["latitude"], "longitude": source["longitude"],
            "temperature_unit": source["units"], "wind_speed_unit": "mph" if source["units"] == "fahrenheit" else "kmh",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m", "timezone": "auto",
        }})))
        current, units = data.get("current"), data.get("current_units")
        if not isinstance(current, dict) or not isinstance(units, dict):
            raise ValueError("weather source returned no current conditions")
        def display(key):
            value = current.get(key)
            return "Unavailable" if value is None else f"{value}{units.get(key, '')}"
        return {"title": source.get("title", "Current weather"), "items": [
            {"label": label, "value": display(key)} for label, key in [
                ("Temperature", "temperature_2m"), ("Feels like", "apparent_temperature"),
                ("Humidity", "relative_humidity_2m"), ("Wind", "wind_speed_10m"), ("Precipitation", "precipitation")]],
            "caption": f"Open-Meteo · {current.get('time', '')}", "current": current, "current_units": units}
    raw = _http(source)
    if kind == "json":
        return _object(select_path(_decode(raw), source.get("select")))
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ValueError("HTML sources require beautifulsoup4; install it in the assistant environment") from None
    soup = BeautifulSoup(raw, "html.parser")
    result = {}
    for name, spec in source["fields"].items():
        try:
            node = soup.select_one(spec["selector"])
        except Exception:
            raise ValueError("HTML source contains an invalid CSS selector") from None
        result[name] = None if node is None else node.get(spec["attribute"]) if "attribute" in spec else node.get_text(" ", strip=True)
    return result
