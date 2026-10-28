#!/usr/bin/env python3
"""
ocw2abrp.py - Live Telemetry Bridge between OpenCARWINGS and A Better Routeplanner (ABRP).

Fetches the latest vehicle telematics from the OpenCARWINGS API (SoC, SoH, Wh/SoE,
usable battery capacity, GPS location, charging/driving status, temperatures, odometer,
TPMS tire pressures, and estimated range) and forwards them to the ABRP (Iternio)
Telemetry API to optimize real-time range and route calculations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path: str = ".env") -> bool:
        """Native fallback to load a .env file if python-dotenv is not installed."""
        if not os.path.isfile(dotenv_path):
            return False
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = val
        return True

logger = logging.getLogger("ocw2abrp")

DEFAULT_OCW_URL = "https://opencarwings.viaaq.eu"
# Standard public ABRP API key for v1 generic telemetry (/1/tlm/send)
DEFAULT_ABRP_API_KEY = "32b2162f-9599-4647-8139-66e9f9528370"
ABRP_V1_TLM_URL = "https://api.iternio.com/1/tlm/send"
ABRP_V2_TLM_URL = "https://api.iternio.com/2/app/tlm"

PLACEHOLDER_OCW_KEYS = {
    "your_opencarwings_api_token_here",
    "coloque_aqui_o_seu_token_opencarwings",
}
PLACEHOLDER_ABRP_TOKENS = {
    "your_abrp_user_token_here",
    "coloque_aqui_o_seu_token_de_utilizador_abrp",
}


@dataclass
class SnapshotState:
    """Stores the previous reading state to compute differential power (kW) and speed (km/h)."""
    timestamp_epoch: float
    wh_content: Optional[float]
    lat: Optional[float]
    lon: Optional[float]
    odometer_km: Optional[float]
    ev_updated_iso: Optional[str]


def parse_iso_timestamp(iso_str: Optional[str]) -> Optional[float]:
    """Converts an ISO-8601 string (e.g. '2026-10-28T19:50:00Z') to Unix epoch seconds."""
    if not iso_str:
        return None
    try:
        cleaned = iso_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the great-circle distance in kilometers between two GPS coordinates."""
    r_earth = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class OpenCarWingsClient:
    """Client for the OpenCARWINGS API (supports Personal API Token or JWT Bearer auth)."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

        self._jwt_access: Optional[str] = None
        self._jwt_refresh: Optional[str] = None

        if self.api_key and self.api_key not in PLACEHOLDER_OCW_KEYS:
            self.session.headers.update({"Authorization": f"Token {self.api_key}"})
        elif self.username and self.password:
            self._obtain_jwt()
        else:
            raise ValueError(
                "Missing OpenCARWINGS credentials. Set OCW_API_KEY (recommended) "
                "or OCW_USERNAME + OCW_PASSWORD in your .env file."
            )

    def _obtain_jwt(self) -> None:
        url = f"{self.base_url}/api/token/obtain/"
        resp = self.session.post(
            url,
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._jwt_access = data.get("access")
        self._jwt_refresh = data.get("refresh")
        if not self._jwt_access:
            raise RuntimeError("Failed to obtain JWT access token from OpenCARWINGS.")
        self.session.headers.update({"Authorization": f"Bearer {self._jwt_access}"})
        logger.info("Authenticated with OpenCARWINGS via JWT as user '%s'.", self.username)

    def _refresh_jwt_if_needed(self) -> None:
        if not self._jwt_refresh:
            self._obtain_jwt()
            return
        url = f"{self.base_url}/api/token/refresh/"
        resp = self.session.post(url, json={"refresh": self._jwt_refresh}, timeout=self.timeout)
        if resp.status_code in (200, 201):
            data = resp.json()
            self._jwt_access = data.get("access")
            if self._jwt_access:
                self.session.headers.update({"Authorization": f"Bearer {self._jwt_access}"})
                return
        self._obtain_jwt()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code == 401 and (not self.api_key or self.api_key in PLACEHOLDER_OCW_KEYS) and self.username:
            logger.info("JWT token expired, refreshing...")
            self._refresh_jwt_if_needed()
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def list_cars(self) -> list[Dict[str, Any]]:
        """Retrieves the list of vehicles associated with the account (/api/car/)."""
        return self._request("GET", "/api/car/")

    def get_car(self, vin: str) -> Dict[str, Any]:
        """Retrieves full vehicle details (/api/car/{vin}/)."""
        return self._request("GET", f"/api/car/{vin}/")

    def get_latest_trip(self, vin: str) -> Optional[Dict[str, Any]]:
        """Retrieves the latest probe trip record (/api/car/{vin}/probe/trips?page_size=1) for extra temperatures."""
        try:
            data = self._request("GET", f"/api/car/{vin}/probe/trips", params={"page_size": 1})
            results = data.get("results") if isinstance(data, dict) else None
            if results and len(results) > 0:
                return results[0]
        except Exception as exc:
            logger.debug("Could not fetch probe/trips for %s: %s", vin, exc)
        return None

    def request_tcu_refresh(self, vin: str) -> Dict[str, Any]:
        """Sends a 'Refresh data' command (command_type=1) to the vehicle (/api/command/{vin}/)."""
        payload = {"vin": vin, "command_type": 1}
        return self._request("POST", f"/api/command/{vin}/", json=payload)


class ABRPClient:
    """Client for pushing live telemetry to A Better Routeplanner (Iternio API v1 and v2)."""

    def __init__(
        self,
        user_token: str,
        api_key: str = DEFAULT_ABRP_API_KEY,
        api_version: str = "v1",
        timeout: int = 15,
    ) -> None:
        self.user_token = (user_token or "").strip()
        self.api_key = (api_key or DEFAULT_ABRP_API_KEY).strip()
        self.api_version = api_version.lower().strip()
        self.timeout = timeout
        self.session = requests.Session()

        if not self.user_token or self.user_token in PLACEHOLDER_ABRP_TOKENS:
            raise ValueError(
                "Missing ABRP_USER_TOKEN. Obtain your token in ABRP -> Car Settings -> "
                "Live Data -> Generic and set it in your .env file."
            )

    def send_telemetry(self, tlm: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Sends a telemetry packet to ABRP."""
        if self.api_version == "v2":
            return self._send_v2(tlm, dry_run=dry_run)
        return self._send_v1(tlm, dry_run=dry_run)

    def _send_v1(self, tlm: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Sends telemetry to https://api.iternio.com/1/tlm/send (standard Postman v1 format)."""
        if dry_run:
            logger.info("[DRY-RUN] ABRP v1 telemetry payload: %s", json.dumps(tlm))
            return {"status": "dry_run"}

        params = {
            "api_key": self.api_key,
            "token": self.user_token,
            "tlm": json.dumps(tlm, separators=(",", ":")),
        }
        resp = self.session.post(ABRP_V1_TLM_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _send_v2(self, tlm: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """Sends telemetry to https://api.iternio.com/2/app/tlm (defined in abrp_api.yaml)."""
        iso_time = dt.datetime.fromtimestamp(tlm["utc"], tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        point: Dict[str, Any] = {"provider": "ABRP_OTA"}

        if "soc" in tlm:
            point["soc"] = {"time": iso_time, "frac": round(float(tlm["soc"]) / 100.0, 4)}
        if "soh" in tlm:
            point["soh"] = {"time": iso_time, "frac": round(float(tlm["soh"]) / 100.0, 4)}
        if "soe" in tlm:
            point["soe"] = {"time": iso_time, "wh": round(float(tlm["soe"]) * 1000.0, 1)}
        if "capacity" in tlm:
            point["batteryCapacity"] = {"time": iso_time, "wh": round(float(tlm["capacity"]) * 1000.0, 1)}
        if "lat" in tlm and "lon" in tlm:
            point["location"] = {"time": iso_time, "lat": float(tlm["lat"]), "lon": float(tlm["lon"])}
        if "speed" in tlm:
            point["speed"] = {"time": iso_time, "ms": round(float(tlm["speed"]) / 3.6, 2)}
        if "power" in tlm:
            point["power"] = {"time": iso_time, "w": round(float(tlm["power"]) * 1000.0, 1)}
        if "odometer" in tlm:
            point["odometer"] = {"time": iso_time, "m": round(float(tlm["odometer"]) * 1000.0, 1)}
        if "est_battery_range" in tlm:
            point["estimatedBatteryRange"] = {
                "time": iso_time,
                "m": round(float(tlm["est_battery_range"]) * 1000.0, 1),
            }
        if "cabin_temp" in tlm:
            point["cabinTemperature"] = {"time": iso_time, "c": float(tlm["cabin_temp"])}
        if "ext_temp" in tlm:
            point["externalTemperature"] = {"time": iso_time, "c": float(tlm["ext_temp"])}
        if "batt_temp" in tlm:
            point["batteryTemperature"] = {"time": iso_time, "c": float(tlm["batt_temp"])}

        if tlm.get("is_charging"):
            chg_state = "CHARGING_DC" if tlm.get("is_dcfc") else "CHARGING_AC"
        else:
            chg_state = "NOT_CHARGING"
        point["chargingState"] = {"time": iso_time, "state": chg_state}

        drv_state = "PARK" if tlm.get("is_parked", 1) else "DRIVE"
        point["drivingState"] = {"time": iso_time, "state": drv_state}

        payload = [point]
        if dry_run:
            logger.info("[DRY-RUN] ABRP v2 telemetry payload: %s", json.dumps(payload))
            return {"status": "dry_run"}

        headers = {
            "X-TLM-TOKEN": self.user_token,
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        resp = self.session.post(ABRP_V2_TLM_URL, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "ok"}


def build_abrp_telemetry(
    car_data: Dict[str, Any],
    prev_state: Optional[SnapshotState],
    latest_trip: Optional[Dict[str, Any]] = None,
    use_dash_soc: bool = True,
    gid_wh: float = 80.0,
) -> tuple[Dict[str, Any], SnapshotState]:
    """
    Maps the OpenCARWINGS `Car` object to the `tlm` dictionary expected by ABRP.
    Also estimates average power (kW) and speed (km/h) across consecutive snapshots.
    """
    ev_info: Dict[str, Any] = car_data.get("ev_info") or {}
    loc_info: Dict[str, Any] = car_data.get("location") or {}
    veh_health: Dict[str, Any] = car_data.get("veh_health") or {}

    now_epoch = time.time()
    ev_updated_iso = ev_info.get("last_updated") or car_data.get("last_connection")
    loc_updated_iso = loc_info.get("last_updated")

    ev_epoch = parse_iso_timestamp(ev_updated_iso)
    loc_epoch = parse_iso_timestamp(loc_updated_iso)
    reading_epoch = max(
        [ts for ts in (ev_epoch, loc_epoch) if ts is not None],
        default=now_epoch,
    )

    # ABRP requires `utc` to be current to treat telemetry as "Live Data".
    # If the reading was captured within the last 10 minutes, use its exact timestamp;
    # otherwise (e.g. parked heartbeat), use current UTC epoch so ABRP keeps the link active.
    utc_to_send = int(reading_epoch if (now_epoch - reading_epoch) <= 600 else now_epoch)

    tlm: Dict[str, Any] = {"utc": utc_to_send}

    # 1. State of Charge (SoC %)
    soc_display = ev_info.get("soc_display")
    soc_bms = ev_info.get("soc")
    force_display = ev_info.get("force_soc_display", False)

    soc: Optional[float] = None
    if (use_dash_soc or force_display) and soc_display is not None and float(soc_display) > 0:
        soc = round(float(soc_display), 2)
    elif soc_bms is not None and float(soc_bms) >= 0:
        soc = round(float(soc_bms), 2)
    elif soc_display is not None:
        soc = round(float(soc_display), 2)

    if soc is not None:
        tlm["soc"] = soc

    # 2. State of Health (SoH %)
    soh = ev_info.get("soh")
    if soh is not None and float(soh) > 0:
        tlm["soh"] = round(float(soh), 2)

    # 3. Remaining Energy (SoE in kWh) and Usable Battery Capacity (capacity in kWh)
    wh_content = ev_info.get("wh_content")
    max_gids = ev_info.get("max_gids")
    gids = ev_info.get("gids")

    curr_wh: Optional[float] = None
    if wh_content is not None and float(wh_content) > 0:
        curr_wh = float(wh_content)
    elif gids is not None and int(gids) > 0:
        curr_wh = float(gids) * gid_wh

    if curr_wh is not None:
        tlm["soe"] = round(curr_wh / 1000.0, 3)

    if max_gids is not None and int(max_gids) > 0:
        tlm["capacity"] = round((float(max_gids) * gid_wh) / 1000.0, 3)
    elif curr_wh is not None and soc is not None and soc > 5.0:
        tlm["capacity"] = round((curr_wh / 1000.0) / (soc / 100.0), 3)

    # 4. Charging and Driving State
    is_charging = bool(ev_info.get("charging", False))
    is_quick_charging = bool(ev_info.get("quick_charging", False))
    car_running = bool(ev_info.get("car_running", False))
    car_gear = ev_info.get("car_gear", 0)

    is_parked = (not car_running) or (car_gear == 0) or is_charging

    tlm["is_charging"] = 1 if is_charging else 0
    tlm["is_dcfc"] = 1 if (is_charging and is_quick_charging) else 0
    tlm["is_parked"] = 1 if is_parked else 0

    # 5. GPS Coordinates (lat / lon)
    curr_lat: Optional[float] = None
    curr_lon: Optional[float] = None
    raw_lat = loc_info.get("lat")
    raw_lon = loc_info.get("lon")
    if raw_lat is not None and raw_lon is not None:
        try:
            lat_f, lon_f = float(raw_lat), float(raw_lon)
            if not (math.isclose(lat_f, 0.0) and math.isclose(lon_f, 0.0)):
                curr_lat, curr_lon = lat_f, lon_f
                tlm["lat"] = round(curr_lat, 6)
                tlm["lon"] = round(curr_lon, 6)
        except (ValueError, TypeError):
            pass

    # 6. Odometer (km) and Tire Pressures (TPMS in kPa)
    odometer_km: Optional[float] = None
    raw_odo = car_data.get("odometer") or veh_health.get("mileage")
    if raw_odo is not None and float(raw_odo) > 0:
        odometer_km = float(raw_odo)
        tlm["odometer"] = round(odometer_km, 1)

    tpms_map = {
        "tire_pressure_fl": veh_health.get("tpms_fl_float") or veh_health.get("tpms_fl"),
        "tire_pressure_fr": veh_health.get("tpms_fr_float") or veh_health.get("tpms_fr"),
        "tire_pressure_rl": veh_health.get("tpms_rl_float") or veh_health.get("tpms_rl"),
        "tire_pressure_rr": veh_health.get("tpms_rr_float") or veh_health.get("tpms_rr"),
    }
    for abrp_key, raw_tpms in tpms_map.items():
        if raw_tpms is not None and float(raw_tpms) > 0:
            val = float(raw_tpms)
            # OpenCARWINGS reports TPMS in PSI (e.g. 36.0) or bar (e.g. 2.5); ABRP expects kPa
            if 10.0 <= val <= 80.0:
                val_kpa = val * 6.89476
            elif 0.8 <= val < 10.0:
                val_kpa = val * 100.0
            else:
                val_kpa = val
            tlm[abrp_key] = round(val_kpa, 1)

    # 7. Vehicle Estimated Range (km)
    ac_on = bool(ev_info.get("ac_status", False))
    raw_range = ev_info.get("range_acon") if ac_on else ev_info.get("range_acoff")
    if raw_range is None:
        raw_range = ev_info.get("range_acoff") or ev_info.get("range_acon")
    if raw_range is not None and float(raw_range) > 0:
        range_val = float(raw_range)
        # In CARWINGS protocol, range_acon/acoff may be reported in meters (e.g. 125000) or km (e.g. 125)
        if range_val > 1000.0:
            range_val = range_val / 1000.0
        tlm["est_battery_range"] = round(range_val, 1)

    # 8. Temperatures (Cabin, External, Battery)
    cabin_temp = ev_info.get("cabin_temp")
    if cabin_temp is not None and float(cabin_temp) != 0.0:
        tlm["cabin_temp"] = round(float(cabin_temp), 1)

    if latest_trip:
        trip_end_epoch = parse_iso_timestamp(latest_trip.get("end_ts"))
        # If the latest probe trip ended recently (within 30 min), include probe temperatures
        if trip_end_epoch and abs(reading_epoch - trip_end_epoch) <= 1800:
            ext_t = latest_trip.get("outside_temp_end")
            batt_t = latest_trip.get("bda_avg_temp_end")
            if ext_t is not None:
                tlm["ext_temp"] = round(float(ext_t), 1)
            if batt_t is not None and float(batt_t) > -40:
                tlm["batt_temp"] = round(float(batt_t), 1)

    # 9. Speed (km/h) and Power (kW)
    # In ABRP: power > 0 means discharging/driving, power < 0 means charging.
    speed_kmh: Optional[float] = None
    power_kw: Optional[float] = None

    if is_parked and not is_charging:
        speed_kmh = 0.0
        power_kw = 0.0
    elif is_charging:
        speed_kmh = 0.0

    if prev_state is not None and reading_epoch > prev_state.timestamp_epoch:
        dt_hours = (reading_epoch - prev_state.timestamp_epoch) / 3600.0
        # Only estimate derivatives when the interval between samples is between 10s and 30 min
        if 0.0027 <= dt_hours <= 0.5:
            if curr_wh is not None and prev_state.wh_content is not None:
                delta_kwh = (prev_state.wh_content - curr_wh) / 1000.0
                calc_power = delta_kwh / dt_hours
                # Filter out physically implausible spikes (-150 kW to +200 kW)
                if -150.0 <= calc_power <= 200.0:
                    if is_charging and calc_power > 0:
                        calc_power = -calc_power
                    power_kw = round(calc_power, 2)

            if not is_parked:
                if (
                    curr_lat is not None
                    and curr_lon is not None
                    and prev_state.lat is not None
                    and prev_state.lon is not None
                ):
                    dist_km = haversine_km(prev_state.lat, prev_state.lon, curr_lat, curr_lon)
                    calc_speed = dist_km / dt_hours
                    if 0.0 <= calc_speed <= 200.0:
                        speed_kmh = round(calc_speed, 1)
                elif odometer_km is not None and prev_state.odometer_km is not None:
                    dist_km = max(0.0, odometer_km - prev_state.odometer_km)
                    calc_speed = dist_km / dt_hours
                    if 0.0 <= calc_speed <= 200.0:
                        speed_kmh = round(calc_speed, 1)

    if speed_kmh is not None:
        tlm["speed"] = speed_kmh
    if power_kw is not None:
        tlm["power"] = power_kw

    new_state = SnapshotState(
        timestamp_epoch=reading_epoch,
        wh_content=curr_wh,
        lat=curr_lat,
        lon=curr_lon,
        odometer_km=odometer_km,
        ev_updated_iso=ev_updated_iso,
    )
    return tlm, new_state


def str_to_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "sim", "on")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Synchronizes live vehicle telemetry from OpenCARWINGS to A Better Routeplanner (ABRP)."
    )
    parser.add_argument("--vin", help="Vehicle VIN (overrides OCW_VIN in .env)")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
        help="OpenCARWINGS polling interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=int(os.getenv("HEARTBEAT_SECONDS", "300")),
        help="Maximum interval in seconds between ABRP heartbeat updates when data is unchanged (default: 300)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch and send telemetry once, then exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data from OpenCARWINGS and print the ABRP JSON payload without sending it.",
    )
    parser.add_argument(
        "--active-refresh",
        action="store_true",
        default=str_to_bool(os.getenv("OCW_ACTIVE_REFRESH"), False),
        help="Send an active TCU refresh command when the vehicle is driving or charging.",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help="Use ABRP API v2 (/2/app/tlm) instead of v1 (/1/tlm/send).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose (DEBUG) logging."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ocw_url = os.getenv("OCW_BASE_URL", DEFAULT_OCW_URL)
    ocw_api_key = os.getenv("OCW_API_KEY")
    ocw_user = os.getenv("OCW_USERNAME")
    ocw_pass = os.getenv("OCW_PASSWORD")
    use_dash_soc = str_to_bool(os.getenv("OCW_USE_DASH_SOC"), True)
    gid_wh = float(os.getenv("OCW_GID_WH", "80.0"))
    active_refresh_interval = int(os.getenv("OCW_ACTIVE_REFRESH_INTERVAL", "300"))

    abrp_token = os.getenv("ABRP_USER_TOKEN", "")
    abrp_api_key = os.getenv("ABRP_API_KEY", DEFAULT_ABRP_API_KEY)
    abrp_version = "v2" if args.v2 else os.getenv("ABRP_API_VERSION", "v1")

    try:
        ocw = OpenCarWingsClient(
            base_url=ocw_url,
            api_key=ocw_api_key,
            username=ocw_user,
            password=ocw_pass,
        )
        abrp = ABRPClient(
            user_token=abrp_token if not args.dry_run else (abrp_token or "dry_run_token"),
            api_key=abrp_api_key,
            api_version=abrp_version,
        )
    except Exception as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    vin = (args.vin or os.getenv("OCW_VIN") or "").strip()
    if not vin:
        try:
            cars = ocw.list_cars()
            if not cars:
                logger.error("No vehicles found in your OpenCARWINGS account.")
                return 1
            vin = cars[0]["vin"]
            nickname = cars[0].get("nickname") or vin
            logger.info("Auto-detected vehicle: %s (VIN: %s)", nickname, vin)
        except Exception as exc:
            logger.error("Failed to list vehicles from OpenCARWINGS: %s", exc)
            return 1

    prev_state: Optional[SnapshotState] = None
    last_sent_time: float = 0.0
    last_active_refresh_time: float = 0.0

    logger.info(
        "Starting OpenCARWINGS -> ABRP (%s) bridge for VIN %s (poll interval: %ds)...",
        abrp_version.upper(),
        vin,
        args.interval,
    )

    try:
        while True:
            try:
                car_data = ocw.get_car(vin)
                latest_trip = ocw.get_latest_trip(vin)

                tlm, curr_state = build_abrp_telemetry(
                    car_data=car_data,
                    prev_state=prev_state,
                    latest_trip=latest_trip,
                    use_dash_soc=use_dash_soc,
                    gid_wh=gid_wh,
                )

                now = time.time()
                is_new_reading = (
                    prev_state is None
                    or curr_state.ev_updated_iso != prev_state.ev_updated_iso
                    or curr_state.timestamp_epoch != prev_state.timestamp_epoch
                )
                heartbeat_due = (now - last_sent_time) >= args.heartbeat

                if is_new_reading or heartbeat_due or args.once:
                    abrp_resp = abrp.send_telemetry(tlm, dry_run=args.dry_run)
                    last_sent_time = now
                    prev_state = curr_state

                    state_label = (
                        "Charging"
                        if tlm.get("is_charging")
                        else ("Parked" if tlm.get("is_parked") else "Driving")
                    )
                    logger.info(
                        "Sent to ABRP | SoC: %s%% | SoE: %s kWh | Cap: %s kWh | "
                        "Range: %s km | State: %s | GPS: (%s, %s) | Resp: %s",
                        tlm.get("soc", "-"),
                        tlm.get("soe", "-"),
                        tlm.get("capacity", "-"),
                        tlm.get("est_battery_range", "-"),
                        state_label,
                        tlm.get("lat", "-"),
                        tlm.get("lon", "-"),
                        abrp_resp,
                    )
                else:
                    logger.debug(
                        "No new OpenCARWINGS update since %s (next heartbeat in %ds).",
                        curr_state.ev_updated_iso,
                        max(0, int(args.heartbeat - (now - last_sent_time))),
                    )

                # If active refresh is enabled and the car is driving or charging,
                # request a TCU data refresh while respecting the minimum interval.
                if (
                    args.active_refresh
                    and not args.dry_run
                    and (not tlm.get("is_parked") or tlm.get("is_charging"))
                    and not car_data.get("command_requested", False)
                    and (now - last_active_refresh_time) >= active_refresh_interval
                ):
                    logger.info("Requesting active TCU data refresh (command_type=1)...")
                    ocw.request_tcu_refresh(vin)
                    last_active_refresh_time = now

            except requests.HTTPError as http_err:
                body = http_err.response.text if http_err.response is not None else ""
                logger.error("HTTP error during sync: %s | Details: %s", http_err, body)
            except Exception as exc:
                logger.error("Unexpected error during sync cycle: %s", exc)

            if args.once:
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Interrupted by user. Exiting...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
