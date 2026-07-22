from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_preparation_preserves_operator_gateway_authorization(tmp_path: Path) -> None:
    operator_id = "111111111111111111"
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "discord_bot_token").write_text("test-live-token\n", encoding="utf-8")
    (credentials / "docket_to_hermes_token").write_text("mcp-token\n", encoding="utf-8")
    (credentials / "hermes_to_docket_token").write_text(
        "callback-token\n", encoding="utf-8"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"DOCKET_CREDENTIALS_DIR={credentials}",
                f"DOCKET_OPERATOR_DISCORD_USER_ID={operator_id}",
                "DOCKET_DISCORD_GUILD_ID=222222222222222222",
                "DOCKET_CHAT_CHANNEL_ID=333333333333333333",
                "DOCKET_QUEUE_CHANNEL_ID=444444444444444444",
                "DOCKET_SYSTEM_CHANNEL_ID=555555555555555555",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    hermes_home = tmp_path / "hermes"
    environment = dict(os.environ)
    environment.update(
        {
            "DOCKET_ENV_FILE": str(env_file),
            "DOCKET_HERMES_HOME": str(hermes_home),
            "DOCKET_CREDENTIALS_DIR": str(credentials),
        }
    )

    subprocess.run(
        ["sh", "scripts/prepare-hermes-home.sh"],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )

    runtime = dict(
        line.split("=", 1)
        for line in (hermes_home / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert runtime["DISCORD_ALLOWED_USERS"] == operator_id
    assert "DISCORD_HOME_CHANNEL" not in runtime
