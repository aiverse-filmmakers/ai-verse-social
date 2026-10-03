from __future__ import annotations

import copy
import os
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .util import UserError, read_json, write_json

DEFAULTS = {
    "schema_version": 1,
    "timezone": "UTC",
    "language": "en",
    "paused": False,
    "profiles": [],
    "accounts": {"auto_enroll": False, "future_backfill": True},
    "approval": {"preview_required": True, "recurring_authorized": False},
    "captions": {
        "mode": "idea", "idea": "", "ask_custom_every_time": False,
        "voice": "", "avoid": [], "cta": "", "platforms": {},
        "learn_from_user_feedback": True,
    },
    "live": {"enroll_library": False},
    "editing": {"recipe": "passthrough", "options": {}},
    "schedule": {"enabled": False, "windows": ["10:00"], "min_gap_minutes": 120,
                 "max_posts_per_day": 4, "minimum_best_time_samples": 10},
    "drive": {"enabled": False, "folders": {}, "credentials_file": "",
              "client_secrets_file": "", "archive_local_uploads": False},
    "repeat_guard": {"enabled": True, "window_days": 60, "hamming_bits": 6, "duration_tolerance": 0.05, "index_batch": 3, "batch_seconds": 120},
    "limits": {"max_jobs_per_tick": 3, "tick_seconds": 240, "request_timeout": 60},
}


def validate(config: dict) -> dict:
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
        raise UserError("Unsupported configuration version; settings must be a version 1 object.")
    # Additive defaults preserve old customer workspaces without resetting choices.
    def merge(default, supplied, path="settings"):
        if not isinstance(supplied, dict):
            raise UserError(f"{path} must be an object.")
        result = copy.deepcopy(default)
        for key, value in supplied.items():
            result[key] = merge(default[key], value, path+"."+key) if key in default and isinstance(default[key], dict) else copy.deepcopy(value)
        return result
    config = merge(DEFAULTS, config)
    def boolean(value, label):
        if type(value) is not bool:
            raise UserError(f"{label} must be true or false.")
    def number(value, label, low, high, integer=True):
        if (type(value) not in ((int,) if integer else (int,float)) or not math.isfinite(value) or not low <= value <= high):
            raise UserError(f"{label} must be {'an integer' if integer else 'a number'} between {low} and {high}.")
    try:
        if not isinstance(config["timezone"], str): raise ValueError()
        ZoneInfo(config["timezone"])
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise UserError("Choose a valid IANA timezone, for example Europe/Bucharest.") from None
    for path in ("paused",): boolean(config[path],path)
    for section, fields in {"accounts":("auto_enroll","future_backfill"), "approval":("preview_required","recurring_authorized"),
        "live":("enroll_library",), "captions":("ask_custom_every_time","learn_from_user_feedback"),
        "drive":("enabled","archive_local_uploads"), "schedule":("enabled",)}.items():
        for key in fields: boolean(config[section][key],section+"."+key)
    repeat=config['repeat_guard']
    boolean(repeat['enabled'],'repeat_guard.enabled')
    number(repeat['window_days'],'repeat_guard.window_days',1,3650)
    number(repeat['hamming_bits'],'repeat_guard.hamming_bits',0,20)
    number(repeat['duration_tolerance'],'repeat_guard.duration_tolerance',0,.25,False)
    number(repeat['index_batch'],'repeat_guard.index_batch',1,20)
    number(repeat['batch_seconds'],'repeat_guard.batch_seconds',10,120)
    profiles=config["profiles"]
    if not isinstance(profiles,list) or any(not isinstance(v,str) or not v.strip() for v in profiles) or len(set(profiles))!=len(profiles):
        raise UserError("profiles must be a unique list of verified Zernio profile IDs.")
    captions=config["captions"]
    if not isinstance(captions["mode"],str) or captions["mode"] not in {"idea","custom","transcript","hybrid"}:
        raise UserError("Caption mode must be idea, custom, transcript, or hybrid.")
    for key in ("idea","voice","cta"):
        if not isinstance(captions[key],str): raise UserError(f"captions.{key} must be text.")
    if not isinstance(captions["avoid"],list) or any(not isinstance(v,str) for v in captions["avoid"]) or not isinstance(captions["platforms"],dict):
        raise UserError("Caption avoid words must be a list and platform preferences must be an object.")
    if any(not isinstance(k,str) or not isinstance(v,dict) for k,v in captions["platforms"].items()): raise UserError("Platform caption preferences must map platform names to objects.")
    if not isinstance(config["language"],str) or not config["language"].strip(): raise UserError("language must contain text.")
    schedule=config["schedule"]
    windows=schedule["windows"]
    if not isinstance(windows,list) or not windows or any(not isinstance(v,str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]",v) for v in windows) or len(set(windows))!=len(windows):
        raise UserError("Schedule windows must be a nonempty unique list of HH:MM times.")
    number(schedule["max_posts_per_day"],"schedule.max_posts_per_day",1,1000)
    number(schedule["min_gap_minutes"],"schedule.min_gap_minutes",0,10080)
    number(schedule["minimum_best_time_samples"],"schedule.minimum_best_time_samples",1,1000000)
    limits=config["limits"]
    number(limits["max_jobs_per_tick"],"max_jobs_per_tick",1,100)
    number(limits["tick_seconds"],"tick_seconds",1,900)
    number(limits["request_timeout"],"request_timeout",1,120)
    drive=config["drive"]
    if not isinstance(drive["folders"],dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9_-]+",v) for k,v in drive["folders"].items()):
        raise UserError("Drive folder mappings must contain valid customer-approved folder IDs.")
    for key in ("credentials_file","client_secrets_file"):
        if not isinstance(drive[key],str): raise UserError(f"drive.{key} must be a file path.")
    if not isinstance(config["editing"]["recipe"],str) or config["editing"]["recipe"] not in {"passthrough","standard"} or not isinstance(config["editing"]["options"],dict):
        raise UserError("Editing recipe must be passthrough or standard with an options object.")
    if schedule["enabled"] and not config["approval"]["recurring_authorized"]:
        raise UserError("Recurring scheduling requires recorded scope approval before it can be enabled.")
    forbidden={"api_key","access_token","refresh_token","password","cookies","client_secret"}
    def check(value):
        if isinstance(value,dict):
            if forbidden.intersection(str(k).lower() for k in value): raise UserError("Store credentials in a secret store or environment, not settings.json.")
            for v in value.values(): check(v)
        elif isinstance(value,list):
            for v in value: check(v)
    check(config)
    return config


def validate_capability(value):
    if not isinstance(value,dict) or value.get("video") is not True or not isinstance(value.get("evidence"),str) or not value["evidence"].strip():
        raise UserError("Video capability needs an evidence source; unknown support cannot be enabled.")
    if not isinstance(value.get("proof","public_url"),str) or value.get("proof","public_url") not in {"public_url","provider_identifier"}:
        raise UserError("Capability proof must be public_url or provider_identifier.")
    for key in ("caption_limit","max_bytes","max_duration"):
        if key in value and (type(value[key]) not in (int,float) or not math.isfinite(value[key]) or value[key]<=0):
            raise UserError(f"Capability {key} must be a positive number.")
    required=value.get("required_fields",[])
    if not isinstance(required,list) or any(not isinstance(v,str) or not v for v in required):
        raise UserError("Capability required_fields must be a list of field names.")
    return value


class Workspace:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        skill_root=Path(__file__).resolve().parents[2]
        if self.path==skill_root or self.path.is_relative_to(skill_root):
            raise UserError("Customer workspace must be separate from the installed skill folder.")

    @property
    def settings_path(self) -> Path:
        return self.path / "settings.json"

    def init(self) -> dict:
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name=="posix":
            try:
                self.path.chmod(0o700)
            except OSError:
                raise UserError("Could not restrict the customer workspace permissions.") from None
        for name in ("media/originals", "media/ready", "media/posted", "media/review",
                     "logs/accounts", "captions", "requests", "backups", "transcripts", "branding"):
            directory=self.path/name
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name=="posix":
                try:
                    directory.chmod(0o700)
                except OSError:
                    raise UserError("Could not restrict a customer workspace folder.") from None
        if not self.settings_path.exists():
            write_json(self.settings_path, copy.deepcopy(DEFAULTS))
        return self.config

    @property
    def config(self) -> dict:
        return validate(read_json(self.settings_path))

    def save(self, config: dict) -> None:
        write_json(self.settings_path, validate(config))
