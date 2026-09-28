import pytest

from blueprint.redaction import redact_snapshot, redact_text


class TestRedactText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("write to dana@example.com please", "write to [redacted:email] please"),
            ("call me on 555-123-4567", "call me on [redacted:phone]"),
            ("call (555) 123-4567 today", "call [redacted:phone] today"),
            ("+1 555 123 4567 is the line", "[redacted:phone] is the line"),
            ("SSN 123-45-6789 is on the form", "SSN [redacted:ssn] is on the form"),
            ("card 4111 1111 1111 1111 expired", "card [redacted:card] expired"),
            ("see https://intranet.local/hr?id=3", "see [redacted:url]"),
            ("account 123456789012 is stale", "account [redacted:number] is stale"),
        ],
    )
    def test_identifiers_are_replaced(self, raw: str, expected: str) -> None:
        assert redact_text(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "We do 30 to 40 new hires a month.",
            "It takes 20-30 minutes each, every day.",
            "The 2026 handbook says three business days.",
            "Section 2 has to be signed within 3 days.",
            "Roughly 12 hours a month chasing people.",
            "We use Workday and ADP.",
            "I-9 and W-4 forms, plus direct deposit.",
        ],
    )
    def test_the_substance_of_a_discovery_conversation_survives(self, raw: str) -> None:
        # These numbers are the answer to "how many" and "how often". Shredding them would
        # make the stored spec useless, which is a worse outcome than a missed identifier.
        assert redact_text(raw) == raw

    def test_several_identifiers_in_one_sentence(self) -> None:
        raw = "Email dana@example.com or call 555-123-4567 about SSN 123-45-6789."
        assert redact_text(raw) == (
            "Email [redacted:email] or call [redacted:phone] about SSN [redacted:ssn]."
        )

    def test_an_ssn_is_labelled_as_one_not_as_a_number(self) -> None:
        # Pattern order matters: the looser digit rule must not claim it first.
        assert redact_text("123-45-6789") == "[redacted:ssn]"

    def test_redaction_is_idempotent(self) -> None:
        once = redact_text("mail dana@example.com")
        assert redact_text(once) == once

    def test_empty_and_plain_text(self) -> None:
        assert redact_text("") == ""
        assert redact_text("no identifiers here at all") == "no identifiers here at all"


class TestRedactSnapshot:
    def test_free_text_fields_are_redacted(self) -> None:
        snapshot = {
            "turns": [
                {"user_text": "reach me at dana@example.com", "assistant_text": "Noted.", "turn": 1}
            ]
        }
        out = redact_snapshot(snapshot)
        assert out["turns"][0]["user_text"] == "reach me at [redacted:email]"
        assert out["turns"][0]["turn"] == 1

    def test_canvas_current_is_redacted_despite_its_category_keys(self) -> None:
        snapshot = {
            "canvas": {
                "current": {
                    "key_stakeholders": "Dana, on 555-123-4567",
                    "output_format": None,
                }
            }
        }
        current = redact_snapshot(snapshot)["canvas"]["current"]
        assert current["key_stakeholders"] == "Dana, on [redacted:phone]"
        assert current["output_format"] is None

    def test_structure_and_non_text_values_are_untouched(self) -> None:
        snapshot = {
            "session_id": "11111111-2222-3333-4444-555555555555",
            "total_cost_usd": 0.1466,
            "turn": 8,
            "is_complete": True,
            "canvas": {"missing": ["output_format"]},
        }
        assert redact_snapshot(snapshot) == snapshot

    def test_a_session_id_is_not_mistaken_for_an_identifier(self) -> None:
        # It is ours, it is in the URL, and redacting it would break every lookup.
        snapshot = {"session_id": "11111111-2222-3333-4444-555555555555"}
        assert redact_snapshot(snapshot)["session_id"] == snapshot["session_id"]

    def test_nested_lists_and_dicts(self) -> None:
        snapshot = {
            "reviews": [{"note": "ping dana@example.com", "reviewer": "Dana", "action": "approve"}],
            "feedback": [{"comment": "call 555-123-4567", "rating": "down"}],
        }
        out = redact_snapshot(snapshot)
        assert out["reviews"][0]["note"] == "ping [redacted:email]"
        assert out["reviews"][0]["reviewer"] == "Dana"  # a name is not a mechanical shape
        assert out["feedback"][0]["comment"] == "call [redacted:phone]"

    def test_the_original_is_not_mutated(self) -> None:
        snapshot = {"turns": [{"user_text": "dana@example.com"}]}
        redact_snapshot(snapshot)
        assert snapshot["turns"][0]["user_text"] == "dana@example.com"
