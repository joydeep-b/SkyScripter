from sky_scripter.discord_roof_watchdog import (
    discord_message_text,
    infer_roof_state,
    pick_roof_from_messages,
)


def test_discord_message_text_includes_embeds():
    msg = {
        "content": "",
        "embeds": [{
            "title": "Observatory",
            "description": "Roof opening",
            "fields": [{"name": "Site", "value": "SFRO"}],
        }],
    }

    text = discord_message_text(msg)

    assert "Observatory" in text
    assert "Roof opening" in text
    assert "SFRO" in text


def test_infer_roof_state():
    assert infer_roof_state("Roof opening") == "OPEN"
    assert infer_roof_state("Roof closing") == "CLOSED"
    assert infer_roof_state("cloud sensor online") == "UNKNOWN"


def test_pick_roof_from_messages_newest_first():
    messages = [
        {"content": "unrelated"},
        {"content": "Roof closing"},
        {"content": "Roof opening"},
    ]

    msg, text = pick_roof_from_messages(messages)

    assert msg == messages[1]
    assert text == "Roof closing"
