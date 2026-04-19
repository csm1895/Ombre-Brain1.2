import re
import uuid
from datetime import datetime
from pathlib import Path

def generate_bucket_id():
    return uuid.uuid4().hex[:12]

def sanitize_name(name: str) -> str:
    if not name:
        return "untitled"
    name = re.sub(r'[\\/:*?"<>|]+', "_", str(name))
    name = name.strip().replace(" ", "_")
    return name[:80] or "untitled"

def safe_path(base, *parts):
    base_path = Path(base).resolve()
    target = base_path.joinpath(*parts).resolve()
    if not str(target).startswith(str(base_path)):
        raise ValueError("unsafe path")
    return target

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

import os
import yaml
import logging

def load_config():
    config = {}
    if Path("config.yaml").exists():
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    elif Path("config.example.yaml").exists():
        with open("config.example.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR")
    if buckets_dir:
        config["buckets_dir"] = buckets_dir

    config.setdefault("buckets_dir", str(Path.cwd() / "buckets_test"))
    config.setdefault("log_level", "INFO")
    return config

def setup_logging(level="INFO"):
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

def strip_wikilinks(text):
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    return text

def count_tokens_approx(text):
    return max(1, len(str(text)) // 4)
