#!/usr/bin/env python3
"""
Ragalahari Gallery Downloader v2.0
===================================
Search actors, browse galleries, batch download HD images.

Usage:
    python ragalahari_dl.py

Requirements:
    pip install requests beautifulsoup4
"""

import os
import re
import sys
import time
import json
import select
import threading
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────────────

BASE_URL = "https://www.ragalahari.com"
DOWNLOAD_DIR = "downloads"
MAX_WORKERS = 5
RETRY_COUNT = 3
DELAY_BETWEEN_PAGES = 0.5
VERSION = "2.0"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL,
}

session = requests.Session()
session.headers.update(HEADERS)


def load_config():
    """Load saved settings from config.json."""
    global DOWNLOAD_DIR, MAX_WORKERS, DELAY_BETWEEN_PAGES
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            DOWNLOAD_DIR = cfg.get('download_dir', DOWNLOAD_DIR)
            MAX_WORKERS = cfg.get('max_workers', MAX_WORKERS)
            DELAY_BETWEEN_PAGES = cfg.get('delay', DELAY_BETWEEN_PAGES)
        except (json.JSONDecodeError, IOError):
            pass  # use defaults if config is corrupted


def save_config():
    """Save current settings to config.json."""
    cfg = {
        'download_dir': DOWNLOAD_DIR,
        'max_workers': MAX_WORKERS,
        'delay': DELAY_BETWEEN_PAGES,
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except IOError:
        pass


# ─── Download Session (Save/Resume State) ────────────────────────────────────

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".download_session.json")


class DownloadSession:
    """Persists download state so interrupted downloads can be resumed later."""

    def __init__(self):
        self.active = False
        self.queue = []       # list of {gallery_url, gallery_name, actor_name, images: [...]}
        self.current_idx = 0  # which queue item we're on
        self.completed_files = set()  # filenames already downloaded in current gallery

    def add_to_queue(self, gallery_url, gallery_name, actor_name, images):
        """Add a gallery to the download queue."""
        self.queue.append({
            'gallery_url': gallery_url,
            'gallery_name': gallery_name,
            'actor_name': actor_name,
            'images': images,
            'done': False,
        })

    def mark_current_done(self):
        """Mark the current gallery as completed."""
        if self.current_idx < len(self.queue):
            self.queue[self.current_idx]['done'] = True
            self.current_idx += 1

    def get_pending(self):
        """Get list of galleries not yet completed."""
        return [g for g in self.queue if not g.get('done', False)]

    def save(self):
        """Save session state to disk."""
        if not self.queue:
            self.clear()
            return
        data = {
            'queue': self.queue,
            'current_idx': self.current_idx,
            'timestamp': datetime.now().isoformat(),
        }
        try:
            with open(SESSION_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    @staticmethod
    def load():
        """Load a saved session from disk. Returns DownloadSession or None."""
        if not os.path.exists(SESSION_FILE):
            return None
        try:
            with open(SESSION_FILE, 'r') as f:
                data = json.load(f)
            sess = DownloadSession()
            sess.queue = data.get('queue', [])
            sess.current_idx = data.get('current_idx', 0)
            pending = sess.get_pending()
            if not pending:
                sess.clear()
                return None
            sess.active = True
            sess._timestamp = data.get('timestamp', '')
            return sess
        except (json.JSONDecodeError, IOError, KeyError):
            return None

    def clear(self):
        """Remove the session file."""
        self.queue = []
        self.current_idx = 0
        self.active = False
        try:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        except IOError:
            pass


# Global session
dl_session = DownloadSession()


# ─── Pause / Resume Controller ───────────────────────────────────────────────

class PauseController:
    """Thread-safe pause/resume controller for downloads."""

    def __init__(self):
        self._resume_event = threading.Event()
        self._resume_event.set()  # start in "running" state
        self._paused = False
        self._stop_listener = False
        self._listener_thread = None
        self._total_paused_time = 0
        self._pause_start = None

    @property
    def is_paused(self):
        return self._paused

    def pause(self):
        """Pause all downloads."""
        self._paused = True
        self._pause_start = time.time()
        self._resume_event.clear()

    def resume(self):
        """Resume all downloads."""
        if self._pause_start:
            self._total_paused_time += time.time() - self._pause_start
            self._pause_start = None
        self._paused = False
        self._resume_event.set()

    def reset_timer(self):
        """Reset paused time tracker for a new download session."""
        self._total_paused_time = 0
        self._pause_start = None

    @property
    def total_paused_time(self):
        """Total seconds spent paused."""
        extra = 0
        if self._pause_start:
            extra = time.time() - self._pause_start
        return self._total_paused_time + extra

    def toggle(self):
        """Toggle between paused and running."""
        if self._paused:
            self.resume()
        else:
            self.pause()

    def wait_if_paused(self):
        """Block calling thread while paused. Call this before each download."""
        self._resume_event.wait()

    def start_listener(self):
        """Start background thread that listens for 'p' key to toggle pause."""
        self._stop_listener = False
        self._listener_thread = threading.Thread(target=self._listen_keys, daemon=True)
        self._listener_thread.start()

    def stop_listener(self):
        """Stop the key listener thread."""
        self._stop_listener = True
        if self._paused:
            self.resume()  # unblock any waiting threads

    def _listen_keys(self):
        """Listen for keypress in background. Works on both Unix and Windows."""
        if os.name == 'nt':
            self._listen_windows()
        else:
            self._listen_unix()

    def _listen_unix(self):
        """Unix key listener using termios for raw input."""
        import termios
        import tty
        old_settings = None
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            while not self._stop_listener:
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    ch = sys.stdin.read(1).lower()
                    if ch == 'p':
                        self.toggle()
                        if self._paused:
                            print(f"\n  {C.YELLOW}⏸  PAUSED{C.RESET} — press {C.BOLD}P{C.RESET} to resume\n")
                        else:
                            print(f"\n  {C.GREEN}▶  RESUMED{C.RESET}\n")
        except (Exception,):
            pass  # fallback: if terminal doesn't support raw mode, skip listener
        finally:
            if old_settings:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

    def _listen_windows(self):
        """Windows key listener using msvcrt."""
        try:
            import msvcrt
            while not self._stop_listener:
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                    if ch == 'p':
                        self.toggle()
                        if self._paused:
                            print(f"\n  {C.YELLOW}⏸  PAUSED{C.RESET} — press {C.BOLD}P{C.RESET} to resume\n")
                        else:
                            print(f"\n  {C.GREEN}▶  RESUMED{C.RESET}\n")
                time.sleep(0.2)
        except Exception:
            pass

# Global pause controller
pause_ctrl = PauseController()


# ─── Colors (ANSI) ──────────────────────────────────────────────────────────

class C:
    """ANSI color codes. Disabled on non-TTY or Windows without support."""
    _enabled = sys.stdout.isatty()
    RESET   = "\033[0m"   if _enabled else ""
    BOLD    = "\033[1m"    if _enabled else ""
    DIM     = "\033[2m"    if _enabled else ""
    GREEN   = "\033[32m"   if _enabled else ""
    YELLOW  = "\033[33m"   if _enabled else ""
    BLUE    = "\033[34m"   if _enabled else ""
    MAGENTA = "\033[35m"   if _enabled else ""
    CYAN    = "\033[36m"   if _enabled else ""
    RED     = "\033[31m"   if _enabled else ""
    WHITE   = "\033[97m"   if _enabled else ""


# ─── Utility ─────────────────────────────────────────────────────────────────

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def fetch(url, retries=RETRY_COUNT):
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  {C.RED}[ERROR]{C.RESET} Failed to fetch: {e}")
                return None


def get_soup(url):
    resp = fetch(url)
    if resp:
        return BeautifulSoup(resp.text, 'html.parser')
    return None


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def parse_range_selection(selection_str, max_val):
    """
    Parse a range/selection string into a list of indices.
    Supports: single numbers, ranges (1-10), comma-separated, 'all'
    Examples: "1-10", "3,5,7", "1-5,8,10-15", "all"
    Returns 0-based indices.
    """
    selection_str = selection_str.strip().lower()
    if selection_str in ('all', '*', 'a'):
        return list(range(max_val))

    indices = set()
    parts = selection_str.replace(' ', '').split(',')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                start = int(start)
                end = int(end)
                for i in range(start, end + 1):
                    if 1 <= i <= max_val:
                        indices.add(i - 1)
            except ValueError:
                continue
        else:
            try:
                val = int(part)
                if 1 <= val <= max_val:
                    indices.add(val - 1)
            except ValueError:
                continue
    return sorted(indices)


def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def progress_bar(current, total, width=30):
    filled = int(width * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (width - filled)
    pct = (current / total * 100) if total > 0 else 0
    return f"{bar} {pct:5.1f}%"


# ─── Search for actors ───────────────────────────────────────────────────────

def discover_letter_urls(soup, base_search_url):
    letter_links = {}
    if not soup:
        return letter_links
    for link in soup.find_all('a', href=True):
        text = link.get_text(strip=True).upper()
        href = link.get('href', '')
        if len(text) == 1 and text.isalpha():
            letter_links[text] = urljoin(base_search_url, href)
    return letter_links


def find_profile_links(soup, query_lower=None):
    results = []
    if not soup:
        return results
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        name = link.get_text(strip=True)
        is_profile = (
            '/stars/profile/' in href or
            '/star/' in href or
            re.search(r'/stars?/\d+/', href)
        )
        if is_profile and name and len(name) > 1:
            if name.upper() in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                continue
            if name.lower() in ('home', 'next', 'prev', 'back', 'search', 'login'):
                continue
            if query_lower is None or query_lower in name.lower():
                full_url = urljoin(BASE_URL, href)
                results.append({'name': name, 'url': full_url})
    return results


def search_actors(query):
    results = []
    query_lower = query.lower().strip()
    first_letter = query_lower[0].upper() if query_lower else 'A'

    search_url = f"{BASE_URL}/starzonesearch.aspx"
    print(f"  {C.DIM}Loading search index...{C.RESET}")
    soup = get_soup(search_url)

    letter_links = {}
    if soup:
        letter_links = discover_letter_urls(soup, search_url)

    letter_soup = None
    if first_letter in letter_links:
        letter_url = letter_links[first_letter]
        print(f"  {C.DIM}Browsing letter '{first_letter}'...{C.RESET}")
        letter_soup = get_soup(letter_url)
    else:
        letter_url_patterns = [
            f"{BASE_URL}/starzonesearch.aspx?letter={first_letter}",
            f"{BASE_URL}/starzonesearch.aspx?l={first_letter}",
            f"{BASE_URL}/starzonesearch.aspx?alpha={first_letter}",
            f"{BASE_URL}/starzone{first_letter.lower()}.aspx",
        ]
        for pattern_url in letter_url_patterns:
            letter_soup = get_soup(pattern_url)
            if letter_soup:
                test_results = find_profile_links(letter_soup, query_lower)
                if test_results:
                    results.extend(test_results)
                    break
                all_profiles = find_profile_links(letter_soup)
                if all_profiles:
                    break
            letter_soup = None

    if letter_soup and not results:
        results = find_profile_links(letter_soup, query_lower)
    if not results and soup:
        results = find_profile_links(soup, query_lower)

    # Aggressive fallback
    if not results and (letter_soup or soup):
        target_soup = letter_soup or soup
        for link in target_soup.find_all('a', href=True):
            href = link.get('href', '')
            name = link.get_text(strip=True)
            if name and query_lower in name.lower() and href.endswith('.aspx'):
                full_url = urljoin(BASE_URL, href)
                results.append({'name': name, 'url': full_url})

    # Deduplicate
    seen = set()
    unique = []
    for r in results:
        if r['url'] not in seen:
            seen.add(r['url'])
            unique.append(r)
    return unique


# ─── Get galleries ───────────────────────────────────────────────────────────

def extract_gallery_id(url):
    match = re.search(r'/(?:actress|actor|gallery|photos|starzone)/(\d+)/', url)
    return match.group(1) if match else ''


def get_galleries(actor_url):
    galleries = []
    soup = get_soup(actor_url)
    if not soup:
        return galleries

    gallery_patterns = ['/actress/', '/actor/', '/gallery/', '/photos/']

    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        if any(p in href for p in gallery_patterns) and href.endswith('.aspx'):
            name = link.get_text(strip=True)
            if not name:
                img = link.find('img')
                if img:
                    name = img.get('alt', '') or img.get('title', '')
            if not name:
                name = href.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()

            full_url = urljoin(BASE_URL, href)
            gal_id = extract_gallery_id(full_url)

            if full_url != actor_url and name:
                galleries.append({'name': name, 'url': full_url, 'id': gal_id})

    seen = set()
    unique = []
    for g in galleries:
        if g['url'] not in seen:
            seen.add(g['url'])
            unique.append(g)
    return unique


# ─── Image extraction ────────────────────────────────────────────────────────

def thumbnail_to_fullsize(thumb_url):
    return re.sub(r't(\.(jpg|jpeg|png|webp|gif))$', r'\1', thumb_url, flags=re.I)


def get_gallery_pages(gallery_url):
    soup = get_soup(gallery_url)
    if not soup:
        return [(gallery_url, None)]

    pages = [(gallery_url, soup)]
    page_links = set()

    paging_cell = soup.find('td', id='pagingCell')
    if paging_cell:
        for link in paging_cell.find_all('a', href=True):
            href = link.get('href', '')
            text = link.get_text(strip=True)
            full_url = urljoin(BASE_URL, href)
            if full_url != gallery_url:
                if text.isdigit() or 'next' in text.lower() or link.get('id') == 'linkNext':
                    page_links.add(full_url)
    else:
        for link in soup.find_all('a', class_='otherPage'):
            full_url = urljoin(BASE_URL, link['href'])
            if full_url != gallery_url:
                page_links.add(full_url)
        next_link = soup.find('a', id='linkNext')
        if next_link and next_link.get('href'):
            full_url = urljoin(BASE_URL, next_link['href'])
            if full_url != gallery_url:
                page_links.add(full_url)

    if not page_links:
        for link in soup.find_all('a', href=True):
            text = link.get_text(strip=True)
            if text.isdigit() and int(text) > 1:
                full_url = urljoin(BASE_URL, link['href'])
                if full_url != gallery_url:
                    page_links.add(full_url)

    for url in sorted(page_links):
        pages.append((url, None))
    return pages


def get_images_from_page(soup):
    images = []
    if not soup:
        return images

    galdiv = soup.find('div', id='galdiv')
    if galdiv:
        thumb_imgs = galdiv.find_all('img', class_=re.compile(r'thumbnail|lazyload', re.I))
        if not thumb_imgs:
            thumb_imgs = galdiv.find_all('img', src=True)
    else:
        thumb_imgs = soup.find_all('img', class_=re.compile(r'thumbnail|lazyload', re.I))

    seen = set()
    for img in thumb_imgs:
        thumb_url = img.get('data-srcset', '') or img.get('srcset', '') or img.get('src', '')
        if not thumb_url or 'galpreload' in thumb_url or 'preload' in thumb_url:
            thumb_url = img.get('data-srcset', '')
            if not thumb_url:
                continue
        if 'ragalahari.com' not in thumb_url:
            continue
        fullsize_url = thumbnail_to_fullsize(thumb_url)
        if fullsize_url not in seen:
            seen.add(fullsize_url)
            images.append(fullsize_url)

    if not images:
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            if not re.search(r'/image\d+\.aspx', href):
                continue
            img_tag = link.find('img')
            if img_tag:
                thumb_url = img_tag.get('data-srcset', '') or img_tag.get('src', '')
                if thumb_url and 'ragalahari.com' in thumb_url and 'galpreload' not in thumb_url:
                    fullsize_url = thumbnail_to_fullsize(thumb_url)
                    if fullsize_url not in seen:
                        seen.add(fullsize_url)
                        images.append(fullsize_url)
    return images


def get_all_gallery_images(gallery_url, quiet=False):
    if not quiet:
        print(f"\n  {C.DIM}Scanning gallery pages...{C.RESET}")
    pages = get_gallery_pages(gallery_url)
    if not quiet:
        print(f"  {C.CYAN}Found {len(pages)} page(s){C.RESET}")

    all_images = []
    seen_urls = set()

    for i, (page_url, page_soup) in enumerate(pages, 1):
        if page_soup is None:
            if not quiet:
                print(f"  {C.DIM}Scanning page {i}/{len(pages)}...{C.RESET}", end=" ")
            page_soup = get_soup(page_url)
            if not page_soup:
                if not quiet:
                    print(f"{C.RED}failed{C.RESET}")
                continue
        else:
            if not quiet:
                print(f"  {C.DIM}Scanning page {i}/{len(pages)}...{C.RESET}", end=" ")

        images = get_images_from_page(page_soup)
        new_images = [img for img in images if img not in seen_urls]
        seen_urls.update(new_images)
        all_images.extend(new_images)
        if not quiet:
            print(f"{C.GREEN}{len(new_images)} images{C.RESET}")

        if i < len(pages):
            time.sleep(DELAY_BETWEEN_PAGES)

    if not all_images and not quiet:
        print(f"  {C.YELLOW}No gallery images found.{C.RESET}")

    return all_images


# ─── Download ────────────────────────────────────────────────────────────────

def download_image(url, save_path):
    try:
        resp = session.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        content_length = resp.headers.get('content-length')
        if content_length and int(content_length) < 5000:
            return False, 0
        size = 0
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                size += len(chunk)
        if size < 5000:
            os.remove(save_path)
            return False, 0
        return True, size
    except Exception:
        if os.path.exists(save_path):
            os.remove(save_path)
        return False, 0


def download_gallery(images, gallery_name, actor_name, gallery_url=""):
    actor_dir = sanitize_filename(actor_name)
    gallery_dir = sanitize_filename(gallery_name)
    save_dir = os.path.join(DOWNLOAD_DIR, actor_dir, gallery_dir)
    os.makedirs(save_dir, exist_ok=True)

    total = len(images)
    print(f"\n  {C.BOLD}Downloading {total} images{C.RESET}")
    print(f"  {C.DIM}Location: {os.path.abspath(save_dir)}{C.RESET}")
    print(f"  {C.DIM}Threads:  {MAX_WORKERS}{C.RESET}")
    print(f"  {C.DIM}Press {C.YELLOW}P{C.DIM} to pause/resume  |  {C.YELLOW}Ctrl+C{C.DIM} to stop & save progress{C.RESET}\n")

    # Save this gallery to session so it can be resumed if interrupted
    if not dl_session.queue:
        dl_session.queue = [{
            'gallery_url': gallery_url,
            'gallery_name': gallery_name,
            'actor_name': actor_name,
            'images': images,
            'done': False,
        }]
        dl_session.current_idx = 0
        dl_session.save()

    stats = {'downloaded': 0, 'failed': 0, 'skipped': 0, 'bytes': 0}
    interrupted = False
    pause_ctrl.reset_timer()
    start_time = time.time()

    def do_download(idx_url):
        idx, url = idx_url
        # Wait here if paused
        pause_ctrl.wait_if_paused()

        parsed = urlparse(url)
        raw_filename = unquote(os.path.basename(parsed.path))

        if not raw_filename or raw_filename == '/' or '.' not in raw_filename:
            raw_filename = f"image_{idx:04d}.jpg"

        filename = sanitize_filename(raw_filename)
        if len(filename) > 150 or not re.search(r'\.(jpg|jpeg|png|webp|gif)$', filename, re.I):
            ext = os.path.splitext(raw_filename)[1] or '.jpg'
            filename = f"image_{idx:04d}{ext}"

        save_path = os.path.join(save_dir, filename)

        if os.path.exists(save_path):
            return 'skipped', filename, 0

        ok, size = download_image(url, save_path)
        return ('ok' if ok else 'failed'), filename, size

    # Start pause key listener
    pause_ctrl.start_listener()

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(do_download, (i, url)): url
                       for i, url in enumerate(images, 1)}

            for future in as_completed(futures):
                status, filename, size = future.result()
                done = stats['downloaded'] + stats['failed'] + stats['skipped'] + 1

                if status == 'ok':
                    stats['downloaded'] += 1
                    stats['bytes'] += size
                    icon = f"{C.GREEN}✓{C.RESET}"
                elif status == 'skipped':
                    stats['skipped'] += 1
                    icon = f"{C.YELLOW}○{C.RESET}"
                else:
                    stats['failed'] += 1
                    icon = f"{C.RED}✗{C.RESET}"

                bar = progress_bar(done, total)
                print(f"  {icon} [{done}/{total}] {bar} {filename}")

    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\n  {C.YELLOW}⏹  Download interrupted!{C.RESET}")
        print(f"  {C.DIM}Saving progress... you can resume later from the main menu.{C.RESET}")

    finally:
        # Always stop the listener when done
        pause_ctrl.stop_listener()

    elapsed = time.time() - start_time - pause_ctrl.total_paused_time

    print(f"\n  {C.BOLD}{'─' * 50}{C.RESET}")
    print(f"  {C.GREEN}✓ Downloaded:{C.RESET} {stats['downloaded']}")
    if stats['skipped']:
        print(f"  {C.YELLOW}○ Skipped:{C.RESET}    {stats['skipped']}")
    if stats['failed']:
        print(f"  {C.RED}✗ Failed:{C.RESET}     {stats['failed']}")
    print(f"  {C.CYAN}◷ Time:{C.RESET}       {elapsed:.1f}s")
    if pause_ctrl.total_paused_time > 0.5:
        print(f"  {C.DIM}  (paused for {pause_ctrl.total_paused_time:.1f}s){C.RESET}")
    if stats['bytes']:
        print(f"  {C.CYAN}◉ Size:{C.RESET}       {format_size(stats['bytes'])}")
    print(f"  {C.DIM}Saved to: {os.path.abspath(save_dir)}{C.RESET}")

    if interrupted:
        print(f"\n  {C.YELLOW}⚡ Progress saved!{C.RESET} Run the tool again and choose")
        print(f"     {C.BOLD}'R' (Resume Downloads){C.RESET} from the main menu.\n")

    return save_dir, interrupted


def download_batch_with_session(galleries, actor_name, fetch_images=True):
    """Download multiple galleries with session persistence.
    If interrupted, saves remaining galleries to session file for later resume.
    galleries: list of dicts with 'name', 'url' keys.
    """
    total_galleries = len(galleries)
    total_images = 0

    for i, gal in enumerate(galleries):
        gal_name = gal.get('name', 'Unknown')
        gal_url = gal.get('url', '')
        gal_actor = gal.get('actor_name', actor_name)

        print(f"\n  {C.BOLD}{C.CYAN}[{i + 1}/{total_galleries}]{C.RESET} {gal_name}")

        # Get images: either from saved list or by fetching
        images = gal.get('images')
        if not images and fetch_images:
            images = get_all_gallery_images(gal_url, quiet=True)

        if not images:
            print(f"  {C.YELLOW}No images found, skipping{C.RESET}")
            continue

        print(f"  {C.GREEN}{len(images)} images found{C.RESET}")
        total_images += len(images)

        # Save remaining galleries to session before downloading
        remaining = []
        for j in range(i, total_galleries):
            g = galleries[j]
            remaining.append({
                'gallery_url': g.get('url', g.get('gallery_url', '')),
                'gallery_name': g.get('name', g.get('gallery_name', '')),
                'actor_name': g.get('actor_name', actor_name),
                'images': g.get('images') if j > i else images,  # save fetched images for current
                'done': False,
            })
        dl_session.queue = remaining
        dl_session.current_idx = 0
        dl_session.save()

        save_dir, interrupted = download_gallery(images, gal_name, gal_actor, gallery_url=gal_url)

        if interrupted:
            # Update session: mark current as partially done (files on disk will be skipped)
            # Remaining galleries (including current) are still in session
            return total_images, True

        # Mark this one done — remove from session queue
        if dl_session.queue:
            dl_session.queue.pop(0)
            dl_session.save()

    # All done — clear session
    dl_session.clear()

    print(f"\n  {C.BOLD}{C.GREEN}Batch complete!{C.RESET} "
          f"{total_galleries} galleries, {total_images} total images")
    return total_images, False


def resume_downloads():
    """Check for saved session and resume incomplete downloads."""
    sess = DownloadSession.load()
    if not sess:
        print(f"\n  {C.DIM}No incomplete downloads found.{C.RESET}")
        return

    pending = sess.get_pending()
    ts = sess._timestamp[:19].replace('T', ' ') if hasattr(sess, '_timestamp') else '?'

    print(f"\n  {C.BOLD}{C.YELLOW}── RESUME DOWNLOADS ──{C.RESET}")
    print(f"  {C.DIM}Session from: {ts}{C.RESET}")
    print(f"  {C.BOLD}{len(pending)} gallery(s) to resume:{C.RESET}")
    print(f"  {C.DIM}{'─' * 50}{C.RESET}")

    for i, g in enumerate(pending, 1):
        name = g.get('gallery_name', 'Unknown')
        actor = g.get('actor_name', '')
        n_images = len(g.get('images', []))
        actor_str = f" {C.DIM}({actor}){C.RESET}" if actor else ""
        img_str = f" — {n_images} images" if n_images else ""
        print(f"  {C.CYAN}{i:3d}.{C.RESET} {name}{actor_str}{C.DIM}{img_str}{C.RESET}")

    print(f"  {C.DIM}{'─' * 50}{C.RESET}")
    print(f"  {C.DIM}Already-downloaded images will be auto-skipped.{C.RESET}")

    choice = get_input(f"\n  {C.WHITE}Resume all? (y/n) >{C.RESET} ", 'n')
    if not choice or choice.lower() not in ('y', 'yes'):
        discard = get_input(f"  {C.WHITE}Discard saved session? (y/n) >{C.RESET} ", 'n')
        if discard and discard.lower() in ('y', 'yes'):
            sess.clear()
            print(f"  {C.GREEN}Session cleared.{C.RESET}")
        return

    # Convert session queue to gallery list format for download_batch_with_session
    galleries_to_resume = []
    for g in pending:
        galleries_to_resume.append({
            'name': g.get('gallery_name', 'Unknown'),
            'url': g.get('gallery_url', ''),
            'actor_name': g.get('actor_name', 'Resumed'),
            'images': g.get('images'),
        })

    actor = pending[0].get('actor_name', 'Resumed') if pending else 'Resumed'
    download_batch_with_session(galleries_to_resume, actor, fetch_images=True)


# ─── UI Components ───────────────────────────────────────────────────────────

ASCII_ART = f"""{C.MAGENTA}
     ┌─┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─┐
     │ │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│ │
     ├─┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴─┤
     │  {C.YELLOW}     ___                                {C.MAGENTA}│
     │  {C.YELLOW}    (o o)    {C.WHITE}/\\    {C.CYAN}Search . Browse{C.MAGENTA}       │
     │  {C.YELLOW}    __|__   {C.WHITE}/  \\   {C.CYAN}Select . Download{C.MAGENTA}     │
     │  {C.YELLOW}   /     \\ {C.WHITE}/ HD \\  {C.CYAN}Batch  . Resume{C.MAGENTA}       │
     │  {C.YELLOW}  / [====] {C.WHITE}\\    /                   {C.MAGENTA}│
     │  {C.YELLOW}  \\       / {C.WHITE}\\  /   {C.GREEN}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{C.MAGENTA}   │
     │  {C.YELLOW}   \\_____/   {C.WHITE}\\/    {C.GREEN}▓ {C.WHITE}RAGALAHARI{C.GREEN}  ▓{C.MAGENTA}   │
     │  {C.YELLOW}    |   |         {C.GREEN}▓ {C.WHITE}  GALLERY  {C.GREEN} ▓{C.MAGENTA}   │
     │  {C.YELLOW}   /|   |\\        {C.GREEN}▓ {C.WHITE} DOWNLOAD {C.GREEN} ▓{C.MAGENTA}   │
     │  {C.YELLOW}  / |   | \\       {C.GREEN}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{C.MAGENTA}   │
     │  {C.YELLOW} /__|   |__\\                         {C.MAGENTA}│
     ├─┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─┤
     │ │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│  │▓▓│ │
     └─┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴─┘
{C.RESET}"""

BANNER = f"""
{C.CYAN}╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   {C.BOLD}{C.WHITE}RAGALAHARI  GALLERY  DOWNLOADER{C.RESET}{C.CYAN}                v{VERSION}   ║
║                                                          ║
║   {C.DIM}Bulk HD Photo Downloader  •  ragalahari.com{C.RESET}{C.CYAN}            ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝{C.RESET}
"""

def print_banner():
    print(ASCII_ART)
    print(BANNER)


def get_input(prompt, default=None):
    try:
        val = input(prompt).strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        return None


def display_list(items, label, show_id=False, page_size=30):
    """Display a paginated list of items."""
    total = len(items)
    if total == 0:
        print(f"\n  {C.YELLOW}No {label}s found.{C.RESET}")
        return

    page = 0
    total_pages = (total - 1) // page_size + 1

    while True:
        start = page * page_size
        end = min(start + page_size, total)

        print(f"\n  {C.BOLD}Found {total} {label}(s){C.RESET}", end="")
        if total_pages > 1:
            print(f"  {C.DIM}(page {page + 1}/{total_pages}){C.RESET}")
        else:
            print()
        print(f"  {C.DIM}{'─' * 55}{C.RESET}")

        for i in range(start, end):
            item = items[i]
            num = f"{i + 1:>4d}"
            gal_id = item.get('id', '')
            id_str = f" {C.DIM}({gal_id}){C.RESET}" if (show_id and gal_id) else ""
            print(f"  {C.CYAN}{num}{C.RESET}  {item['name']}{id_str}")

        print(f"  {C.DIM}{'─' * 55}{C.RESET}")

        nav_hints = []
        if total_pages > 1:
            if page > 0:
                nav_hints.append("p=prev page")
            if page < total_pages - 1:
                nav_hints.append("n=next page")
        nav_hints.append("0=back")
        print(f"  {C.DIM}{' | '.join(nav_hints)}{C.RESET}")

        return total_pages > 1  # returns whether pagination is active


def select_single(items, label, show_id=False, page_size=30):
    """Select a single item from a list."""
    if not items:
        print(f"\n  {C.YELLOW}No {label}s found.{C.RESET}")
        return None

    page = 0
    total_pages = (len(items) - 1) // page_size + 1

    while True:
        start = page * page_size
        end = min(start + page_size, len(items))

        print(f"\n  {C.BOLD}Found {len(items)} {label}(s){C.RESET}", end="")
        if total_pages > 1:
            print(f"  {C.DIM}(page {page + 1}/{total_pages}){C.RESET}")
        else:
            print()
        print(f"  {C.DIM}{'─' * 55}{C.RESET}")

        for i in range(start, end):
            item = items[i]
            num = f"{i + 1:>4d}"
            gal_id = item.get('id', '')
            id_str = f" {C.DIM}({gal_id}){C.RESET}" if (show_id and gal_id) else ""
            print(f"  {C.CYAN}{num}{C.RESET}  {item['name']}{id_str}")

        print(f"  {C.DIM}{'─' * 55}{C.RESET}")

        nav_hints = []
        if total_pages > 1:
            if page > 0:
                nav_hints.append("p=prev")
            if page < total_pages - 1:
                nav_hints.append("n=next")
        nav_hints.append("0=back")
        print(f"  {C.DIM}{' | '.join(nav_hints)}{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}Select {label} >{C.RESET} ")
        if choice is None or choice == '0':
            return None
        if choice.lower() == 'n' and page < total_pages - 1:
            page += 1
            continue
        if choice.lower() == 'p' and page > 0:
            page -= 1
            continue

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
            print(f"  {C.RED}Invalid number. Range: 1-{len(items)}{C.RESET}")
        except ValueError:
            print(f"  {C.RED}Enter a number, 'n', 'p', or '0'{C.RESET}")


def select_multiple(items, label, show_id=False, page_size=30):
    """Select multiple items using range syntax."""
    if not items:
        print(f"\n  {C.YELLOW}No {label}s found.{C.RESET}")
        return []

    page = 0
    total_pages = (len(items) - 1) // page_size + 1

    while True:
        start = page * page_size
        end = min(start + page_size, len(items))

        print(f"\n  {C.BOLD}Found {len(items)} {label}(s){C.RESET}", end="")
        if total_pages > 1:
            print(f"  {C.DIM}(page {page + 1}/{total_pages}){C.RESET}")
        else:
            print()
        print(f"  {C.DIM}{'─' * 55}{C.RESET}")

        for i in range(start, end):
            item = items[i]
            num = f"{i + 1:>4d}"
            gal_id = item.get('id', '')
            id_str = f" {C.DIM}({gal_id}){C.RESET}" if (show_id and gal_id) else ""
            print(f"  {C.CYAN}{num}{C.RESET}  {item['name']}{id_str}")

        print(f"  {C.DIM}{'─' * 55}{C.RESET}")
        print(f"  {C.DIM}Format: 1-10 | 3,5,7 | 1-5,8,10-15 | all{C.RESET}")

        nav_hints = []
        if total_pages > 1:
            if page > 0:
                nav_hints.append("p=prev")
            if page < total_pages - 1:
                nav_hints.append("n=next")
        nav_hints.append("0=back")
        print(f"  {C.DIM}{' | '.join(nav_hints)}{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}Select {label}(s) >{C.RESET} ")
        if choice is None or choice == '0':
            return []
        if choice.lower() == 'n' and page < total_pages - 1:
            page += 1
            continue
        if choice.lower() == 'p' and page > 0:
            page -= 1
            continue

        indices = parse_range_selection(choice, len(items))
        if indices:
            selected = [items[i] for i in indices]
            print(f"\n  {C.GREEN}Selected {len(selected)} {label}(s){C.RESET}")
            return selected
        else:
            print(f"  {C.RED}Invalid selection. Try: 1-10, 3,5,7, or all{C.RESET}")


# ─── Simple Mode ─────────────────────────────────────────────────────────────

def simple_mode():
    while True:
        print(f"\n  {C.BOLD}{C.CYAN}── SIMPLE MODE ──{C.RESET}")
        print(f"  {C.WHITE}1{C.RESET}  Search & Download")
        print(f"  {C.WHITE}2{C.RESET}  Paste Gallery URL")
        print(f"  {C.WHITE}3{C.RESET}  Paste Actor Profile URL")
        print(f"  {C.DIM}0  Back to main menu{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}>{C.RESET} ")
        if choice is None or choice == '0':
            return

        if choice == '1':
            query = get_input(f"\n  {C.WHITE}Actor/Actress name >{C.RESET} ")
            if not query:
                continue

            print(f"\n  {C.DIM}Searching for '{query}'...{C.RESET}")
            actors = search_actors(query)
            actor = select_single(actors, "actor", show_id=True)
            if not actor:
                continue

            print(f"\n  {C.DIM}Loading galleries for {actor['name']}...{C.RESET}")
            galleries = get_galleries(actor['url'])
            if not galleries:
                print(f"  {C.YELLOW}No galleries found.{C.RESET}")
                continue

            while True:
                gallery = select_single(galleries, "gallery", show_id=True)
                if not gallery:
                    break

                images = get_all_gallery_images(gallery['url'])
                if not images:
                    continue

                print(f"\n  {C.GREEN}{C.BOLD}{len(images)} HD images ready{C.RESET}")
                confirm = get_input(f"  {C.WHITE}Download? (y/n) >{C.RESET} ", 'n')
                if confirm and confirm.lower() in ('y', 'yes'):
                    _, was_interrupted = download_gallery(images, gallery['name'], actor['name'], gallery_url=gallery['url'])
                    if not was_interrupted:
                        dl_session.clear()

                again = get_input(f"\n  {C.WHITE}Another gallery? (y/n) >{C.RESET} ", 'n')
                if not again or again.lower() not in ('y', 'yes'):
                    break

        elif choice == '2':
            url = get_input(f"\n  {C.WHITE}Gallery URL >{C.RESET} ")
            if not url:
                continue
            images = get_all_gallery_images(url)
            if not images:
                continue
            gallery_name = url.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()
            print(f"\n  {C.GREEN}{C.BOLD}{len(images)} HD images ready{C.RESET}")
            confirm = get_input(f"  {C.WHITE}Download? (y/n) >{C.RESET} ", 'n')
            if confirm and confirm.lower() in ('y', 'yes'):
                _, was_interrupted = download_gallery(images, gallery_name, "Direct", gallery_url=url)
                if not was_interrupted:
                    dl_session.clear()

        elif choice == '3':
            url = get_input(f"\n  {C.WHITE}Actor profile URL >{C.RESET} ")
            if not url:
                continue
            actor_name = url.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()
            print(f"\n  {C.DIM}Loading galleries for {actor_name}...{C.RESET}")
            galleries = get_galleries(url)
            if not galleries:
                print(f"  {C.YELLOW}No galleries found.{C.RESET}")
                continue

            while True:
                gallery = select_single(galleries, "gallery", show_id=True)
                if not gallery:
                    break
                images = get_all_gallery_images(gallery['url'])
                if not images:
                    continue
                print(f"\n  {C.GREEN}{C.BOLD}{len(images)} HD images ready{C.RESET}")
                confirm = get_input(f"  {C.WHITE}Download? (y/n) >{C.RESET} ", 'n')
                if confirm and confirm.lower() in ('y', 'yes'):
                    _, was_interrupted = download_gallery(images, gallery['name'], actor_name, gallery_url=gallery['url'])
                    if not was_interrupted:
                        dl_session.clear()
                again = get_input(f"\n  {C.WHITE}Another gallery? (y/n) >{C.RESET} ", 'n')
                if not again or again.lower() not in ('y', 'yes'):
                    break


# ─── Advanced Mode ───────────────────────────────────────────────────────────

def advanced_mode():
    while True:
        print(f"\n  {C.BOLD}{C.MAGENTA}── ADVANCED MODE ──{C.RESET}")
        print(f"  {C.WHITE}1{C.RESET}  Batch Download Galleries {C.DIM}(select multiple with ranges){C.RESET}")
        print(f"  {C.WHITE}2{C.RESET}  Download ALL Galleries for an Actor")
        print(f"  {C.WHITE}3{C.RESET}  Batch from Gallery URLs {C.DIM}(paste multiple URLs){C.RESET}")
        print(f"  {C.WHITE}4{C.RESET}  Browse by Category {C.DIM}(latest, events, photoshoots){C.RESET}")
        print(f"  {C.WHITE}5{C.RESET}  Inspect Page {C.DIM}(debug HTML structure){C.RESET}")
        print(f"  {C.WHITE}6{C.RESET}  Settings")
        print(f"  {C.DIM}0  Back to main menu{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}>{C.RESET} ")
        if choice is None or choice == '0':
            return

        if choice == '1':
            # Batch download: search actor, select multiple galleries
            query = get_input(f"\n  {C.WHITE}Actor/Actress name >{C.RESET} ")
            if not query:
                continue

            print(f"\n  {C.DIM}Searching for '{query}'...{C.RESET}")
            actors = search_actors(query)
            actor = select_single(actors, "actor", show_id=True)
            if not actor:
                continue

            print(f"\n  {C.DIM}Loading galleries for {actor['name']}...{C.RESET}")
            galleries = get_galleries(actor['url'])
            if not galleries:
                print(f"  {C.YELLOW}No galleries found.{C.RESET}")
                continue

            selected = select_multiple(galleries, "gallery", show_id=True)
            if not selected:
                continue

            # Confirm batch download
            print(f"\n  {C.BOLD}Batch Download Plan:{C.RESET}")
            for i, gal in enumerate(selected, 1):
                print(f"  {C.CYAN}{i:3d}.{C.RESET} {gal['name']}")
            print()

            confirm = get_input(f"  {C.WHITE}Start batch download? (y/n) >{C.RESET} ", 'n')
            if not confirm or confirm.lower() not in ('y', 'yes'):
                continue

            download_batch_with_session(selected, actor['name'])

        elif choice == '2':
            # Download ALL galleries for an actor
            query = get_input(f"\n  {C.WHITE}Actor/Actress name >{C.RESET} ")
            if not query:
                continue

            print(f"\n  {C.DIM}Searching for '{query}'...{C.RESET}")
            actors = search_actors(query)
            actor = select_single(actors, "actor", show_id=True)
            if not actor:
                continue

            print(f"\n  {C.DIM}Loading ALL galleries for {actor['name']}...{C.RESET}")
            galleries = get_galleries(actor['url'])
            if not galleries:
                print(f"  {C.YELLOW}No galleries found.{C.RESET}")
                continue

            print(f"\n  {C.BOLD}{C.YELLOW}WARNING:{C.RESET} This will download ALL "
                  f"{C.BOLD}{len(galleries)}{C.RESET} galleries!")
            confirm = get_input(f"  {C.WHITE}Are you sure? (yes/no) >{C.RESET} ", 'no')
            if confirm != 'yes':
                print(f"  {C.DIM}Cancelled.{C.RESET}")
                continue

            download_batch_with_session(galleries, actor['name'])

        elif choice == '3':
            # Batch from pasted gallery URLs
            print(f"\n  {C.DIM}Paste gallery URLs, one per line. Type 'done' when finished:{C.RESET}")
            urls = []
            while True:
                line = get_input(f"  {C.WHITE}URL >{C.RESET} ")
                if line is None or line.lower() == 'done':
                    break
                if line.startswith('http'):
                    urls.append(line)

            if not urls:
                continue

            print(f"\n  {C.BOLD}Downloading {len(urls)} galleries...{C.RESET}")
            url_galleries = []
            for url in urls:
                gallery_name = url.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()
                url_galleries.append({'name': gallery_name, 'url': url})
            download_batch_with_session(url_galleries, "Batch")

        elif choice == '4':
            # Browse by category
            print(f"\n  {C.BOLD}Browse Categories:{C.RESET}")
            categories = [
                {'name': 'Latest Galleries',   'url': f'{BASE_URL}/starzone.aspx'},
                {'name': 'Actress Galleries',   'url': f'{BASE_URL}/actresslist.aspx'},
                {'name': 'Events & Functions',  'url': f'{BASE_URL}/functions.aspx'},
                {'name': 'Exclusive Shoots',    'url': f'{BASE_URL}/exclusives.aspx'},
                {'name': 'Movie Stills',        'url': f'{BASE_URL}/moviestills.aspx'},
                {'name': 'Movie Posters',       'url': f'{BASE_URL}/posters.aspx'},
            ]
            cat = select_single(categories, "category")
            if not cat:
                continue

            print(f"\n  {C.DIM}Loading {cat['name']}...{C.RESET}")
            soup = get_soup(cat['url'])
            if not soup:
                continue

            # Extract galleries from category page
            galleries = []
            gallery_patterns = ['/actress/', '/actor/', '/gallery/', '/photos/',
                               '/functions/', '/exclusives/', '/moviestills/', '/posters/']
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                if any(p in href for p in gallery_patterns) and href.endswith('.aspx'):
                    name = link.get_text(strip=True)
                    if not name:
                        img = link.find('img')
                        if img:
                            name = img.get('alt', '') or img.get('title', '')
                    if not name:
                        name = href.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()
                    if name and len(name) > 3:
                        full_url = urljoin(BASE_URL, href)
                        gal_id = extract_gallery_id(full_url)
                        galleries.append({'name': name, 'url': full_url, 'id': gal_id})

            # Deduplicate
            seen = set()
            unique_gals = []
            for g in galleries:
                if g['url'] not in seen:
                    seen.add(g['url'])
                    unique_gals.append(g)

            if not unique_gals:
                print(f"  {C.YELLOW}No galleries found on this page.{C.RESET}")
                continue

            selected = select_multiple(unique_gals, "gallery", show_id=True)
            if not selected:
                continue

            download_batch_with_session(selected, "Category")

        elif choice == '5':
            # Inspect page
            url = get_input(f"\n  {C.WHITE}URL to inspect >{C.RESET} ")
            if not url:
                url = f"{BASE_URL}/starzonesearch.aspx"

            print(f"\n  {C.DIM}Fetching {url}...{C.RESET}")
            soup = get_soup(url)
            if not soup:
                print(f"  {C.RED}Failed to fetch page.{C.RESET}")
                continue

            title = soup.find('title')
            print(f"\n  {C.BOLD}Title:{C.RESET} {title.get_text(strip=True) if title else 'N/A'}")

            all_links = soup.find_all('a', href=True)
            print(f"  {C.BOLD}Total links:{C.RESET} {len(all_links)}")

            cats = {
                'Profile links': [a for a in all_links if '/stars/profile/' in a.get('href', '')],
                'Gallery links': [a for a in all_links if '/actress/' in a.get('href', '') or '/actor/' in a.get('href', '')],
                'Letter links':  [a for a in all_links if len(a.get_text(strip=True)) == 1 and a.get_text(strip=True).isalpha()],
            }

            for cat_name, links in cats.items():
                if links:
                    print(f"\n  {C.BOLD}{cat_name} ({len(links)}):{C.RESET}")
                    for a in links[:15]:
                        text = a.get_text(strip=True)[:50]
                        href = a['href'][:70]
                        print(f"    {C.CYAN}{text:50s}{C.RESET} {C.DIM}{href}{C.RESET}")
                    if len(links) > 15:
                        print(f"    {C.DIM}... and {len(links) - 15} more{C.RESET}")

            galdiv = soup.find('div', id='galdiv')
            if galdiv:
                thumbs = galdiv.find_all('img', class_=re.compile(r'thumbnail|lazyload', re.I))
                print(f"\n  {C.BOLD}Gallery images (galdiv): {len(thumbs)}{C.RESET}")
                for img in thumbs[:5]:
                    src = img.get('data-srcset', '') or img.get('src', '')
                    print(f"    {C.DIM}{src[:80]}{C.RESET}")

        elif choice == '6':
            # Settings
            global DOWNLOAD_DIR, MAX_WORKERS, DELAY_BETWEEN_PAGES
            print(f"\n  {C.BOLD}Current Settings:{C.RESET}")
            print(f"  {C.CYAN}1{C.RESET} Download folder:  {C.WHITE}{os.path.abspath(DOWNLOAD_DIR)}{C.RESET}")
            print(f"  {C.CYAN}2{C.RESET} Parallel threads: {C.WHITE}{MAX_WORKERS}{C.RESET}")
            print(f"  {C.CYAN}3{C.RESET} Page delay:       {C.WHITE}{DELAY_BETWEEN_PAGES}s{C.RESET}")
            print(f"  {C.CYAN}4{C.RESET} Reset to defaults")
            print(f"  {C.DIM}Saved to: {CONFIG_FILE}{C.RESET}")
            print(f"  {C.DIM}0  Back{C.RESET}")

            setting = get_input(f"\n  {C.WHITE}Change setting >{C.RESET} ")
            if setting == '1':
                new_dir = get_input(f"  {C.WHITE}New download folder >{C.RESET} ")
                if new_dir:
                    DOWNLOAD_DIR = new_dir
                    save_config()
                    print(f"  {C.GREEN}Saved!{C.RESET}")
            elif setting == '2':
                new_workers = get_input(f"  {C.WHITE}Threads (1-20) >{C.RESET} ")
                if new_workers and new_workers.isdigit():
                    MAX_WORKERS = max(1, min(20, int(new_workers)))
                    save_config()
                    print(f"  {C.GREEN}Saved! Using {MAX_WORKERS} threads{C.RESET}")
            elif setting == '3':
                new_delay = get_input(f"  {C.WHITE}Delay in seconds >{C.RESET} ")
                if new_delay:
                    try:
                        DELAY_BETWEEN_PAGES = max(0, float(new_delay))
                        save_config()
                        print(f"  {C.GREEN}Saved! Delay: {DELAY_BETWEEN_PAGES}s{C.RESET}")
                    except ValueError:
                        pass
            elif setting == '4':
                DOWNLOAD_DIR = "downloads"
                MAX_WORKERS = 5
                DELAY_BETWEEN_PAGES = 0.5
                save_config()
                print(f"  {C.GREEN}Reset to defaults and saved!{C.RESET}")


# ─── Latest Galleries ────────────────────────────────────────────────────────

def extract_galleries_from_page(soup):
    """Extract gallery items from a starzone/category page."""
    galleries = []
    if not soup:
        return galleries

    # Look for gallery links — these are <a> tags containing thumbnail <img> tags
    # that link to /actress/, /actor/, /functions/ etc.
    gallery_patterns = ['/actress/', '/actor/', '/gallery/', '/photos/',
                       '/functions/', '/exclusives/', '/moviestills/', '/posters/']

    seen = set()
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        if not any(p in href for p in gallery_patterns):
            continue
        if not href.endswith('.aspx'):
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url in seen:
            continue

        # Get name from text, img alt, or URL
        name = link.get_text(strip=True)
        if not name:
            img = link.find('img')
            if img:
                name = img.get('alt', '') or img.get('title', '')
        if not name:
            name = href.split('/')[-1].replace('.aspx', '').replace('-', ' ').title()

        if name and len(name) > 3:
            seen.add(full_url)
            gal_id = extract_gallery_id(full_url)
            galleries.append({'name': name, 'url': full_url, 'id': gal_id})

    return galleries


def latest_galleries():
    """Browse and download from latest galleries on the homepage."""
    print(f"\n  {C.BOLD}{C.YELLOW}── LATEST GALLERIES ──{C.RESET}")
    print(f"  {C.DIM}Loading latest from ragalahari.com...{C.RESET}")

    soup = get_soup(f"{BASE_URL}/starzone.aspx")
    if not soup:
        print(f"  {C.RED}Failed to load page.{C.RESET}")
        return

    galleries = extract_galleries_from_page(soup)

    if not galleries:
        print(f"  {C.YELLOW}No galleries found.{C.RESET}")
        return

    print(f"  {C.GREEN}Loaded {len(galleries)} latest galleries{C.RESET}")

    while True:
        print(f"\n  {C.BOLD}What would you like to do?{C.RESET}")
        print(f"  {C.WHITE}1{C.RESET}  Browse & pick one gallery")
        print(f"  {C.WHITE}2{C.RESET}  Select multiple galleries {C.DIM}(range: 1-10, 3,5,7, all){C.RESET}")
        print(f"  {C.DIM}0  Back{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}>{C.RESET} ")
        if choice is None or choice == '0':
            return

        if choice == '1':
            gallery = select_single(galleries, "gallery", show_id=True)
            if not gallery:
                continue
            images = get_all_gallery_images(gallery['url'])
            if not images:
                continue
            print(f"\n  {C.GREEN}{C.BOLD}{len(images)} HD images ready{C.RESET}")
            confirm = get_input(f"  {C.WHITE}Download? (y/n) >{C.RESET} ", 'n')
            if confirm and confirm.lower() in ('y', 'yes'):
                _, was_interrupted = download_gallery(images, gallery['name'], "Latest", gallery_url=gallery['url'])
                if not was_interrupted:
                    dl_session.clear()

        elif choice == '2':
            selected = select_multiple(galleries, "gallery", show_id=True)
            if not selected:
                continue
            print(f"\n  {C.BOLD}Batch Download: {len(selected)} galleries{C.RESET}")
            confirm = get_input(f"  {C.WHITE}Start? (y/n) >{C.RESET} ", 'n')
            if not confirm or confirm.lower() not in ('y', 'yes'):
                continue

            download_batch_with_session(selected, "Latest")


# ─── Main Menu ───────────────────────────────────────────────────────────────

def main():
    load_config()
    clear_screen()
    print_banner()

    # Check for incomplete downloads on startup
    saved_session = DownloadSession.load()
    if saved_session:
        pending = saved_session.get_pending()
        n = len(pending)
        print(f"  {C.YELLOW}{C.BOLD}⚡ {n} incomplete download(s) found!{C.RESET}")
        print(f"  {C.DIM}Select 'Resume Downloads' below to continue.{C.RESET}")

    while True:
        # Check if there's a session to resume
        has_session = os.path.exists(SESSION_FILE)

        print(f"\n  {C.BOLD}Main Menu{C.RESET}")
        print(f"  {C.DIM}{'─' * 45}{C.RESET}")
        if has_session:
            print(f"  {C.YELLOW}{C.BOLD}R{C.RESET}  {C.YELLOW}Resume Downloads{C.RESET}   {C.DIM}Continue where you left off{C.RESET}")
        print(f"  {C.YELLOW}1{C.RESET}  Latest Galleries  {C.DIM}New & trending on the site{C.RESET}")
        print(f"  {C.GREEN}2{C.RESET}  Simple Mode       {C.DIM}Search, select, download{C.RESET}")
        print(f"  {C.MAGENTA}3{C.RESET}  Advanced Mode     {C.DIM}Batch, bulk, categories{C.RESET}")
        print(f"  {C.DIM}0  Exit{C.RESET}")

        choice = get_input(f"\n  {C.WHITE}>{C.RESET} ")
        if choice is None or choice == '0':
            print(f"\n  {C.DIM}Goodbye!{C.RESET}\n")
            break
        elif choice and choice.lower() == 'r' and has_session:
            resume_downloads()
        elif choice == '1':
            latest_galleries()
        elif choice == '2':
            simple_mode()
        elif choice == '3':
            advanced_mode()
        else:
            print(f"  {C.RED}Invalid choice{C.RESET}")


if __name__ == "__main__":
    main()
