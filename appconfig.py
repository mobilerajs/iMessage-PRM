# appconfig.py
"""Single source of truth for settings, with precedence env > config.json >
default. config.json is OPTIONAL (copy from config.example.json). This makes the
config.json keys (user_name, model, chat_db, contacts_vcf) actually do something
— previously only user_name was read and the path/model keys were dead."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(HERE, "config.json")


def _load(config_path=None):
    path = config_path or _DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def resolve(config_key, env_var, default, config_path=None):
    """env_var, then config.json[config_key], then default. Paths are NOT
    expanduser'd here — callers that take paths should wrap in os.path.expanduser."""
    env = os.environ.get(env_var)
    if env:
        return env
    val = _load(config_path).get(config_key)
    return val if val not in (None, "") else default
