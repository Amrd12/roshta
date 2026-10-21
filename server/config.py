import os
import json
import re
from dataclasses import dataclass, asdict, field
from typing import Tuple, Optional

# Base directory for the server
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@dataclass
class AppConfig:
    # Network & Server
    host: str = "127.0.0.1"
    port: int = 49200
    secret_token: str = "roshta-dev-token-secret"
    
    # Paths & Storage
    database_path: str = os.path.join(BASE_DIR, "roshta.db")
    log_dir: str = os.path.join(BASE_DIR, "logs")
    source_folder: str = ""
    finished_folder: str = ""
    hot_folder: str = ""
    disk_min_bytes: int = 5 * 1024 ** 3  # 5 GB threshold by default
    
    # Scanner & Filename Rules
    scan_interval_seconds: float = 5.0
    file_min_age_seconds: float = 3.0
    allowed_extensions: Tuple[str, ...] = (".psd", ".psb", ".tif", ".tiff")
    doctor_regex_pattern: str = r"^(.*?)(?:[_\-\s]\d+)?\.(psd|psb|tif|tiff)$"
    
    # Prepress Defaults
    canvas_width_cm: float = 50.0
    canvas_height_cm: float = 34.0
    canvas_dpi: int = 300
    color_mode: str = "CMYK"
    color_profile: str = "ISO Coated v2 (ECI)"
    bleed_margin_mm: float = 0.0
    
    # Process Watcher & Actions
    photoshop_grace_period_seconds: int = 45
    after_finish_action: str = "none" # "none", "shutdown", "sleep", "lock", "close_ps"
    after_finish_countdown_seconds: int = 60

    # Runtime compiled regex
    _compiled_doctor_regex: Optional[re.Pattern] = field(init=False, default=None, repr=False)

    def __post_init__(self):
        # Ensure log directory exists
        os.makedirs(self.log_dir, exist_ok=True)
        # Ensure database directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.database_path)), exist_ok=True)
        # Compile regex
        self._compiled_doctor_regex = re.compile(self.doctor_regex_pattern, re.IGNORECASE)

    @property
    def doctor_regex(self) -> re.Pattern:
        if self._compiled_doctor_regex is None:
            self._compiled_doctor_regex = re.compile(self.doctor_regex_pattern, re.IGNORECASE)
        return self._compiled_doctor_regex

    def to_dict(self) -> dict:
        data = asdict(self)
        data["allowed_extensions"] = list(self.allowed_extensions)
        return data

    @classmethod
    def load_from_file(cls, config_path: str) -> "AppConfig":
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "allowed_extensions" in data:
                        data["allowed_extensions"] = tuple(data["allowed_extensions"])
                    return cls(**data)
            except Exception:
                pass
        return cls()

    def save_to_file(self, config_path: str):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

# Global default configuration instance
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
config = AppConfig.load_from_file(DEFAULT_CONFIG_PATH)
