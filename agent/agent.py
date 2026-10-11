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

    try:
        data = http_post_json(f"{api_url}/agent/auto-enroll", payload, timeout=15)

        cfg = {
            "api_base_url": api_url,
            "device_id": data["device_id"],
            "device_token": data["device_token"],
        }

        # Save config next to executable
        if getattr(sys, "frozen", False):
            save_path = Path(sys.executable).resolve().parent / "device_config.json"
        else:
            save_path = Path(__file__).resolve().parent / "device_config.json"

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with save_path.open("w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            logging.info("Configuration saved automatically to: %s", save_path)
        except Exception as save_err:
            logging.warning("Could not persist config to disk (%s), using in-memory token.", save_err)

        logging.info("Auto-enrollment SUCCESS! Assigned Device ID: %s", data["device_id"])
        return cfg
    except Exception as err:
        logging.error("Auto-enrollment failed: %s", err)
        raise


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
    Automatically configures Windows Location Service, desktop app consent,
    and system background services so the employee does not need to manually
    toggle anything in Windows Settings.
    """
    logging.info("Optimizing Windows Location Service & Privacy Consent...")

    # 1. HKEY_CURRENT_USER: Standard user permissions allow writing here
    hkcu_entries = [
        (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location", "Value", winreg.REG_SZ, "Allow"),
        (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location\nonPackaged", "Value", winreg.REG_SZ, "Allow"),
        (r"Software\Policies\Microsoft\Windows\LocationAndSensors", "DisableLocation", winreg.REG_DWORD, 0),
    ]
    for subkey, name, reg_type, val in hkcu_entries:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, name, 0, reg_type, val)
        except Exception:
            pass

    # 2. HKEY_LOCAL_MACHINE: Master switch & system policy (active when running elevated or system service)
    hklm_entries = [
        (r"SYSTEM\CurrentControlSet\Services\lfsvc\Service\Configuration", "Status", winreg.REG_DWORD, 1),
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location", "Value", winreg.REG_SZ, "Allow"),
        (r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location\nonPackaged", "Value", winreg.REG_SZ, "Allow"),
        (r"SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors", "DisableLocation", winreg.REG_DWORD, 0),
        (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Sensor\Overrides\{BFA794E4-F964-4FDB-90F6-51056BFE4B44}", "SensorPermissionState", winreg.REG_DWORD, 1),
    ]
    for subkey, name, reg_type, val in hklm_entries:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, name, 0, reg_type, val)
        except Exception:
            pass

    # 3. Configure and start Geolocation Service (lfsvc)
    no_window_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.run(
            ["sc.exe", "config", "lfsvc", "start=", "auto"],
            capture_output=True,
            timeout=4,
            creationflags=no_window_flag,
        )
    except Exception:
        pass

    try:
        subprocess.run(
            ["sc.exe", "start", "lfsvc"],
            capture_output=True,
            timeout=4,
            creationflags=no_window_flag,
        )
    except Exception:
        pass


def get_ip_fallback_location():
    """Fallback geolocation if physical Windows Location is temporarily unavailable or warming up."""
    for service_url in ["https://ipapi.co/json/", "https://ipwho.is/"]:
        try:
            req = urllib.request.Request(
                service_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OrgAssetAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                lat = float(data.get("latitude") or data.get("lat"))
                lon = float(data.get("longitude") or data.get("lon"))
                city = data.get("city") or data.get("region") or "Network"
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


def _query_windows_geolocator_sync():
    """Worker function executed in isolated thread to query Windows Built-in Location."""
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
    if not WINSDK_AVAILABLE:
        raise RuntimeError("Windows Location SDK (winsdk) is not available on this machine.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_query_windows_geolocator_sync)
        return future.result(timeout=10.0)


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

    cfg = load_config()
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
        logging.error("Agent startup failed: %s", exc)
        time.sleep(3)
        sys.exit(1)
