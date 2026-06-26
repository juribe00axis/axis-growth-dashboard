# Axis Growth — Style Guide

Visual rules for the Axis Growth dashboard. Keep it bold, playful, and
scoreboard-energetic. Apply these as CSS variables so every panel stays
consistent.

## Mood
Bold & playful, game-like. Dark scoreboard base so the lime hero color
pops. Rounded corners everywhere, generous spacing, big confident numbers. 

## Color tokens
:root {
  --bg:        #101014;  /* near-black base */
  --surface:   #1C1C24;  /* cards / panels */
  --surface-2: #262630;  /* raised elements, hover */
  --hero:      #C8FF01;  /* THE accent — key numbers, daily-leads bar, highlights */
  --text:      #F5F5F7;  /* primary text */
  --text-mute: #9A9AA5;  /* labels, secondary text */
  --won:       #C8FF01;  /* wins reuse the hero lime (growth = money) */
  --lost:      #FF5C5C;  /* lost/negative */
  --line:      #2E2E38;  /* gridlines, borders */
}

## Usage rules
- Hero lime (--hero) is for emphasis ONLY: the headline daily-leads
  number, key stat values, the main bar series. Don't flood the page with
  it — its power is scarcity. Most of the page is dark + white text.
- Big numbers are the stars. Stat values large and bold; labels small,
  muted, uppercase.
- Cards: --surface background, border-radius 18px, soft shadow, ample
  padding. Rounded = the playful feel.
- Charts: dark background, --line gridlines, --hero for the primary
  series, white/--text-mute for secondary.

## Typography
- Brand font (intended): Eurostile.
- Web fallback (use this now): "Saira", sans-serif — load from Google
  Fonts. Closest free match to Eurostile's squared letterforms.
- Headings/numbers: bold weight, slightly condensed feel.
- Body/labels: regular weight, uppercase + letter-spacing for labels to
  reinforce the scoreboard look.
- Font stack: font-family: "Saira", "Eurostile", system-ui, sans-serif;

## Layout feel
- Generous whitespace, clear hierarchy: hero metric biggest, funnel
  central, summary tiles smaller.
- Rounded buttons/tiles, subtle hover lift on interactive elements.

## Assets
- Logo: assets/logo.png — the Axis Growth logo.
- Place it in the dashboard header, top-left, at a comfortable size
  (~40–48px tall). Link the path relatively as assets/logo.png so it
  loads when axis-growth.html is opened from this folder.
- Give it breathing room; don't crowd it against the title text.
- If the logo has its own colors, let it sit on the dark --bg without
  recoloring it.
