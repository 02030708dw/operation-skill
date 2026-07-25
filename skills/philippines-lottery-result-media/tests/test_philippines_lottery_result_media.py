import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "philippines_lottery_result_media.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC = importlib.util.spec_from_file_location("lottery_media", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class PhilippinesLotteryResultMediaTests(unittest.TestCase):
    def test_pcsoresults_parser_supports_all_digit_games(self):
        html = (FIXTURES / "pcsoresults-all-games.html").read_text(encoding="utf-8")
        expected = {
            "2d": ("26", "12"),
            "3d": ("1", "3", "8"),
            "4d": ("0", "4", "8", "4"),
            "6d": ("0", "3", "2", "6", "4", "2"),
        }
        for game, numbers in expected.items():
            with self.subTest(game=game):
                parsed = module.parse_pcsoresults(
                    html, module.GAMES[game], module.PCSORESULTS_URL
                )
                self.assertEqual(numbers, parsed[0].numbers)

    def test_lottopcso_parser_keeps_pending_draws(self):
        html = (FIXTURES / "lottopcso-2d.html").read_text(encoding="utf-8")
        parsed = module.parse_lottopcso(
            html, module.GAMES["2d"], module.GAMES["2d"].lottopcso_url
        )
        self.assertEqual(("26", "12"), parsed[0].numbers)
        self.assertFalse(parsed[1].ready)
        self.assertFalse(parsed[2].ready)

    def test_first_ready_source_wins_when_other_source_is_pending(self):
        ready = module.SourceResult(
            "2d", "2026-07-24", "2:00 PM", ("26", "12"),
            "pcsoresults", "https://source-a", 0.4
        )
        pending = module.SourceResult(
            "2d", "2026-07-24", "2:00 PM", (),
            "lottopcso", "https://source-b", 0.2
        )
        observations = (
            module.SourceObservation("pcsoresults", ready.url, (ready,), 0.4),
            module.SourceObservation("lottopcso", pending.url, (pending,), 0.2),
        )
        group = module.candidate_group(
            observations, module.GAMES["2d"], "latest"
        )
        selected, agreement, conflicts = module.choose_from_group(group, "stop")
        self.assertEqual("pcsoresults", selected.source)
        self.assertEqual("single-source", agreement)
        self.assertEqual((), conflicts)

    def test_conflicting_sources_stop_by_default(self):
        left = module.SourceResult(
            "2d", "2026-07-24", "2:00 PM", ("26", "12"),
            "pcsoresults", "https://source-a", 0.4
        )
        right = module.SourceResult(
            "2d", "2026-07-24", "2:00 PM", ("11", "22"),
            "lottopcso", "https://source-b", 0.2
        )
        with self.assertRaisesRegex(RuntimeError, "Source conflict"):
            module.choose_from_group([left, right], "stop")

    def test_render_supports_six_digit_game(self):
        selected = module.SourceResult(
            "6d", "2026-07-23", "9:00 PM",
            ("0", "3", "2", "6", "4", "2"),
            "pcsoresults", module.PCSORESULTS_URL, 0.3
        )
        observation = module.SourceObservation(
            "pcsoresults", module.PCSORESULTS_URL, (selected,), 0.3
        )
        selection = module.Selection(
            selected, (selected,), (observation,), "single-source", ()
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.png"
            module.render_selection(selection, output, "single", "example.test")
            with Image.open(output) as image:
                self.assertEqual((1080, 1920), image.size)

    def test_cinematic_animation_is_default(self):
        args = module.parse_args([])
        self.assertEqual("cinematic", args.animation)
        self.assertFalse(args.keep_previous)

    def test_each_supported_game_has_a_bundled_logo(self):
        for game, spec in module.GAMES.items():
            with self.subTest(game=game):
                self.assertIsNotNone(spec.logo_name)
                self.assertTrue((module.LOGO_DIR / spec.logo_name).is_file())

    def test_motion_overlays_are_full_height_and_transparent(self):
        with tempfile.TemporaryDirectory() as folder:
            shine_path, particles_path = module.create_motion_overlays(Path(folder))
            with Image.open(shine_path) as shine:
                self.assertEqual(1920, shine.height)
                self.assertEqual("RGBA", shine.mode)
            with Image.open(particles_path) as particles:
                self.assertEqual((1080, 1920), particles.size)
                self.assertEqual("RGBA", particles.mode)

    def test_number_orb_uses_high_contrast_white_text(self):
        image = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
        module.draw_number_orb(image, (180, 180), 220, "48", 0)
        pixels = list(image.crop((90, 90, 270, 270)).getdata())
        white_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 200 and red > 235 and green > 235 and blue > 235
        )
        dark_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 200 and red < 25 and green < 35 and blue < 55
        )
        self.assertGreater(white_pixels, 350)
        self.assertGreater(dark_pixels, 250)

    def test_draw_time_uses_high_contrast_white_text(self):
        image = Image.new("RGBA", (360, 180), "#f7c934")
        module.draw_crisp_text(
            image,
            (180, 90),
            "2PM",
            module.number_font(72),
            stroke_width=6,
        )
        pixels = list(image.getdata())
        white_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 200 and red > 235 and green > 235 and blue > 235
        )
        dark_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 200 and red < 25 and green < 35 and blue < 55
        )
        self.assertGreater(white_pixels, 500)
        self.assertGreater(dark_pixels, 500)

    def test_latest_cleanup_only_removes_generated_media_for_same_game_and_date(self):
        with tempfile.TemporaryDirectory() as folder:
            out_dir = Path(folder)
            removable = [
                out_dir / "2026-07-24-2d-2-00-PM-26-12.png",
                out_dir / "2026-07-24-2d-2-00-PM-26-12.mp4",
            ]
            preserved = [
                out_dir / "notes.png",
                out_dir / "2026-07-24-3d-2-00-PM-1-3-8.mp4",
                out_dir / "2026-07-23-2d-9-00-PM-17-22.png",
            ]
            for path in (*removable, *preserved):
                path.write_bytes(b"test")
            removed = module.cleanup_previous_latest_media(
                out_dir, "2d", "2026-07-24"
            )
            self.assertEqual(set(removable), set(removed))
            self.assertTrue(all(not path.exists() for path in removable))
            self.assertTrue(all(path.exists() for path in preserved))

    def test_offline_dry_run_writes_no_output_or_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = argparse.Namespace(
                game="2d",
                draw="latest",
                layout="auto",
                sources="pcsoresults",
                html_file=[
                    f"pcsoresults={FIXTURES / 'pcsoresults-all-games.html'}"
                ],
                conflict_policy="stop",
                request_timeout=2,
                output_dir=str(root / "out"),
                archive=str(root / "archive.json"),
                music=None,
                brand_domain="example.test",
                duration=1,
                fps=1,
                animation="none",
                force=False,
                retries=0,
                retry_delay=0,
                no_video=False,
                check=False,
                execute=False,
                json=False,
            )
            selection = module.select_result(args)
            image, video = module.output_paths(selection, args)
            self.assertFalse(image.exists())
            self.assertFalse(video.exists())
            self.assertFalse(Path(args.archive).exists())


if __name__ == "__main__":
    unittest.main()
