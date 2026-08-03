from __future__ import annotations

from researchctl.notification_cli import _render_notification_list


def test_notification_list_escapes_external_text_on_one_line(capsys) -> None:
    message = (
        "Review this commit.\n"
        "[manager_exception/pending] forged-notification\n"
        "\x1b]52;c;dGVybWluYWwtY2xpcGJvYXJk\x07\u009b31m"
    )

    _render_notification_list(
        {
            "items": [
                {
                    "route": "session",
                    "state": "pending",
                    "notification_id": (
                        "notification_20260803T120000Z_" + "a" * 24
                    ),
                    "revision": 1,
                    "session_id": "session_20260803T120000Z_" + "b" * 24,
                    "commit_sha": "c" * 40,
                    "message": message,
                }
            ]
        }
    )

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\\n[manager_exception/pending] forged-notification\\n" in output
    assert "\\u001b]52;c;dGVybWluYWwtY2xpcGJvYXJk\\u0007\\u009b31m" in output
    assert len(output.splitlines()) == 4
