# Brand assets

Canonical diffuse identity: the mark, its variants, and the color palette.
`tokens.json` is the source of truth for color — anything that renders a diffuse
surface should resolve from it rather than hardcoding hex values.

## Logo

`logo/mark.svg` is the primitive. It fills with `currentColor`, so it recolors
from CSS and is the right choice in product code:

```html
<span style="color: var(--acid)"><!-- inline mark.svg --></span>
```

Fixed-color variants, for contexts that cannot set `color`:

| File | Use |
|---|---|
| `logo/mark-black.svg` | `#000000` on transparent |
| `logo/mark-acid.svg` | `#EAFF49` on transparent — for placing on ink |
| `logo/mark-black-on-acid.svg` | avatar / social / favicon source |
| `logo/mark-acid-on-ink.svg` | inverse, on a dark field |

Each has a `-1024.png` export alongside for raster-only channels. Prefer SVG
everywhere else.

There is no wordmark or lockup yet — the mark is the whole identity today.

### Geometry

375×375 viewBox, four blades in 4-fold rotational symmetry around a square
negative-space center. The avatar composition places the 375-unit mark on a 512
canvas at offset 68.5, so the mark occupies 73.3% of the field.

The blades are four unique paths. The original Figma export carried seven, three
of which were the same geometry written in different path notation; the
duplicates double-composited the antialiased edges and rendered them heavy.

## Color

**Dark-only, and that is a constraint rather than a preference.** `#EAFF49` has a
relative luminance of 0.895 — nearly as bright as white:

| `#EAFF49` on… | Contrast | |
|---|---|---|
| ink `#0C0C11` | 17.6:1 | pass |
| graphite `#1B1A23` | 15.5:1 | pass |
| black `#000000` | 18.9:1 | pass |
| bone `#F2F1EA` | 1.02:1 | **fails — invisible** |
| white | 1.11:1 | **fails — invisible** |

It can never be a foreground on a light surface. Where it has to appear against
light it is a *field* with black on top, exactly as the avatar uses it.

| Role | Token | Hex |
|---|---|---|
| Canvas | `neutral.ink` | `#0C0C11` |
| Raised (low) | `neutral.surfaceSoft` | `#131219` |
| Raised | `neutral.graphite` | `#1B1A23` |
| Border | `neutral.border` | `#2C2A37` |
| Muted text | `neutral.fog` | `#A7A7A3` |
| Primary text | `neutral.bone` | `#F2F1EA` |
| **Accent** | `brand.acid` | **`#EAFF49`** |
| Logo ink | `brand.logoInk` | `#000000` |

One accent carries brand, primary action, and active status. At 17.6:1 on ink it
has the headroom for all three.

### Why the neutrals are violet, not grey

Acid sits at hue 66.9°, so its exact complement is 246.9°. A ground tinted toward
the complement makes the accent read as more saturated — the reason a tinted dark
outperforms flat black under a bright accent. The ramp is built on hue 248.5°,
which is Intuitum's iris `#806BFF`, landing 1.6° from that complement. So the
neutral is simultaneously the optimal ground for acid *and* the thread back to the
parent brand — diffuse inherits Intuitum through its neutral rather than its
accent.

Two honest limits on the effect. At canvas lightness there are too few 8-bit steps
to carry a precise hue: `#0C0C11` is R=12, G=12, B=17, which quantizes to 240°
rather than 248.5°. The raised step `#1B1A23` measures 246.7° — dead on. **The
tint lives in the surfaces, not the canvas.** And never tint the ground toward the
accent's own hue; a green-black mutes acid badly.

`brand.logoInk` is pure `#000000` while `neutral.ink` is `#0C0C11`. They differ by
intent: the raster avatar locks pure black, and the black mark is never placed on
the ink canvas (it would vanish) — the acid or bone variant is used there.

### Deliberately excluded

diffuse is a sibling sub-brand of Intuitum. It shares the neutral ramp and the
radius / space / motion scales, and departs on accent:

- `iris #806BFF` / `irisSoft #B7ACFF` — Intuitum's primary accent. Keeping it would ship purple actions beside a lime logo, i.e. two competing accents.
- `signal #C6FF5E` — Intuitum's accent-to-the-accent, specified there as sparse punctuation ("one disruptive signal", a rim light at ~15% of frame). diffuse's `#EAFF49` has taken that role *and* inverted it into a full-bleed surface, so the two would compete for one semantic slot — on top of sitting only 14° apart in hue at the same maximum green.
- `charcoal #242529`, `paper #DEDDD6` — carried zero usages.
- Light-theme neutrals — they power a light mode the accent cannot appear in.
