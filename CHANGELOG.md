# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-02-27

### Added
- **Latest Galleries** — quick access to newest galleries from main menu
- **Simple + Advanced dual mode** menu system
- **Batch download** with range selection (`1-50`, `3,5,7`, `1-10,15-20`, `all`)
- **Download ALL galleries** for an actor with one command
- **Batch from URLs** — paste multiple gallery URLs for bulk download
- **Category browsing** — Events, Exclusives, Movie Stills, Posters
- **Paginated lists** with `n`/`p` navigation for large result sets
- **Progress bars** with download speed and file size tracking
- **Settings menu** — configure download folder, threads, delay at runtime
- **Color-coded CLI** with ANSI colors (auto-disabled on non-TTY)
- **Gallery IDs** shown in listings for easy identification
- **Page inspection tool** for debugging HTML structure
- `setup.py` for pip-installable package with `ragalahari-dl` command

### Improved
- HD image extraction uses direct URL conversion (thumbnail `t` suffix removal) instead of visiting detail pages — much faster
- Gallery-only image filtering via `<div id="galdiv">` — no more downloading news/ads/banners
- Pagination detection using `pagingCell` and `otherPage` class selectors
- Lazy-loaded image support via `data-srcset` attribute
- Clean filenames with URL decoding
- Minimum file size validation (skips broken images < 5KB)
- Parallel detail page fetching with ThreadPoolExecutor

## [1.0.0] - 2026-02-27

### Added
- Initial release
- Actor search with alphabetical index navigation
- Gallery listing from actor profile pages
- Multi-page gallery support
- Parallel image downloads (5 threads)
- Skip existing files (resume support)
- Direct gallery URL and actor profile URL input
- Interactive CLI menu
