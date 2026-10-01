import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as module


def test_empty_access_token_is_generated_as_valid_yaml(monkeypatch, tmp_path):
    path = tmp_path / "onebot_v11.yml"
    path.write_text(
        'ws_url: "ws://127.0.0.1:8080"\n'
        'access_token: ""  # Auto-generated on first init if left empty\n'
        "access_token_in_url: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ONEBOT_V11_CONFIG_PATH", path)
    monkeypatch.setattr(module, "_generate_token", lambda: "9digit-first-token")

    assert module._ensure_access_token_in_file() == "9digit-first-token"

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["access_token"] == "9digit-first-token"
    assert data["access_token_in_url"] is True
    assert "# Auto-generated on first init if left empty" in path.read_text(encoding="utf-8")


def test_existing_access_token_is_kept(monkeypatch, tmp_path):
    path = tmp_path / "onebot_v11.yml"
    path.write_text('access_token: "kept"\n', encoding="utf-8")
    monkeypatch.setattr(module, "ONEBOT_V11_CONFIG_PATH", path)

    assert module._ensure_access_token_in_file() == "kept"
    assert path.read_text(encoding="utf-8") == 'access_token: "kept"\n'
