---
name: philippines-lottery-result-media
description: Fetch, compare, and monitor multiple Philippine lottery result sources, then create branded vertical PNG or MP4 media for PCSO 2D/EZ2, 3D/Swertres, 4D, and 6D games using shared logos and background music. Use when Hermes needs to preview or generate Philippine lottery result material, use the first source that publishes a valid draw, detect conflicting result sources, retry pending draws, or diagnose lottery media generation.
---

# Philippines Lottery Result Media

Create 1080×1920 Philippine lottery result images and short videos. Resolve `<skill-dir>` from this loaded `SKILL.md`; never assume a drive letter, username, output directory, or Hermes installation path.

Supported games:

- `2d`: PCSO 2D / EZ2, with 2PM, 5PM, and 9PM draws
- `3d`: PCSO 3D / Swertres, with 2PM, 5PM, and 9PM draws
- `4d`: PCSO 4D, with a 9PM draw on scheduled days
- `6d`: PCSO 6D, with a 9PM draw on scheduled days

Read [references/sources-games-and-assets.md](references/sources-games-and-assets.md) when adding a game, adding a data source, or diagnosing a parser or asset problem.

## Safety And Accuracy

- Treat every configured website as a third-party data source, not as PCSO itself.
- Compare sources for the same game, date, and draw whenever more than one source has published a result.
- If one source has a valid result while another is pending, missing, stale, or unavailable, allow generation and report `single-source`.
- If sources publish different numbers for the same game, date, and draw, stop by default. Do not generate until the user verifies the result or explicitly chooses a conflict override.
- Never invent or fill a missing number.
- Keep the source and verification disclaimer on generated media.
- Do not present generated material as betting advice or an official PCSO publication.
- Preview by default. Add `--execute` only when the user clearly requests file generation.
- Add `--force` only when the user explicitly requests regeneration.
- Do not upload or publish generated files unless the user separately asks.
- Use the bundled logos and audio only if the user has permission to use them.

## Shared Assets

All games use the same asset root:

```text
assets/
├── logos/
│   ├── 2d-lotto.webp
│   ├── 3d-lotto.webp
│   ├── 4d-lotto.webp
│   ├── 6d-lotto.webp
│   └── 88reels-logo-outline.png
└── audio/
    └── background.mp3
```

Game-specific logos stay under `assets/logos/`; shared brand logos and audio remain available to every game. A future game without its own logo uses a generated text badge.

## Entry Point

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" <arguments>
```

## Required Intent

Determine or ask for the lottery game before generation. Do not silently assume 2D from a generic request such as “生成菲律宾彩票素材”.

Map user wording as follows:

- “2D、EZ2” -> `--game 2d`
- “3D、Swertres” -> `--game 3d`
- “4D” -> `--game 4d`
- “6D、6 Digit” -> `--game 6d`
- “最新、哪个先开奖就用哪个” -> `--draw latest --sources auto`
- “按当前时间段” -> `--draw auto`
- “2点、5点、9点” -> `--draw 2pm|5pm|9pm`
- “只显示一个时段” -> `--layout single`
- “显示当天全部时段” -> `--layout all`
- “只生成图片” -> `--no-video`
- “电影感动画、完整动画” -> `--animation cinematic`
- “轻量动画、低性能模式” -> `--animation subtle`
- “不要动画” -> `--animation none`
- “保留以前生成的图片和视频” -> `--keep-previous`
- “预演、不要生成” -> omit `--execute`
- “立即生成、制作素材” -> add `--execute`
- “重试 N 次” -> `--retries N`
- “重新生成” -> add `--force` only after explicit instruction

## Multi-Source Rules

The default `--sources auto` concurrently reads:

1. `pcsoresults.org`
2. `lottopcso.com`

Selection order:

1. Discard invalid and pending values.
2. For `--draw latest`, choose the newest date and latest published draw.
3. For a specified draw, choose only that draw.
4. Compare all ready values for the selected game, date, and draw.
5. If values agree, select the faster completed source and report `confirmed`.
6. If only one source is ready, select it and report `single-source`.
7. If values conflict, stop with `Source conflict`.

Never automatically bypass a conflict. Use one of these only after the user verifies the intended source or accepts the risk:

```text
--conflict-policy pcsoresults
--conflict-policy lottopcso
--conflict-policy first
```

## Common Operations

### Check Dependencies And Shared Assets

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --check
```

### Preview Latest 2D Result

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 2d --draw latest --sources auto --json
```

### Generate Latest 2D Board

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 2d --draw latest --layout all --sources auto --execute --json
```

### Generate A 3D Result

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 3d --draw 5pm --layout single --sources auto --execute --json
```

### Generate 4D Or 6D

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 4d --draw latest --execute --json
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 6d --draw latest --execute --json
```

### Poll Multiple Sources Until A Result Appears

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 2d --draw 5pm --sources auto --retries 20 --retry-delay 30 --execute --json
```

Keep monitoring this one process. Do not start duplicate pollers or renderers when a terminal call yields.

### Diagnose Saved Source Pages

```text
python "<skill-dir>/scripts/philippines_lottery_result_media.py" --game 2d --sources pcsoresults,lottopcso --html-file "pcsoresults=<page-a.html>" --html-file "lottopcso=<page-b.html>" --json
```

This remains a dry run without `--execute`.

## Execution Procedure

1. Identify the game, draw, layout, media type, and whether execution is authorized.
2. Run `--sources auto` unless the user explicitly restricts the source.
3. Preview when execution is not explicit.
4. Report each source's date, draw, result, latency, parse error, and selected-source status.
5. Stop on a source conflict; request verification instead of guessing.
6. Generate only a valid, ready result.
7. For video rendering, allow at least 600 seconds of outer terminal time. A long render is not a failure while the process remains active.
8. Return exact PNG and MP4 paths. Return only the PNG when `--no-video` is used.
9. If the user later requests an R2 upload, pass the MP4 path to the separate `cloudflare-r2-video-upload` Skill.

## Latest Media Replacement

When `--draw latest --execute` is used, delete the previous generated PNG and MP4 for the same game and result date before writing the newest files.

- Delete only files matching `<date>-<game>-*.png` or `<date>-<game>-*.mp4` in the selected output directory.
- Do not delete unrelated images, videos, documents, other games, or other dates.
- Report every deleted path in terminal output and JSON under `removed_previous`.
- Use `--keep-previous` only when the user explicitly wants to retain older generated files.
- Do not clean files during a dry run.

## Defaults

- Game: `2d` for CLI backward compatibility; Hermes must still identify the user's intended game
- Draw: `latest`
- Sources: both sources concurrently
- Layout: all draws for 2D/3D; single result for 4D/6D
- Output: `Desktop/Philippines-Lottery-Result-Media/<game>/<date>/`
- Video: 10 seconds, 30 FPS, H.264/AAC
- Visual style: layered blue-purple background, glass panels, gradient/glow headings, illuminated number balls, and high-contrast white draw-time/number text with dark outlines
- Animation: `cinematic` by default, with camera push, moving light sweep, drifting particles, breathing light, vignette, and fade
- Latest replacement: remove prior generated PNG/MP4 for the same game and date unless `--keep-previous` is present
- Music: `assets/audio/background.mp3`
- Execution: dry run
- Conflict policy: stop

## Dependencies

Install into the Python environment Hermes uses:

```text
python -m pip install -r "<skill-dir>/scripts/requirements.txt"
```

`imageio-ffmpeg` supplies the FFmpeg binary used by the renderer.

## Environment Overrides

- `PHILIPPINES_LOTTERY_OUTPUT_DIR`
- `PHILIPPINES_LOTTERY_ARCHIVE`
- `PHILIPPINES_LOTTERY_ASSET_DIR`
- `PHILIPPINES_LOTTERY_BRAND_DOMAIN`

Keep machine-specific paths and credentials out of the public Skill.
