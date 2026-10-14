import asyncio
import concurrent.futures
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
import winreg
from datetime import datetime, timezone
from pathlib import Path

import urllib.error
import urllib.request
from typing import Optional


def load_dotenv_simple(path: Path):
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


# Load local environment if present
load_dotenv_simple(Path(__file__).parent / ".env")
load_dotenv_simple(Path(__file__).parent.parent / ".env")


class AgentHTTPError(Exception):
    def __init__(self, code: int, message: str):
        self.status_code = code
        super().__init__(message)


def http_post_json(url: str, payload: dict, token: Optional[str] = None, timeout: float = 12.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "OrgAssetAgent/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise AgentHTTPError(err.code, f"HTTP {err.code}: {err.reason}")
    except urllib.error.URLError as err:
        raise RuntimeError(f"Network error: {err.reason}")

# Optional winsdk import (winsdk is 38MB; native WinRT via Windows PowerShell is preferred to keep binary light)
try:
    from winsdk.windows.devices.geolocation import (
        Geolocator,
        GeolocationAccessStatus,
        PositionAccuracy,
    )
    WINSDK_AVAILABLE = True
except Exception:
    WINSDK_AVAILABLE = False

INTERVAL_SECONDS = int(os.getenv("REPORT_INTERVAL_SECONDS", "10"))
DEFAULT_API_URL = os.getenv("API_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "https://asset-tracker-backend-tqx7.onrender.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def get_possible_config_paths():
    """Return ordered list of paths to look for device_config.json."""
    paths = []
    # 1. Alongside the executable or running script
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.append(exe_dir / "device_config.json")
    else:
        script_dir = Path(__file__).resolve().parent
        paths.append(script_dir / "device_config.json")

    # 2. Current working directory
    paths.append(Path.cwd() / "device_config.json")
    paths.append(Path.cwd() / "agent" / "device_config.json")

    # 3. Local AppData / ProgramData paths
    local_app_data = os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", ""))
    if local_app_data:
        paths.append(Path(local_app_data) / "OrgAssetTracker" / "device_config.json")

    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    paths.append(Path(program_data) / "OrgAssetTracker" / "device_config.json")

    seen = set()
    unique_paths = []
    for p in paths:
        try:
            normalized = str(p.resolve())
        except Exception:
            normalized = str(p)
        if normalized not in seen:
            seen.add(normalized)
            unique_paths.append(p)
    return unique_paths


def auto_enroll_device(api_url: str):
    """Automatically enroll the machine on the server without any user intervention."""
    hostname = socket.gethostname()
    api_url = api_url.rstrip("/")
    logging.info("Auto-registering device '%s' with Fleet Management Gateway...", hostname)

    payload = {
        "hostname": hostname,
        "asset_tag": hostname,
        "machine_id": f"{hostname}-{os.environ.get('USERNAME', 'user')}",
    }

    # Render free tier sleeps after 15 min; cold boot takes 30-50s.
    # Retry up to 8 times with generous timeouts so the agent never crashes during cold starts.
    max_retries = 8
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                logging.info("[Attempt %d/%d] Contacting Gateway (waking up cloud server)...", attempt, max_retries)
            data = http_post_json(f"{api_url}/agent/auto-enroll", payload, timeout=45.0)

            cfg = {
                "api_base_url": api_url,
                "device_id": data["device_id"],
                "device_token": data["device_token"],
            }

            # Persist configuration to multiple locations (exe directory + LocalAppData + ProgramData)
            # so moving the exe to Desktop or another directory never loses device registration.
            target_dirs = []
            if getattr(sys, "frozen", False):
                target_dirs.append(Path(sys.executable).resolve().parent)
            else:
                target_dirs.append(Path(__file__).resolve().parent)

            local_app_data = os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", ""))
            if local_app_data:
                target_dirs.append(Path(local_app_data) / "OrgAssetTracker")

            program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            if program_data:
                target_dirs.append(Path(program_data) / "OrgAssetTracker")

            saved_anywhere = False
            for target_dir in target_dirs:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    save_path = target_dir / "device_config.json"
                    with save_path.open("w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                    saved_anywhere = True
                    logging.info("Configuration saved automatically to: %s", save_path)
                except Exception:
                    pass

            if not saved_anywhere:
                logging.warning("Could not persist config to disk, using in-memory token.")

            logging.info("Auto-enrollment SUCCESS! Assigned Device ID: %s", data["device_id"])
            return cfg
        except Exception as err:
            last_error = err
            if attempt < max_retries:
                logging.warning("Gateway waking up / connecting (%s). Retrying in 6 seconds... (%d/%d)", err, attempt, max_retries)
                time.sleep(6)
            else:
                logging.error("Auto-enrollment failed after %d attempts: %s", max_retries, err)

    if last_error:
        raise last_error
    raise RuntimeError("Failed to auto-enroll device.")


def load_config():
    # 1. Look for existing config
    for p in get_possible_config_paths():
        if p.exists() and p.is_file():
            try:
                with p.open("r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                for key in ("api_base_url", "device_id", "device_token"):
                    if not cfg.get(key):
                        raise ValueError(f"Missing '{key}' in {p}")
                cfg["api_base_url"] = cfg["api_base_url"].rstrip("/")

                # If config points to localhost but .env provides an active remote URL, use remote URL
                env_url = (os.getenv("API_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
                if env_url and ("127.0.0.1" in cfg["api_base_url"] or "localhost" in cfg["api_base_url"]) and ("127.0.0.1" not in env_url and "localhost" not in env_url):
                    logging.info("Overriding localhost API url with environment API URL: %s", env_url)
                    cfg["api_base_url"] = env_url

                logging.info("Loaded device configuration from: %s", p)
                return cfg
            except Exception as e:
                logging.warning("Failed to parse config at %s: %s", p, e)

    # 2. No config found -> Perform 100% automatic zero-touch self-enrollment
    logging.info("No prior configuration found. Performing automatic device enrollment...")
    return auto_enroll_device(DEFAULT_API_URL)


def ensure_windows_location_service_active():
    """
    Silently forces Windows Location Service, registry keys, and privacy consent to ON
    in the background without locking Windows Settings or displaying "managed by your organization".
    Works seamlessly across Windows 10, Windows 11, and Windows 11 24H2.
    """
    logging.info("Silently configuring & enabling Windows Location Services...")
    no_window_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    # 1. HKLM & HKCU Consent configuration + Clean up any enterprise policy locks
    ps_force_on = (
        "Remove-Item -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\AppPrivacy' -Recurse -Force -ErrorAction SilentlyContinue; "
        "Remove-Item -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors' -Recurse -Force -ErrorAction SilentlyContinue; "
        "Remove-Item -Path 'HKCU:\\Software\\Policies\\Microsoft\\Windows\\LocationAndSensors' -Recurse -Force -ErrorAction SilentlyContinue; "
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\lfsvc' -Name 'Start' -Value 2 -Type DWord -Force -ErrorAction SilentlyContinue; "
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\lfsvc\\Service\\Configuration' -Name 'Status' -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue; "
        "$cp = @('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location', 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location', 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location\\NonPackaged', 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location\\NonPackaged'); "
        "foreach ($c in $cp) { if (-not (Test-Path $c)) { New-Item -Path $c -Force -ErrorAction SilentlyContinue | Out-Null }; Set-ItemProperty -Path $c -Name 'Value' -Value 'Allow' -Type String -Force -ErrorAction SilentlyContinue }; "
        "$sensor = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Sensor\\Overrides\\{BFA794E4-F964-4FDB-90F6-51056BFE4B44}'; "
        "if (-not (Test-Path $sensor)) { New-Item -Path $sensor -Force -ErrorAction SilentlyContinue | Out-Null }; Set-ItemProperty -Path $sensor -Name 'SensorPermissionState' -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue; "
        "if (Test-Path \"$env:WINDIR\\System32\\SystemSettingsAdminFlows.exe\") { Start-Process \"$env:WINDIR\\System32\\SystemSettingsAdminFlows.exe\" -ArgumentList 'SetCamSystemGlobal location 1' -Wait -WindowStyle Hidden -ErrorAction SilentlyContinue }; "
        "Set-Service Lfsvc -StartupType Automatic -ErrorAction SilentlyContinue; Start-Service Lfsvc -ErrorAction SilentlyContinue"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_force_on],
            capture_output=True,
            timeout=10,
            creationflags=no_window_flag,
        )
    except Exception:
        pass


def get_ip_fallback_location():
    """Fallback geolocation if physical Windows Location is temporarily unavailable or warming up."""
    # List of dependable, zero-auth IP geolocation services
    services = [
        ("https://ipwho.is/", lambda d: (float(d["latitude"]), float(d["longitude"]), d.get("city") or "Network")),
        ("https://ipinfo.io/json", lambda d: (float(d["loc"].split(",")[0]), float(d["loc"].split(",")[1]), d.get("city") or "Network")),
        ("http://ip-api.com/json/", lambda d: (float(d["lat"]), float(d["lon"]), d.get("city") or "Network")),
        ("https://ipapi.co/json/", lambda d: (float(d.get("latitude") or d.get("lat")), float(d.get("longitude") or d.get("lon")), d.get("city") or "Network")),
    ]
    for service_url, parser in services:
        try:
            req = urllib.request.Request(
                service_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OrgAssetAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                lat, lon, city = parser(data)
                return {
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy_meters": 1500.0,
                    "position_source": f"Network Geolocation ({city})",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception:
            continue
    raise RuntimeError("Fallback network geolocation services were unreachable.")


# Pure Windows native WinRT geolocation query (zero external Python packages, uses built-in Windows runtime)
WINRT_NATIVE_PS = """
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(12000) | Out-Null
    $netTask.Result
}
[Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime] | Out-Null

# 1. Non-blocking permission registration
try {
    $null = Await ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus])
} catch {}

# 2. Direct high-accuracy physical coordinates acquisition
$locator = [Windows.Devices.Geolocation.Geolocator]::new()
$locator.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
$pos = Await ($locator.GetGeopositionAsync()) ([Windows.Devices.Geolocation.Geoposition])
if ($pos -and $pos.Coordinate -and $pos.Coordinate.Point) {
    [PSCustomObject]@{
        lat = $pos.Coordinate.Point.Position.Latitude
        lon = $pos.Coordinate.Point.Position.Longitude
        acc = $pos.Coordinate.Accuracy
        src = [int]$pos.Coordinate.PositionSource
    } | ConvertTo-Json -Compress
} else {
    Write-Error "Location warming up or returned null"
}
"""


def _query_windows_geolocator_native():
    """Query Windows Built-in Location directly via native WinRT subsystem (zero-dependency)."""
    no_window_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", WINRT_NATIVE_PS],
        capture_output=True,
        text=True,
        timeout=16,
        creationflags=no_window_flag,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"Native WinRT query failed: {proc.stderr.strip()[:100]}")
    data = json.loads(proc.stdout.strip())
    raw_source = data.get("src")
    source_map = {
        0: "Cellular Tower Triangulation",
        1: "Satellite GNSS/GPS",
        2: "Wi-Fi Triangulation (Microsoft/Google WPS)",
        3: "ISP Network IP (Coarse)",
        4: "Windows Location Platform",
        5: "Default Location Service",
    }
    if raw_source is not None:
        source_label = source_map.get(int(raw_source), f"Windows Location ({raw_source})")
    else:
        source_label = "Wi-Fi / Hardware Geolocation"

    return {
        "latitude": float(data["lat"]),
        "longitude": float(data["lon"]),
        "accuracy_meters": float(data.get("acc") or 10.0),
        "position_source": source_label,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def _query_windows_geolocator_sync():
    """Worker function executed in isolated thread to query Windows Built-in Location via winsdk."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        access = loop.run_until_complete(Geolocator.request_access_async())
        if access != GeolocationAccessStatus.ALLOWED:
            # Auto-enable location services in case consent wasn't applied yet
            ensure_windows_location_service_active()
            access = loop.run_until_complete(Geolocator.request_access_async())
            if access != GeolocationAccessStatus.ALLOWED:
                raise PermissionError("Windows Location access permission is not granted.")

        locator = Geolocator()
        locator.desired_accuracy = PositionAccuracy.HIGH
        locator.desired_accuracy_in_meters = 1

        pos = loop.run_until_complete(locator.get_geoposition_async())
        coord = pos.coordinate
        point = coord.point.position

        # Detect physical positioning technology used by Windows
        raw_source = getattr(coord, "position_source", None)
        source_map = {
            0: "Cellular Tower Triangulation",
            1: "Satellite GNSS/GPS",
            2: "Wi-Fi Triangulation (Microsoft/Google WPS)",
            3: "ISP Network IP (Coarse)",
            4: "Windows Location Platform",
            5: "Default Location Service",
        }
        if raw_source is not None:
            source_label = source_map.get(int(raw_source), f"Windows Location ({raw_source})")
        else:
            source_label = "Wi-Fi / Hardware Geolocation"

        return {
            "latitude": float(point.latitude),
            "longitude": float(point.longitude),
            "accuracy_meters": float(coord.accuracy or 10.0),
            "position_source": source_label,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        loop.close()


def get_windows_location():
    """Query coordinates directly from Windows Built-in Location Service."""
    last_err = None
    # Method 1: Ultra-fast native WinRT via Windows PowerShell (0 external dependencies, 100% native)
    try:
        return _query_windows_geolocator_native()
    except Exception as native_err:
        last_err = native_err

    # Method 2: winsdk fallback if installed
    if WINSDK_AVAILABLE:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_query_windows_geolocator_sync)
                return future.result(timeout=10.0)
        except Exception as winsdk_err:
            last_err = winsdk_err

    if last_err:
        raise last_err
    raise RuntimeError("Windows native geolocation subsystem did not return coordinates.")


async def get_location():
    """Acquires high-precision physical location, falling back gracefully if necessary."""
    try:
        win_loc = await asyncio.to_thread(get_windows_location)
        return win_loc
    except Exception as exc:
        logging.warning("Windows Location query: %s. Engaging automatic service repair & fallback...", exc)
        ensure_windows_location_service_active()
        try:
            return await asyncio.to_thread(get_ip_fallback_location)
        except Exception:
            raise exc


def send_report(cfg, location):
    hostname = socket.gethostname()
    payload = {
        "hostname": hostname,
        **location,
    }
    return http_post_json(
        f'{cfg["api_base_url"]}/agent/report',
        payload=payload,
        token=cfg.get("device_token"),
        timeout=10,
    )


async def main():
    # Automatically ensure Windows location service and consent are active
    ensure_windows_location_service_active()

    cfg = None
    startup_retries = 0
    while cfg is None:
        try:
            cfg = load_config()
        except Exception as startup_err:
            startup_retries += 1
            logging.warning(
                "Gateway connection attempt #%d paused (%s). Retrying in 8 seconds... (please keep this window open)",
                startup_retries,
                startup_err,
            )
            time.sleep(8)

    logging.info("==================================================")
    logging.info("     OrgAssetAgent - Real-Time Tracking Engine    ")
    logging.info("==================================================")
    logging.info("Device ID : %s", cfg["device_id"])
    logging.info("Hostname  : %s", socket.gethostname())
    logging.info("Gateway   : Fleet Telemetry Gateway [Active]")
    logging.info("Interval  : Real-time sync every %s seconds", INTERVAL_SECONDS)
    logging.info("Press Ctrl+C to stop.")

    seq = 0
    while True:
        cycle_start = time.monotonic()
        seq += 1
        try:
            location = await get_location()
            t0 = time.monotonic()
            result = send_report(cfg, location)
            lat_ms = (time.monotonic() - t0) * 1000
            logging.info(
                "[Sync #%04d] SUCCESS -> Telemetry Ingested | Source: %s (±%.1fm) | Ping: %.0fms",
                seq,
                location.get("position_source", "Hardware Geolocation"),
                location["accuracy_meters"],
                lat_ms,
            )
        except AgentHTTPError as http_err:
            if http_err.status_code in (401, 403):
                logging.warning("[Sync #%04d] Device token rejected/revoked (HTTP %d). Attempting automatic re-enrollment...", seq, http_err.status_code)
                try:
                    cfg = auto_enroll_device(cfg.get("api_base_url") or DEFAULT_API_URL)
                    logging.info("Auto-recovery SUCCESS! Re-enrolled as Device ID: %s", cfg["device_id"])
                except Exception as enroll_err:
                    logging.error("Auto-recovery re-enrollment failed: %s", enroll_err)
            else:
                logging.error("[Sync #%04d] HTTP error: %s", seq, http_err)
        except Exception as exc:
            logging.error("[Sync #%04d] Report cycle failed: %s", seq, exc)

        elapsed = time.monotonic() - cycle_start
        sleep_duration = max(0.5, INTERVAL_SECONDS - elapsed)
        await asyncio.sleep(sleep_duration)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Agent stopped by user.")
    except Exception as exc:
        logging.error("Agent stopped unexpectedly: %s", exc)
        print("\n[INFO] Agent will close in 15 seconds, or press Enter to exit...")
        try:
            input()
        except Exception:
            time.sleep(15)
        sys.exit(1)
