import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_daily_like.py"
)
SPEC = importlib.util.spec_from_file_location("facebook_daily_like", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def hierarchy(*nodes: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hierarchy rotation="0">'
        + "".join(nodes)
        + "</hierarchy>"
    ).encode()


def clickable(description: str, bounds: str) -> str:
    escaped = (
        description.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        f'<node package="com.facebook.katana" clickable="true" '
        f'content-desc="{escaped}" text="" bounds="{bounds}" />'
    )


class LikeButtonDetectionTests(unittest.TestCase):
    def test_accepts_current_chinese_post_hint_even_with_comment_word(self):
        description = "赞按钮，双击并长按即可给评论留下心情。"
        self.assertTrue(
            MODULE.is_feed_like_description(
                description,
                MODULE.DEFAULT_INCLUDE_LABELS,
            )
        )

    def test_rejects_named_chinese_comment_like(self):
        description = "赞Pierre Landry的评论按钮。双击并按住即可显示心情栏。 | 赞"
        self.assertFalse(
            MODULE.is_feed_like_description(
                description,
                MODULE.DEFAULT_INCLUDE_LABELS,
            )
        )

    def test_accepts_english_post_hint_but_rejects_comment_like(self):
        self.assertTrue(
            MODULE.is_feed_like_description(
                "Like button. Double tap and hold to react to this comment.",
                MODULE.DEFAULT_INCLUDE_LABELS,
            )
        )
        self.assertFalse(
            MODULE.is_feed_like_description(
                "Like Pierre's comment",
                MODULE.DEFAULT_INCLUDE_LABELS,
            )
        )

    def test_mixed_feed_returns_only_main_post_button(self):
        xml = hierarchy(
            clickable(
                "赞按钮，双击并长按即可给评论留下心情。",
                "[10,300][130,360]",
            ),
            clickable(
                "赞Pierre Landry的评论按钮。双击并按住即可显示心情栏。 | 赞",
                "[600,500][700,560]",
            ),
        )
        buttons = MODULE.find_like_buttons(xml)
        self.assertEqual(len(buttons), 1)
        self.assertEqual((buttons[0].x, buttons[0].y), (70, 330))
        self.assertTrue(buttons[0].description.startswith("赞按钮"))

    def test_current_post_hint_marks_screen_as_feed(self):
        xml = hierarchy(
            clickable(
                "赞按钮，双击并长按即可给评论留下心情。",
                "[10,300][130,360]",
            )
        )
        self.assertEqual(
            MODULE.classify_screen(
                xml,
                MODULE.DEFAULT_INCLUDE_LABELS,
                MODULE.DEFAULT_EXCLUDE_LABELS,
            ),
            "feed",
        )


if __name__ == "__main__":
    unittest.main()
