"""
EchoAtlas - Telegram Bot for Song Metadata
Sources: Wikipedia (metadata) + Genius API (about/lyrics, no scraping) + MusicBrainz (fallback)
"""

import os
import re
import logging
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── API config ─────────────────────────────────────────────────────────────────
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
USER_AGENT      = "EchoAtlasBot/2.0 (echoatlasbot@telegram.com)"

GENIUS_ACCESS_TOKEN = os.getenv(
    'GENIUS_ACCESS_TOKEN',
    'ENTER_GENIUS_TOKEN'
)
GENIUS_API = "https://api.genius.com"

WIKIPEDIA_API  = "https://en.wikipedia.org/w/api.php"
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN',
                           'ENTER_TEL_TOKEN')

# Keywords that flag a release as a compilation / hits album
_COMPILATION_RE = re.compile(
    r'\b(hits|best of|greatest|collection|playlist|vol\.|volume|'
    r'compilation|anthology|essentials|now that\'s|top\s*\d|'
    r'\d+\s*%|nrj|universal music)\b',
    re.IGNORECASE,
)


# ══════════════════════════════════════════════════════════════════════════════
class SongMetadataFetcher:
# ══════════════════════════════════════════════════════════════════════════════

    # ── MusicBrainz search ────────────────────────────────────────────────────
    @staticmethod
    def search_songs(song_name: str) -> List[Dict]:
        try:
            headers = {'User-Agent': USER_AGENT}
            params  = {'query': f'recording:"{song_name}"', 'fmt': 'json', 'limit': 15}

            resp = requests.get(f"{MUSICBRAINZ_API}/recording/", params=params,
                                headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not data.get('recordings') or len(data['recordings']) < 3:
                params['query'] = song_name
                resp = requests.get(f"{MUSICBRAINZ_API}/recording/", params=params,
                                    headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()

            results, seen = [], set()
            for rec in data.get('recordings', []):
                credits = rec.get('artist-credit', [])
                if not credits:
                    continue
                artist_name = credits[0]['name']
                all_artists = [c['name'] for c in credits if 'name' in c]
                featured    = all_artists[1:]
                title       = rec.get('title', '')
                key         = f"{title.lower()}_{'&'.join(sorted(all_artists)).lower()}"
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    'id':               rec.get('id'),
                    'title':            title,
                    'artist':           artist_name,
                    'featured_artists': featured,
                    'score':            rec.get('score', 0),
                })

            results.sort(key=lambda x: x.get('score', 0), reverse=True)
            return results[:10]
        except Exception as e:
            logger.error(f"MusicBrainz search error: {e}")
            return []

    # ── Wikipedia metadata ────────────────────────────────────────────────────
    @staticmethod
    def get_wikipedia_metadata(track: str, artist: str) -> Optional[Dict]:
        try:
            resp = requests.get(WIKIPEDIA_API, params={
                'action': 'query', 'list': 'search',
                'srsearch': f'"{track}" {artist} song',
                'format': 'json', 'srlimit': 3,
            }, timeout=10)
            resp.raise_for_status()
            results = resp.json().get('query', {}).get('search', [])
            if not results:
                return None

            for result in results[:2]:
                page_title = result['title']
                cresp = requests.get(WIKIPEDIA_API, params={
                    'action': 'parse', 'page': page_title,
                    'prop': 'text|wikitext', 'format': 'json',
                }, timeout=10)
                cresp.raise_for_status()
                cdata = cresp.json()
                if 'parse' not in cdata:
                    continue

                soup    = BeautifulSoup(cdata['parse']['text']['*'], 'html.parser')
                infobox = soup.find('table', class_='infobox')
                if not infobox:
                    continue

                metadata: Dict = {}
                for row in infobox.find_all('tr'):
                    th = row.find('th')
                    td = row.find('td')
                    if not th or not td:
                        continue
                    key = th.get_text(strip=True).lower()
                    val = td.get_text(separator=' ', strip=True)

                    if 'artist' in key:
                        parts = [a.strip() for a in re.split(r'featuring|feat\.|ft\.|,|&', val)]
                        if parts:
                            metadata['artist'] = parts[0]
                            if len(parts) > 1:
                                metadata['featured_artists'] = [p for p in parts[1:] if p]
                    elif 'album' in key:
                        metadata['album'] = val.split('(')[0].strip()
                    elif 'released' in key or 'published' in key:
                        m = re.search(r'\b(19|20)\d{2}\b', val)
                        if m:
                            metadata['year'] = m.group(0)
                    elif 'genre' in key:
                        genres = [g.strip() for g in re.split(r',|;|\n', val)
                                  if g.strip() and len(g.strip()) > 2]
                        if genres:
                            metadata['genre'] = ', '.join(genres[:4])

                metadata['url'] = f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
                if metadata.get('artist') or metadata.get('album'):
                    return metadata

            return None
        except Exception as e:
            logger.error(f"Wikipedia error: {e}")
            return None

    # ── Genius: API-only, no browser scraping ─────────────────────────────────
    @staticmethod
    def _html_to_plain(html: str) -> str:
        """Convert Genius lyrics HTML snippet to clean plain text."""
        soup = BeautifulSoup(html, 'html.parser')

        def walk(node) -> str:
            buf = []
            for child in node.children:
                if isinstance(child, NavigableString):
                    buf.append(str(child))
                elif isinstance(child, Tag):
                    if child.name == 'br':
                        buf.append('\n')
                    elif child.name in ('a', 'span', 'b', 'i', 'em', 'strong'):
                        buf.append(walk(child))
                    elif child.name == 'div':
                        inner = walk(child).strip()
                        if inner:
                            buf.append('\n' + inner + '\n')
            return ''.join(buf)

        containers = soup.find_all('div', {'data-lyrics-container': 'true'})
        if containers:
            parts = [walk(d).strip() for d in containers]
        else:
            body  = soup.find('div', class_=re.compile(r'lyrics', re.I)) or soup
            parts = [walk(body).strip()]

        raw = '\n\n'.join(p for p in parts if p)
        raw = raw.replace('&amp;', '&').replace('&apos;', "'").replace('&#x27;', "'")
        raw = re.sub(r'[ \t]{2,}', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        raw = '\n'.join(line.rstrip() for line in raw.splitlines())
        raw = re.sub(r'(?<!\w)\d{4,6}(?!\w)', '', raw)
        return re.sub(r'\n{3,}', '\n\n', raw).strip()

    # Junk Genius injects before/after About text
    _ABOUT_PREFIX = re.compile(
        r'^(Song\s+Bio\s*|\d+\s+contributors?\s*|'
        r'[\d\s]+(Contributors?|Translations?|Comments?)\s*)+',
        re.IGNORECASE,
    )
    _ABOUT_SUFFIX = re.compile(
        r'\s*(Expand\s*\+?\d*\s*\d*\s*Share|Read\s*More.*|'
        r'Ask\s+us.*|Add\s+a\s+comment.*)\s*$',
        re.IGNORECASE | re.DOTALL,
    )

    @staticmethod
    def _clean_about(text: str) -> str:
        import re as _re
        text = SongMetadataFetcher._ABOUT_PREFIX.sub('', text).strip()
        text = SongMetadataFetcher._ABOUT_SUFFIX.sub('', text).strip()
        # Also strip inline "Read More" links Genius appends mid-paragraph
        text = _re.sub(r'\s*Read More\s*$', '', text, flags=_re.IGNORECASE).strip()
        return text

    @staticmethod
    def _scrape_page(song_url: str) -> dict:
        """Scrape Genius page for About text AND lyrics in one request."""
        from bs4 import NavigableString, Tag as BSTag
        out: dict = {}
        try:
            resp = requests.get(
                song_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                                       'Chrome/120.0.0.0 Safari/537.36'},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"_scrape_page: HTTP {resp.status_code} for {song_url}")
                return out
            # Cloudflare challenge page — no usable HTML
            if 'cf-browser-verification' in resp.text or 'cf_clearance' in resp.text:
                logger.warning(f"_scrape_page: Cloudflare blocked for {song_url}")
                return out
            soup = BeautifulSoup(resp.text, 'html.parser')

            # ── About ──────────────────────────────────────────────────────
            for tag in soup.find_all(['section', 'div']):
                cls = ' '.join(tag.get('class', []))
                if re.search(r'About\w*Content|SongDescription', cls):
                    text = SongMetadataFetcher._clean_about(
                        tag.get_text(separator=' ', strip=True)
                    )
                    if len(text) > 40:
                        out['about'] = text
                        break
            if not out.get('about'):
                for heading in soup.find_all(['h2', 'h3']):
                    if heading.get_text(strip=True).lower() == 'about':
                        chunks = []
                        for sib in heading.find_next_siblings():
                            if sib.name in ('h2', 'h3'):
                                break
                            t = sib.get_text(separator=' ', strip=True)
                            if t:
                                chunks.append(t)
                        text = SongMetadataFetcher._clean_about(' '.join(chunks))
                        if len(text) > 40:
                            out['about'] = text
                            break

            # ── Lyrics ─────────────────────────────────────────────────────
            def _walk(node) -> str:
                buf = []
                for child in node.children:
                    if isinstance(child, NavigableString):
                        buf.append(str(child))
                    elif isinstance(child, BSTag):
                        if child.name == 'br':
                            buf.append('\n')
                        elif child.name in ('a', 'span', 'b', 'i', 'em', 'strong'):
                            buf.append(_walk(child))
                        elif child.name == 'div':
                            inner = _walk(child).strip()
                            if inner:
                                buf.append('\n' + inner + '\n')
                return ''.join(buf)

            lyrics_divs = soup.find_all('div', {'data-lyrics-container': 'true'})
            if lyrics_divs:
                sections = []
                for div in lyrics_divs:
                    raw = _walk(div).strip()
                    raw = re.sub(r'[ \t]{2,}', ' ', raw)
                    raw = re.sub(r'\n{3,}', '\n\n', raw)
                    raw = '\n'.join(line.rstrip() for line in raw.splitlines())
                    if raw:
                        sections.append(raw)
                lyrics = '\n\n'.join(sections)
                lyrics = lyrics.replace('&amp;', '&').replace('&apos;', "'").replace('&#x27;', "'")
                lyrics = re.sub(r'(?<!\w)\d{4,6}(?!\w)', '', lyrics)
                lyrics = re.sub(r'\n{3,}', '\n\n', lyrics).strip()
                if lyrics:
                    out['lyrics'] = lyrics
        except Exception as e:
            logger.warning(f"_scrape_page failed: {e}")
        return out

    @staticmethod
    def _scrape_about(song_url: str) -> Optional[str]:
        return SongMetadataFetcher._scrape_page(song_url).get('about')

    # Known junk lines in description.plain fallback
    _JUNK_LINE = re.compile(
        r'^(\d+\s+(Contributors?|Translations?|Comments?|Pyccкий)|'
        r'Translations?|Romanization|Read\s*More|'
        r'English|Deutsch|Italiano|Espa\u00f1ol|Polski|Fran\u00e7ais|'
        r'T\u00fcrk\u00e7e|\u010cesky|Nederlands|Portugu[e\u00ea]s|srpski|'
        r'Ti\u1ebfng\s*Vi\u1ec7t|\u0939\u093f\u0928\u094d\u0926\u0940.*|'
        r'[A-Z][a-z]+\s+Lyrics|.*\bLyrics\b)$',
        re.IGNORECASE,
    )

    @classmethod
    def _clean_genius_desc(cls, raw: str) -> str:
        lines = raw.splitlines()
        start = 0
        for i, line in enumerate(lines):
            s = line.strip()
            if s and not cls._JUNK_LINE.match(s):
                start = i
                break
        return '\n'.join(lines[start:]).strip()

    @staticmethod
    def get_genius_full_data(track: str, artist: str) -> Optional[Dict]:
        """
        Fetch About description + Lyrics from Genius REST API only.

        Cloud servers (Railway, Heroku, Render) get blocked by Genius Cloudflare
        when doing browser-style scraping. The authenticated API endpoints bypass
        this completely.

        Lyrics retrieval order:
          1. song.lyrics.plain field (newer API response)
          2. /songs/{id}/embed.js endpoint (parse HTML blob inside the JS)
        """
        if GENIUS_ACCESS_TOKEN == 'YOUR_GENIUS_ACCESS_TOKEN':
            return None

        hdrs = {'Authorization': f'Bearer {GENIUS_ACCESS_TOKEN}'}

        try:
            # 1. Search
            sresp = requests.get(f"{GENIUS_API}/search", headers=hdrs,
                                 params={'q': f"{track} {artist}"}, timeout=10)
            sresp.raise_for_status()
            hits = sresp.json().get('response', {}).get('hits', [])
            if not hits:
                return None

            song_info = None
            for hit in hits[:5]:
                r        = hit['result']
                r_artist = r.get('primary_artist', {}).get('name', '').lower()
                if artist.lower() in r_artist or r_artist in artist.lower():
                    song_info = r
                    break
            if not song_info:
                song_info = hits[0]['result']

            song_id  = song_info['id']
            song_url = song_info.get('url', '')

            # 2. Song detail
            dresp = requests.get(f"{GENIUS_API}/songs/{song_id}", headers=hdrs,
                                 params={'text_format': 'plain'}, timeout=10)
            dresp.raise_for_status()
            song_details = dresp.json().get('response', {}).get('song', {})

            result: Dict = {'url': song_url}

            # Album from Genius
            genius_album = (song_details.get('album') or {}).get('name', '')
            if genius_album:
                result['genius_album'] = genius_album

            # 3. About + Lyrics
            # Strategy A: scrape the Genius page (works locally; may be blocked by
            # Cloudflare on cloud servers — we catch that and fall through)
            if song_url:
                scraped = SongMetadataFetcher._scrape_page(song_url)
                if scraped.get('about'):
                    result['description'] = scraped['about']
                    logger.info(f"Genius: about via page scrape for {song_id}")
                if scraped.get('lyrics'):
                    result['lyrics'] = scraped['lyrics']
                    logger.info(f"Genius: lyrics via page scrape for {song_id}")
                else:
                    logger.warning(f"Genius: page scrape returned no lyrics for {song_id} "
                                   f"(url={song_url!r})")

            # Strategy B: API html format — fetch lyrics as HTML then parse to plain text.
            # This uses the Bearer token so Cloudflare is bypassed entirely.
            if not result.get('lyrics'):
                try:
                    hresp = requests.get(
                        f"{GENIUS_API}/songs/{song_id}",
                        headers=hdrs,
                        params={'text_format': 'html'},
                        timeout=10,
                    )
                    if hresp.status_code == 200:
                        hdata = hresp.json().get('response', {}).get('song', {})
                        lyrics_html = (hdata.get('lyrics') or {}).get('html', '')
                        if lyrics_html:
                            parsed = SongMetadataFetcher._html_to_plain(lyrics_html)
                            if parsed and len(parsed) > 30:
                                result['lyrics'] = parsed
                                logger.info(f"Genius: lyrics via API html for {song_id}")
                        # Also grab about from html if still missing
                        if not result.get('description'):
                            desc_html = (hdata.get('description') or {}).get('html', '')
                            if desc_html:
                                parsed_desc = SongMetadataFetcher._html_to_plain(desc_html)
                                cleaned = SongMetadataFetcher._clean_genius_desc(parsed_desc)
                                if cleaned and len(cleaned) > 40:
                                    result['description'] = cleaned
                                    logger.info(f"Genius: about via API html for {song_id}")
                except Exception as he:
                    logger.warning(f"Genius API html fallback failed: {he}")

            # Strategy C: plain text description fallback
            if not result.get('description'):
                desc = (song_details.get('description') or {}).get('plain', '').strip()
                if desc and desc != '?':
                    cleaned = SongMetadataFetcher._clean_genius_desc(desc)
                    if cleaned:
                        result['description'] = cleaned

            if not result.get('lyrics'):
                logger.warning(
                    f"Genius: ALL lyrics strategies failed for {song_id} "
                    f"(lyrics_state={song_info.get('lyrics_state')!r})"
                )

            return result if result.get('description') or result.get('lyrics') else None

        except Exception as e:
            logger.error(f"Genius API error: {e}")
            return None

    # ── Assemble full metadata ─────────────────────────────────────────────────
    @staticmethod
    def get_detailed_metadata(recording_id: str, song_title: str, artist: str) -> Dict:
        metadata: Dict = {
            'title':            song_title,
            'artist':           artist,
            'featured_artists': [],
            'album':            'Unknown',
            'year':             'Unknown',
            'genre':            'Unknown',
            'description':      'No description available',
            'lyrics':           None,
            'genius_url':       None,
            'wikipedia_url':    None,
        }

        try:
            # PRIMARY: Wikipedia
            wiki = SongMetadataFetcher.get_wikipedia_metadata(song_title, artist)
            if wiki:
                for key in ('artist', 'featured_artists', 'album', 'year', 'genre'):
                    if wiki.get(key):
                        metadata[key] = wiki[key]
                if wiki.get('url'):
                    metadata['wikipedia_url'] = wiki['url']

            # FALLBACK: MusicBrainz
            if any(metadata[k] == 'Unknown' for k in ('album', 'year', 'genre')):
                hdrs = {'User-Agent': USER_AGENT}
                mresp = requests.get(
                    f"{MUSICBRAINZ_API}/recording/{recording_id}",
                    params={'inc': 'releases+artist-credits+genres+tags', 'fmt': 'json'},
                    headers=hdrs, timeout=10,
                )
                mresp.raise_for_status()
                mdata = mresp.json()

                if not metadata['featured_artists']:
                    ac = mdata.get('artist-credit', [])
                    if len(ac) > 1:
                        metadata['featured_artists'] = [
                            c['name'] for c in ac[1:] if 'name' in c
                        ]

                if metadata['genre'] == 'Unknown':
                    mb_genres = (
                        [g['name'] for g in mdata.get('genres', [])[:3]] or
                        [t['name'] for t in mdata.get('tags',   [])[:3]]
                    )
                    if mb_genres:
                        metadata['genre'] = ', '.join(mb_genres)

                if metadata['album'] == 'Unknown' or metadata['year'] == 'Unknown':
                    releases = mdata.get('releases', [])
                    if releases:
                        def _sort_key(r):
                            return (int(bool(_COMPILATION_RE.search(r.get('title', '')))),
                                    r.get('date', '9999'))
                        best = sorted(releases, key=_sort_key)[0]
                        if metadata['album'] == 'Unknown':
                            metadata['album'] = best.get('title', 'Unknown')
                        if metadata['year'] == 'Unknown':
                            d = best.get('date', '')
                            if d:
                                metadata['year'] = d.split('-')[0]

            # GENIUS: About + Lyrics + album override
            genius = SongMetadataFetcher.get_genius_full_data(song_title, artist)
            if genius:
                if genius.get('description'):
                    metadata['description'] = genius['description']
                if genius.get('lyrics'):
                    metadata['lyrics'] = genius['lyrics']
                if genius.get('url'):
                    metadata['genius_url'] = genius['url']
                # Use Genius album if current one looks like a compilation
                g_album = genius.get('genius_album', '')
                if g_album and (
                    metadata['album'] == 'Unknown' or
                    _COMPILATION_RE.search(metadata['album'])
                ):
                    metadata['album'] = g_album

        except Exception as e:
            logger.error(f"get_detailed_metadata error: {e}")

        return metadata


# ══════════════════════════════════════════════════════════════════════════════
# Bot handlers
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f3b5 *Welcome to EchoAtlas!*\n\n"
        "Your free music metadata companion.\n\n"
        "\U0001f4cc Get song information from Wikipedia\n"
        "\U0001f4d6 Get song meanings from Genius\n"
        "\U0001f4dd View full lyrics with one tap\n"
        "\U0001f517 Direct links to sources\n\n"
        "\u2728 *Just type any Song name with Artist*\n\n"
        '_Example: "Wildflower - Billie Eilish"_',
        parse_mode='Markdown',
    )


async def handle_song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    song_name = update.message.text.strip()
    if not song_name:
        await update.message.reply_text("Please enter a song name!")
        return

    msg = await update.message.reply_text(f"\U0001f50d Searching for '{song_name}'...")
    results = SongMetadataFetcher.search_songs(song_name)
    if not results:
        await msg.edit_text("\u274c No results found. Try a different search!")
        return

    keyboard = []
    for idx, song in enumerate(results[:10]):
        artist_text = song['artist']
        if song['featured_artists']:
            artist_text += f" ft. {', '.join(song['featured_artists'])}"
        keyboard.append([InlineKeyboardButton(
            f"\U0001f3b5 {song['title']} - {artist_text}",
            callback_data=f"select_{idx}",
        )])

    context.user_data['search_results'] = results
    await msg.edit_text(
        "\U0001f4cb *Select the correct song:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def handle_song_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb = query.data

    # ── Lyrics button ──────────────────────────────────────────────────────────
    if cb == 'show_lyrics':
        meta = context.user_data.get('current_metadata')
        if not (meta and meta.get('lyrics')):
            await query.answer("Lyrics not available", show_alert=True)
            return

        lyrics = meta['lyrics']
        title  = meta.get('title', 'Lyrics')
        artist = meta.get('artist', '')

        header = f"\U0001f4dd *{title}*"
        if artist:
            header += f" \u2014 {artist}"
        header += "\n\n"

        max_body = 4096 - len(header) - 60
        body, suffix = lyrics, ""
        if len(lyrics) > max_body:
            body   = lyrics[:max_body]
            body   = body[:body.rfind('\n')]
            suffix = "\n\n_\u2026(truncated \u2014 see full lyrics on Genius)_"

        # Escape Markdown v1 special chars in the lyrics body only
        safe = (body
                .replace('\\', '\\\\')
                .replace('_', '\\_')
                .replace('*', '\\*')
                .replace('[', '\\[')
                .replace('`', '\\`'))

        await query.message.reply_text(
            f"{header}{safe}{suffix}",
            parse_mode='Markdown',
            disable_web_page_preview=True,
        )
        return

    # ── Song selection ─────────────────────────────────────────────────────────
    if not cb.startswith('select_'):
        return

    idx = int(cb.split('_')[1])
    if 'search_results' not in context.user_data:
        await query.edit_message_text("\u274c Session expired. Please search again.")
        return

    results = context.user_data['search_results']
    if idx >= len(results):
        await query.edit_message_text("\u274c Invalid selection.")
        return

    await query.edit_message_text("\u23f3 Fetching metadata...")

    song = results[idx]
    meta = SongMetadataFetcher.get_detailed_metadata(
        song['id'], song['title'], song['artist']
    )
    context.user_data['current_metadata'] = meta

    artist_line = meta['artist']
    if meta.get('featured_artists'):
        artist_line += f" ft. {', '.join(meta['featured_artists'])}"

    # Heading: keep the em-dash OUTSIDE any *bold* span to avoid Markdown v1 breakage
    msg  = "\U0001f3b5 *Here's the Metadata...*\n\n"
    msg += f"\U0001f4cc *Title:* {meta['title']}\n"
    msg += f"\U0001f3a4 *Artist:* {artist_line}\n"

    if meta['album'] != 'Unknown':
        msg += f"\U0001f4bf *Album:* {meta['album']}\n"
    if meta['year'] != 'Unknown':
        msg += f"\U0001f4c5 *Year:* {meta['year']}\n"
    if meta['genre'] != 'Unknown':
        msg += f"\U0001f3b6 *Genre:* {meta['genre'].title()}\n"

    if meta['description'] != 'No description available':
        desc = meta['description']
        # Show full text; trim at last complete sentence if ending is abrupt
        if desc and desc.rstrip()[-1] not in ('.', '!', '?'):
            last = max(desc.rfind('. '), desc.rfind('! '), desc.rfind('? '))
            if last > len(desc) // 2:
                desc = desc[:last + 1].strip()
        msg += f"\n\U0001f4d6 *About:*\n_{desc}_\n"

    links = []
    if meta.get('genius_url'):
        links.append(f"[Genius]({meta['genius_url']})")
    if meta.get('wikipedia_url'):
        links.append(f"[Wikipedia]({meta['wikipedia_url']})")
    if links:
        msg += f"\n\U0001f517 More: {' \u2022 '.join(links)}"

    buttons = []
    if meta.get('lyrics'):
        buttons.append([InlineKeyboardButton("\U0001f4dd View Lyrics",
                                             callback_data="show_lyrics")])

    await query.edit_message_text(
        msg,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f4d6 *How to use EchoAtlas:*\n\n"
        "1\u20e3 Send a song name\n"
        "2\u20e3 Select the correct match\n"
        "3\u20e3 Get metadata + tap for lyrics!\n\n"
        "*Data Sources:*\n"
        "\U0001f4ca Wikipedia \u2014 Metadata\n"
        "\U0001f4d6 Genius \u2014 About & Lyrics\n"
        "\U0001f3b5 MusicBrainz \u2014 Fallback\n\n"
        "*Commands:*\n"
        "/start \u2014 Start the bot\n"
        "/help \u2014 Show this help",
        parse_mode='Markdown',
    )


def main():
    if TELEGRAM_TOKEN == 'ENTER_TEL_TOKEN':
        logger.error("Please set your Telegram bot token!")
        print("\n\u26a0\ufe0f  ERROR: Telegram bot token not set!")
        return

    import asyncio, sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song_search))
    app.add_handler(CallbackQueryHandler(handle_song_selection))

    logger.info("EchoAtlas bot started.")
    print("\u2705 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == '__main__':
    main()
