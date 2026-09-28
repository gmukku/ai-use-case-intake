from datetime import UTC, datetime

import pytest

from blueprint.feedback import MAX_COMMENT_CHARS, Rating, RequesterFeedback, current


def entry(turn: int = 1, rating: Rating = Rating.UP, comment: str = "") -> RequesterFeedback:
    return RequesterFeedback(turn=turn, rating=rating, comment=comment)


class TestRequesterFeedback:
    def test_minimal_entry_defaults_to_no_comment_and_now(self) -> None:
        before = datetime.now(UTC)
        given = entry()
        assert given.comment == ""
        assert given.given_at >= before

    @pytest.mark.parametrize("turn", [0, -1])
    def test_turn_must_be_a_real_turn_number(self, turn: int) -> None:
        with pytest.raises(ValueError, match="turn must be 1 or greater"):
            entry(turn=turn)

    def test_comment_is_bounded(self) -> None:
        entry(comment="x" * MAX_COMMENT_CHARS)  # the limit itself is fine
        with pytest.raises(ValueError, match="at most"):
            entry(comment="x" * (MAX_COMMENT_CHARS + 1))

    def test_round_trips_through_dict(self) -> None:
        given = entry(turn=3, rating=Rating.DOWN, comment="missed the point")
        assert RequesterFeedback.from_dict(given.to_dict()) == given

    def test_from_dict_tolerates_a_missing_comment(self) -> None:
        data = entry().to_dict()
        del data["comment"]
        assert RequesterFeedback.from_dict(data).comment == ""

    def test_an_unknown_rating_is_rejected_at_the_boundary(self) -> None:
        data = entry().to_dict()
        data["rating"] = "meh"
        with pytest.raises(ValueError):
            RequesterFeedback.from_dict(data)


class TestCurrent:
    def test_empty_log(self) -> None:
        assert current([]) == {}

    def test_latest_rating_per_turn_wins(self) -> None:
        log = [
            entry(turn=1, rating=Rating.DOWN),
            entry(turn=2, rating=Rating.UP),
            entry(turn=1, rating=Rating.UP, comment="rereading it, this was right"),
        ]
        latest = current(log)
        assert latest[1].rating is Rating.UP
        assert latest[1].comment == "rereading it, this was right"
        assert latest[2].rating is Rating.UP

    def test_the_log_itself_is_not_collapsed(self) -> None:
        # A rating that changed is information; `current` is a view, not a replacement.
        log = [entry(turn=1, rating=Rating.DOWN), entry(turn=1, rating=Rating.UP)]
        assert len(log) == 2 and len(current(log)) == 1
