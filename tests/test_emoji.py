from ragtone.board import empty_board, pin_node, present_board
from ragtone.channel_preview import slack_plain
from ragtone.emoji import emojize
from ragtone.models import Hit


def test_emojize_turns_shortcodes_into_glyphs() -> None:
    assert emojize("see :thread: please") == "see 🧵 please"
    assert emojize(":+1: done") == "👍 done"
    assert emojize(":THUMBSUP:") == "👍"
    assert emojize("custom :not_a_real_emoji:") == "custom :not_a_real_emoji:"
    assert emojize("") == ""
    assert emojize("no codes") == "no codes"


def test_emojize_keeps_skin_tone_modifiers() -> None:
    assert emojize(":wave::skin-tone-3:") == "👋🏼"


def test_hit_presents_emoji_in_title_and_text() -> None:
    hit = Hit(
        id="chat:1",
        score=1.0,
        source="chat",
        title=":thread: sso",
        text="please :wave: here",
        url="",
        native_id="1",
    )
    data = hit.as_dict()
    assert data["title"] == "🧵 sso"
    assert data["text"] == "please 👋 here"


def test_present_board_renders_emoji_on_cards() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "title": "see :thread:",
            "text": ":+1: ok",
            "thread_id": "a",
        },
    )
    view = present_board(board)
    assert view["nodes"][0]["title"] == "see 🧵"
    assert view["nodes"][0]["excerpt"] == "👍 ok"
    assert board.nodes[0].title == "see :thread:"


def test_slack_plain_renders_emoji_shortcodes() -> None:
    assert slack_plain("<@U123> see :thread:") == "see 🧵"
