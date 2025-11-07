#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""

RANet Bot — System + VNStat + Speedtest + Tools + Scheduler + NetBird Cache + CLI + NetBird Setup Menu

OpenWrt-ready (WIB), persist interface VNStat & speedtest history in SQLite.

IP/ISP multi-fallback (curl/wget/uclient-fetch/urllib). Memory dari `free` (MB).

+ Alerts (Disk/CPU/VNStat/Temp) + Backup/Restore (DB bot + vnstat)

+ Settings menu (kuota, suhu, fix jam)

+ NetBird Setup helper (setup-netbird.sh)

"""



import os, re, shlex, subprocess, glob, sqlite3, time, math, urllib.request, urllib.parse
import sys, asyncio, tempfile, json, stat, contextlib
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from pathlib import Path
import shutil, errno  # — PATCH: untuk copy fallback EXDEV
from dataclasses import dataclass, field


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from telegram.constants import ParseMode

from telegram.ext import (

    ApplicationBuilder, CommandHandler, CallbackQueryHandler,

    ContextTypes, MessageHandler, filters

)

from telegram.error import NetworkError, RetryAfter, TimedOut



# ================== KONFIGURASI ==================

CMD_TIMEOUT = int(os.getenv("VNSTAT_CMD_TIMEOUT", "60"))

DEFAULT_IFACE = os.getenv("VNSTAT_DEFAULT_IFACE", "")

LIVE_SECONDS = int(os.getenv("VNSTAT_LIVE_SECONDS", "5"))

TZ = timezone(timedelta(hours=7))  # WIB

DB_PATH = os.getenv("RANET_DB_PATH", "/opt/ranet-bot/speedtest.db")



SPEEDTEST_BIN_ENV = os.getenv("SPEEDTEST_BIN", "").strip()



# Path script setup netbird

SETUP_NB_SH = os.getenv("SETUP_NB_SH", "/opt/ranet-bot/setup-netbird.sh")

USB_WD_SETUP_SH = os.getenv("USB_WD_SETUP_SH", "/opt/ranet-bot/usb-watchdog-setup.sh")



# ==== PEMISAHAN TOKEN/CHAT_ID KE FILE ====

ID_FILE = os.getenv("RANET_ID_FILE", "/opt/ranet-bot/id-telegram.txt")



def _read_kv_file(path: str) -> dict:

    """

    Baca file KEY=VALUE (abaikan baris kosong/komentar '#').

    Nilai boleh diberi kutip '...' atau "..."

    """

    data = {}

    try:

        with open(path, "r", encoding="utf-8", errors="replace") as f:

            for raw in f:

                line = raw.strip()

                if not line or line.startswith("#") or "=" not in line:

                    continue

                k, v = line.split("=", 1)

                k = k.strip().upper()

                v = v.strip().strip('"').strip("'")

                data[k] = v

    except FileNotFoundError:

        pass

    except Exception:

        # Jangan bubarin bot hanya karena error baca file

        pass

    return data



_idcfg = _read_kv_file(ID_FILE)

# Prioritas: FILE > ENV > default lama

BOT_TOKEN = _idcfg.get("TOKEN") or os.getenv("VNSTAT_BOT_TOKEN", "7252280832:AAFXrJOf68MZwGNglZE8q2wHiNnL4d9NRpU")

_chat_ids_raw = _idcfg.get("CHAT_ID") or os.getenv("REPORT_CHAT_ID", "7102028483")



def _parse_chat_ids(s: str) -> List[int]:

    out = []

    for piece in re.split(r"[,\s]+", str(s).strip()):

        if not piece:

            continue

        try:

            out.append(int(piece))

        except ValueError:

            continue

    return out or [7102028483]



CHAT_IDS = _parse_chat_ids(_chat_ids_raw)

REPORT_CHAT_ID = CHAT_IDS[0]

ALLOWED_IDS = set(CHAT_IDS)  # whitelist user (boleh multi)

ORIG_BOT_TOKEN = BOT_TOKEN
ORIG_CHAT_IDS_RAW = _chat_ids_raw


def _sanitize_kv_value(value: str) -> str:
    return str(value).replace("\n", " ").strip()


def _write_kv_file(path: str, data: Dict[str, str]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_dir = directory or None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=tmp_dir, delete=False) as tmp:
        for key, value in sorted(data.items()):
            if value is None:
                continue
            tmp.write(f"{key}={value}\n")
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _refresh_runtime_credentials(cfg: Dict[str, str]):
    global _idcfg, BOT_TOKEN, _chat_ids_raw, CHAT_IDS, REPORT_CHAT_ID, ALLOWED_IDS
    _idcfg = dict(cfg)
    token = cfg.get("TOKEN") or os.getenv("VNSTAT_BOT_TOKEN") or ORIG_BOT_TOKEN
    BOT_TOKEN = token
    raw_ids = cfg.get("CHAT_ID") or os.getenv("REPORT_CHAT_ID") or ORIG_CHAT_IDS_RAW
    _chat_ids_raw = raw_ids
    chat_ids = _parse_chat_ids(raw_ids)
    CHAT_IDS = chat_ids
    REPORT_CHAT_ID = chat_ids[0]
    ALLOWED_IDS = set(chat_ids)


def persist_credentials(new_token: Optional[str] = None, new_chat_ids: Optional[List[int]] = None) -> Tuple[bool, Optional[str]]:
    cfg = _read_kv_file(ID_FILE)
    if not cfg:
        cfg = {}
    for key, value in _idcfg.items():
        cfg.setdefault(key, value)
    if new_token is not None:
        cfg["TOKEN"] = _sanitize_kv_value(new_token)
    if new_chat_ids is not None:
        cfg["CHAT_ID"] = ",".join(str(i) for i in new_chat_ids)
    try:
        _write_kv_file(ID_FILE, cfg)
    except Exception as exc:
        return False, str(exc)
    _refresh_runtime_credentials(cfg)
    return True, None


def schedule_bot_restart(application, delay: float = 3.0) -> None:
    """Minta runner/service me-restart bot dengan cara keluar dari proses utama."""

    if application is None:
        return

    delay = max(0.1, float(delay or 0))

    async def _restart():
        await asyncio.sleep(delay)
        restart_cmd = os.getenv("RANET_RESTART_CMD", "").strip()
        if restart_cmd:
            try:
                proc = await asyncio.create_subprocess_shell(restart_cmd)
                await proc.communicate()
            except Exception:
                pass
        os._exit(0)

    try:
        application.create_task(_restart())
    except Exception:
        pass


def current_token(mask: bool = False) -> str:
    cfg = _read_kv_file(ID_FILE)
    token = cfg.get("TOKEN") or BOT_TOKEN
    if not mask:
        return token
    if not token:
        return "-"
    token = token.strip()
    if len(token) <= 10:
        return token
    return f"{token[:6]}…{token[-4:]}"


def current_chat_ids() -> List[int]:
    cfg = _read_kv_file(ID_FILE)
    raw = cfg.get("CHAT_ID") or _chat_ids_raw
    return _parse_chat_ids(raw)



# Laporan otomatis (overview + vnstat -d)

REPORT_HOUR = int(os.getenv("REPORT_HOUR", "6"))  # 06:00 WIB

DISK_THRESH_PCT = int(os.getenv("DISK_THRESH_PCT", "10"))



# Preset server speedtest (Ookla server-id).

SPEEDTEST_PRESETS = [

    ("CBN - Singapore", "59016"),

    ("Biznet - Jakarta", "36927"),

    ("Telkom - Jakarta", "40703"),

    ("MyRepublic - SG", "7556"),

    ("Singtel - SG", "21541"),

]



# ===== Alerts & Backup/Restore =====

CPU_LOAD_THRESH = float(os.getenv("CPU_LOAD_THRESH", "0.90"))   # rasio load/core, 0.90=90%

VNSTAT_GIB_LIMIT = float(os.getenv("VNSTAT_GIB_LIMIT", "500"))  # ambang pemakaian bulan ini (GiB)

BACKUP_DIR = os.getenv("RANET_BACKUP_DIR", "/tmp")              # lokasi file backup .tgz

VNSTAT_DB_DIR = os.getenv("VNSTAT_DB_DIR", "/etc/vnstat")       # direktori database vnstat

TEMP_ALERT_DEFAULT = float(os.getenv("TEMP_ALERT_LIMIT_C", "75"))  # ambang suhu default (°C)

# ==========================================



def _resolve_bot_file() -> str:

    custom = os.getenv("RANET_BOT_FILE")

    return os.path.realpath(custom) if custom else os.path.realpath(__file__)



BOT_FILE_PATH = _resolve_bot_file()

BOT_BACKUP_PATH = os.path.realpath(os.getenv("RANET_BOT_BACKUP_FILE", f"{BOT_FILE_PATH}.bak"))

BOT_UPDATE_URL = os.getenv(

    "RANET_BOT_UPDATE_URL",

    "https://raw.githubusercontent.com/Ridhan354/ra-wrt-v2/main/ra-bot.py",

)



# ------------------ UTIL CMD ---------------------

def run_cmd(cmd: str, timeout: Optional[int] = None) -> str:

    try:

        out = subprocess.check_output(shlex.split(cmd), stderr=subprocess.STDOUT, timeout=timeout or CMD_TIMEOUT)

        return out.decode("utf-8", errors="replace").rstrip()

    except subprocess.CalledProcessError as e:

        return f"[ERR] Command failed ({cmd}):\n{e.output.decode('utf-8', errors='replace')}"

    except subprocess.TimeoutExpired:

        return f"[ERR] Command timeout ({cmd}) after {(timeout or CMD_TIMEOUT)}s"

    except Exception as e:

        return f"[ERR] {e}"



def run_shell(cmd: str, timeout: Optional[int] = None) -> str:

    try:

        out = subprocess.check_output(["/bin/sh", "-c", cmd], stderr=subprocess.STDOUT, timeout=timeout or CMD_TIMEOUT)

        return out.decode("utf-8", errors="replace").rstrip()

    except subprocess.CalledProcessError as e:

        return e.output.decode("utf-8", errors="replace").rstrip() or f"[exit {e.returncode}]"

    except subprocess.TimeoutExpired:

        return f"[ERR] Shell command timeout after {(timeout or CMD_TIMEOUT)}s"

    except Exception as e:

        return f"[ERR] {e}"


async def telegram_call_with_retry(fn, *args, retries: int = 3, retry_delay: float = 3.0, **kwargs):

    attempt = 0

    while True:

        try:

            return await fn(*args, **kwargs)

        except RetryAfter as exc:

            await asyncio.sleep(float(getattr(exc, "retry_after", 1)) + 1.0)

        except (TimedOut, NetworkError):

            attempt += 1

            if attempt > retries:

                raise

            await asyncio.sleep(retry_delay * attempt)


def usb_watchdog_available() -> bool:

    return os.path.exists(USB_WD_SETUP_SH) and os.access(USB_WD_SETUP_SH, os.X_OK)


def run_usb_watchdog_cmd(*args: str) -> str:

    if not usb_watchdog_available():

        return "[ERR] Script usb-watchdog-setup.sh tidak ditemukan. Jalankan update installer untuk mendapatkannya."

    cmd_parts = [shlex.quote(USB_WD_SETUP_SH)]

    cmd_parts.extend(shlex.quote(arg) for arg in args if arg is not None and arg != "")

    return run_shell(" ".join(cmd_parts))


def parse_usb_watchdog_input(text: str) -> Tuple[bool, str, Dict[str, Optional[str]]]:

    tokens = re.split(r"[\s,]+", text.strip())

    mapping: Dict[str, str] = {}

    positional: List[str] = []

    for token in tokens:

        if not token:

            continue

        if "=" in token:

            key, value = token.split("=", 1)

            mapping[key.strip().lower()] = value.strip()

        else:

            positional.append(token.strip())

    interface = mapping.get("interface") or mapping.get("iface") or mapping.get("if")

    if not interface and positional:

        interface = positional.pop(0)

    interval = mapping.get("interval") or mapping.get("check_interval") or mapping.get("sec")

    if interval is None and positional:

        interval = positional.pop(0)

    attempts = mapping.get("max_attempts") or mapping.get("max") or mapping.get("attempts")

    if attempts is None and positional:

        attempts = positional.pop(0)

    log_file = mapping.get("log_file") or mapping.get("logfile") or mapping.get("log")

    if log_file is None and positional:

        log_file = positional.pop(0)

    logging = mapping.get("logging") or mapping.get("logging_enabled") or mapping.get("enable_logging")

    if logging is None and positional:

        logging = positional.pop(0)

    if not interface:

        return False, "❌ Interface wajib diisi (contoh: usb0 atau wwan0).", {}

    params = {

        "interface": interface,

        "interval": interval,

        "attempts": attempts,

        "log_file": log_file,

        "logging": logging,

    }

    return True, "", params


def usb_watchdog_configure(params: Dict[str, Optional[str]]) -> str:

    args = ["configure", "--interface", params["interface"] or "usb0"]

    interval = params.get("interval")

    if interval:

        args.extend(["--interval", str(interval)])

    attempts = params.get("attempts")

    if attempts:

        args.extend(["--max-attempts", str(attempts)])

    log_file = params.get("log_file")

    if log_file:

        args.extend(["--log-file", log_file])

    logging = params.get("logging")

    if logging:

        args.extend(["--logging", logging])

    return run_usb_watchdog_cmd(*args)



def which(bin_name: str) -> bool:

    out = run_cmd(f"which {shlex.quote(bin_name)}")

    return (not out.startswith("[ERR]")) and bool(out.strip())



def code_block(txt: str) -> str:

    return f"```\n{txt}\n```"



def mdv2_escape(s: str) -> str:

    # escape semua karakter spesial MarkdownV2

    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s)



async def edit_progress(msg, text):

    # aman untuk MarkdownV2 + code block

    if "```" in text:

        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    else:

        await msg.edit_text(mdv2_escape(text), parse_mode=ParseMode.MARKDOWN_V2)



def split_chunks(text: str, limit: int = 3800):

    lines, buf, size = text.splitlines(True), [], 0

    for ln in lines:

        n = len(ln)

        if size + n > limit and buf:

            yield "".join(buf); buf, size = [ln], n

        else:

            buf.append(ln); size += n

    if buf: yield "".join(buf)



def allowed(update: Update) -> bool:

    uid = update.effective_user.id if update.effective_user else None

    return (uid in ALLOWED_IDS)



# ------------------ ANDROID SUPPORT ------------------


@dataclass
class AndroidDevice:

    serial: str

    status: str

    model: str = ""

    product: str = ""

    device: str = ""

    transport_id: str = ""

    extra: Dict[str, str] = field(default_factory=dict)

    raw: str = ""


@dataclass
class AndroidSMS:

    ts_ms: Optional[int]

    address: str

    body: str

    raw_ts: str = ""


ANDROID_SMS_DB_PATHS = [

    "/data/data/com.android.providers.telephony/databases/mmssms.db",

    "/data/user_de/0/com.android.providers.telephony/databases/mmssms.db",

]


def android_adb_available() -> bool:

    return shutil.which("adb") is not None


def android_exec(device: Optional[str], *args: str, timeout: Optional[int] = None) -> str:

    cmd = ["adb"]

    if device:

        cmd.extend(["-s", device])

    cmd.extend(args)

    try:

        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout or CMD_TIMEOUT)

        return out.decode("utf-8", errors="replace").strip()

    except subprocess.CalledProcessError as exc:

        detail = exc.output.decode("utf-8", errors="replace").strip()

        return f"[ERR] {detail or exc}"

    except subprocess.TimeoutExpired:

        return f"[ERR] adb timeout after {(timeout or CMD_TIMEOUT)}s"

    except FileNotFoundError:

        return "[ERR] adb tidak ditemukan di PATH"

    except Exception as exc:

        return f"[ERR] {exc}"


def android_shell(device: str, *cmd: str, timeout: Optional[int] = None) -> str:

    return android_exec(device, "shell", *cmd, timeout=timeout)


def android_shell_root(device: str, command: str, timeout: Optional[int] = None) -> str:

    return android_exec(device, "shell", "su", "-c", command, timeout=timeout)


def android_list_devices() -> List[AndroidDevice]:

    out = android_exec(None, "devices", "-l")

    if not out or out.startswith("[ERR]"):

        return []

    devices: List[AndroidDevice] = []

    for raw in out.splitlines():

        line = raw.strip()

        if not line or line.lower().startswith("list of devices"):

            continue

        parts = line.split()

        if len(parts) < 2:

            continue

        serial, status = parts[0], parts[1]

        extras: Dict[str, str] = {}

        for piece in parts[2:]:

            if ":" in piece:

                k, v = piece.split(":", 1)

                extras[k] = v

        devices.append(AndroidDevice(

            serial=serial,

            status=status,

            model=extras.get("model", ""),

            product=extras.get("product", ""),

            device=extras.get("device", ""),

            transport_id=extras.get("transport_id", ""),

            extra=extras,

            raw=line,

        ))

    return devices


def android_device_label(dev: AndroidDevice) -> str:

    base = dev.model or dev.product or dev.device or dev.extra.get("product", "") or "-"

    if dev.status and dev.status != "device":

        return f"{base} ({dev.status})"

    return base


def android_choice_label(dev: AndroidDevice) -> str:

    base = android_device_label(dev)

    if base == "-":

        return dev.serial

    label = f"{dev.serial} · {base}"

    return label if len(label) <= 60 else f"{label[:57]}…"


def android_selected_device(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:

    if ctx.application:

        return ctx.application.bot_data.get("android_device")

    return None


def android_selected_label(ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:

    if ctx.application:

        return ctx.application.bot_data.get("android_device_label")

    return None


def android_set_selected_device(ctx: ContextTypes.DEFAULT_TYPE, serial: str, label: str) -> None:

    if ctx.application:

        ctx.application.bot_data["android_device"] = serial

        ctx.application.bot_data["android_device_label"] = label


def android_clear_selected_device(ctx: ContextTypes.DEFAULT_TYPE) -> None:

    if ctx.application:

        ctx.application.bot_data.pop("android_device", None)

        ctx.application.bot_data.pop("android_device_label", None)


def android_resolve_selection(ctx: ContextTypes.DEFAULT_TYPE, devices: List[AndroidDevice]) -> Tuple[Optional[str], Optional[str]]:

    selected = android_selected_device(ctx)

    label = android_selected_label(ctx)

    if not selected:

        return None, None

    for dev in devices:

        if dev.serial == selected and dev.status == "device":

            if not label:

                label = android_device_label(dev)

                android_set_selected_device(ctx, selected, label)

            return selected, label

    android_clear_selected_device(ctx)

    return None, None


def android_ensure_selection(ctx: ContextTypes.DEFAULT_TYPE, devices: List[AndroidDevice]) -> Tuple[Optional[str], Optional[str]]:

    selected, label = android_resolve_selection(ctx, devices)

    if selected:

        return selected, label

    ready = [dev for dev in devices if dev.status == "device"]

    if len(ready) == 1:

        dev = ready[0]

        label = android_device_label(dev)

        android_set_selected_device(ctx, dev.serial, label)

        return dev.serial, label

    return None, None


def android_device_ready(serial: str) -> bool:

    state = android_exec(serial, "get-state")

    return bool(state) and (not state.startswith("[ERR]")) and (state.strip() == "device")


def android_has_root(serial: str) -> bool:

    res = android_exec(serial, "shell", "su", "-c", "id")

    return bool(res) and (not res.startswith("[ERR]")) and ("uid=0" in res)


def android_getprop(serial: str, prop: str) -> str:

    out = android_shell(serial, "getprop", prop)

    if not out or out.startswith("[ERR]"):

        return ""

    return out.strip()


def android_sdk(serial: str) -> Optional[int]:

    val = android_getprop(serial, "ro.build.version.sdk")

    try:

        return int(val)

    except (TypeError, ValueError):

        return None


def android_fmt_duration(seconds: int) -> str:

    d, rem = divmod(max(0, int(seconds)), 86400)

    h, rem = divmod(rem, 3600)

    m, s = divmod(rem, 60)

    return f"{d}d {h:02d}:{m:02d}:{s:02d}"


def android_uptime_value(serial: str) -> str:

    out = android_shell(serial, "cat", "/proc/uptime")

    if not out or out.startswith("[ERR]"):

        return "?"

    try:

        secs = int(float(out.split()[0]))

    except (ValueError, IndexError):

        return "?"

    return android_fmt_duration(secs)


def android_battery_text(serial: str) -> str:

    raw = android_shell(serial, "dumpsys", "battery")

    if not raw or raw.startswith("[ERR]"):

        return "Battery: ?"

    def _val(key: str) -> Optional[str]:

        m = re.search(rf"{re.escape(key)}:\s*(\S+)", raw)

        return m.group(1) if m else None

    level = _val("level") or "?"

    status_code = _val("status") or "0"

    status_map = {"2": "Charging", "3": "Discharging", "4": "Not charging", "5": "Full"}

    status = status_map.get(status_code, "Unknown")

    temp_raw = _val("temperature") or ""

    temp_text = "?"

    if temp_raw:

        try:

            val = int(temp_raw)

            if val > 1000:

                temp_text = f"{val / 1000:.1f}"

            else:

                temp_text = f"{val / 10:.1f}"

        except ValueError:

            temp_text = temp_raw

    volt = _val("voltage") or "?"

    return f"Battery: {level}% ({status}), Temp: {temp_text}°C, Volt: {volt} mV"


def android_airplane_status(serial: str) -> Tuple[str, Optional[bool], str]:

    out = android_shell(serial, "cmd", "connectivity", "airplane-mode")

    if out and not out.startswith("[ERR]"):

        m = re.search(r"enabled:?\s*(\w+)", out)

        if m:

            val = m.group(1).strip().lower()

            if val in {"true", "false"}:

                return "new", (val == "true"), out

    legacy = android_shell(serial, "settings", "get", "global", "airplane_mode_on")

    if legacy and not legacy.startswith("[ERR]"):

        val = legacy.strip()

        if val == "1":

            return "legacy", True, legacy

        if val == "0":

            return "legacy", False, legacy

    return "unknown", None, out or legacy or ""


def android_mobile_data_status(serial: str) -> Tuple[Optional[bool], str]:

    out = android_shell(serial, "settings", "get", "global", "mobile_data")

    if out and not out.startswith("[ERR]"):

        val = out.strip()

        if val == "1":

            return True, out

        if val == "0":

            return False, out

    return None, out or ""


def android_network_text(serial: str) -> str:

    operator = android_getprop(serial, "gsm.sim.operator.alpha") or "?"

    network = android_getprop(serial, "gsm.network.type") or "?"

    route_raw = android_shell(serial, "ip", "route")

    if not route_raw or route_raw.startswith("[ERR]"):

        route_raw = android_shell(serial, "ip", "route", "show")

    route_line = "?"

    if route_raw and not route_raw.startswith("[ERR]"):

        for ln in route_raw.splitlines():

            ln = ln.strip()

            if ln:

                route_line = ln

                break

    return f"Operator: {operator} | RAT: {network}\nRoute: {route_line}"


def android_memory_text(serial: str) -> str:

    raw = android_shell(serial, "cat", "/proc/meminfo")

    if not raw or raw.startswith("[ERR]"):

        return raw or "?"

    def _extract(key: str) -> Optional[str]:

        m = re.search(rf"{key}:\s*(\d+)\s*(\w+)?", raw)

        if m:

            val, unit = m.group(1), m.group(2) or ""

            return f"{val} {unit}".strip()

        return None

    total = _extract("MemTotal") or "?"

    avail = _extract("MemAvailable") or "?"

    return f"total={total}, available={avail}"


def android_storage_info_lines(serial: str) -> Tuple[str, List[str]]:

    raw = android_shell(serial, "dumpsys", "diskstats")

    if raw and not raw.startswith("[ERR]"):

        lines = [ln.rstrip() for ln in raw.splitlines()[:15]]

        return "Storage (diskstats)", lines

    raw = android_shell(serial, "df", "-h", "/data")

    if raw and not raw.startswith("[ERR]"):

        return "Storage (df -h /data)", [ln.rstrip() for ln in raw.splitlines()]

    return "Storage", [(raw or "(tidak tersedia)").strip()]


def android_process_lines(serial: str) -> List[str]:

    raw = android_shell(serial, "ps", "-eo", "pid,user,%cpu,%mem,cmd,time+")

    if not raw or raw.startswith("[ERR]"):

        raw = android_shell(serial, "ps")

    if not raw or raw.startswith("[ERR]"):

        return [(raw or "(tidak tersedia)").strip()]

    return [ln.rstrip() for ln in raw.splitlines()[:15]]


def android_signal_strength_text(serial: str) -> str:

    raw = android_shell(serial, "dumpsys", "telephony.registry")

    if not raw or raw.startswith("[ERR]"):

        return raw or "Signal: unknown"

    for ln in raw.splitlines():

        if "mSignalStrength=SignalStrength" in ln:

            part = ln.split("mSignalStrength=SignalStrength:", 1)[-1].strip()

            return f"Signal: {part}" if part else "Signal: unknown"

    return "Signal: unknown"


def android_collect_info(serial: str) -> Dict[str, Any]:

    info: Dict[str, Any] = {

        "serial": serial,

        "model": android_getprop(serial, "ro.product.model") or "-",

        "product": android_getprop(serial, "ro.product.name") or "-",

        "android_version": android_getprop(serial, "ro.build.version.release") or "?",

    }

    sdk_int = android_sdk(serial)

    info["sdk_int"] = sdk_int

    info["sdk_text"] = str(sdk_int) if sdk_int is not None else "?"

    info["uptime"] = android_uptime_value(serial)

    info["battery_text"] = android_battery_text(serial)

    mode, enabled, _ = android_airplane_status(serial)

    if mode == "new":

        info["airplane_text"] = f"Airplane Mode: {'enabled' if enabled else 'disabled'} (new API)"

    elif mode == "legacy":

        info["airplane_text"] = f"Airplane Mode: {'true' if enabled else 'false'} (legacy)"

    else:

        info["airplane_text"] = "Airplane Mode: unknown"

    mobile, _ = android_mobile_data_status(serial)

    if mobile is True:

        info["mobile_data_text"] = "Mobile Data: enabled"

    elif mobile is False:

        info["mobile_data_text"] = "Mobile Data: disabled"

    else:

        info["mobile_data_text"] = "Mobile Data: unknown"

    info["network_text"] = android_network_text(serial)

    info["memory_text"] = android_memory_text(serial)

    storage_title, storage_lines = android_storage_info_lines(serial)

    info["storage_title"] = storage_title

    info["storage_lines"] = storage_lines

    info["process_lines"] = android_process_lines(serial)

    return info


def android_summary_text(info: Dict[str, Any], label: Optional[str] = None) -> str:

    display_label = label or info.get("model") or info.get("product") or "-"

    lines = [

        f"Device : {info.get('serial', '-') } ({display_label})",

        f"Model  : {info.get('model', '-')}",

        f"Product: {info.get('product', '-')}",

        f"Android: {info.get('android_version', '?')} (SDK {info.get('sdk_text', '?')})",

        "",

        f"Uptime : {info.get('uptime', '?')}",

        info.get("battery_text", "Battery: ?"),

        f"{info.get('airplane_text', 'Airplane Mode: unknown')} | {info.get('mobile_data_text', 'Mobile Data: unknown')}",

        info.get("network_text", "Network: ?"),

        "",

        "Memory:",

        f"  {info.get('memory_text', '?')}",

        "",

        f"{info.get('storage_title', 'Storage')}:",

    ]

    storage_lines = info.get("storage_lines") or []

    if storage_lines:

        lines.extend(f"  {ln}" for ln in storage_lines)

    else:

        lines.append("  (tidak tersedia)")

    lines.append("")

    lines.append("Processes (top 15):")

    proc_lines = info.get("process_lines") or []

    if proc_lines:

        lines.extend(f"  {ln}" for ln in proc_lines)

    else:

        lines.append("  (tidak tersedia)")

    return "\n".join(lines)


def android_parse_content_sms(output: str) -> List[AndroidSMS]:

    entries: List[AndroidSMS] = []

    if not output or output.startswith("[ERR]"):

        return entries

    for line in output.splitlines():

        line = line.strip()

        if not line or "address=" not in line or " body=" not in line:

            continue

        try:

            addr_part = line.split("address=", 1)[1]

            addr, rest = addr_part.split(" body=", 1)

            body_part, rest = rest.split(" date=", 1)

        except ValueError:

            continue

        date_raw = rest.split()[0]

        address = addr.strip()

        body = body_part.replace("\\n", " ").replace("\\r", " ").strip()

        raw_ts = date_raw.strip()

        ts_ms: Optional[int] = None

        if raw_ts.isdigit():

            try:

                ts_ms = int(raw_ts)

            except ValueError:

                ts_ms = None

        entries.append(AndroidSMS(ts_ms=ts_ms, address=address, body=body, raw_ts=raw_ts))

    entries.sort(key=lambda sms: sms.ts_ms or 0, reverse=True)

    return entries[:5]


def android_parse_sqlite_sms(output: str) -> List[AndroidSMS]:

    entries: List[AndroidSMS] = []

    if not output or output.startswith("[ERR]"):

        return entries

    for line in output.splitlines():

        if not line.strip():

            continue

        parts = line.split("|")

        if len(parts) < 3:

            continue

        address, body, raw_ts = parts[0].strip(), parts[1].strip(), parts[2].strip()

        ts_ms: Optional[int] = None

        if raw_ts.isdigit():

            try:

                ts_ms = int(raw_ts)

            except ValueError:

                ts_ms = None

        entries.append(AndroidSMS(ts_ms=ts_ms, address=address, body=body, raw_ts=raw_ts))

    entries.sort(key=lambda sms: sms.ts_ms or 0, reverse=True)

    return entries[:5]


def android_content_query(serial: str, sort: bool = True) -> str:

    base = "content query --user 0 --uri content://sms/inbox --projection address,body,date"

    if sort:

        base += ' --sort "date DESC"'

    return android_shell(serial, "sh", "-c", base)


def android_content_query_root(serial: str, sort: bool = True) -> str:

    base = "content query --user 0 --uri content://sms/inbox --projection address,body,date"

    if sort:

        base += ' --sort "date DESC"'

    return android_shell_root(serial, base)


def android_device_has_sqlite(serial: str) -> bool:

    res = android_shell_root(serial, "which sqlite3")

    return bool(res) and not res.startswith("[ERR]")


def android_sqlite_query_device(serial: str, path: str) -> List[AndroidSMS]:

    cmd = f"sqlite3 '{path}' \"SELECT address, body, date FROM sms WHERE type=1 ORDER BY date DESC LIMIT 5;\""

    out = android_shell_root(serial, cmd)

    return android_parse_sqlite_sms(out)


def android_sqlite_read_local(path: Path) -> List[AndroidSMS]:

    entries: List[AndroidSMS] = []

    conn = None

    try:

        conn = sqlite3.connect(path)

        cur = conn.cursor()

        rows = cur.execute(

            "SELECT address, body, date FROM sms WHERE type=1 ORDER BY date DESC LIMIT 5;"

        ).fetchall()

    except Exception:

        rows = []

    finally:

        if conn:

            conn.close()

    for address, body, raw_ts in rows:

        text_ts = str(raw_ts)

        ts_ms: Optional[int] = None

        if text_ts.isdigit():

            try:

                ts_ms = int(text_ts)

            except ValueError:

                ts_ms = None

        entries.append(

            AndroidSMS(

                ts_ms=ts_ms,

                address=str(address or ""),

                body=str(body or ""),

                raw_ts=text_ts,

            )

        )

    entries.sort(key=lambda sms: sms.ts_ms or 0, reverse=True)

    return entries[:5]


def android_sqlite_pull_and_query(serial: str) -> List[AndroidSMS]:

    remote_tmp = "/sdcard/mmssms.db"

    for src in ANDROID_SMS_DB_PATHS:

        tmpdir = Path(tempfile.mkdtemp())

        local_path = tmpdir / "mmssms.db"

        try:

            copy_cmd = (

                f"cp {shlex.quote(src)} {shlex.quote(remote_tmp)} && "

                f"chmod 0644 {shlex.quote(remote_tmp)}"

            )

            res = android_shell_root(serial, copy_cmd)

            if res.startswith("[ERR]"):

                continue

            pull = android_exec(serial, "pull", remote_tmp, str(local_path))

            if pull.startswith("[ERR]") or (not local_path.exists()):

                continue

            entries = android_sqlite_read_local(local_path)

            if entries:

                return entries

        finally:

            android_shell(serial, "rm", remote_tmp)

            shutil.rmtree(tmpdir, ignore_errors=True)

    return []


def android_fetch_sms_entries(serial: str, sdk_int: Optional[int]) -> Tuple[List[AndroidSMS], Optional[str]]:

    out = android_content_query(serial, sort=True)

    entries = android_parse_content_sms(out)

    if entries:

        return entries, None

    out = android_content_query(serial, sort=False)

    entries = android_parse_content_sms(out)

    if entries:

        return entries, None

    has_root = android_has_root(serial)

    if has_root:

        out = android_content_query_root(serial, sort=True)

        entries = android_parse_content_sms(out)

        if entries:

            return entries, None

    if sdk_int is not None and sdk_int >= 28:

        if has_root:

            out = android_content_query_root(serial, sort=False)

            entries = android_parse_content_sms(out)

            if entries:

                return entries, None

        return [], "Tidak bisa ambil SMS di Android ≥9 (ROM/izin?)."

    if not has_root:

        return [], "Butuh root untuk SMS di Android < 9."

    out = android_content_query_root(serial, sort=False)

    entries = android_parse_content_sms(out)

    if entries:

        return entries, None

    if android_device_has_sqlite(serial):

        for path in ANDROID_SMS_DB_PATHS:

            entries = android_sqlite_query_device(serial, path)

            if entries:

                return entries, None

    entries = android_sqlite_pull_and_query(serial)

    if entries:

        return entries, None

    return [], "Gagal akses SMS di Android < 9 meski root. Pastikan sqlite3 tersedia (device/host)."


def android_format_sms(entries: List[AndroidSMS]) -> str:

    if not entries:

        return "(Tidak ada SMS)"

    lines: List[str] = []

    for sms in entries:

        if sms.ts_ms is not None and sms.ts_ms > 0:

            try:

                ts = datetime.fromtimestamp(sms.ts_ms / 1000, tz=timezone.utc).astimezone(TZ)

                ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")

            except Exception:

                ts_text = sms.raw_ts or "-"

        else:

            ts_text = sms.raw_ts or "-"

        addr = sms.address or "-"

        body = sms.body.replace("\r", " ").replace("\n", " ")

        lines.append(f"{ts_text}  |  {addr}")

        lines.append(f"  {body[:300]}")

        lines.append("")

    return "\n".join(lines).strip()


def android_sms_text(serial: str, sdk_int: Optional[int], limit: int = 5) -> Tuple[str, Optional[str]]:

    entries, error = android_fetch_sms_entries(serial, sdk_int)

    if entries:
        entries = entries[: max(1, limit)]

    return android_format_sms(entries), error


def android_toggle_airplane(serial: str, pause_seconds: float = 3.0) -> str:

    mode, _, _ = android_airplane_status(serial)

    if mode == "unknown":

        return "❌ Tidak bisa membaca status Airplane Mode."

    wait_time = max(0.0, float(pause_seconds))

    def _pause() -> None:

        if wait_time > 0:

            time.sleep(wait_time)

    if mode == "new":

        res = android_shell(serial, "cmd", "connectivity", "airplane-mode", "enable")

        if res and res.startswith("[ERR]"):

            return f"❌ Gagal mengaktifkan Airplane Mode: {res}"

        _pause()

        res = android_shell(serial, "cmd", "connectivity", "airplane-mode", "disable")

        if res and res.startswith("[ERR]"):

            return f"❌ Gagal mematikan Airplane Mode: {res}"

        _, final_state, _ = android_airplane_status(serial)

        if final_state:

            return "⚠️ Airplane Mode tetap aktif setelah refresh. Nonaktifkan manual."

        return "✈️ Airplane Mode dinyalakan sementara untuk refresh IP dan sudah dimatikan lagi."

    if android_has_root(serial):

        enable_cmd = (

            "settings put global airplane_mode_on 1; "

            "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true"

        )

        disable_cmd = (

            "settings put global airplane_mode_on 0; "

            "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false"

        )

        res = android_shell_root(serial, enable_cmd)

        if res and res.startswith("[ERR]"):

            return f"❌ Gagal mengaktifkan Airplane Mode: {res}"

        _pause()

        res = android_shell_root(serial, disable_cmd)

        if res and res.startswith("[ERR]"):

            return f"❌ Gagal mematikan Airplane Mode: {res}"

        _, final_state, _ = android_airplane_status(serial)

        if final_state:

            return "⚠️ Airplane Mode tetap aktif setelah refresh. Nonaktifkan manual."

        return "✈️ Airplane Mode dinyalakan sementara (legacy) dan sudah dimatikan lagi."

    res = android_shell(serial, "settings", "put", "global", "airplane_mode_on", "1")

    if res and res.startswith("[ERR]"):

        return f"❌ Gagal mengaktifkan Airplane Mode: {res}"

    _pause()

    res = android_shell(serial, "settings", "put", "global", "airplane_mode_on", "0")

    if res and res.startswith("[ERR]"):

        return f"❌ Gagal mematikan Airplane Mode: {res}"

    return "⚠️ Airplane Mode direfresh tanpa root (mungkin tidak persist)."



def android_safe_serial(serial: str) -> str:

    return re.sub(r"[^A-Za-z0-9_.-]", "_", serial or "device")


def android_export_report(serial: str, info: Dict[str, Any], sms_text: str) -> Path:

    reports_dir = Path("reports")

    reports_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=TZ).strftime("%Y%m%d-%H%M%S")

    filename = reports_dir / f"adbreport-{android_safe_serial(serial)}-{ts}.md"

    with open(filename, "w", encoding="utf-8") as fh:

        fh.write("# ADB Device Report\n")

        fh.write(f"- **Device**: `{serial}`\n")

        fh.write(f"- **Model**: {info.get('model', '-') }\n")

        fh.write(f"- **Product**: {info.get('product', '-') }\n")

        fh.write(f"- **Android**: {info.get('android_version', '?')} (SDK {info.get('sdk_text', '?')})\n")

        fh.write(f"- **Generated**: {datetime.now(tz=TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n")

        fh.write("## Summary\n")

        fh.write(f"- Uptime: {info.get('uptime', '?')}\n")

        fh.write(f"- {info.get('battery_text', 'Battery: ?')}\n")

        fh.write(f"- {info.get('airplane_text', 'Airplane Mode: unknown')}\n")

        fh.write(f"- {info.get('mobile_data_text', 'Mobile Data: unknown')}\n")

        network_line = (info.get('network_text', 'Network: ?') or '').replace('\n', ' | ')
        fh.write(f"- {network_line}\n\n")

        fh.write("## Memory\n")

        fh.write(f"{info.get('memory_text', '?')}\n\n")

        fh.write("## Storage\n")

        for ln in info.get("storage_lines", []):

            fh.write(f"    {ln}\n")

        if not info.get("storage_lines"):

            fh.write("    (tidak tersedia)\n")

        fh.write("\n## Top Processes\n")

        for ln in info.get("process_lines", []):

            fh.write(f"    {ln}\n")

        if not info.get("process_lines"):

            fh.write("    (tidak tersedia)\n")

        fh.write("\n## Last 5 SMS (Inbox)\n")

        if sms_text.strip():

            for ln in sms_text.splitlines():

                fh.write(f"    {ln}\n")

        else:

            fh.write("    (Tidak ada SMS)\n")

    return filename


def android_menu_message(devices: List[AndroidDevice], selected: Optional[str], label: Optional[str]) -> str:

    lines: List[str] = ["🤖 *Android Device Center*"]

    if not devices:

        lines.append("Tidak ada device ADB yang terdeteksi. Pastikan perangkat terhubung dan USB debugging aktif.")

        return "\n".join(lines)

    if selected:

        display_label = label

        if not display_label:

            for dev in devices:

                if dev.serial == selected:

                    display_label = android_device_label(dev)

                    break

        if display_label:

            lines.append(f"Device aktif: `{mdv2_escape(selected)}` — {mdv2_escape(display_label)}")

        else:

            lines.append(f"Device aktif: `{mdv2_escape(selected)}`")

    else:

        lines.append("Pilih device yang ingin digunakan dengan tombol di bawah.")

    lines.append("")

    lines.append("*Daftar device:*")

    for dev in devices:

        status_icon = "✅" if dev.status == "device" else "⚠️" if dev.status in {"unauthorized", "offline"} else "ℹ️"

        desc = android_device_label(dev)

        lines.append(f"{status_icon} `{mdv2_escape(dev.serial)}` — {mdv2_escape(desc)}")

    return "\n".join(lines)


async def android_signal_monitor_task(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, serial: str,

                                    samples: int = 10, interval: float = 2.0) -> None:

    for _ in range(max(1, samples)):

        info = android_signal_strength_text(serial)

        now = datetime.now(tz=TZ).strftime("%H:%M:%S")

        try:

            await ctx.bot.send_message(chat_id=chat_id, text=f"{now}  {info}")

        except Exception:

            break

        await asyncio.sleep(interval)

    try:

        await ctx.bot.send_message(chat_id=chat_id, text="✅ Monitor sinyal selesai.")

    except Exception:

        pass


# ------------------ DB (Speedtest + Settings + Alerts) -----

def db_connect():

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    return sqlite3.connect(DB_PATH)



def db_init():

    conn = db_connect(); cur = conn.cursor()

    cur.execute("""

        CREATE TABLE IF NOT EXISTS results (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            ts INTEGER NOT NULL,

            latency_ms REAL,

            jitter_ms REAL,

            download_mbps REAL,

            upload_mbps REAL,

            loss_pct REAL,

            url TEXT

        )

    """)

    cur.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, value TEXT)""")

    conn.commit(); conn.close()



def settings_get(key: str, default: Optional[str] = None) -> Optional[str]:

    conn = db_connect(); cur = conn.cursor()

    try:

        cur.execute("SELECT value FROM settings WHERE key=?", (key,))

    except sqlite3.OperationalError:

        conn.close()

        db_init()

        conn = db_connect(); cur = conn.cursor()

        cur.execute("SELECT value FROM settings WHERE key=?", (key,))

    row = cur.fetchone(); conn.close()

    return row[0] if row else default



def settings_set(key: str, value: str):

    conn = db_connect(); cur = conn.cursor()

    try:

        cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    except sqlite3.OperationalError:

        conn.close()

        db_init()

        conn = db_connect(); cur = conn.cursor()

        cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    conn.commit(); conn.close()



def alert_get(key: str) -> Optional[str]:

    conn = db_connect(); cur = conn.cursor()

    try:

        cur.execute("SELECT value FROM alerts WHERE key=?", (key,))

    except sqlite3.OperationalError:

        conn.close()

        db_init()

        conn = db_connect(); cur = conn.cursor()

        cur.execute("SELECT value FROM alerts WHERE key=?", (key,))

    row = cur.fetchone(); conn.close()

    return row[0] if row else None



def alert_set(key: str, value: str):

    conn = db_connect(); cur = conn.cursor()

    try:

        cur.execute("INSERT INTO alerts(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    except sqlite3.OperationalError:

        conn.close()

        db_init()

        conn = db_connect(); cur = conn.cursor()

        cur.execute("INSERT INTO alerts(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    conn.commit(); conn.close()



def db_insert_result(ts:int, latency:float, jitter:float, down:float, up:float, loss:float, url:str):

    conn = db_connect(); cur = conn.cursor()

    cur.execute("""INSERT INTO results (ts,latency_ms,jitter_ms,download_mbps,upload_mbps,loss_pct,url)

                   VALUES (?,?,?,?,?,?,?)""", (ts, latency, jitter, down, up, loss, url))

    conn.commit(); conn.close()



def db_prune_keep_latest(n:int=5):

    conn = db_connect(); cur = conn.cursor()

    cur.execute("SELECT id FROM results ORDER BY id DESC LIMIT ?", (n,))

    keep_ids = [row[0] for row in cur.fetchall()]

    if keep_ids:

        qmarks = ",".join(["?"]*len(keep_ids))

        cur.execute(f"DELETE FROM results WHERE id NOT IN ({qmarks})", keep_ids)

        conn.commit()

    conn.close()



def db_fetch_latest(limit:int=5):

    conn = db_connect(); cur = conn.cursor()

    cur.execute("SELECT ts,latency_ms,jitter_ms,download_mbps,upload_mbps,loss_pct,url FROM results ORDER BY id DESC LIMIT ?", (limit,))

    rows = cur.fetchall(); conn.close(); return rows



# ------------------ SPEEDTEST BIN DETECTION -------

def find_speedtest_bin() -> Tuple[str, str]:

    if SPEEDTEST_BIN_ENV:

        base = os.path.basename(SPEEDTEST_BIN_ENV)

        return SPEEDTEST_BIN_ENV, ("ookla" if base == "speedtest" else "cli")

    if which("speedtest"):     return "speedtest", "ookla"

    if which("speedtest-cli"): return "speedtest-cli", "cli"

    return "", ""



# ------------------ VNSTAT HELPERS ----------------

def autodetect_iface() -> str:

    out = run_cmd("vnstat --iflist")

    m = re.search(r"interfaces:\s*(.+)$", out, re.IGNORECASE | re.MULTILINE)

    if not m: return "br-lan"

    names = m.group(1).strip().split()

    if "br-lan" in names: return "br-lan"

    for n in names:

        if n.startswith(("en", "eth", "ens", "eno", "wan", "br-")): return n

    return names[0] if names else "br-lan"



def list_ifaces() -> List[str]:

    out = run_cmd("vnstat --iflist")

    m = re.search(r"interfaces:\s*(.+)$", out, re.IGNORECASE | re.MULTILINE)

    return (m.group(1).strip().split() if m else ["br-lan"]) or ["br-lan"]



def vnstat_overview() -> str: return run_cmd("vnstat")

def vnstat_daily(iface: str) -> str: return run_cmd(f"vnstat -d -i {shlex.quote(iface)}")

def vnstat_monthly(iface: str) -> str: return run_cmd(f"vnstat -m -i {shlex.quote(iface)}")

def vnstat_hourly(iface: str) -> str: return run_cmd(f"vnstat -h -i {shlex.quote(iface)}")

def vnstat_live(iface: str, s: int) -> str: return run_cmd(f"vnstat -tr {int(s)} -i {shlex.quote(iface)}")



def vnstat_month_total_this_month(iface: str) -> str:

    ym = datetime.now(TZ).strftime("%Y-%m")

    text = run_cmd(f"vnstat -m -i {shlex.quote(iface)}")

    for line in text.splitlines():

        if line.strip().startswith(ym):

            parts = re.split(r"\s*\|\s*", line.strip())

            if len(parts) >= 3:

                return parts[2].strip()

    return "0.00 B"



def vnstat_last_day_total(iface: str) -> str:

    text = vnstat_daily(iface)

    last_date = ""

    last_total = "0.00 B"

    pattern = re.compile(r"\s*(\d{4}-\d{2}-\d{2})\s+([0-9.]+\s+\w+i?B)\s+\|\s+([0-9.]+\s+\w+i?B)\s+\|\s+([0-9.]+\s+\w+i?B)")

    for line in text.splitlines():

        m = pattern.match(line)

        if not m:

            continue

        date = m.group(1)

        total = m.group(4).strip()

        if date >= last_date:

            last_date = date

            last_total = total

    return last_total



# ------------- ASCII GRAPH from vnstat -d ----------

def size_to_gib(txt: str) -> float:

    m = re.match(r"([0-9.]+)\s*([KMGT]i?B)", txt, re.IGNORECASE)

    if not m:

        m2 = re.match(r"([0-9.]+)\s*B", txt)

        if m2: return float(m2.group(1)) / (1024**3)

        return 0.0

    value = float(m.group(1)); unit = m.group(2).lower()

    factor = {"kib":2**10, "mib":2**20, "gib":2**30, "tib":2**40, "kb":10**3, "mb":10**6, "gb":10**9, "tb":10**12}

    bytes_val = value * factor.get(unit, 1)

    return bytes_val / (2**30)



def parse_vnstat_daily_totals(iface: str, days: int = 30) -> List[Tuple[str, float]]:

    text = vnstat_daily(iface)

    rows = []

    for line in text.splitlines():

        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s+([0-9.]+\s+\w+i?B)\s+\|\s+([0-9.]+\s+\w+i?B)\s+\|\s+([0-9.]+\s+\w+i?B)", line)

        if m:

            date = m.group(1); total = m.group(4)

            rows.append((date, size_to_gib(total)))

    rows = rows[-days:] if len(rows) > days else rows

    return rows



def sparkline(vals: List[float]) -> str:

    if not vals: return "(no data)"

    blocks = "▁▂▃▄▅▆▇█"

    vmin, vmax = min(vals), max(vals)

    if math.isclose(vmax, vmin): return blocks[0]*len(vals)

    out = []

    for v in vals:

        idx = int((v - vmin) / (vmax - vmin) * (len(blocks)-1) + 1e-9)

        out.append(blocks[idx])

    return "".join(out)



def build_daily_graph_text(iface: str, days: int) -> str:

    data = parse_vnstat_daily_totals(iface, days)

    if not data: return f"📈 *Grafik {days} hari* (iface `{iface}`)\n`(no data)`"

    dates = [d for d,_ in data]; vals = [v for _,v in data]

    sl = sparkline(vals); last = f"{vals[-1]:.2f} GiB pada {dates[-1]}"

    return (f"📈 *Grafik {days} hari* (iface `{iface}`)\n"

            f"`{sl}`\n"

            f"Min: `{min(vals):.2f} GiB`  Max: `{max(vals):.2f} GiB`  Last: `{last}`")



# ------------------ SYSTEM/OS INFO ----------------

def _pgrep(pattern: str) -> list[int]:

    out = run_cmd(f"pgrep -f {shlex.quote(pattern)}", timeout=5)

    if out.startswith("[ERR]") or not out.strip():

        return []

    return [int(x) for x in out.strip().split() if x.isdigit()]



def get_openclash_status() -> str:

    """

    Deteksi status OpenClash via proses.

    """

    clash_pids = _pgrep(r"/etc/openclash/clash\b")

    wdog_pids  = _pgrep(r"openclash_watchdog\.sh\b")

    if clash_pids:

        info = f"clash PID(s): {','.join(map(str, clash_pids))}"

        if wdog_pids:

            info += f", watchdog: {','.join(map(str, wdog_pids))}"

        return f"🟢 openclash: running ({info})"



    init_stat = run_cmd("/etc/init.d/openclash status", timeout=5).lower()

    if "running" in init_stat:

        return f"🟡 openclash: unclear (init.d says running, but no clash PID)"

    enabled_out = run_cmd("/etc/init.d/openclash enabled", timeout=5).strip().lower()

    autostart = "enabled" if enabled_out == "enabled" else "disabled"

    return f"🔴 openclash: inactive (autostart {autostart})"



def get_cpu_model_from_proc() -> str:

    try:

        txt = open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace").read()

    except Exception:

        return "Unknown"

    keys = ["model name", "Hardware", "Processor", "cpu model", "system type"]

    for key in keys:

        m = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", txt, re.MULTILINE)

        if m:

            return m.group(1).strip()

    for line in txt.splitlines():

        line = line.strip()

        if line and ":" in line:

            return line.split(":", 1)[1].strip()

    return "Unknown"



def get_openwrt_syscfg() -> Tuple[Optional[str], Optional[str]]:

    txt = run_cmd("cat /etc/config/system")

    if txt.startswith("[ERR]"): return None, None

    hn = None; zn = None

    m1 = re.search(r"option\s+hostname\s+'([^']+)'", txt)

    if m1: hn = m1.group(1)

    m2 = re.search(r"option\s+zonename\s+'([^']+)'", txt)

    if m2: zn = m2.group(1)

    return hn, zn



def get_os_firmware() -> str:

    osrel = run_cmd("cat /etc/os-release")

    m = re.search(r'PRETTY_NAME="?([^"\n]+)"?', osrel)

    return m.group(1) if m else "Linux"



def get_kernel() -> str: return run_cmd("uname -r")



def get_uptime() -> str:

    up = run_cmd("uptime -p")

    if up.startswith("[ERR]"):

        try:

            with open("/proc/uptime") as f:

                seconds = float(f.read().split()[0])

            days = int(seconds // 86400); seconds %= 86400

            hours = int(seconds // 3600); seconds %= 3600

            mins = int(seconds // 60)

            return f"{days} days, {hours} hours, {mins} minutes"

        except:

            return "Unknown"

    return up.replace("up ", "")



def get_temperature() -> Optional[float]:

    try:

        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):

            with open(path) as f:

                v = f.read().strip()

            if v.isdigit():

                val = int(v) / (1000.0 if int(v) > 200 else 1.0)

                return float(val)

    except: pass

    s = run_cmd("sensors")

    if not s.startswith("[ERR]"):

        m = re.search(r"([-+]?\d+\.\d+)\s*°C", s)

        if m:

            try: return float(m.group(1))

            except: pass

    return None



def get_memory_from_free_mb() -> Tuple[int, int, int, int]:

    """

    Ambil data RAM dari output `free` (BusyBox/OpenWrt biasanya kB).

    Return: total_mb, used_mb, free_mb, avail_mb (dibulatkan).

    """

    out = run_cmd("free")

    try:

        for line in out.splitlines():

            if line.strip().lower().startswith("mem:"):

                parts = [p for p in line.split() if p]

                total_kb = int(parts[1]); used_kb = int(parts[2]); free_kb = int(parts[3])

                avail_kb = int(parts[6]) if len(parts) >= 7 else (total_kb - used_kb - int(parts[4]) - int(parts[5]))

                return total_kb // 1024, used_kb // 1024, free_kb // 1024, avail_kb // 1024

    except Exception:

        pass

    try:

        mem = open("/proc/meminfo").read()

        mt_kb = int(re.search(r"MemTotal:\s+(\d+)\s+kB", mem).group(1))

        ma_kb = int(re.search(r"MemAvailable:\s+(\d+)\s+kB", mem).group(1))

        mf_kb = int(re.search(r"MemFree:\s+(\d+)\s+kB", mem).group(1))

        total_mb = mt_kb // 1024

        avail_mb = ma_kb // 1024

        free_mb  = mf_kb // 1024

        used_mb  = total_mb - avail_mb  # pemakaian efektif

        return total_mb, used_mb, free_mb, avail_mb

    except Exception:

        return 0, 0, 0, 0



def get_rootfs_info() -> Tuple[str, str, str, int]:

    d = run_cmd("df -h /"); d2 = run_cmd("df /")

    lines = d.splitlines()

    if len(lines) >= 2:

        parts = lines[1].split()

        parts2 = d2.splitlines()[1].split()

        use_pct = int(parts2[4].strip("%")) if len(parts2) >= 5 and parts2[4].endswith("%") else 0

        return parts[1], parts[2], parts[3], use_pct

    return "-", "-", "-", 0



def get_service_status(name: str) -> str:

    out = run_cmd(f"/etc/init.d/{name} status", timeout=5).lower()

    if "running" in out:

        return f"🟢 {name}: running"

    elif "inactive" in out or "stopped" in out or not out.strip():

        return f"🔴 {name}: inactive"

    else:

        return f"🟡 {name}: {out.strip()[:30]}"



def get_vnstat_limit_gib() -> float:

    val = settings_get("vnstat_limit_gib")

    if val:

        try:

            return float(val)

        except ValueError:

            pass

    return VNSTAT_GIB_LIMIT



def set_vnstat_limit_gib(val: float) -> None:

    settings_set("vnstat_limit_gib", f"{val:.2f}")



def get_temperature_limit() -> float:

    val = settings_get("temp_alert_limit_c")

    if val:

        try:

            return float(val)

        except ValueError:

            pass

    return TEMP_ALERT_DEFAULT



def set_temperature_limit(val: float) -> None:

    settings_set("temp_alert_limit_c", f"{val:.1f}")



def parse_float_from_text(text: str) -> Optional[float]:

    """Ambil angka pertama (boleh desimal/koma) dari teks. Jika tidak ada angka valid, return None."""

    if not text:

        return None

    cleaned = text.replace(",", ".")

    m = re.search(r"[-+]?\d*\.?\d+", cleaned)

    if not m:

        return None

    try:

        return float(m.group())

    except ValueError:

        return None



# --------- Fix Jam (NTP sync) ----------

def fix_system_time() -> str:

    """

    Jalankan langkah NTP:

    - lihat waktu sebelum,

    - set uci ntp,

    - enable+restart sysntpd,

    - lihat waktu sesudah.

    Return string log.

    """

    logs = []

    before = run_cmd("date")

    logs.append("[BEFORE]")

    logs.append(before)



    cmds = [

    # Step 1: Set zona waktu permanen ke WIB (Asia/Jakarta)

    "echo '=== [1/4] Set zona waktu ke WIB ==='",

    "uci set system.@system[0].zonename=\"Asia/Jakarta\"",

    "uci set system.@system[0].timezone=\"WIB-7\"",

    "uci commit system",

    "/etc/init.d/system reload",



    # Step 2: Hentikan sysntpd dulu supaya kita bisa force sync manual

    "echo '=== [2/4] Hentikan NTP bawaan sementara ==='",

    "/etc/init.d/sysntpd stop",



    # Step 3: Paksa sinkron jam sekali via NTP (fallback ke busybox ntpd kalau ntpd biasa gak ada)

    "echo '=== [3/4] Sinkron jam sekarang dari internet ==='",

    "ntpd -q -p pool.ntp.org || busybox ntpd -q -p pool.ntp.org",

    "date",



    # Step 4: Nyalakan lagi sysntpd dan enable autostart supaya next reboot auto sync waktu

    "echo '=== [4/4] Aktifkan NTP service untuk otomatis ke depannya ==='",

    "/etc/init.d/sysntpd start",

    "/etc/init.d/sysntpd enable",

    ]



    for c in cmds:

        logs.append(f"$ {c}")

        logs.append(run_shell(c))



    after = run_cmd("date")

    logs.append("[AFTER]")

    logs.append(after)



    return "\n".join(logs)



# ------------- Bot Update Helpers -------------

def format_temperature(value: Optional[float]) -> str:

    return f"{value:.1f}°C" if value is not None else "Unknown"



def download_bot_source(url: str, timeout: int = 30) -> bytes:

    req = urllib.request.Request(

        url,

        headers={

            "User-Agent": "Mozilla/5.0 (RANetBot Update)",

            "Accept": "text/plain,application/json",

        },

    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:

        return resp.read()



def backup_bot_file() -> Tuple[bool, str]:

    try:

        if not os.path.exists(BOT_FILE_PATH):

            return False, f"File sumber tidak ditemukan: {BOT_FILE_PATH}"

        shutil.copy2(BOT_FILE_PATH, BOT_BACKUP_PATH)

        return True, f"Backup tersimpan di {BOT_BACKUP_PATH}"

    except Exception as exc:

        return False, f"Gagal membuat backup: {exc}"



def apply_bot_content(content: bytes) -> None:

    directory = os.path.dirname(BOT_FILE_PATH) or "."

    tmp_path: Optional[str] = None

    try:

        with tempfile.NamedTemporaryFile("wb", delete=False, dir=directory) as tmp:

            tmp.write(content)

            tmp_path = tmp.name

        if os.path.exists(BOT_FILE_PATH):

            shutil.copymode(BOT_FILE_PATH, tmp_path)

        else:

            os.chmod(tmp_path, 0o755)

        os.replace(tmp_path, BOT_FILE_PATH)

        tmp_path = None

    finally:

        if tmp_path and os.path.exists(tmp_path):

            try:

                os.remove(tmp_path)

            except OSError:

                pass



async def restart_bot_after_delay(delay: float = 2.0) -> None:

    await asyncio.sleep(delay)

    os.execv(sys.executable, [sys.executable] + sys.argv)



# ------------- Helpers Alerts & Backup/Restore -------------

def get_cpu_cores() -> int:

    try:

        txt = open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace").read()

        n = len(re.findall(r"^processor\s*:\s*\d+", txt, re.MULTILINE))

        return n or (os.cpu_count() or 1)

    except:

        return os.cpu_count() or 1



def get_loadavg() -> Tuple[float, float, float]:

    try:

        la = open("/proc/loadavg").read().split()

        return float(la[0]), float(la[1]), float(la[2])

    except:

        out = run_cmd("uptime")

        m = re.search(r"load average[s]?:\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)", out)

        if m:

            return float(m.group(1)), float(m.group(2)), float(m.group(3))

        return 0.0, 0.0, 0.0



def vnstat_current_month_gib(iface: str) -> float:

    text = vnstat_monthly(iface)

    ym = datetime.now(TZ).strftime("%Y-%m")

    for line in text.splitlines():

        if line.strip().startswith(ym):

            parts = re.split(r"\s*\|\s*", line.strip())

            if len(parts) >= 3:

                return size_to_gib(parts[2].strip())

    return 0.0



def _ensure_dir(p: str):

    try: os.makedirs(p, exist_ok=True)

    except: pass



def create_full_backup() -> Tuple[str, str]:

    """

    Buat .tgz berisi:

      - DB bot (SQLite): DB_PATH

      - Direktori vnstat: VNSTAT_DB_DIR

    Return: (path, logs)

    """

    _ensure_dir(BACKUP_DIR)

    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")

    out_tgz = os.path.join(BACKUP_DIR, f"ranet-backup-{ts}.tgz")

    logs = []

    to_include = []



    if os.path.isfile(DB_PATH):

        to_include.append((DB_PATH, "opt/ranet-bot/speedtest.db"))

    else:

        logs.append(f"[WARN] bot DB tidak ditemukan: {DB_PATH}")



    if os.path.isdir(VNSTAT_DB_DIR):

        to_include.append((VNSTAT_DB_DIR, "var/lib/vnstat"))

    else:

        logs.append(f"[WARN] vnstat dir tidak ditemukan: {VNSTAT_DB_DIR}")



    import tarfile

    with tarfile.open(out_tgz, "w:gz") as tar:

        for src, arc in to_include:

            try:

                tar.add(src, arcname=arc)

                logs.append(f"[ADD] {src} -> {arc}")

            except Exception as e:

                logs.append(f"[ERR] add {src}: {e}")

    return out_tgz, "\n".join(logs) if logs else "OK"



# ===== PATCH: helper copy untuk EXDEV =====

def _copyfile_atomic(src: str, dst: str):

    """

    Salin berkas dengan metadata, lalu atomic swap ke dst.

    """

    parent = os.path.dirname(dst) or "."

    os.makedirs(parent, exist_ok=True)

    tmpdst = os.path.join(parent, f".__tmp.{os.getpid()}.{int(time.time()*1000)}")

    shutil.copy2(src, tmpdst)

    os.replace(tmpdst, dst)  # atomic di dalam filesystem tujuan



def _copytree_merge(src: str, dst: str, logs: list):

    """

    Merge copy direktori: buat dst jika belum ada, lalu copy semua isi src ke dst.

    File yang sudah ada di-overwrite.

    """

    os.makedirs(dst, exist_ok=True)

    for root, dirs, files in os.walk(src):

        rel = os.path.relpath(root, src)

        target_root = os.path.join(dst, rel) if rel != "." else dst

        os.makedirs(target_root, exist_ok=True)

        for d in dirs:

            try:

                os.makedirs(os.path.join(target_root, d), exist_ok=True)

            except Exception as e:

                logs.append(f"[WARN] mkdir {os.path.join(target_root, d)}: {e}")

        for f in files:

            s = os.path.join(root, f)

            t = os.path.join(target_root, f)

            try:

                _copyfile_atomic(s, t)

            except Exception as e:

                logs.append(f"[ERR] copy {s} -> {t}: {e}")



def _move_with_overwrite(src: str, dst: str, logs: list):

    """

    Pindah antar filesystem dengan aman.

    """

    try:

        os.makedirs(os.path.dirname(dst), exist_ok=True)

        os.replace(src, dst)

        logs.append(f"[MOVE] {src} -> {dst}")

        return

    except OSError as e:

        if e.errno != errno.EXDEV:

            logs.append(f"[ERR] move {src} -> {dst}: {e}")

            return

    # EXDEV fallback:

    try:

        if os.path.isdir(src) and not os.path.islink(src):

            _copytree_merge(src, dst, logs)

            try: shutil.rmtree(src)

            except Exception as ee: logs.append(f"[WARN] cleanup src dir: {ee}")

            logs.append(f"[COPY] {src} => {dst} (dir, EXDEV)")

        else:

            _copyfile_atomic(src, dst)

            try: os.remove(src)

            except Exception as ee: logs.append(f"[WARN] cleanup src file: {ee}")

            logs.append(f"[COPY] {src} => {dst} (file, EXDEV)")

    except Exception as e:

        logs.append(f"[ERR] copy-fallback {src} -> {dst}: {e}")

# ===== END PATCH =====



async def restore_with_progress(ctx: ContextTypes.DEFAULT_TYPE, query_msg, tgz_path: str) -> str:

    """

    Restore backup vnstat + DB bot.

    """

    logs = []

    if not os.path.isfile(tgz_path):

        return f"[ERR] File tidak ditemukan: {tgz_path}"



    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")

    workdir = f"/tmp/ranet-restore-{ts}"

    os.makedirs(workdir, exist_ok=True)



    msg = await query_msg.reply_text("⏳ Menyiapkan restore…", parse_mode=ParseMode.MARKDOWN_V2)



    # 1) Inspect archive

    try:

        listing = run_shell(f"tar -tzf {shlex.quote(tgz_path)} | head -n 50")

        await edit_progress(msg, "🔍 Memeriksa arsip…\n" + code_block(listing or "(kosong)"))

    except Exception as e:

        return f"[ERR] Tidak bisa membaca arsip: {e}"



    # 2) Extract to /tmp

    await edit_progress(msg, "📦 Mengekstrak arsip ke /tmp…")

    try:

        import tarfile

        with tarfile.open(tgz_path, "r:gz") as tar:

            tar.extractall(workdir)

        logs.append(f"[OK] Extracted to {workdir}")

    except Exception as e:

        logs.append(f"[ERR] extract: {e}")

        await edit_progress(msg, "❌ Gagal extract arsip.")

        return "\n".join(logs)



    # 3) Validasi konten

    paths = {

        "bot_db": os.path.join(workdir, "opt/ranet-bot/speedtest.db"),

        "vnstat_dir": os.path.join(workdir, "var/lib/vnstat"),

    }

    ok_any = False

    if os.path.isfile(paths["bot_db"]):

        ok_any = True

        logs.append(f"[FOUND] {paths['bot_db']}")

    else:

        logs.append("[MISS] speedtest.db")



    if os.path.isdir(paths["vnstat_dir"]):

        ok_any = True

        logs.append(f"[FOUND] {paths['vnstat_dir']}")

    else:

        logs.append("[MISS] var/lib/vnstat directory")



    await edit_progress(msg, "🧪 Validasi konten…\n" + code_block("\n".join(logs[-4:])))



    if not ok_any:

        await edit_progress(msg, "❌ Arsip tidak berisi konten yang diharapkan.")

        return "\n".join(logs)



    # 4) Stop layanan vnstat

    await edit_progress(msg, "🛑 Menghentikan layanan vnstat…")

    _ = run_cmd("/etc/init.d/vnstat stop", timeout=10)

    time.sleep(1)



    # 5) Pindahkan file ke lokasi final

    await edit_progress(msg, "🚚 Memindahkan berkas ke lokasi final…")



    # 5a) Bot DB

    if os.path.isfile(paths["bot_db"]):

        try:

            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

            _move_with_overwrite(paths["bot_db"], DB_PATH, logs)

            try: os.chmod(DB_PATH, 0o640)

            except Exception as e: logs.append(f"[WARN] chmod db: {e}")

        except Exception as e:

            logs.append(f"[ERR] restore DB: {e}")



    # 5b) vnstat dir

    if os.path.isdir(paths["vnstat_dir"]):

        try:

            os.makedirs(VNSTAT_DB_DIR, exist_ok=True)

            _move_with_overwrite(paths["vnstat_dir"], VNSTAT_DB_DIR, logs)

            try:

                for root, dirs, files in os.walk(VNSTAT_DB_DIR):

                    for d in dirs:

                        try: os.chmod(os.path.join(root, d), 0o755)

                        except: pass

                    for f in files:

                        try: os.chmod(os.path.join(root, f), 0o644)

                        except: pass

            except Exception as e:

                logs.append(f"[WARN] chmod vnstat: {e}")

        except Exception as e:

            logs.append(f"[ERR] restore vnstat: {e}")



    await edit_progress(msg, "🧹 Menyelesaikan penataan izin & struktur…")



    # 6) Start vnstat lagi

    _ = run_cmd("/etc/init.d/vnstat start", timeout=10)

    time.sleep(1)

    _ = run_cmd("/etc/init.d/vnstat status", timeout=10)



    # 7) Cleanup

    try:

        run_shell(f"rm -rf {shlex.quote(workdir)}")

        logs.append("[CLEAN] workspace dihapus")

    except Exception as e:

        logs.append(f"[WARN] cleanup: {e}")



    await edit_progress(msg, "✅ Restore selesai. Mengirim ringkasan log…")

    return "\n".join(logs)



# --- HTTP multi-fallback (curl/wget/uclient-fetch/urllib) ---

def _http_get(url: str, timeout: int = 4) -> str:

    out = run_cmd(f"curl -4 -m {timeout} -fsSL {shlex.quote(url)}", timeout=timeout+1)

    if not out.startswith("[ERR]") and out.strip(): return out.strip()

    if which("wget"):

        out2 = run_cmd(f"wget -q -T {timeout} -O - {shlex.quote(url)}", timeout=timeout+1)

        if not out2.startswith("[ERR]") and out2.strip(): return out2.strip()

    if which("uclient-fetch"):

        out3 = run_cmd(f"uclient-fetch -q -T {timeout} -O - {shlex.quote(url)}", timeout=timeout+1)

        if not out3.startswith("[ERR]") and out3.strip(): return out3.strip()

    try:

        with urllib.request.urlopen(url, timeout=timeout) as r:

            return r.read().decode("utf-8", errors="replace").strip()

    except Exception:

        return ""



def get_public_ip() -> str:

    for url in [

        "https://ipinfo.io/ip","https://api.ipify.org","https://ifconfig.me/ip","https://icanhazip.com",

        "http://ipinfo.io/ip","http://api.ipify.org","http://ifconfig.me/ip","http://icanhazip.com",

    ]:

        ip = _http_get(url, timeout=4)

        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip): return ip

    return "Unknown"



def get_isp() -> str:

    org = _http_get("https://ipinfo.io/org", timeout=4) or _http_get("http://ipinfo.io/org", timeout=4)

    if org: return org

    isp = _http_get("http://ip-api.com/line?fields=isp", timeout=4)

    return isp if isp else "Unknown"



# ------------------ NETBIRD CACHE -----------------

def netbird_status_update(force: bool=False, timeout: int = 90) -> str:

    now = int(time.time())

    ts_s = settings_get("netbird_ts")

    cached = settings_get("netbird_status")

    if not force and ts_s:

        try:

            ts = int(ts_s)

            if now - ts < 24*3600 and cached:

                return cached

        except: pass

    out = run_cmd("netbird status", timeout=timeout)

    settings_set("netbird_status", out)

    settings_set("netbird_ts", str(now))

    return out



def get_netbird_ip_cached() -> Optional[str]:

    text = netbird_status_update(force=False)

    m = re.search(r"NetBird IP:\s*([0-9./]+)", text)

    return m.group(1) if m else None



# ------------------ STATE -------------------------

def init_current_iface() -> str:

    saved = settings_get("vnstat_iface")

    if saved: return saved

    return DEFAULT_IFACE or autodetect_iface()



def db_init_once():

    try: db_init()

    except Exception: pass



db_init_once()  # init DB dulu, sebelum akses settings
CURRENT_IFACE = init_current_iface()
CLI_SESSIONS: Dict[int, bool] = {}
CLI_HISTORY: Dict[int, List[str]] = defaultdict(list)
NB_WAIT_SETUP_KEY = set()   # chat_id waiting for netbird setup key input
POWER_TASKS: Dict[int, asyncio.Task] = {}

PROMPT_KEYS_BOOL = {
    "await_set_quota",
    "await_set_temp",
    "await_set_token",
    "await_set_chat_id",
    "await_netbird_ip",
    "await_restore",
    "await_bot_upload",
    "await_wifi_config",
    "await_log_search",
    "await_file_browse",
    "await_file_download",
    "await_file_edit",
    "await_file_upload",
    "await_firewall_action",
    "await_portfwd_action",
    "await_process_action",
    "await_opkg_action",
    "await_scheduler_action",
    "await_power_custom",
    "await_usbwd_config",
}

PROMPT_KEYS_VALUE = {
    "restore_path",
    "fileman_path",
    "file_edit_target",
    "file_edit_mode",
    "file_upload_dir",
    "opkg_action",
    "firewall_action",
    "portfwd_action",
    "process_action",
    "scheduler_action",
    "power_action",
}

def reset_user_state(state: Dict):
    for key in PROMPT_KEYS_BOOL:
        state[key] = False
    for key in PROMPT_KEYS_VALUE:
        state.pop(key, None)


# ------------------ UI KEYBOARDS ------------------
def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Speedtest", callback_data="SPD_NOW"),
         InlineKeyboardButton("🕒 VNStat per Jam", callback_data="QA_VNSTAT_HOURLY")],
        [InlineKeyboardButton("🖥️ CLI", callback_data="MENU_CLI"),
         InlineKeyboardButton("🧹 Refresh NetBird", callback_data="NB_REFRESH")],
        [InlineKeyboardButton("📱 Main Menu", callback_data="SHOW_MAIN_MENU")],
    ])

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Dashboard", callback_data="MENU_DASHBOARD")],
        [InlineKeyboardButton("🖥️ System", callback_data="MENU_SYSTEM_ROOT")],
        [InlineKeyboardButton("📦 Packages", callback_data="MENU_PACKAGES")],
        [InlineKeyboardButton("📡 Network", callback_data="MENU_NETWORK_ROOT")],
        [InlineKeyboardButton("📊 Monitoring", callback_data="MENU_MONITORING")],
        [InlineKeyboardButton("📁 File Manager", callback_data="MENU_FILEMAN")],
        [InlineKeyboardButton("⚡ Speedtest", callback_data="MENU_SPEEDTEST")],
        [InlineKeyboardButton("🛠️ Tools", callback_data="MENU_TOOLS_ROOT")],
        [InlineKeyboardButton("🤖 Android", callback_data="MENU_ANDROID")],
        [InlineKeyboardButton("🔔 Alerts", callback_data="MENU_ALERTS")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="MENU_SETTINGS")],
        [InlineKeyboardButton("💾 Backup/Restore", callback_data="MENU_BACKUP")],
    ])


def android_menu_keyboard(has_device: bool) -> InlineKeyboardMarkup:

    rows: List[List[InlineKeyboardButton]] = []

    if has_device:

        rows.append([

            InlineKeyboardButton("ℹ️ Ringkasan", callback_data="ANDROID_SUMMARY"),

            InlineKeyboardButton("📨 5 SMS Terakhir", callback_data="ANDROID_SMS_5"),

        ])

        rows.append([

            InlineKeyboardButton("📨 10 SMS Terakhir", callback_data="ANDROID_SMS_10"),

            InlineKeyboardButton("✈️ Refresh IP (Airplane)", callback_data="ANDROID_TOGGLE_AIRPLANE"),

        ])

        rows.append([

            InlineKeyboardButton("📡 Monitor Sinyal", callback_data="ANDROID_SIGNAL_MONITOR"),

            InlineKeyboardButton("📝 Export Report", callback_data="ANDROID_EXPORT"),

        ])

        rows.append([

            InlineKeyboardButton("🔄 Ganti Device", callback_data="ANDROID_CHOOSE"),

        ])

    else:

        rows.append([

            InlineKeyboardButton("🔄 Cari Device", callback_data="ANDROID_REFRESH"),

        ])

    rows.append([InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")])

    return InlineKeyboardMarkup(rows)


def android_device_select_keyboard(devices: List[AndroidDevice]) -> InlineKeyboardMarkup:

    rows: List[List[InlineKeyboardButton]] = []

    ready_devices = [dev for dev in devices if dev.status == "device"]

    for dev in ready_devices:

        label = android_choice_label(dev)

        callback = f"ANDROID_SET:{dev.serial}"

        if len(callback) > 64:

            callback = callback[:64]

        rows.append([InlineKeyboardButton(label, callback_data=callback)])

    if not rows:

        rows.append([InlineKeyboardButton("❌ Tidak ada device siap", callback_data="ANDROID_REFRESH")])

    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="ANDROID_REFRESH")])

    rows.append([InlineKeyboardButton("📱 Menu Android", callback_data="MENU_ANDROID")])

    return InlineKeyboardMarkup(rows)


def vnstat_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Ringkasan", callback_data="VN_OVERVIEW"),
         InlineKeyboardButton("🗓️ Harian", callback_data="VN_DAILY")],
        [InlineKeyboardButton("📅 Bulanan", callback_data="VN_MONTH"),
         InlineKeyboardButton(f"📡 Live {LIVE_SECONDS}s", callback_data="VN_LIVE")],
        [InlineKeyboardButton("🔎 Pilih Interface", callback_data="VN_IFLIST")],
        [InlineKeyboardButton("📈 Grafik 7h", callback_data="VN_G7"),
         InlineKeyboardButton("📈 Grafik 30h", callback_data="VN_G30")],
        [InlineKeyboardButton("🔙 Menu Monitoring", callback_data="MENU_MONITORING")],
    ])

def iface_menu() -> InlineKeyboardMarkup:
    rows, tmp = [], []
    for i, iface in enumerate(list_ifaces(), 1):
        label = f"{'✅ ' if iface == CURRENT_IFACE else ''}{iface}"
        tmp.append(InlineKeyboardButton(label, callback_data=f"SET_IFACE:{iface}"))
        if i % 2 == 0:
            rows.append(tmp); tmp = []
    if tmp: rows.append(tmp)
    rows.append([InlineKeyboardButton("🔙 Menu VNStat", callback_data="MENU_VNSTAT")])
    return InlineKeyboardMarkup(rows)


def system_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ Info", callback_data="SYS_INFO")],
        [InlineKeyboardButton("🔌 Reboot / Shutdown", callback_data="MENU_SYS_POWER")],
        [InlineKeyboardButton("🧠 Processes", callback_data="MENU_PROCESS")],
        [InlineKeyboardButton("🧾 Logs", callback_data="MENU_SYS_LOGS")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def system_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Refresh Info", callback_data="SYS_REFRESH")],
        [InlineKeyboardButton("🧹 Refresh NetBird Cache", callback_data="NB_REFRESH")],
        [InlineKeyboardButton("🔙 Menu System", callback_data="MENU_SYSTEM_ROOT")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def power_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reboot (5s)", callback_data="SYS_REBOOT:5"),
         InlineKeyboardButton("🔄 Reboot (30s)", callback_data="SYS_REBOOT:30")],
        [InlineKeyboardButton("⏱️ Reboot Custom", callback_data="SYS_REBOOT_CUSTOM")],
        [InlineKeyboardButton("⏹️ Shutdown (5s)", callback_data="SYS_SHUTDOWN:5"),
         InlineKeyboardButton("⏹️ Shutdown (30s)", callback_data="SYS_SHUTDOWN:30")],
        [InlineKeyboardButton("⏱️ Shutdown Custom", callback_data="SYS_SHUTDOWN_CUSTOM")],
        [InlineKeyboardButton("🛑 Batalkan Jadwal", callback_data="SYS_POWER_CANCEL")],
        [InlineKeyboardButton("🔙 Menu System", callback_data="MENU_SYSTEM_ROOT")],
    ])

def process_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List Processes", callback_data="PROC_LIST")],
        [InlineKeyboardButton("📈 Top Snapshot", callback_data="PROC_TOP")],
        [InlineKeyboardButton("🔥 Kill PID", callback_data="PROC_KILL")],
        [InlineKeyboardButton("🔁 Restart Service", callback_data="PROC_RESTART")],
        [InlineKeyboardButton("🔙 Menu System", callback_data="MENU_SYSTEM_ROOT")],
    ])

def logs_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪵 Syslog", callback_data="LOG_SYSLOG")],
        [InlineKeyboardButton("🐧 Kernel Log", callback_data="LOG_KERNEL")],
        [InlineKeyboardButton("🧠 dmesg", callback_data="LOG_DMESG")],
        [InlineKeyboardButton("🔍 Cari di Syslog", callback_data="LOG_SEARCH")],
        [InlineKeyboardButton("🔙 Menu System", callback_data="MENU_SYSTEM_ROOT")],
    ])


def packages_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 opkg update", callback_data="OPKG_UPDATE")],
        [InlineKeyboardButton("⬆️ opkg upgrade", callback_data="OPKG_UPGRADE")],
        [InlineKeyboardButton("📦 Install Package", callback_data="OPKG_INSTALL")],
        [InlineKeyboardButton("🧹 Remove Package", callback_data="OPKG_REMOVE")],
        [InlineKeyboardButton("🗂️ List Installed", callback_data="OPKG_LIST_INSTALLED")],
        [InlineKeyboardButton("🔍 Cari Package", callback_data="OPKG_SEARCH")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def network_root_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Interfaces", callback_data="NET_INTERFACES")],
        [InlineKeyboardButton("📡 WiFi Manager", callback_data="MENU_WIFI")],
        [InlineKeyboardButton("🧾 DHCP Leases", callback_data="NET_DHCP")],
        [InlineKeyboardButton("🛡️ Firewall", callback_data="MENU_FIREWALL")],
        [InlineKeyboardButton("🚪 Port Forwarding", callback_data="MENU_PORTFWD")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def wifi_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scan WiFi", callback_data="WIFI_SCAN")],
        [InlineKeyboardButton("📊 Status", callback_data="WIFI_STATUS")],
        [InlineKeyboardButton("👥 Clients", callback_data="WIFI_CLIENTS")],
        [InlineKeyboardButton("⚙️ Konfigurasi", callback_data="WIFI_CONFIG")],
        [InlineKeyboardButton("🔁 Restart WiFi", callback_data="QA_WIFI_RESTART")],
        [InlineKeyboardButton("🔙 Menu Network", callback_data="MENU_NETWORK_ROOT")],
    ])

def firewall_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📃 Lihat Rules", callback_data="FW_LIST")],
        [InlineKeyboardButton("➕ Tambah Rule", callback_data="FW_ADD")],
        [InlineKeyboardButton("➖ Hapus Rule", callback_data="FW_DELETE")],
        [InlineKeyboardButton("🔄 Reload Firewall", callback_data="FW_RELOAD")],
        [InlineKeyboardButton("🔙 Menu Network", callback_data="MENU_NETWORK_ROOT")],
    ])

def port_forward_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📃 Lihat Port Forward", callback_data="PF_LIST")],
        [InlineKeyboardButton("➕ Tambah Port Forward", callback_data="PF_ADD")],
        [InlineKeyboardButton("➖ Hapus Port Forward", callback_data="PF_DELETE")],
        [InlineKeyboardButton("🔄 Reload Firewall", callback_data="FW_RELOAD")],
        [InlineKeyboardButton("🔙 Menu Network", callback_data="MENU_NETWORK_ROOT")],
    ])

def monitoring_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 VNStat", callback_data="MENU_VNSTAT")],
        [InlineKeyboardButton("📡 Bandwidth/Device", callback_data="MON_BANDWIDTH")],
        [InlineKeyboardButton("🕸️ NetBird", callback_data="MENU_NETBIRD")],
        [InlineKeyboardButton("📺 Live Stats", callback_data="MON_LIVE")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def file_manager_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Browse", callback_data="FM_BROWSE")],
        [InlineKeyboardButton("📥 Download File", callback_data="FM_DOWNLOAD")],
        [InlineKeyboardButton("📤 Upload File", callback_data="FM_UPLOAD")],
        [InlineKeyboardButton("📝 Edit File", callback_data="FM_EDIT")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def speedtest_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Speedtest Now", callback_data="SPD_NOW"),
         InlineKeyboardButton("⚙️ Pilih Server", callback_data="SPD_SERVER")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])


def speedtest_server_keyboard(current: Optional[str]) -> InlineKeyboardMarkup:

    rows = []

    for name, sid in SPEEDTEST_PRESETS:

        label = f"{'✅ ' if current == sid else ''}{name}"

        rows.append([InlineKeyboardButton(label, callback_data=f"SPD_SET_SERVER:{sid}")])

    rows.append([InlineKeyboardButton("❌ Hapus Pilihan Server", callback_data="SPD_CLR_SERVER")])

    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="MENU_SPEEDTEST")])

    return InlineKeyboardMarkup(rows)



def nettools_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏓 Ping 8.8.8.8", callback_data="NT_PING:8.8.8.8"),
         InlineKeyboardButton("🏓 Ping 1.1.1.1", callback_data="NT_PING:1.1.1.1")],
        [InlineKeyboardButton("🧭 Traceroute 8.8.8.8", callback_data="NT_TR:8.8.8.8"),
         InlineKeyboardButton("🧭 Traceroute 1.1.1.1", callback_data="NT_TR:1.1.1.1")],
        [InlineKeyboardButton("ℹ️ /ping <host>", callback_data="NT_INFO")],
        [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
    ])

def diag_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Top & Load & Temp", callback_data="DIAG_TOP")],
        [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
    ])

def cli_menu_keyboard(active: bool) -> InlineKeyboardMarkup:
    if active:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Keluar", callback_data="CLI_EXIT"),
             InlineKeyboardButton("📜 History", callback_data="CLI_HISTORY")],
            [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Masuk Remote Terminal", callback_data="CLI_ENTER"),
             InlineKeyboardButton("📜 History", callback_data="CLI_HISTORY")],
            [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
        ])

def netbird_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start (up)", callback_data="NB_UP"),
         InlineKeyboardButton("⏹️ Stop (down)", callback_data="NB_DOWN")],
        [InlineKeyboardButton("📃 Status", callback_data="NB_STATUS"),
         InlineKeyboardButton("🔑 Setup Key", callback_data="NB_SETUPKEY")],
        [InlineKeyboardButton("🗑️ Deregister", callback_data="NB_DEREG"),
         InlineKeyboardButton("🛠️ Setup", callback_data="MENU_NB_SETUP")],
        [InlineKeyboardButton("🔙 Menu Monitoring", callback_data="MENU_MONITORING")],
    ])

def backup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Backup Now", callback_data="BK_DO")],
        [InlineKeyboardButton("♻️ Restore (upload .tgz)", callback_data="BK_RESTORE")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def update_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Update dari GitHub", callback_data="UPD_RUN")],
        [InlineKeyboardButton("⬇️ Downgrade (Restore Backup)", callback_data="UPD_DOWNGRADE")],
        [InlineKeyboardButton("📤 Upload ra-bot.py", callback_data="UPD_UPLOAD")],
        [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
    ])

def settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📶 Set Kuota (GiB)", callback_data="SETTINGS_SET_QUOTA")],
        [InlineKeyboardButton("🌡️ Set Batas Suhu (°C)", callback_data="SETTINGS_SET_TEMP")],
        [InlineKeyboardButton("🕒 Fix Jam (NTP Sync)", callback_data="SETTINGS_FIX_TIME")],
        [InlineKeyboardButton("🔐 Lihat Token & ID", callback_data="SETTINGS_VIEW_CRED")],
        [InlineKeyboardButton("✏️ Ganti Token/API", callback_data="SETTINGS_SET_TOKEN")],
        [InlineKeyboardButton("✏️ Ganti Chat ID", callback_data="SETTINGS_SET_CHAT")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

def netbird_setup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Cek Status", callback_data="NB_SETUP_CEK_STATUS")],
        [InlineKeyboardButton("🔍 Cek Service", callback_data="NB_SETUP_CEK_SERVICE")],
        [InlineKeyboardButton("🧰 Setup", callback_data="NB_SETUP_RUN")],
        [InlineKeyboardButton("🗑️ Remove", callback_data="NB_SETUP_REMOVE")],
        [InlineKeyboardButton("🔄 Ganti IP", callback_data="NB_SETUP_GANTI_IP")],
        [InlineKeyboardButton("🔙 Menu Monitoring", callback_data="MENU_MONITORING")],
    ])

def tools_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡️ USB Watchdog", callback_data="MENU_USB_WD")],
        [InlineKeyboardButton("🧰 Network Tools", callback_data="MENU_NETTOOLS")],
        [InlineKeyboardButton("🩺 Diagnostics", callback_data="MENU_DIAG")],
        [InlineKeyboardButton("🖥️ Remote Terminal", callback_data="MENU_CLI")],
        [InlineKeyboardButton("⏰ Scheduler", callback_data="MENU_SCHEDULER")],
        [InlineKeyboardButton("🧩 Update BOT", callback_data="MENU_UPDATE")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])


def usb_watchdog_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Setup", callback_data="USBWD_SETUP"),
         InlineKeyboardButton("📊 Status", callback_data="USBWD_STATUS")],
        [InlineKeyboardButton("▶️ Start", callback_data="USBWD_START"),
         InlineKeyboardButton("⏹️ Stop", callback_data="USBWD_STOP")],
        [InlineKeyboardButton("🔁 Restart", callback_data="USBWD_RESTART"),
         InlineKeyboardButton("🧾 Lihat Config", callback_data="USBWD_SHOW")],
        [InlineKeyboardButton("📡 List Interface", callback_data="USBWD_LIST_IF")],
        [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
    ])

def scheduler_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Lihat Cron", callback_data="SCH_LIST")],
        [InlineKeyboardButton("➕ Tambah Cron", callback_data="SCH_ADD")],
        [InlineKeyboardButton("🗑️ Hapus Cron", callback_data="SCH_DELETE")],
        [InlineKeyboardButton("🔄 Restart Cron", callback_data="SCH_RESTART")],
        [InlineKeyboardButton("🔙 Menu Tools", callback_data="MENU_TOOLS_ROOT")],
    ])

def alerts_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="ALERTS_REFRESH")],
        [InlineKeyboardButton("🧹 Clear Alerts", callback_data="ALERTS_CLEAR")],
        [InlineKeyboardButton("📱 Menu Utama", callback_data="SHOW_MAIN_MENU")],
    ])

# ------------------ FEATURE HELPERS ----------------
def run_wifi_reload() -> str:
    cmds = ["wifi reload", "wifi"]
    for cmd in cmds:
        out = run_cmd(cmd)
        if not out.startswith("[ERR]"):
            return out or f"Perintah '{cmd}' dieksekusi."
    return out

def get_wifi_interfaces() -> List[str]:
    out = run_cmd("wifi status")
    if out.startswith("[ERR]"):
        return []
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    ifaces = []
    if isinstance(data, dict):
        for radio in data.values():
            for iface in radio.get("interfaces", []):
                ifname = iface.get("ifname")
                if ifname:
                    ifaces.append(ifname)
    return sorted(set(ifaces))

def wifi_status_text() -> str:
    out = run_cmd("wifi status")
    try:
        data = json.loads(out)
        return json.dumps(data, indent=2, sort_keys=True)
    except Exception:
        return out

def wifi_clients_text() -> str:
    ifaces = get_wifi_interfaces()
    if not ifaces:
        return "[ERR] Tidak ada interface WiFi terdeteksi."
    blocks = []
    for iface in ifaces:
        assoc = run_cmd(f"iwinfo {shlex.quote(iface)} assoclist")
        blocks.append(f"### {iface}\n{assoc or '(tidak ada klien)'}")
    return "\n\n".join(blocks)

def wifi_scan_text() -> str:
    ifaces = get_wifi_interfaces()
    if not ifaces:
        return "[ERR] Tidak ada interface WiFi terdeteksi."
    blocks = []
    for iface in ifaces:
        scan = run_cmd(f"iwinfo {shlex.quote(iface)} scan")
        blocks.append(f"### {iface}\n{scan or '(tidak ada hasil)'}")
    return "\n\n".join(blocks)

def dhcp_leases_text() -> str:
    path = "/tmp/dhcp.leases"
    if not os.path.exists(path):
        return "[ERR] File /tmp/dhcp.leases tidak ditemukan."
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            expire, mac, ip, host = parts[:4]
            try:
                expire_ts = datetime.fromtimestamp(int(expire), TZ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                expire_ts = expire
            rows.append(f"{ip:>15}  {mac:17}  {host or '-':20}  exp:{expire_ts}")
    return "\n".join(rows) or "(tidak ada lease)"

def firewall_rules_text() -> str:
    return run_cmd("uci show firewall")

def port_forward_rules_text() -> str:
    out = run_cmd("uci show firewall | grep -E 'redirect|rule' -n")
    if "[ERR]" in out:
        out = run_cmd("uci show firewall")
    return out

def apply_shell_commands(commands: str) -> str:
    script = "\n".join(c for c in commands.splitlines() if c.strip())
    if not script:
        return "[ERR] Tidak ada perintah."
    return run_shell(script)

def interfaces_overview_text() -> str:
    addr = run_cmd("ip -o addr show")
    stats = run_cmd("ip -s link show")
    return f"=== ip addr ===\n{addr}\n\n=== ip -s link ===\n{stats}"

def opkg_update_text() -> str:
    return run_cmd("opkg update", timeout=180)

def opkg_upgrade_text() -> str:
    return run_cmd("opkg upgrade", timeout=240)

def opkg_install(packages: str) -> str:
    pkgs = " ".join(shlex.split(packages))
    if not pkgs:
        return "[ERR] Paket tidak diberikan."
    return run_cmd(f"opkg install {pkgs}", timeout=240)

def opkg_remove(packages: str) -> str:
    pkgs = " ".join(shlex.split(packages))
    if not pkgs:
        return "[ERR] Paket tidak diberikan."
    return run_cmd(f"opkg remove {pkgs}")

def opkg_list_installed() -> str:
    return run_cmd("opkg list-installed")

def opkg_search(term: str) -> str:
    if not term:
        return "[ERR] Kata kunci kosong."
    return run_cmd(f"opkg list | grep -i {shlex.quote(term)}")

def process_list_text() -> str:
    return run_cmd("ps -w")

def process_top_text() -> str:
    out = run_cmd("top -bn1 | head -n 20")
    if out.startswith("[ERR]"):
        out = run_cmd("busybox top -bn1 | head -n 20")
    return out

def kill_process(pid: str) -> str:
    if not pid.isdigit():
        return "[ERR] PID tidak valid."
    return run_cmd(f"kill {pid}")

def restart_service(name: str) -> str:
    if not name:
        return "[ERR] Nama service kosong."
    return run_cmd(f"/etc/init.d/{shlex.quote(name)} restart")

def log_syslog_tail() -> str:
    return run_cmd("logread | tail -n 200")

def log_kernel_tail() -> str:
    out = run_cmd("logread -k | tail -n 200")
    if out.startswith("[ERR]"):
        out = run_cmd("dmesg | tail -n 200")
    return out

def log_dmesg_tail() -> str:
    return run_cmd("dmesg | tail -n 200")

def log_search(term: str) -> str:
    if not term:
        return "[ERR] Kata kunci kosong."
    return run_cmd(f"logread | grep -i {shlex.quote(term)} | tail -n 200")

def resolve_user_path(raw: str, base: str) -> Path:
    base_path = Path(base or "/").expanduser()
    raw = (raw or "").strip()
    if raw in {"", "."}:
        return base_path
    if raw in {"..", "../"}:
        candidate = base_path / ".."
    elif raw.startswith("./"):
        candidate = Path("/" + raw[2:]) if len(raw) > 2 else Path("/")
    else:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base_path / raw
    try:
        return candidate.resolve(strict=False)
    except Exception:
        return candidate


def file_list_directory(path: str) -> str:
    p = Path(path or ".").expanduser()
    if not p.exists():
        return f"[ERR] Path {p} tidak ditemukan."
    if p.is_file():
        return file_read(p)
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            info = child.stat()
            mode = stat.filemode(info.st_mode)
            size = info.st_size
            ts = datetime.fromtimestamp(info.st_mtime, TZ).strftime("%Y-%m-%d %H:%M")
            label = child.name + ("/" if child.is_dir() else "")
            entries.append(f"{mode} {size:>10} {ts}  {label}")
    except PermissionError:
        return f"[ERR] Tidak ada izin untuk membuka {p}."
    return "\n".join(entries) or "(kosong)"

def file_read(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception as exc:
        return f"[ERR] {exc}"
    return content or "(kosong)"

def file_write(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception as exc:
        return f"[ERR] {exc}"
    return "OK"

def file_upload_path(directory: str, filename: str) -> Path:
    base = resolve_user_path(directory or "/tmp", "/")
    if base.is_file():
        base = base.parent
    base.mkdir(parents=True, exist_ok=True)
    return base / filename

def human_bytes(value: float) -> str:
    try:
        value = float(value)
    except Exception:
        return str(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"

def bandwidth_monitor_text(limit: int = 10) -> str:
    if which("ubus"):
        payload = json.dumps({"limit": limit, "order": "bytes", "direction": "both"})
        out = run_cmd(f"ubus call nlbwmon get_stats '{payload}'")
        try:
            data = json.loads(out)
            hosts = data.get("hosts", [])
            rows = []
            for idx, host in enumerate(hosts[:limit], 1):
                ip = host.get("ip") or host.get("host") or "-"
                mac = host.get("mac", "-")
                total = host.get("bytes", 0)
                rx = host.get("rx_bytes", 0)
                tx = host.get("tx_bytes", 0)
                rows.append(f"{idx:02d}. {ip} ({mac})\n   Total: {human_bytes(total)} (⬇️ {human_bytes(rx)} / ⬆️ {human_bytes(tx)})")
            if rows:
                return "\n\n".join(rows)
        except Exception:
            pass
    return "[ERR] Data bandwidth tidak tersedia. Pastikan paket nlbwmon terpasang."

def alerts_overview_text() -> str:
    month_key = datetime.now(TZ).strftime("%Y%m")
    rows = ["🔔 *Alert States*"]
    mapping = [
        ("disk_alert_state", "Disk Space"),
        ("cpu_alert_state", "CPU Load"),
        (f"vnstat_alert_{month_key}", "VNStat Quota"),
        ("temp_alert_state", "Temperature"),
    ]
    for key, label in mapping:
        state = alert_get(key) or "OK"
        rows.append(f"• {label}: `{state}`")
    return "\n".join(rows)

CRON_FILE = "/etc/crontabs/root"

def cron_list_text() -> str:
    try:
        with open(CRON_FILE, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except FileNotFoundError:
        return "(cron file tidak ditemukan)"
    except Exception as exc:
        return f"[ERR] {exc}"
    return content or "(cron kosong)"

def cron_add_line(line: str) -> str:
    if not line.strip():
        return "[ERR] Baris kosong."
    try:
        existing = cron_list_text()
        with open(CRON_FILE, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line.strip() + "\n")
        return "OK"
    except Exception as exc:
        return f"[ERR] {exc}"

def cron_delete_line(pattern: str) -> str:
    if not pattern.strip():
        return "[ERR] Pola kosong."
    try:
        with open(CRON_FILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return "[ERR] cron file tidak ditemukan."
    except Exception as exc:
        return f"[ERR] {exc}"
    new_lines = [ln for ln in lines if pattern not in ln]
    if new_lines == lines:
        return "[ERR] Tidak ada baris yang cocok."
    try:
        with open(CRON_FILE, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)
    except Exception as exc:
        return f"[ERR] {exc}"
    return "OK"

def cron_restart() -> str:
    out = run_cmd("/etc/init.d/cron restart")
    if out.startswith("[ERR]"):
        out = run_cmd("service cron restart")
    return out

def schedule_power(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str, delay: int) -> None:
    task = POWER_TASKS.get(chat_id)
    if task and not task.done():
        task.cancel()

    async def _runner():
        try:
            await asyncio.sleep(max(delay, 0))
            cmd = "reboot" if action == "reboot" else "poweroff"
            run_cmd(cmd)
        except asyncio.CancelledError:
            return

    POWER_TASKS[chat_id] = ctx.application.create_task(_runner()) if ctx.application else asyncio.create_task(_runner())

def cancel_power(chat_id: int) -> None:
    task = POWER_TASKS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()

# ------------------ OVERVIEW & SYSTEM --------------
def build_overview_text(iface: str) -> str:

    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    owrt_hn, owrt_zone = get_openwrt_syscfg()

    osfw = get_os_firmware()

    temp = get_temperature()

    uptime = get_uptime()

    bw_month = vnstat_month_total_this_month(iface) or "0.00 B"

    bw_day = vnstat_last_day_total(iface) or "0.00 B"

    ip_pub = get_public_ip()

    isp = get_isp()

    nb_ip = get_netbird_ip_cached()



    openclash_stat = get_openclash_status()

    nikki_stat = get_service_status("nikki")



    lines = [

        "👋 *Selamat datang di RANET Bot-V2*",

        f"🕒 *Waktu server:* `{ts}`",

        "",

        "ℹ️ *INFORMATION*",

        f"• 🖥️ Hostname (OpenWrt): `{owrt_hn or '-'}`",

        f"• 🌏 Zonename: `{owrt_zone or '-'}`",

        f"• 💽 OS: `{osfw}`",

        f"• 🌡️ Temperature : `{format_temperature(temp)}`",

        f"• 📡 Interface aktif: `{iface}`",

        f"• 🕸️ NetBird IP: `{nb_ip or '-'}`",

        "",

        "⏱️ *UPTIME*",

        f"`{uptime}`",

        "",

        "📈 *Bandwidth Usage (BULAN TERAKHIR)*",

        f"`{bw_month}`",

        "",

        "📊 *Bandwidth Usage (1 HARI TERAKHIR)*",

        f"`{bw_day}`",

        "",

        "🌐 *IP PUBLIC*",

        f"`{ip_pub}`",

        "",

        "🏢 *ISP*",

        f"`{isp}`",

        "",

        "🧰 *Service Status*",

        f"• {openclash_stat}",
        f"• {nikki_stat}",
        "",
        "*Gunakan Quick Actions di bawah atau buka 📱 Main Menu untuk navigasi lengkap.*",
    ]
    return "\n".join(lines)



def build_system_info(iface_for_bw: str) -> str:

    now = datetime.now(TZ).strftime("%a %b %d %H:%M:%S UTC%z %Y")

    model_arch = run_cmd("uname -m").strip() or "Unknown"

    model = get_cpu_model_from_proc() or model_arch

    osfw = get_os_firmware()

    kernel = get_kernel()

    uptime = get_uptime()

    temp = get_temperature()

    mem_t, mem_u, mem_f, mem_av = get_memory_from_free_mb()

    fs_t, fs_u, fs_f, _ = get_rootfs_info()

    isp = get_isp()

    month_total = vnstat_month_total_this_month(iface_for_bw) or "0.00 B"

    la1, la5, la15 = get_loadavg()

    cores = get_cpu_cores()

    ratio = (la1 / max(cores, 1)) if cores else 0.0



    header = "━━━━━━━━━━━━━━━━━━━━━━━━\n🍺 SYSTEM INFORMATION 🍺\n━━━━━━━━━━━━━━━━━━━━━━━━"

    lines = [

        header,

        now,

        "",

        f"🧱 Model : {model}",

        f"☠️ Architecture : {model_arch}",

        f"💻 Firmware Version: {osfw}",

        f"🧾 Kernel Version : {kernel}",

        f"🌱 Uptime : {uptime}",

        f"🌡️ Temperature : {format_temperature(temp)}",

        f"🧠 Memory : {mem_t}MB, used: {mem_u}MB, free: {mem_f}MB (avail: {mem_av}MB)",

        f"🧮 CPU Load: {la1:.2f} {la5:.2f} {la15:.2f} (cores:{cores}, ratio1:{ratio:.2f})",

        f"🗂️ RootFS:  {fs_t}, Used: {fs_u}, Free: {fs_f}",

        f"🕸️ ISP : {isp}",

        f"💾 Bandwidth Usage : {month_total}",

        "",

        "━━━━━━━━━━━━━━━━━━━━━━━━",

    ]

    return "\n".join(lines)



# ------------------ SPEEDTEST ---------------------

def run_speedtest_and_parse(server_id: Optional[str] = None) -> Tuple[float,float,float,float,float,str,str]:

    bin_name, mode = find_speedtest_bin()

    if not bin_name:

        msg = ("[ERR] speedtest tidak ditemukan.\n"

               "- Install Ookla CLI (x86_64/aarch64), atau\n"

               "- Install Python speedtest-cli: pip3 install --break-system-packages speedtest-cli")

        return 0.0, 0.0, 0.0, 0.0, 0.0, "", msg

    if mode == "ookla":

        cmd = bin_name

        if server_id: cmd = f"{bin_name} --server-id {server_id}"

        out = run_cmd(cmd, timeout=120)

        lat_pat = re.search(r"(?:Idle\s+)?Latency:\s*([0-9.]+)\s*ms\s*\(jitter:\s*([0-9.]+)ms", out)

        if not lat_pat:

            lat_pat = re.search(r"(?:Idle\s+)?Latency:\s*([0-9.]+)\s*ms", out); jitter = 0.0

        else:

            jitter = float(lat_pat.group(2))

        latency = float(lat_pat.group(1)) if lat_pat else 0.0

        d_pat = re.search(r"Download:\s*([0-9.]+)\s*Mbps", out)

        u_pat = re.search(r"Upload:\s*([0-9.]+)\s*Mbps", out)

        loss_pat = re.search(r"Packet\s+Loss:\s*([0-9.]+)\s*%", out)

        url_pat = re.search(r"Result\s+URL:\s*(\S+)", out)

        down = float(d_pat.group(1)) if d_pat else 0.0

        up = float(u_pat.group(1)) if u_pat else 0.0

        loss = float(loss_pat.group(1)) if loss_pat else 0.0

        url = url_pat.group(1) if url_pat else ""

        return latency, jitter, down, up, loss, url, out

    else:

        out = run_cmd(bin_name, timeout=180)

        lat_pat = re.search(r"Latency:\s*([0-9.]+)\s*ms", out) or re.search(r"Ping:\s*([0-9.]+)\s*ms", out)

        jitter = 0.0

        latency = float(lat_pat.group(1)) if lat_pat else 0.0

        d_pat = re.search(r"Download:\s*([0-9.]+)\s*Mb/s", out) or re.search(r"Download:\s*([0-9.]+)\s*Mbps", out)

        u_pat = re.search(r"Upload:\s*([0-9.]+)\s*Mb/s", out) or re.search(r"Upload:\s*([0-9.]+)\s*Mbps", out)

        down = float(d_pat.group(1)) if d_pat else 0.0

        up = float(u_pat.group(1)) if u_pat else 0.0

        loss_pat = re.search(r"Packet\s*Loss:\s*([0-9.]+)\s*%", out)

        loss = float(loss_pat.group(1)) if loss_pat else 0.0

        url_pat = re.search(r"Result\s*URL:\s*(\S+)", out) or re.search(r"Share results:\s*(\S+)", out)

        url = url_pat.group(1) if url_pat else ""

        return latency, jitter, down, up, loss, url, out


async def run_speedtest_and_parse_async(server_id: Optional[str] = None) -> Tuple[float,float,float,float,float,str,str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_speedtest_and_parse, server_id)


def format_speedtest_entry(idx:int, ts:int, lat:float, jit:float, down:float, up:float, loss:float, url:str) -> str:

    dt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    link_md = f"[LINK]({url})" if url else "LINK"

    return (

        f"{idx}. {dt}\n"

        f"   ⏱ Latency : {lat:.2f} ms  ~ jitter {jit:.2f} ms\n"

        f"   ⬇️ Download : {down:.2f} Mbps\n"

        f"   ⬆️ Upload : {up:.2f} Mbps\n"

        f"   🧪 Packet Loss : {loss:.2f}%\n"

        f"   🔗 {link_md}"

    )



def build_speedtest_history_text(rows) -> str:

    if not rows: return "🚀 Riwayat Speedtest (kosong)"

    lines = ["🚀 Riwayat Speedtest (5 Terbaru)", ""]

    for idx, (ts, lat, jit, down, up, loss, url) in enumerate(rows, 1):

        lines.append(format_speedtest_entry(idx, ts, lat, jit or 0.0, down, up, loss, url))

        lines.append("")

    if lines[-1] == "": lines.pop()

    return "\n".join(lines)



def build_speedtest_result_text(ts:int, lat:float, jit:float, down:float, up:float, loss:float, url:str) -> str:

    dt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    link_md = f"[LINK]({url})" if url else "LINK"

    return (

        f"⚡ Speedtest — {dt}\n\n"

        f"   ⏱ Latency : {lat:.2f} ms  ~ jitter {jit:.2f} ms\n"

        f"   ⬇️ Download : {down:.2f} Mbps\n"

        f"   ⬆️ Upload : {up:.2f} Mbps\n"

        f"   🧪 Packet Loss : {loss:.2f}%\n"

        f"   🔗 {link_md}"

    )



# ------------------ HANDLERS ----------------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update):

        await update.message.reply_text("Maaf, akses ditolak."); return

    if update.effective_chat:

        CLI_SESSIONS[update.effective_chat.id] = False

        NB_WAIT_SETUP_KEY.discard(update.effective_chat.id)

    overview = build_overview_text(CURRENT_IFACE)
    await update.message.reply_text(overview, parse_mode="Markdown", reply_markup=dashboard_keyboard())


async def system_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update):

        await update.message.reply_text("Maaf, akses ditolak."); return

    info = build_system_info(CURRENT_IFACE)
    for chunk in split_chunks(info):
        await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=system_info_keyboard())


async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update):

        await update.message.reply_text("Maaf, akses ditolak."); return

    args = update.message.text.split()

    if len(args) < 2:

        await update.message.reply_text("Usage: /ping <host>"); return

    host = args[1]

    out = run_cmd(f"ping -c 5 -W 2 {shlex.quote(host)}", timeout=20)

    await update.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)



async def trace_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update):

        await update.message.reply_text("Maaf, akses ditolak."); return

    args = update.message.text.split()

    if len(args) < 2:

        await update.message.reply_text("Usage: /trace <host>"); return

    host = args[1]

    if not which("traceroute"):

        await update.message.reply_text("Traceroute tidak tersedia. Install: opkg install traceroute"); return

    out = run_cmd(f"traceroute -m 15 {shlex.quote(host)}", timeout=40)

    await update.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)



# ---- Handle dokumen untuk Restore (.tgz) & Upload bot

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update):

        return

    doc = update.message.document

    if not doc:

        return



    state = ctx.user_data



    if state.get("await_restore"):

        fname = (doc.file_name or "").lower()

        if not fname.endswith(".tgz"):

            await update.message.reply_text("File harus berekstensi .tgz")

            return

        ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")

        dst = f"/tmp/restore-{ts}.tgz"

        try:

            f = await doc.get_file()

            await f.download_to_drive(dst)

        except Exception as e:

            await update.message.reply_text(f"Gagal mengunduh file: {e}")

            return



        size_b = os.path.getsize(dst) if os.path.exists(dst) else 0

        listing = run_shell(f"tar -tzf {shlex.quote(dst)} | head -n 50")

        state["restore_path"] = dst



        kb = InlineKeyboardMarkup([

            [InlineKeyboardButton("✅ Apply Restore", callback_data="RESTORE_APPLY")],

            [InlineKeyboardButton("🛑 Batalkan", callback_data="RESTORE_CANCEL")]

        ])



        head = mdv2_escape(f"💾 Backup diterima ({size_b} bytes). Berikut sebagian isi arsip:")

        await update.message.reply_text(

            head + "\n" + code_block(listing),

            parse_mode=ParseMode.MARKDOWN_V2,

            reply_markup=kb

        )

        return



    if state.get("await_bot_upload"):
        fname = (doc.file_name or "").strip()
        if (fname or "").lower() != "ra-bot.py":
            await update.message.reply_text("Nama file harus ra-bot.py")
            return
        ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")

        dst = f"/tmp/ra-bot-upload-{ts}.py"

        try:

            file_obj = await doc.get_file()

            await file_obj.download_to_drive(dst)

        except Exception as exc:

            await update.message.reply_text(f"Gagal mengunduh file: {exc}")

            return

        try:

            with open(dst, "rb") as fh:

                content = fh.read()

        except Exception as exc:

            await update.message.reply_text(f"Gagal membaca file: {exc}")

            return

        finally:

            try:

                os.remove(dst)

            except Exception:

                pass



        ok, backup_msg = backup_bot_file()

        if not ok:

            await update.message.reply_text(f"❌ Update dibatalkan.\n{backup_msg}")

            return

        try:

            apply_bot_content(content)

        except Exception as exc:

            await update.message.reply_text(f"❌ Gagal menerapkan file: {exc}")

            return



        state["await_bot_upload"] = False

        await update.message.reply_text(f"✅ ra-bot.py berhasil diterapkan.\n{backup_msg}\nBot akan restart dalam 2 detik.")
        if ctx.application:
            ctx.application.create_task(restart_bot_after_delay())
        return

    upload_dir = state.get("file_upload_dir")
    if upload_dir:
        target = file_upload_path(upload_dir, doc.file_name or f"upload-{int(time.time())}")
        try:
            file_obj = await doc.get_file()
            await file_obj.download_to_drive(str(target))
        except Exception as exc:
            await update.message.reply_text(f"❌ Gagal menyimpan file: {exc}")
            return
        state["file_upload_dir"] = None
        await update.message.reply_text(f"✅ File disimpan di `{target}`", parse_mode="Markdown")
        return


# ------------------ CALLBACKS ---------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if not allowed(update):

        await query.answer("Akses ditolak.", show_alert=True); return

    data = query.data or ""

    await query.answer()

    global CURRENT_IFACE



    # HOME

    if data in {"BACK_HOME", "MENU_DASHBOARD"}:
        if update.effective_chat:
            CLI_SESSIONS[update.effective_chat.id] = False
            NB_WAIT_SETUP_KEY.discard(update.effective_chat.id)
        reset_user_state(ctx.user_data)
        overview = build_overview_text(CURRENT_IFACE)
        await query.edit_message_text(overview, parse_mode="Markdown", reply_markup=dashboard_keyboard()); return

    if data == "SHOW_MAIN_MENU":
        reset_user_state(ctx.user_data)
        await query.edit_message_text("📱 *Main Menu*", parse_mode="Markdown", reply_markup=main_menu()); return

    if data in {"MENU_ANDROID", "ANDROID_REFRESH"}:
        if not android_adb_available():
            text = "❌ *Android tools tidak tersedia.*\nadb tidak ditemukan di PATH."
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2,
                                          reply_markup=android_menu_keyboard(False))
            return
        devices = android_list_devices()
        selected, label = android_ensure_selection(ctx, devices)
        text = android_menu_message(devices, selected, label)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2,
                                      reply_markup=android_menu_keyboard(bool(selected)))
        return

    if data == "ANDROID_CHOOSE":
        if not android_adb_available():
            await query.message.reply_text("❌ adb tidak ditemukan di PATH.")
            return
        devices = android_list_devices()
        selected, label = android_resolve_selection(ctx, devices)
        text = android_menu_message(devices, selected, label)
        text += "\n\n" + mdv2_escape("Pilih device siap status ✅ di bawah.")
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2,
                                      reply_markup=android_device_select_keyboard(devices))
        return

    if data.startswith("ANDROID_SET:"):
        serial = data.split(":", 1)[1]
        devices = android_list_devices()
        dev = next((d for d in devices if d.serial == serial), None)
        if not dev:
            await query.message.reply_text("❌ Device tidak ditemukan. Coba refresh dari Menu Android.")
            return
        if dev.status != "device":
            await query.message.reply_text(
                f"❌ Device `{mdv2_escape(serial)}` belum siap (status {mdv2_escape(dev.status)}).",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        label = android_device_label(dev)
        android_set_selected_device(ctx, dev.serial, label)
        text = android_menu_message(devices, dev.serial, label)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2,
                                      reply_markup=android_menu_keyboard(True))
        return

    if data == "ANDROID_SUMMARY":
        device = android_selected_device(ctx)
        if not device:
            await query.message.reply_text("❌ Pilih device terlebih dahulu melalui Menu Android.")
            return
        if not android_device_ready(device):
            await query.message.reply_text("❌ Device tidak siap. Buka Menu Android dan lakukan refresh.")
            return
        info = android_collect_info(device)
        label = android_selected_label(ctx)
        summary = android_summary_text(info, label)
        await query.message.reply_text(code_block(summary), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data in {"ANDROID_SMS_5", "ANDROID_SMS_10"}:
        device = android_selected_device(ctx)
        if not device:
            await query.message.reply_text("❌ Pilih device terlebih dahulu melalui Menu Android.")
            return
        if not android_device_ready(device):
            await query.message.reply_text("❌ Device tidak siap. Buka Menu Android dan lakukan refresh.")
            return
        info = android_collect_info(device)
        limit = 5 if data.endswith("5") else 10
        sms_text, error = android_sms_text(device, info.get("sdk_int"), limit=limit)
        header = f"{limit} SMS Terakhir (Inbox)"
        payload = f"{header}\n\n{sms_text}".strip()
        await query.message.reply_text(code_block(payload), parse_mode=ParseMode.MARKDOWN_V2)
        if error:
            await query.message.reply_text(f"⚠️ {error}")
        return

    if data == "ANDROID_TOGGLE_AIRPLANE":
        device = android_selected_device(ctx)
        if not device:
            await query.message.reply_text("❌ Pilih device terlebih dahulu melalui Menu Android.")
            return
        if not android_device_ready(device):
            await query.message.reply_text("❌ Device tidak siap. Buka Menu Android dan lakukan refresh.")
            return
        result = android_toggle_airplane(device)
        await query.message.reply_text(result)
        return


    if data == "ANDROID_SIGNAL_MONITOR":
        device = android_selected_device(ctx)
        if not device:
            await query.message.reply_text("❌ Pilih device terlebih dahulu melalui Menu Android.")
            return
        if not android_device_ready(device):
            await query.message.reply_text("❌ Device tidak siap. Buka Menu Android dan lakukan refresh.")
            return
        if update.effective_chat is None or ctx.application is None:
            await query.message.reply_text("❌ Monitor tidak tersedia pada konteks ini.")
            return
        await query.message.reply_text("▶️ Monitor sinyal dimulai (10 sampel).")
        ctx.application.create_task(android_signal_monitor_task(ctx, update.effective_chat.id, device))
        return

    if data == "ANDROID_EXPORT":
        device = android_selected_device(ctx)
        if not device:
            await query.message.reply_text("❌ Pilih device terlebih dahulu melalui Menu Android.")
            return
        if not android_device_ready(device):
            await query.message.reply_text("❌ Device tidak siap. Buka Menu Android dan lakukan refresh.")
            return
        info = android_collect_info(device)
        sms_text, error = android_sms_text(device, info.get("sdk_int"))
        try:
            path = android_export_report(device, info, sms_text)
            caption = "✅ Report Android berhasil dibuat."
            if error:
                caption += f"\n⚠️ {error}"
            with open(path, "rb") as fh:
                await query.message.reply_document(fh, filename=os.path.basename(path), caption=caption)
        except Exception as exc:
            await query.message.reply_text(f"❌ Gagal mengirim report: {exc}")
        return

    if data == "QA_WIFI_RESTART":
        out = run_wifi_reload()
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "QA_VNSTAT_HOURLY":
        out = vnstat_hourly(CURRENT_IFACE)
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return


    # SYSTEM

    if data == "MENU_SYSTEM_ROOT":
        await query.edit_message_text("🖥️ *System Menu*", parse_mode="Markdown", reply_markup=system_menu_keyboard()); return
    if data in {"SYS_INFO", "SYS_REFRESH"}:
        info = build_system_info(CURRENT_IFACE)
        for chunk in split_chunks(info):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=system_info_keyboard())
        return
    if data == "MENU_SYS_POWER":
        await query.edit_message_text("🔌 *Reboot / Shutdown*", parse_mode="Markdown", reply_markup=power_menu_keyboard()); return
    if data.startswith("SYS_REBOOT:"):
        delay = int(data.split(":", 1)[1] or 5)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        schedule_power(ctx, chat_id, "reboot", delay)
        await query.message.reply_text(f"🔄 Reboot dijadwalkan dalam {delay} detik.")
        return
    if data.startswith("SYS_SHUTDOWN:"):
        delay = int(data.split(":", 1)[1] or 5)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        schedule_power(ctx, chat_id, "shutdown", delay)
        await query.message.reply_text(f"⏹️ Shutdown dijadwalkan dalam {delay} detik.")
        return
    if data == "SYS_REBOOT_CUSTOM":
        ctx.user_data["await_power_custom"] = True
        ctx.user_data["power_action"] = "reboot"
        await query.message.reply_text("Masukkan delay reboot (detik):")
        return
    if data == "SYS_SHUTDOWN_CUSTOM":
        ctx.user_data["await_power_custom"] = True
        ctx.user_data["power_action"] = "shutdown"
        await query.message.reply_text("Masukkan delay shutdown (detik):")
        return
    if data == "SYS_POWER_CANCEL":
        if update.effective_chat:
            cancel_power(update.effective_chat.id)
        await query.message.reply_text("🛑 Jadwal power dibatalkan.")
        return
    if data == "MENU_PROCESS":
        await query.edit_message_text("🧠 *Process Manager*", parse_mode="Markdown", reply_markup=process_menu_keyboard()); return
    if data == "PROC_LIST":
        out = process_list_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "PROC_TOP":
        out = process_top_text()
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "PROC_KILL":
        ctx.user_data["await_process_action"] = True
        ctx.user_data["process_action"] = "kill"
        await query.message.reply_text("Masukkan PID yang akan di-kill:")
        return
    if data == "PROC_RESTART":
        ctx.user_data["await_process_action"] = True
        ctx.user_data["process_action"] = "restart"
        await query.message.reply_text("Masukkan nama service (init.d) untuk restart:")
        return
    if data == "MENU_SYS_LOGS":
        await query.edit_message_text("🧾 *System Logs*", parse_mode="Markdown", reply_markup=logs_menu_keyboard()); return
    if data == "LOG_SYSLOG":
        out = log_syslog_tail()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "LOG_KERNEL":
        out = log_kernel_tail()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "LOG_DMESG":
        out = log_dmesg_tail()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "LOG_SEARCH":
        ctx.user_data["await_log_search"] = True
        await query.message.reply_text("Masukkan kata kunci pencarian log:")
        return
    if data == "NB_REFRESH":
        waiting = await query.message.reply_text("🧹 Memperbarui cache NetBird…")
        _ = netbird_status_update(force=True, timeout=90)
        await waiting.delete()
        ip = get_netbird_ip_cached()
        await query.message.reply_text(f"✅ NetBird cache diupdate.\nIP: `{ip or '-'}`", parse_mode="Markdown"); return

    if data == "MENU_PACKAGES":
        await query.edit_message_text("📦 *Package Management*", parse_mode="Markdown", reply_markup=packages_menu_keyboard()); return
    if data == "OPKG_UPDATE":
        out = opkg_update_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "OPKG_UPGRADE":
        out = opkg_upgrade_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "OPKG_INSTALL":
        ctx.user_data["await_opkg_action"] = True
        ctx.user_data["opkg_action"] = "install"
        await query.message.reply_text("Masukkan nama paket yang akan di-install (boleh banyak, pisah spasi):")
        return
    if data == "OPKG_REMOVE":
        ctx.user_data["await_opkg_action"] = True
        ctx.user_data["opkg_action"] = "remove"
        await query.message.reply_text("Masukkan nama paket yang akan dihapus:")
        return
    if data == "OPKG_LIST_INSTALLED":
        out = opkg_list_installed()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "OPKG_SEARCH":
        ctx.user_data["await_opkg_action"] = True
        ctx.user_data["opkg_action"] = "search"
        await query.message.reply_text("Masukkan kata kunci pencarian paket:")
        return

    # SETTINGS (Set quota & temp & fix jam via tombol)
    if data == "MENU_SETTINGS":
        await query.edit_message_text("⚙️ *Pengaturan Bot*", parse_mode="Markdown", reply_markup=settings_menu_keyboard())
        return
    if data == "SETTINGS_SET_QUOTA":

        ctx.user_data["await_set_quota"] = True

        await query.message.reply_text("📶 Masukkan batas kuota dalam GiB (contoh: 500):")

        return

    if data == "SETTINGS_SET_TEMP":

        ctx.user_data["await_set_temp"] = True

        await query.message.reply_text("🌡️ Masukkan batas suhu dalam °C (contoh: 75):")

        return

    if data == "SETTINGS_FIX_TIME":
        out = fix_system_time()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == "SETTINGS_VIEW_CRED":
        token = current_token()
        chat_ids = ", ".join(str(i) for i in current_chat_ids()) or "-"
        txt = (
            "🔐 *Kredensial Telegram*\n"
            f"• Token/API : `{mdv2_escape(token) if token else '-'}`\n"
            f"• Chat ID   : `{mdv2_escape(chat_ids)}`\n"
            f"• File      : `{mdv2_escape(ID_FILE)}`"
        )
        await query.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == "SETTINGS_SET_TOKEN":
        ctx.user_data["await_set_token"] = True
        await query.message.reply_text(
            "Kirim Token/API Bot Telegram baru.\n"
            "Contoh format: 123456:ABCDEF...\n"
            "Token akan disimpan ke file id-telegram dan bot perlu direstart agar aktif."
        )
        return

    if data == "SETTINGS_SET_CHAT":
        ctx.user_data["await_set_chat_id"] = True
        await query.message.reply_text(
            "Kirim daftar Chat ID (boleh banyak) dipisah koma atau spasi.\n"
            "Contoh: 12345, 67890."
        )
        return

    # NETWORK
    if data == "MENU_NETWORK_ROOT":
        await query.edit_message_text("📡 *Network Center*", parse_mode="Markdown", reply_markup=network_root_menu()); return
    if data == "NET_INTERFACES":
        out = interfaces_overview_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "MENU_WIFI":
        await query.edit_message_text("📡 *WiFi Manager*", parse_mode="Markdown", reply_markup=wifi_menu_keyboard()); return
    if data == "WIFI_STATUS":
        out = wifi_status_text()
        for chunk in split_chunks(out, limit=3000):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "WIFI_CLIENTS":
        out = wifi_clients_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "WIFI_SCAN":
        out = wifi_scan_text()
        for chunk in split_chunks(out, limit=3000):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "WIFI_CONFIG":
        ctx.user_data["await_wifi_config"] = True
        await query.message.reply_text("Kirim perintah konfigurasi WiFi (misal: `uci set ...`), satu atau beberapa baris. Kirim 'apply' jika ingin menjalankan `wifi reload` setelahnya.")
        return
    if data == "NET_DHCP":
        out = dhcp_leases_text()
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "MENU_FIREWALL":
        await query.edit_message_text("🛡️ *Firewall Manager*", parse_mode="Markdown", reply_markup=firewall_menu_keyboard()); return
    if data == "FW_LIST":
        out = firewall_rules_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "FW_ADD":
        ctx.user_data["await_firewall_action"] = True
        ctx.user_data["firewall_action"] = "add"
        await query.message.reply_text("Masukkan perintah UCI untuk menambah rule (contoh: `uci add firewall rule; ...`). Akhiri dengan `uci commit firewall`.")
        return
    if data == "FW_DELETE":
        ctx.user_data["await_firewall_action"] = True
        ctx.user_data["firewall_action"] = "delete"
        await query.message.reply_text("Masukkan perintah untuk menghapus rule (contoh: `uci delete firewall.@rule[2]`).")
        return
    if data == "FW_RELOAD":
        out = run_cmd("fw4 reload")
        if out.startswith("[ERR]"):
            out = run_cmd("fw3 reload")
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "MENU_PORTFWD":
        await query.edit_message_text("🚪 *Port Forwarding*", parse_mode="Markdown", reply_markup=port_forward_menu_keyboard()); return
    if data == "PF_LIST":
        out = port_forward_rules_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "PF_ADD":
        ctx.user_data["await_portfwd_action"] = True
        ctx.user_data["portfwd_action"] = "add"
        await query.message.reply_text("Masukkan perintah untuk menambah redirect (contoh: `uci add firewall redirect; ...`).")
        return
    if data == "PF_DELETE":
        ctx.user_data["await_portfwd_action"] = True
        ctx.user_data["portfwd_action"] = "delete"
        await query.message.reply_text("Masukkan perintah untuk menghapus redirect (contoh: `uci delete firewall.@redirect[0]`).")
        return

    # MONITORING & VNSTAT
    if data == "MENU_MONITORING":
        await query.edit_message_text("📊 *Monitoring Center*", parse_mode="Markdown", reply_markup=monitoring_menu_keyboard()); return
    if data == "MON_BANDWIDTH":
        out = bandwidth_monitor_text()
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "MON_LIVE":
        txt = f"(Sampling {LIVE_SECONDS}s di {CURRENT_IFACE})\n{vnstat_live(CURRENT_IFACE, LIVE_SECONDS)}"
        for c in split_chunks(txt):
            await query.message.reply_text(code_block(c), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "MENU_VNSTAT":
        txt = f"📊 *Menu VNStat*\nInterface aktif: *{CURRENT_IFACE}*"
        await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=vnstat_menu()); return
    if data == "VN_OVERVIEW":

        out = vnstat_overview()

        for c in split_chunks(out):

            await query.message.reply_text(code_block(c), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "VN_DAILY":

        out = vnstat_daily(CURRENT_IFACE)

        for c in split_chunks(out):

            await query.message.reply_text(code_block(c), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "VN_MONTH":

        out = vnstat_monthly(CURRENT_IFACE)

        for c in split_chunks(out):

            await query.message.reply_text(code_block(c), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "VN_LIVE":

        txt = f"(Sampling {LIVE_SECONDS}s di {CURRENT_IFACE})\n{vnstat_live(CURRENT_IFACE, LIVE_SECONDS)}"

        for c in split_chunks(txt):

            await query.message.reply_text(code_block(c), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "VN_IFLIST":

        await query.edit_message_text("Pilih interface:", reply_markup=iface_menu()); return

    if data.startswith("SET_IFACE:"):

        CURRENT_IFACE = data.split(":", 1)[1]

        settings_set("vnstat_iface", CURRENT_IFACE)

        await query.edit_message_text(f"Interface aktif diganti ke *{CURRENT_IFACE}*", parse_mode="Markdown", reply_markup=iface_menu()); return

    if data == "VN_G7":

        txt = build_daily_graph_text(CURRENT_IFACE, 7)

        await query.message.reply_text(txt, parse_mode="Markdown"); return

    if data == "VN_G30":

        txt = build_daily_graph_text(CURRENT_IFACE, 30)

        await query.message.reply_text(txt, parse_mode="Markdown"); return



    # FILE MANAGER
    if data == "MENU_FILEMAN":
        base = ctx.user_data.get("fileman_path") or "/"
        ctx.user_data["fileman_path"] = base
        listing = file_list_directory(base)
        await query.edit_message_text(f"📁 *File Manager*\nPath saat ini: `{base}`", parse_mode="Markdown", reply_markup=file_manager_menu_keyboard())
        await query.message.reply_text(code_block(listing), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "FM_BROWSE":
        ctx.user_data["await_file_browse"] = True
        await query.message.reply_text("Masukkan path direktori atau file (gunakan awalan `./` untuk path absolut, misal `./root`):", parse_mode="Markdown")
        return
    if data == "FM_DOWNLOAD":
        ctx.user_data["await_file_download"] = True
        await query.message.reply_text("Masukkan path file yang akan diunduh (contoh: `./root/setup-netbird.sh`):", parse_mode="Markdown")
        return
    if data == "FM_UPLOAD":
        ctx.user_data["await_file_upload"] = True
        await query.message.reply_text("Masukkan direktori tujuan upload (contoh: `./tmp`):", parse_mode="Markdown")
        return
    if data == "FM_EDIT":
        ctx.user_data["await_file_edit"] = True
        ctx.user_data["file_edit_mode"] = "path"
        await query.message.reply_text("Masukkan path file yang akan diedit (contoh: `./etc/config/network`):", parse_mode="Markdown")
        return

    # SPEEDTEST
    if data == "MENU_SPEEDTEST":
        rows = db_fetch_latest(5)
        text = build_speedtest_history_text(rows)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=speedtest_menu_keyboard()); return

    if data == "SPD_SERVER":

        cur_sid = settings_get("speedtest_server_id", "")

        await query.edit_message_text("Pilih server speedtest:", reply_markup=speedtest_server_keyboard(cur_sid)); return

    if data.startswith("SPD_SET_SERVER:"):

        sid = data.split(":", 1)[1]

        settings_set("speedtest_server_id", sid)

        await query.edit_message_text(f"Server diset ke *{sid}*", parse_mode="Markdown", reply_markup=speedtest_server_keyboard(sid)); return

    if data == "SPD_CLR_SERVER":

        settings_set("speedtest_server_id", "")

        await query.edit_message_text("Pilihan server dihapus. Gunakan auto server.", reply_markup=speedtest_server_keyboard("")); return

    if data == "SPD_NOW":

        waiting = None

        try:

            waiting = await telegram_call_with_retry(

                query.message.reply_text,

                "Menjalankan speedtest... mohon tunggu ±20–90 detik.",

            )

        except (TimedOut, NetworkError) as exc:

            print(f"[WARN] Gagal mengirim pesan awal speedtest: {exc}")

        except Exception as exc:

            print(f"[WARN] Gagal mengirim pesan awal speedtest: {exc}")

        sid = settings_get("speedtest_server_id", "")

        error_msg = None

        try:

            lat, jit, down, up, loss, url, _raw = await run_speedtest_and_parse_async(sid if sid else None)

        except Exception as exc:

            error_msg = f"[ERR] Speedtest gagal: {exc}"

        finally:

            if waiting is not None:

                with contextlib.suppress(Exception):

                    await waiting.delete()

        if error_msg:

            try:

                await telegram_call_with_retry(

                    query.message.reply_text,

                    error_msg,

                    reply_markup=speedtest_menu_keyboard(),

                )

            except Exception as exc:

                print(f"[WARN] Gagal mengirim pesan error speedtest: {exc}")

            return

        raw_clean = _raw.strip()

        if raw_clean.startswith("[ERR]"):

            try:

                await telegram_call_with_retry(

                    query.message.reply_text,

                    raw_clean,

                    reply_markup=speedtest_menu_keyboard(),

                )

            except Exception as exc:

                print(f"[WARN] Gagal mengirim hasil error speedtest: {exc}")

            return

        ts = int(time.time())

        try:

            db_insert_result(ts, lat, jit, down, up, loss, url); db_prune_keep_latest(5)

        except Exception:

            pass

        result_text = build_speedtest_result_text(ts, lat, jit, down, up, loss, url)

        try:

            await telegram_call_with_retry(

                query.message.reply_text,

                result_text,

                parse_mode="Markdown",

                reply_markup=speedtest_menu_keyboard(),

            )

        except Exception as exc:

            print(f"[WARN] Gagal mengirim hasil speedtest: {exc}")

        return



    # NETBIRD MENU

    if data == "MENU_NETBIRD":

        NB_WAIT_SETUP_KEY.discard(update.effective_chat.id)

        await query.edit_message_text("🛠️ *NetBird Control*", parse_mode="Markdown", reply_markup=netbird_menu_keyboard()); return

    if data == "NB_UP":

        out = run_cmd("netbird up", timeout=90)

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2); return

    if data == "NB_DOWN":

        out = run_cmd("netbird down", timeout=60)

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2); return

    if data == "NB_STATUS":

        out = run_cmd("netbird status", timeout=120)

        settings_set("netbird_status", out)

        settings_set("netbird_ts", str(int(time.time())))

        for chunk in split_chunks(out):

            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "NB_SETUPKEY":

        NB_WAIT_SETUP_KEY.add(update.effective_chat.id)

        await query.message.reply_text("🔑 Kirim *setup key* Anda dalam satu pesan.\nContoh: `70B919CE-27DF-4CD9-BA04-48BBDC7B6105`", parse_mode="Markdown")

        return

    if data == "NB_DEREG":

        out = run_cmd("netbird deregister", timeout=90)

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2); return



    # NETBIRD SETUP (setup-netbird.sh)

    if data == "MENU_NB_SETUP":

        await query.edit_message_text("🛠️ *NetBird Setup Menu*", parse_mode="Markdown", reply_markup=netbird_setup_menu())

        return

    if data == "NB_SETUP_CEK_STATUS":

        out = run_cmd(f"{SETUP_NB_SH} cek-status")

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "NB_SETUP_CEK_SERVICE":

        out = run_cmd(f"{SETUP_NB_SH} cek-service")

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "NB_SETUP_RUN":

        out = run_cmd(f"{SETUP_NB_SH} setup")

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "NB_SETUP_REMOVE":

        out = run_cmd(f"{SETUP_NB_SH} remove")

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)

        return

    if data == "NB_SETUP_GANTI_IP":

        ctx.user_data["await_netbird_ip"] = True

        await query.message.reply_text("Masukkan IP baru untuk NetBird (contoh: 100.99.160.251):")

        return



    # NETWORK TOOLS
    if data == "MENU_TOOLS_ROOT":
        await query.edit_message_text("🛠️ *Tools & Utilities*", parse_mode="Markdown", reply_markup=tools_menu_keyboard()); return
    if data == "MENU_USB_WD":
        if usb_watchdog_available():
            await query.edit_message_text("🛡️ *USB Watchdog*", parse_mode="Markdown", reply_markup=usb_watchdog_menu_keyboard())
        else:
            await query.message.reply_text("❌ Script usb-watchdog-setup.sh tidak ditemukan. Jalankan update installer agar fitur tersedia.")
        return
    if data == "USBWD_STATUS":
        out = run_usb_watchdog_cmd("status")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_SHOW":
        out = run_usb_watchdog_cmd("show-config")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_LIST_IF":
        out = run_usb_watchdog_cmd("list-if")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_START":
        out = run_usb_watchdog_cmd("start-service")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_STOP":
        out = run_usb_watchdog_cmd("stop-service")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_RESTART":
        out = run_usb_watchdog_cmd("restart-service")
        for chunk in split_chunks(out):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "USBWD_SETUP":
        if not usb_watchdog_available():
            await query.message.reply_text("❌ Script usb-watchdog-setup.sh tidak ditemukan. Update installer terlebih dahulu.")
            return
        ctx.user_data["await_usbwd_config"] = True
        prompt = (
            "Kirim konfigurasi USB Watchdog dengan format:\n"
            "iface interval attempts [log_file] [logging]\n"
            "Contoh: `wwan0 20 5 /var/log/usb-watchdog.log yes`\n"
            "Atau gunakan key=value: `interface=usb0 interval=15 max=3 logging=no`.\n"
            "Balas 'batal' untuk membatalkan."
        )
        await query.message.reply_text(prompt, parse_mode="Markdown")
        return
    if data == "MENU_NETTOOLS":
        await query.edit_message_text("🧪 *Network Tools*\nGunakan tombol di bawah atau perintah /ping <host> dan /trace <host>.",
                                      parse_mode="Markdown", reply_markup=nettools_menu()); return
    if data.startswith("NT_PING:"):

        host = data.split(":", 1)[1]

        out = run_cmd(f"ping -c 5 -W 2 {shlex.quote(host)}", timeout=20)

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2); return

    if data.startswith("NT_TR:"):

        host = data.split(":", 1)[1]

        if not which("traceroute"):

            await query.message.reply_text("Traceroute tidak tersedia. Install: opkg install traceroute"); return

        out = run_cmd(f"traceroute -m 15 {shlex.quote(host)}", timeout=40)

        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2); return



    # DIAGNOSTICS

    if data == "MENU_DIAG":

        await query.edit_message_text("🧪 *Diagnostics*\nPilih aksi:", parse_mode="Markdown", reply_markup=diag_menu()); return

    if data == "DIAG_TOP":
        cpu = run_cmd("ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 6")
        mem = run_cmd("ps -eo pid,comm,%mem,%cpu --sort=-%mem | head -n 6")
        load = run_cmd("uptime")
        temp = get_temperature()
        txt = f"== LOAD ==\n{load}\n\n== TOP CPU ==\n{cpu}\n\n== TOP MEM ==\n{mem}\n\n== TEMP ==\n{format_temperature(temp)}"
        for chunk in split_chunks(txt):
            await query.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == "MENU_SCHEDULER":
        await query.edit_message_text("⏰ *Scheduler (Cron)*", parse_mode="Markdown", reply_markup=scheduler_menu_keyboard()); return
    if data == "SCH_LIST":
        out = cron_list_text()
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if data == "SCH_ADD":
        ctx.user_data["await_scheduler_action"] = True
        ctx.user_data["scheduler_action"] = "add"
        await query.message.reply_text("Masukkan baris cron baru (contoh: `0 2 * * * /usr/bin/reboot`).")
        return
    if data == "SCH_DELETE":
        ctx.user_data["await_scheduler_action"] = True
        ctx.user_data["scheduler_action"] = "delete"
        await query.message.reply_text("Masukkan pola/baris yang ingin dihapus dari cron:")
        return
    if data == "SCH_RESTART":
        out = cron_restart()
        await query.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if data == "MENU_ALERTS":
        text = alerts_overview_text()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=alerts_menu_keyboard()); return
    if data == "ALERTS_REFRESH":
        text = alerts_overview_text()
        await query.message.reply_text(text, parse_mode="Markdown")
        return
    if data == "ALERTS_CLEAR":
        month_key = datetime.now(TZ).strftime("%Y%m")
        for key in ["disk_alert_state", "cpu_alert_state", f"vnstat_alert_{month_key}", "temp_alert_state"]:
            alert_set(key, "OK")
        await query.message.reply_text("✅ Semua status alert diset ke OK.")
        return

    # UPDATE BOT
    if data == "MENU_UPDATE":
        await query.edit_message_text('🧩 *Update BOT*\nPilih aksi:', parse_mode="Markdown", reply_markup=update_menu_keyboard()); return


    if data == "UPD_RUN":

        waiting = await query.message.reply_text('🧩 Membuat backup lalu mengunduh update dari GitHub...')

        ok, backup_msg = backup_bot_file()

        if not ok:

            await waiting.edit_text(f'❌ Update dibatalkan.\n{backup_msg}')

            return

        try:

            new_content = download_bot_source(BOT_UPDATE_URL, timeout=60)

        except Exception as exc:

            await waiting.edit_text(f'❌ Gagal mengunduh update: {exc}')

            return

        try:

            apply_bot_content(new_content)

        except Exception as exc:

            await waiting.edit_text(f'❌ Gagal menyimpan ra-bot.py: {exc}')

            return

        await waiting.edit_text(f'✅ Update berhasil diterapkan.\n{backup_msg}\nBot akan restart dalam 2 detik.')

        if ctx.application:

            ctx.application.create_task(restart_bot_after_delay())

        return



    if data == "UPD_DOWNGRADE":

        if not os.path.exists(BOT_BACKUP_PATH):

            await query.message.reply_text('⚠️ Tidak ada file backup, tidak ada file yang di downgrade.')

            return

        try:

            with open(BOT_BACKUP_PATH, 'rb') as fh:

                backup_data = fh.read()

        except Exception as exc:

            await query.message.reply_text(f'❌ Gagal membaca backup: {exc}')

            return

        try:

            apply_bot_content(backup_data)

        except Exception as exc:

            await query.message.reply_text(f'❌ Gagal menerapkan backup: {exc}')

            return

        await query.message.reply_text('✅ Downgrade berhasil. Bot akan restart dalam 2 detik.')

        if ctx.application:

            ctx.application.create_task(restart_bot_after_delay())

        return



    if data == "UPD_UPLOAD":

        if update.effective_chat:

            ctx.user_data["await_bot_upload"] = True

        await query.message.reply_text('📤 Kirim file *ra-bot.py* sebagai dokumen.\nBackup otomatis akan dibuat sebelum mengganti file.', parse_mode="Markdown")

        return



    # BACKUP/RESTORE

    if data == "MENU_BACKUP":

        await query.edit_message_text("🗃️ *Backup & Restore*\nPilih aksi:", parse_mode="Markdown", reply_markup=backup_menu_keyboard()); return



    if data == "BK_DO":

        waiting = await query.message.reply_text("⏳ Membuat backup…")

        path, log = create_full_backup()

        try:

            await query.message.reply_document(open(path, "rb"), filename=os.path.basename(path),

                                               caption=f"✅ Backup selesai.\n{log}")

        except Exception as e:

            await query.message.reply_text(f"❌ Gagal kirim file: {e}")

        finally:

            try: os.remove(path)

            except: pass

        await waiting.delete()

        return



    if data == "BK_RESTORE":

        if update.effective_chat:

            ctx.user_data["await_restore"] = True

        await query.message.reply_text("📤 Kirim file backup *.tgz* ke chat ini untuk mulai proses restore.\n"

                                       "Setelah terkirim, bot akan minta konfirmasi sebelum menimpa data.")

        return



    if data == "RESTORE_APPLY":

        rp = ctx.user_data.get("restore_path")

        if not rp:

            await query.message.reply_text("Tidak ada file restore yang pending.")

            return

        final_log = await restore_with_progress(ctx, query.message, rp)

        await query.message.reply_text(code_block(final_log or "(no log)"), parse_mode=ParseMode.MARKDOWN_V2)

        ctx.user_data["restore_path"] = None

        ctx.user_data["await_restore"] = False

        return



    if data == "RESTORE_CANCEL":

        rp = ctx.user_data.get("restore_path")

        if rp and os.path.isfile(rp):

            try: os.remove(rp)

            except: pass

        ctx.user_data["restore_path"] = None

        ctx.user_data["await_restore"] = False

        await query.message.reply_text("🛑 Restore dibatalkan.")

        return



    # CLI

    if data == "MENU_CLI":
        active = CLI_SESSIONS.get(update.effective_chat.id, False)
        NB_WAIT_SETUP_KEY.discard(update.effective_chat.id)
        txt = ("🖥️ *CLI Mode*\n"
               "Kirim perintah shell di chat ini, dan hasilnya akan dibalas.\n"
               "• Ketik `exit` atau tekan *Keluar CLI* untuk menonaktifkan.\n"
               "• Perintah dijalankan sebagai user bot (OpenWrt).")
        await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=cli_menu_keyboard(active))
        return
    if data == "CLI_ENTER":
        CLI_SESSIONS[update.effective_chat.id] = True
        await query.edit_message_text("✅ CLI aktif. Ketik perintah. (Ketik `exit` untuk keluar)", parse_mode="Markdown",
                                      reply_markup=cli_menu_keyboard(True))
        return
    if data == "CLI_EXIT":
        CLI_SESSIONS[update.effective_chat.id] = False
        await query.edit_message_text("❌ CLI nonaktif.", reply_markup=cli_menu_keyboard(False))
        return
    if data == "CLI_HISTORY":
        hist = CLI_HISTORY.get(update.effective_chat.id, [])
        if not hist:
            await query.message.reply_text("(history kosong)")
        else:
            text = "\n".join(f"{idx+1:02d}. {cmd}" for idx, cmd in enumerate(hist[-20:]))
            await query.message.reply_text(code_block(text), parse_mode=ParseMode.MARKDOWN_V2)
        return


# ------------------ TEXT HANDLER ------------------

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if not allowed(update): return

    chat_id = update.effective_chat.id if update.effective_chat else None

    if not chat_id: return

    text = (update.message.text or "").strip()

    if not text: return



    # If waiting for NetBird setup key

    if chat_id in NB_WAIT_SETUP_KEY:

        NB_WAIT_SETUP_KEY.discard(chat_id)

        key = text.split()[0]

        cmd = f"netbird up --setup-key {shlex.quote(key)}"

        await update.message.reply_text(f"▶️ Menjalankan:\n`{cmd}`", parse_mode="Markdown")

        out = run_cmd(cmd, timeout=120)

        try:

            netbird_status_update(force=True, timeout=90)

        except Exception:

            pass

        for chunk in split_chunks(out):

            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)

        return



    # Await set quota (from Settings menu)

    if ctx.user_data.get("await_set_quota"):

        ctx.user_data["await_set_quota"] = False

        value = parse_float_from_text(text)

        if value and value > 0:

            set_vnstat_limit_gib(value)

            await update.message.reply_text(f"✅ Batas kuota diatur ke {value:.1f} GiB.")

        else:

            await update.message.reply_text("❌ Nilai tidak valid.")

        return



    # Await set temp (from Settings menu)

    if ctx.user_data.get("await_set_temp"):

        ctx.user_data["await_set_temp"] = False

        value = parse_float_from_text(text)

        if value and value > 0:

            set_temperature_limit(value)

            await update.message.reply_text(f"✅ Batas suhu diatur ke {value:.1f}°C.")

        else:

            await update.message.reply_text("❌ Nilai tidak valid.")

        return

    if ctx.user_data.get("await_set_token"):
        ctx.user_data["await_set_token"] = False
        token = text.strip()
        if not token or ":" not in token or len(token) < 20:
            await update.message.reply_text("❌ Token/API tidak valid. Pastikan sesuai format Telegram bot.")
            return
        ok, err = persist_credentials(new_token=token)
        if ok:
            await update.message.reply_text(
                "✅ Token/API berhasil disimpan ke id-telegram.txt. Bot akan restart otomatis dalam beberapa detik untuk menerapkan token baru."
            )
            schedule_bot_restart(ctx.application)
        else:
            await update.message.reply_text(f"❌ Gagal menyimpan token: {err}")
        return

    if ctx.user_data.get("await_set_chat_id"):
        ctx.user_data["await_set_chat_id"] = False
        new_ids = _parse_chat_ids(text)
        if not new_ids:
            await update.message.reply_text("❌ Tidak ada Chat ID valid yang ditemukan.")
            return
        ok, err = persist_credentials(new_chat_ids=new_ids)
        if ok:
            listed = ", ".join(str(i) for i in new_ids)
            await update.message.reply_text(
                "✅ Chat ID diperbarui. Daftar aktif: " + listed + "\n🔁 Bot akan restart otomatis untuk menerapkan perubahan."
            )
            schedule_bot_restart(ctx.application)
        else:
            await update.message.reply_text(f"❌ Gagal menyimpan Chat ID: {err}")
        return



    # Await netbird ganti-ip

    if ctx.user_data.get("await_netbird_ip"):
        ctx.user_data["await_netbird_ip"] = False
        ip = text.strip()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            out = run_cmd(f"{SETUP_NB_SH} ganti-ip {shlex.quote(ip)}")
            await update.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text("❌ IP tidak valid.")
        return

    if ctx.user_data.get("await_power_custom"):
        action = ctx.user_data.get("power_action", "reboot")
        ctx.user_data["await_power_custom"] = False
        try:
            delay = int(float(text.strip()))
        except Exception:
            await update.message.reply_text("❌ Nilai delay tidak valid.")
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        schedule_power(ctx, chat_id, action, delay)
        verb = "Reboot" if action == "reboot" else "Shutdown"
        await update.message.reply_text(f"{verb} dijadwalkan dalam {delay} detik.")
        ctx.user_data["power_action"] = None
        return

    if ctx.user_data.get("await_opkg_action"):
        action = ctx.user_data.get("opkg_action")
        ctx.user_data["await_opkg_action"] = False
        if action == "install":
            out = opkg_install(text)
        elif action == "remove":
            out = opkg_remove(text)
        elif action == "search":
            out = opkg_search(text)
        else:
            out = "[ERR] Aksi opkg tidak dikenal."
        ctx.user_data["opkg_action"] = None
        for chunk in split_chunks(out):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if ctx.user_data.get("await_wifi_config"):
        ctx.user_data["await_wifi_config"] = False
        if text.strip().lower() == "apply":
            out = run_wifi_reload()
        else:
            out = apply_shell_commands(text)
        for chunk in split_chunks(out):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if ctx.user_data.get("await_file_browse"):
        ctx.user_data["await_file_browse"] = False
        base = ctx.user_data.get("fileman_path") or "/"
        target = resolve_user_path(text, base)
        listing = file_list_directory(str(target))
        if listing.startswith("[ERR]"):
            await update.message.reply_text(code_block(listing), parse_mode=ParseMode.MARKDOWN_V2)
            return
        resolved = target
        if resolved.is_dir():
            ctx.user_data["fileman_path"] = str(resolved)
        else:
            ctx.user_data["fileman_path"] = str(resolved.parent)
        await update.message.reply_text(code_block(listing), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if ctx.user_data.get("await_file_download"):
        ctx.user_data["await_file_download"] = False
        base = ctx.user_data.get("fileman_path") or "/"
        path = resolve_user_path(text, base)
        if not path.is_file():
            await update.message.reply_text(f"❌ File {path} tidak ditemukan.")
            return
        try:
            await update.message.reply_document(open(path, "rb"), filename=path.name)
        except Exception as exc:
            await update.message.reply_text(f"❌ Gagal mengirim file: {exc}")
        return

    if ctx.user_data.get("await_file_upload"):
        ctx.user_data["await_file_upload"] = False
        base = ctx.user_data.get("fileman_path") or "/"
        target_dir = resolve_user_path(text, base)
        if target_dir.is_file():
            target_dir = target_dir.parent
        ctx.user_data["file_upload_dir"] = str(target_dir)
        await update.message.reply_text("Sekarang kirim file sebagai dokumen.")
        return

    if ctx.user_data.get("await_file_edit"):
        mode = ctx.user_data.get("file_edit_mode")
        if mode == "path":
            base = ctx.user_data.get("fileman_path") or "/"
            target = resolve_user_path(text, base)
            ctx.user_data["file_edit_target"] = str(target)
            ctx.user_data["file_edit_mode"] = "content"
            content = file_read(target)
            if content.startswith("[ERR]"):
                ctx.user_data["await_file_edit"] = False
                ctx.user_data["file_edit_mode"] = None
                ctx.user_data["file_edit_target"] = None
                await update.message.reply_text(content)
                return
            await update.message.reply_text("Kirim konten baru untuk file berikut (akan menimpa seluruh isi):")
            for chunk in split_chunks(content):
                await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
            return
        elif mode == "content":
            target = Path(ctx.user_data.get("file_edit_target") or "")
            ctx.user_data["await_file_edit"] = False
            ctx.user_data["file_edit_mode"] = None
            ctx.user_data["file_edit_target"] = None
            if not target:
                await update.message.reply_text("❌ Target tidak diketahui.")
                return
            result = file_write(target, text)
            if result == "OK":
                await update.message.reply_text(f"✅ File {target} telah diperbarui.")
            else:
                await update.message.reply_text(result)
            return

    if ctx.user_data.get("await_firewall_action"):
        action = ctx.user_data.get("firewall_action")
        ctx.user_data["await_firewall_action"] = False
        out = apply_shell_commands(text)
        for chunk in split_chunks(out):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        if action in {"add", "delete"} and not out.startswith("[ERR]"):
            await update.message.reply_text(code_block(run_cmd("uci commit firewall")), parse_mode=ParseMode.MARKDOWN_V2)
        ctx.user_data["firewall_action"] = None
        return

    if ctx.user_data.get("await_portfwd_action"):
        ctx.user_data["await_portfwd_action"] = False
        out = apply_shell_commands(text)
        for chunk in split_chunks(out):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        ctx.user_data["portfwd_action"] = None
        if not out.startswith("[ERR]"):
            await update.message.reply_text(code_block(run_cmd("uci commit firewall")), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if ctx.user_data.get("await_process_action"):
        action = ctx.user_data.get("process_action")
        ctx.user_data["await_process_action"] = False
        if action == "kill":
            out = kill_process(text.strip())
        elif action == "restart":
            out = restart_service(text.strip())
        else:
            out = "[ERR] Aksi tidak dikenali."
        await update.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        ctx.user_data["process_action"] = None
        return

    if ctx.user_data.get("await_scheduler_action"):
        action = ctx.user_data.get("scheduler_action")
        ctx.user_data["await_scheduler_action"] = False
        if action == "add":
            out = cron_add_line(text)
        elif action == "delete":
            out = cron_delete_line(text)
        else:
            out = "[ERR] Aksi scheduler tidak dikenali."
        await update.message.reply_text(code_block(out), parse_mode=ParseMode.MARKDOWN_V2)
        ctx.user_data["scheduler_action"] = None
        return

    if ctx.user_data.get("await_usbwd_config"):
        if text.strip().lower() in {"batal", "cancel", "stop", "keluar"}:
            ctx.user_data["await_usbwd_config"] = False
            await update.message.reply_text("❌ Konfigurasi USB Watchdog dibatalkan.")
            return
        if not usb_watchdog_available():
            ctx.user_data["await_usbwd_config"] = False
            await update.message.reply_text("❌ Script usb-watchdog-setup.sh tidak ditemukan. Update installer terlebih dahulu.")
            return
        ok, msg, params = parse_usb_watchdog_input(text)
        if not ok:
            await update.message.reply_text(msg)
            return
        ctx.user_data["await_usbwd_config"] = False
        result = usb_watchdog_configure(params)
        for chunk in split_chunks(result):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        status = run_usb_watchdog_cmd("status")
        for chunk in split_chunks(status):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if ctx.user_data.get("await_log_search"):
        ctx.user_data["await_log_search"] = False
        out = log_search(text)
        for chunk in split_chunks(out):
            await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)
        return

    # CLI mode?
    if not CLI_SESSIONS.get(chat_id, False):
        return
    if text.lower() in ("exit","quit","keluar"):
        CLI_SESSIONS[chat_id] = False
        await update.message.reply_text("❌ CLI nonaktif.", reply_markup=cli_menu_keyboard(False))
        return
    hist = CLI_HISTORY[chat_id]
    hist.append(text)
    if len(hist) > 50:
        del hist[0]
    await update.message.reply_text(f"▶️ Menjalankan:\n`{text}`", parse_mode="Markdown")
    out = run_shell(text, timeout=CMD_TIMEOUT)
    if not out: out = "(no output)"

    for chunk in split_chunks(out):

        await update.message.reply_text(code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)



# ------------------ JOBS --------------------------

async def job_daily_report(ctx: ContextTypes.DEFAULT_TYPE):

    try: netbird_status_update(force=True, timeout=90)

    except Exception: pass



    iface = CURRENT_IFACE

    overview = build_overview_text(iface)

    try:

        await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=overview, parse_mode="Markdown")

        out = vnstat_daily(iface)

        for chunk in split_chunks(out):

            await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=code_block(chunk), parse_mode=ParseMode.MARKDOWN_V2)

    except Exception:

        pass



async def job_disk_watch(ctx: ContextTypes.DEFAULT_TYPE):

    _, _, free_h, used_pct = get_rootfs_info()

    free_pct = 100 - used_pct

    key = "disk_alert_state"

    last = alert_get(key)

    state = "LOW" if free_pct < DISK_THRESH_PCT else "OK"

    if state != last:

        alert_set(key, state)

        if state == "LOW":

            msg = (f"⚠️ *Disk Space Alert*\n"

                   f"Root '/' free tinggal ~*{free_pct}%*.\n"

                   f"Free (hr): `{free_h}`  | Threshold: `{DISK_THRESH_PCT}%`\n"

                   f"Waktu: `{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}`")

            try:

                await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=msg, parse_mode="Markdown")

            except Exception:

                pass



async def job_cpu_watch(ctx: ContextTypes.DEFAULT_TYPE):

    cores = get_cpu_cores()

    la1, _, _ = get_loadavg()

    ratio = la1 / max(cores, 1)

    key = "cpu_alert_state"

    last = alert_get(key)

    state = "HIGH" if ratio >= CPU_LOAD_THRESH else "OK"

    if state != last:

        alert_set(key, state)

        if state == "HIGH":

            msg = (f"⚠️ *CPU Load Alert*\n"

                   f"Load1: `{la1:.2f}` | Cores: `{cores}` ⇒ ratio ~*{ratio:.2f}*\n"

                   f"Threshold: `{CPU_LOAD_THRESH:.2f}`")

            try:

                await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=msg, parse_mode="Markdown")

            except Exception:

                pass



async def job_vnstat_watch(ctx: ContextTypes.DEFAULT_TYPE):

    try:

        gib = vnstat_current_month_gib(CURRENT_IFACE)

    except Exception:

        gib = 0.0

    limit = get_vnstat_limit_gib()

    month_key = datetime.now(TZ).strftime("%Y%m")

    key = f"vnstat_alert_{month_key}"

    already = alert_get(key)

    if gib >= limit:

        if already != "SENT":

            alert_set(key, "SENT")

            msg = (f"⚠️ *VNStat Quota Alert*\n"

                   f"Interface: `{CURRENT_IFACE}`\n"

                   f"Pemakaian bulan ini: *{gib:.1f} GiB*\n"

                   f"Ambang: `{limit:.1f} GiB`")

            try:

                await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=msg, parse_mode="Markdown")

            except Exception:

                pass

    else:

        if already == "SENT":

            alert_set(key, "OK")



async def job_temp_watch(ctx: ContextTypes.DEFAULT_TYPE):

    temp = get_temperature()

    if temp is None:

        return

    limit = get_temperature_limit()

    key = "temp_alert_state"

    last = alert_get(key)

    if temp >= limit:

        if last != "HIGH":

            alert_set(key, "HIGH")

            msg = (f"🔥 *Temperature Alert*\n"

                   f"Suhu saat ini: *{temp:.1f}°C*\n"

                   f"Ambang: `{limit:.1f}°C`")

            try:

                await ctx.bot.send_message(chat_id=REPORT_CHAT_ID, text=msg, parse_mode="Markdown")

            except Exception:

                pass

    else:

        if last == "HIGH":

            alert_set(key, "OK")



# ------------------ ERROR HANDLER -----------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:

    err = context.error

    try:

        msg = f"[ERROR] {type(err).__name__}: {err}"

        print(msg)

        # Opsional kirim ke admin:

        # await context.bot.send_message(chat_id=REPORT_CHAT_ID, text=mdv2_escape(msg), parse_mode=ParseMode.MARKDOWN_V2)

    except Exception:

        pass



# ------------------ STARTUP NOTIFY (sinkron) ------

def notify_bot_started_sync():

    text = ("🔔 *Bot aktif kembali*.\n"

            "Ketik */start* untuk memulai.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {"chat_id": REPORT_CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}

    data = urllib.parse.urlencode(payload).encode("utf-8")

    try: urllib.request.urlopen(url, data=data, timeout=5).read()

    except Exception: pass



# ------------------ MAIN --------------------------

def build_application():

    app = ApplicationBuilder().token(BOT_TOKEN).build()



    # Handlers

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("system", system_cmd))

    app.add_handler(CommandHandler("ping", ping_cmd))

    app.add_handler(CommandHandler("trace", trace_cmd))

    # command manual lama (/setquota, /settemp) sengaja dimatikan karena sudah ada tombol

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))  # untuk restore .tgz & upload bot

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))



    # Error handler

    app.add_error_handler(on_error)



    # Scheduler

    jq = app.job_queue

    if jq is None:

        print("⚠️  JobQueue tidak tersedia. Untuk mengaktifkan scheduler:\n"

              "    pip3 install --break-system-packages 'python-telegram-bot[job-queue]>=20,<21'")

    else:

        jq.run_daily(job_daily_report, time=dtime(hour=REPORT_HOUR, minute=0, second=0, tzinfo=TZ), name="daily_overview_d")

        jq.run_repeating(job_disk_watch,   interval=900,  first=30,  name="disk_watch")     # 15 menit

        jq.run_repeating(job_cpu_watch,    interval=120,  first=20,  name="cpu_watch")      # 2 menit

        jq.run_repeating(job_vnstat_watch, interval=1800, first=60,  name="vnstat_watch")   # 30 menit

        jq.run_repeating(job_temp_watch,   interval=180,  first=40,  name="temp_watch")     # 3 menit



    return app



async def run_bot_forever():
    backoff = 5
    while True:
        app = build_application()
        updater = app.updater
        if updater is None:
            raise RuntimeError("Updater tidak tersedia untuk polling bot.")
        initialized = started = polling = False
        try:
            await app.initialize()
            initialized = True
            await app.start()
            started = True
            await updater.start_polling(drop_pending_updates=True)
            polling = True
            wait_method = getattr(updater, "wait", None)
            if wait_method is not None:
                result = wait_method()
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            elif hasattr(updater, "idle"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, updater.idle)
            else:
                while getattr(updater, "running", True):
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait_time = min(backoff, 60)
            print(f"⚠️  Polling gagal: {exc}. Ulangi dalam {wait_time} detik.", flush=True)
            await asyncio.sleep(wait_time)
            backoff = min(backoff * 2, 300)
        else:
            break
        finally:
            if polling:
                with contextlib.suppress(Exception):
                    await updater.stop()
            if started:
                with contextlib.suppress(Exception):
                    await app.stop()
            if initialized:
                with contextlib.suppress(Exception):
                    await app.shutdown()



def main():

    notify_bot_started_sync()

    asyncio.run(run_bot_forever())



if __name__ == "__main__":

    # DB init harus sebelum akses settings/init iface

    db_init_once()

    if settings_get("vnstat_iface") is None:

        # set nilai awal berdasar autodetect jika belum ada

        detected = DEFAULT_IFACE or autodetect_iface()

        settings_set("vnstat_iface", detected)

    # set variabel runtime dari settings (sinkron dengan atas)

    CURRENT_IFACE = settings_get("vnstat_iface") or (DEFAULT_IFACE or autodetect_iface())



    main()
