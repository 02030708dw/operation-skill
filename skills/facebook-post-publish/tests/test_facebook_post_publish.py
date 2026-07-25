from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_post_publish.py"
)
SPEC = importlib.util.spec_from_file_location("facebook_post_publish", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def media(path: str, mtime: int = 1) -> object:
    kind, mime = MODULE.classify_media(path)
    return MODULE.RemoteMedia(path, 100, mtime, kind, mime)


class FacebookPostPublishTest(unittest.TestCase):
    def test_classifies_image_video_and_rejects_other(self) -> None:
        self.assertEqual(MODULE.classify_media("/x/a.png")[0], "image")
        self.assertEqual(MODULE.classify_media("/x/a.mp4")[0], "video")
        self.assertIsNone(MODULE.classify_media("/x/a.json"))

    def test_listing_parser_sorts_newest_first(self) -> None:
        parsed = MODULE.parse_media_listing(
            "10|20|/sdcard/upload/a.png\n"
            "30|40|/sdcard/upload/b.mp4\n"
            "bad line\n"
        )
        self.assertEqual([item.name for item in parsed], ["b.mp4", "a.png"])

    def test_media_type_selects_matching_video(self) -> None:
        selected = MODULE.select_media(
            [media("/x/result.png"), media("/x/result.mp4")],
            media_file=None,
            media_type="video",
            match_text=None,
            latest=False,
        )
        self.assertEqual(selected.path, "/x/result.mp4")

    def test_exact_filename_selects_one(self) -> None:
        selected = MODULE.select_media(
            [media("/x/a.png"), media("/x/b.png")],
            media_file="b.png",
            media_type="auto",
            match_text=None,
            latest=False,
        )
        self.assertEqual(selected.name, "b.png")

    def test_ambiguous_media_stops(self) -> None:
        with self.assertRaises(MODULE.MediaSelectionError):
            MODULE.select_media(
                [media("/x/a.png"), media("/x/b.png")],
                media_file=None,
                media_type="image",
                match_text=None,
                latest=False,
            )

    def test_latest_requires_unique_newest(self) -> None:
        selected = MODULE.select_media(
            [media("/x/a.png", 1), media("/x/b.png", 2)],
            media_file=None,
            media_type="image",
            match_text=None,
            latest=True,
        )
        self.assertEqual(selected.name, "b.png")

    def test_text_only_returns_no_media(self) -> None:
        self.assertIsNone(
            MODULE.select_media(
                [],
                media_file=None,
                media_type="none",
                match_text=None,
                latest=False,
            )
        )

    def test_image_only_post_is_valid_without_text(self) -> None:
        MODULE.validate_post_content("", wants_media=True, list_media=False)

    def test_video_only_post_is_valid_without_text(self) -> None:
        MODULE.validate_post_content("", wants_media=True, list_media=False)

    def test_empty_post_is_rejected(self) -> None:
        with self.assertRaises(MODULE.ConfigurationError):
            MODULE.validate_post_content(
                "", wants_media=False, list_media=False
            )

    def test_publish_button_uses_enabled_clickable_exact_label(self) -> None:
        xml = b"""<hierarchy>
          <node text="Share this thought" clickable="true"
            enabled="true" bounds="[0,100][100,150]" />
          <node text="Post" clickable="true"
            enabled="true" bounds="[800,10][1000,90]" />
        </hierarchy>"""
        target = MODULE.find_publish_button(xml)
        self.assertIsNotNone(target)
        self.assertEqual(target.description, "Post")

    def test_disabled_publish_button_is_ignored(self) -> None:
        xml = b"""<hierarchy>
          <node text="Post" clickable="true"
            enabled="false" bounds="[800,10][1000,90]" />
        </hierarchy>"""
        self.assertIsNone(MODULE.find_publish_button(xml))

    def test_feed_share_button_is_not_publish_button(self) -> None:
        xml = """<hierarchy>
          <node text="分享" clickable="true"
            enabled="true" bounds="[150,200][230,250]" />
        </hierarchy>""".encode("utf-8")
        self.assertIsNone(MODULE.find_publish_button(xml))

    def test_fingerprint_changes_with_media(self) -> None:
        first = MODULE.content_fingerprint("hello", media("/x/a.png"))
        second = MODULE.content_fingerprint("hello", media("/x/b.png"))
        self.assertNotEqual(first, second)

    def test_facebook_home_plus_uses_screenshot_position_fallback(self) -> None:
        xml = b"""<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="" clickable="true" enabled="true"
              bounds="[140,8][180,50]" />
            <node text="" clickable="true" enabled="true"
              bounds="[180,8][210,50]" />
          </node>
        </hierarchy>"""
        target = MODULE.find_home_create_button(xml)
        self.assertIsNotNone(target)
        self.assertEqual(target.x, 160)
        self.assertEqual(MODULE.classify_facebook_screen(xml), "home")

    def test_create_menu_finds_chinese_post_item(self) -> None:
        xml = """<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="帖子" clickable="true" enabled="true"
              bounds="[115,40][210,85]" />
          </node>
        </hierarchy>""".encode("utf-8")
        target = MODULE.find_labeled_target(xml, MODULE.POST_MENU_LABELS)
        self.assertIsNotNone(target)
        self.assertEqual(target.description, "帖子")
        self.assertEqual(MODULE.classify_facebook_screen(xml), "create-menu")

    def test_composer_finds_gallery_while_publish_is_disabled(self) -> None:
        xml = """<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="图库" clickable="true" enabled="true"
              bounds="[10,300][75,360]" />
            <node text="发布" clickable="true" enabled="false"
              bounds="[170,365][235,395]" />
          </node>
        </hierarchy>""".encode("utf-8")
        self.assertIsNotNone(
            MODULE.find_labeled_target(xml, MODULE.GALLERY_LABELS)
        )
        self.assertIsNone(MODULE.find_publish_button(xml))
        self.assertEqual(MODULE.classify_facebook_screen(xml), "composer")

    def test_gallery_video_selects_first_duration_tile_not_image(self) -> None:
        selected = media("/sdcard/upload/result.mp4", 10)
        xml = b"""<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="" clickable="true" enabled="true" selected="true"
              bounds="[0,60][80,150]" />
            <node text="" clickable="true" enabled="true"
              bounds="[80,60][160,150]">
              <node text="00:10" clickable="false" enabled="true"
                bounds="[125,125][158,148]" />
            </node>
            <node text="" clickable="true" enabled="true"
              bounds="[160,60][240,150]">
              <node text="00:10" clickable="false" enabled="true"
                bounds="[205,125][238,148]" />
            </node>
          </node>
        </hierarchy>"""
        target, method = MODULE.find_gallery_tile(xml, selected)
        self.assertIsNotNone(target)
        self.assertEqual((target.x, target.y), (120, 105))
        self.assertEqual(method, "video-duration")

    def test_gallery_image_selects_tile_without_duration(self) -> None:
        selected = media("/sdcard/upload/result.png", 10)
        xml = b"""<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="" clickable="true" enabled="true"
              bounds="[0,60][80,150]" />
            <node text="" clickable="true" enabled="true"
              bounds="[80,60][160,150]">
              <node text="00:10" clickable="false" enabled="true"
                bounds="[125,125][158,148]" />
            </node>
          </node>
        </hierarchy>"""
        target, method = MODULE.find_gallery_tile(xml, selected)
        self.assertIsNotNone(target)
        self.assertEqual((target.x, target.y), (40, 105))
        self.assertEqual(method, "image-no-duration")

    def test_gallery_video_never_falls_back_to_image(self) -> None:
        selected = media("/sdcard/upload/result.mp4", 10)
        xml = b"""<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="" clickable="true" enabled="true"
              bounds="[0,60][80,150]" />
          </node>
        </hierarchy>"""
        target, method = MODULE.find_gallery_tile(xml, selected)
        self.assertIsNone(target)
        self.assertEqual(method, "no-video-tile")

    def test_gallery_reports_previously_selected_wrong_tile(self) -> None:
        xml = b"""<hierarchy>
          <node package="com.facebook.katana" bounds="[0,0][240,400]">
            <node text="" clickable="true" enabled="true" selected="true"
              bounds="[0,60][80,150]" />
            <node text="" clickable="true" enabled="true"
              bounds="[80,60][160,150]">
              <node text="00:10" clickable="false" enabled="true"
                bounds="[125,125][158,148]" />
            </node>
          </node>
        </hierarchy>"""
        selected = MODULE.find_selected_gallery_tiles(xml)
        self.assertEqual([(item.x, item.y) for item in selected], [(40, 105)])


if __name__ == "__main__":
    unittest.main()
