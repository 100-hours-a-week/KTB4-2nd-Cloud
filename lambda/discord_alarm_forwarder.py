import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import boto3


_ssm = boto3.client("ssm")
_webhook_url: str | None = None

_STATE_STYLE = {
    "ALARM": ("🚨", 0xE74C3C),
    "OK": ("✅", 0x2ECC71),
    "INSUFFICIENT_DATA": ("⚠️", 0xF1C40F),
}


def _get_webhook_url() -> str:
    global _webhook_url

    if _webhook_url is not None:
        return _webhook_url

    parameter_name = os.environ["DISCORD_WEBHOOK_PARAMETER_NAME"]
    response = _ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    webhook_url = response["Parameter"]["Value"]
    parsed = urlparse(webhook_url)

    if parsed.scheme != "https" or parsed.hostname not in {
        "discord.com",
        "discordapp.com",
    }:
        raise ValueError("Discord webhook parameter contains an invalid URL")

    _webhook_url = webhook_url
    return webhook_url


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "-")
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _format_state_change_time(value: Any) -> str:
    if not value:
        return "-"

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        korea_standard_time = timezone(timedelta(hours=9))
        return parsed.astimezone(korea_standard_time).strftime("%Y-%m-%d %H:%M:%S KST")
    except ValueError:
        return _truncate(value, 128)


def _build_payload(message: dict[str, Any]) -> dict[str, Any]:
    state = str(message.get("NewStateValue", "UNKNOWN"))
    emoji, color = _STATE_STYLE.get(state, ("ℹ️", 0x3498DB))
    alarm_name = _truncate(message.get("AlarmName", "Unknown alarm"), 240)
    previous_state = _truncate(message.get("OldStateValue", "UNKNOWN"), 128)

    return {
        "username": "Yeodam CloudWatch",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{emoji} {state} | {alarm_name}",
                "description": _truncate(message.get("NewStateReason"), 3000),
                "color": color,
                "fields": [
                    {
                        "name": "상태 변경",
                        "value": f"{previous_state} → {state}",
                        "inline": True,
                    },
                    {
                        "name": "리전",
                        "value": _truncate(message.get("Region", "ap-northeast-2"), 128),
                        "inline": True,
                    },
                    {
                        "name": "발생 시각",
                        "value": _format_state_change_time(message.get("StateChangeTime")),
                        "inline": False,
                    },
                ],
            }
        ],
    }


def _post_to_discord(webhook_url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "yeodam-cloudwatch-alarm-forwarder/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status not in {200, 204}:
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Discord returned HTTP {error.code}") from error


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, int]:
    webhook_url = _get_webhook_url()
    delivered = 0

    for record in event.get("Records", []):
        raw_message = record.get("Sns", {}).get("Message")
        if not raw_message:
            continue

        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            message = {
                "AlarmName": "SNS notification",
                "NewStateValue": "UNKNOWN",
                "NewStateReason": raw_message,
            }

        _post_to_discord(webhook_url, _build_payload(message))
        delivered += 1

    return {"delivered": delivered}
