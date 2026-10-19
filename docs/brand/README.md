# Brand

Sissy Censor System. The video overlay's look lives in the `BRAND` block at
the top of [`src/censor_proxy/static/inject.js`](../../src/censor_proxy/static/inject.js);
the name shown on it comes from `video.brand_name`.

## Files

- `mark.svg`: the mark in petal, for dark grounds
- `mark-deep.svg`: the mark in petal deep, for light grounds
- `banner-dark.svg`, `banner-light.svg`: README banners (1280x320), switched
  by `prefers-color-scheme`
- `browser-icon.svg`: the "Sissy Browser" app icon (256x256): the mark on a
  plum ink tile with a soft petal glow. `install/browser.sh` uses it as is
  on Linux and rasterises it for macOS; `install/browser.ps1` redraws it
  with GDI+ for the Windows `.ico`, so keep the two in step

## Mark

A 3x3 mosaic: a pixelate censor softened into a blurred spot. The centre cell
is solid, the edges are at 60% and the corners at 25%. Works down to 16 px.

## Palette

| Name | Hex | Use |
| --- | --- | --- |
| Plum ink | `#16121A` | Ground: overlay card, dark banner; text on petal |
| Velvet | `#221B27` | Raised surface |
| Petal | `#FF6FAE` | Accent on dark: mark, primary button, progress |
| Petal deep | `#C8337A` | Accent on light grounds |
| Blush | `#F7F1F5` | Text on dark |
| Mauve | `#A898A6` | Secondary text on dark |
| Dusk | `#5E4F5C` | Secondary text on light |
| Line | `#4A3D52` | Outline of the secondary button |

## Type

- Bricolage Grotesque: display, wordmark (800) and body (400/600)
- IBM Plex Mono: labels and the overlay's brand line (500, uppercase,
  tracked)

Neither is fetched: the proxy makes no external requests, so the overlay and
the SVG banners fall back to system fonts unless these are installed.
