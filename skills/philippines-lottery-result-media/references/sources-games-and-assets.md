# Sources, Games, And Assets

## Source Registry

The script concurrently queries these independent publishers:

| Source key | Domain | Parser |
|---|---|---|
| `pcsoresults` | `https://www.pcsoresults.org/lotto-results/` | `.lr-card[data-game]` cards |
| `lottopcso` | Game-specific pages on `https://www.lottopcso.com/` | Result tables |

Both websites state that they are independent or unofficial publishers. Treat their values as informational and verify important results through official PCSO channels.

The default workflow allows the first valid source to be used when the other source is pending, stale, missing the game, or unavailable. It stops when two sources publish different numbers for the same game, date, and draw.

## Supported Games

Game definitions live in the `GAMES` registry inside:

```text
scripts/philippines_lottery_result_media.py
```

Each definition contains:

- command key
- display title
- `pcsoresults.org` card key
- expected number count
- draw schedule
- `lottopcso.com` page
- optional logo filename

Current keys are `2d`, `3d`, `4d`, and `6d`.

To add another game:

1. Add a `GameSpec`.
2. Add or extend parser fixtures.
3. Add number-count and source-selection tests.
4. Add an optional game logo under `assets/logos/`.
5. Update the supported-game list in `SKILL.md`.
6. Run a dry preview against all configured sources before generating media.

Never hardcode a current winning number in the parser or game registry.

## Adding A Source

Add a source only when its page can identify:

- game
- draw date
- draw time
- complete winning number sequence

Then:

1. Add the source key and URL mapping.
2. Implement a source-specific parser.
3. Return normalized `SourceResult` records.
4. Add saved-HTML fixtures for ready, pending, malformed, and conflicting results.
5. Include the source in concurrent collection and JSON reporting.
6. Preserve the default conflict-stop behavior.

Do not scrape authentication-protected pages or bypass access controls.

## Shared Asset Layout

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

All games resolve assets relative to the Skill directory. No asset path may depend on the original computer.

The supplied files do not grant trademark, music, or publication rights. Confirm authorization before public or commercial use.

Override the complete asset root with `PHILIPPINES_LOTTERY_ASSET_DIR`, or only the music with `--music`.

## Archive

The archive stores generated result keys only. It contains no credentials. Use `--force` only when regeneration is intentional.
