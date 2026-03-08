"""
EchoAtlas - Telegram Bot for Song Metadata
Final Version: Wikipedia (metadata) + Genius (about/lyrics) + Dropdown buttons
"""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import requests
from typing import List, Dict, Optional
import re
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# API configurations
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
USER_AGENT = "EchoAtlasBot/2.0 (echoatlasbot@telegram.com)"

GENIUS_ACCESS_TOKEN = os.getenv('GENIUS_ACCESS_TOKEN', 'YOUR_GENIUS_ACCESS_TOKEN')
GENIUS_API = "https://api.genius.com"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')


class SongMetadataFetcher:
    """Handles fetching song metadata from various sources"""
    
    @staticmethod
    def search_songs(song_name: str) -> List[Dict]:
        """Search for songs using MusicBrainz API (for initial search only)"""
        try:
            headers = {'User-Agent': USER_AGENT}
            params = {
                'query': f'recording:"{song_name}"',
                'fmt': 'json',
                'limit': 15
            }
            
            response = requests.get(
                f"{MUSICBRAINZ_API}/recording/",
                params=params,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            if not data.get('recordings') or len(data['recordings']) < 3:
                params['query'] = song_name
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/",
                    params=params,
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
            
            results = []
            seen_combinations = set()
            
            for recording in data.get('recordings', []):
                artist_credit = recording.get('artist-credit', [])
                if not artist_credit:
                    continue
                
                artist_name = artist_credit[0]['name']
                featured_artists = []
                all_artists = []
                
                for idx, credit in enumerate(artist_credit):
                    if 'name' in credit:
                        all_artists.append(credit['name'])
                        if idx > 0:
                            featured_artists.append(credit['name'])
                
                title = recording.get('title', '')
                artist_combo = ' & '.join(sorted(all_artists))
                unique_key = f"{title.lower()}_{artist_combo.lower()}"
                
                if unique_key in seen_combinations:
                    continue
                seen_combinations.add(unique_key)
                
                song_info = {
                    'id': recording.get('id'),
                    'title': title,
                    'artist': artist_name,
                    'featured_artists': featured_artists,
                    'score': recording.get('score', 0)
                }
                results.append(song_info)
            
            results.sort(key=lambda x: x.get('score', 0), reverse=True)
            return results[:10]
            
        except Exception as e:
            logger.error(f"Error searching songs: {e}")
            return []
    
    @staticmethod
    def get_wikipedia_metadata(track: str, artist: str) -> Optional[Dict]:
        """Get song metadata from Wikipedia"""
        try:
            search_params = {
                'action': 'query',
                'list': 'search',
                'srsearch': f'"{track}" {artist} song',
                'format': 'json',
                'srlimit': 3
            }
            
            response = requests.get(WIKIPEDIA_API, params=search_params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('query', {}).get('search'):
                return None
            
            for result in data['query']['search'][:2]:
                page_title = result['title']
                
                content_params = {
                    'action': 'parse',
                    'page': page_title,
                    'prop': 'text|wikitext',
                    'format': 'json'
                }
                
                response = requests.get(WIKIPEDIA_API, params=content_params, timeout=10)
                response.raise_for_status()
                content_data = response.json()
                
                if 'parse' not in content_data:
                    continue
                
                html_content = content_data['parse']['text']['*']
                soup = BeautifulSoup(html_content, 'html.parser')
                
                infobox = soup.find('table', class_='infobox')
                if not infobox:
                    continue
                
                metadata = {}
                
                rows = infobox.find_all('tr')
                for row in rows:
                    header = row.find('th')
                    if not header:
                        continue
                    
                    header_text = header.get_text(strip=True).lower()
                    value_cell = row.find('td')
                    if not value_cell:
                        continue
                    
                    value = value_cell.get_text(separator=' ', strip=True)
                    
                    if 'artist' in header_text:
                        artists = [a.strip() for a in re.split(r'featuring|feat\.|ft\.|,|&', value)]
                        if artists:
                            metadata['artist'] = artists[0]
                            if len(artists) > 1:
                                metadata['featured_artists'] = [a for a in artists[1:] if a]
                    
                    elif 'album' in header_text:
                        metadata['album'] = value.split('(')[0].strip()
                    
                    elif 'released' in header_text or 'published' in header_text:
                        year_match = re.search(r'\b(19|20)\d{2}\b', value)
                        if year_match:
                            metadata['year'] = year_match.group(0)
                    
                    elif 'genre' in header_text:
                        genres = re.split(r',|;|\n', value)
                        clean_genres = [g.strip() for g in genres if g.strip() and len(g.strip()) > 2]
                        if clean_genres:
                            metadata['genre'] = ', '.join(clean_genres[:4])
                
                metadata['url'] = f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
                
                if metadata.get('artist') or metadata.get('album'):
                    return metadata
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting Wikipedia metadata: {e}")
            return None
    
    @staticmethod
    def scrape_genius_page(song_url: str) -> Dict:
        """
        Scrape Genius page for:
        - Full About section (the community-written description under the About heading)
        - Clean lyrics (preserving section headers like [Verse 1], [Chorus], etc.)
        """
        result = {}
        try:
            scrape_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/120.0.0.0 Safari/537.36'
            }
            page_response = requests.get(song_url, headers=scrape_headers, timeout=15)
            
            if page_response.status_code != 200:
                return result
            
            soup = BeautifulSoup(page_response.text, 'html.parser')

            # ── ABOUT SECTION ──────────────────────────────────────────────
            # Genius renders the About text inside a <div> that contains the
            # community annotation.  We look for the section labelled "About"
            # and grab ALL text inside it (not just a 3-sentence truncation).
            about_text = None

            # Strategy 1: look for the labelled "About" section tag
            about_section = soup.find(
                lambda tag: tag.name in ('section', 'div') and
                tag.get('class') and
                any('About' in c or 'about' in c for c in tag.get('class', []))
            )
            if about_section:
                about_text = about_section.get_text(separator=' ', strip=True)

            # Strategy 2: find heading "About" then grab next sibling content
            if not about_text:
                for heading in soup.find_all(['h2', 'h3']):
                    if heading.get_text(strip=True).lower() == 'about':
                        # Collect all following siblings until next heading
                        paragraphs = []
                        for sibling in heading.find_next_siblings():
                            if sibling.name in ('h2', 'h3'):
                                break
                            text = sibling.get_text(separator=' ', strip=True)
                            if text:
                                paragraphs.append(text)
                        if paragraphs:
                            about_text = ' '.join(paragraphs)
                            break

            # Strategy 3: fall back to the API description.plain (handled in caller)
            if about_text and len(about_text) > 20:
                # Remove boilerplate Genius phrases
                about_text = re.sub(
                    r'(Ask us a question about this song.*|Add a comment.*|'
                    r'Genius is the world.*|Sign up.*|Log in.*)',
                    '', about_text, flags=re.IGNORECASE
                ).strip()
                result['about'] = about_text

            # ── LYRICS ────────────────────────────────────────────────────
            # Only data-lyrics-container divs hold actual lyrics.
            # We walk their children manually so we never accidentally pull
            # in page-level junk (contributor counts, translation links, etc.)
            # that lives outside these divs.
            lyrics_divs = soup.find_all('div', {'data-lyrics-container': 'true'})

            if lyrics_divs:
                from bs4 import NavigableString, Tag

                def extract_lyrics_from_div(div) -> str:
                    """
                    Walk a lyrics container element-by-element.
                    - NavigableString  → raw text (same line)
                    - <br>             → newline
                    - <a> / <span>     → recurse (lyric text wrapped in links/annotations)
                    - anything else    → skip (tooltip popups, hidden elements, etc.)
                    """
                    buf = []
                    for child in div.children:
                        if isinstance(child, NavigableString):
                            buf.append(str(child))
                        elif isinstance(child, Tag):
                            if child.name == 'br':
                                buf.append('\n')
                            elif child.name in ('a', 'span', 'b', 'i', 'em', 'strong'):
                                # Recurse to get the text inside links/annotations
                                buf.append(extract_lyrics_from_div(child))
                            elif child.name in ('div',):
                                # Section headers like [Verse 1] live in nested divs
                                inner = extract_lyrics_from_div(child)
                                if inner.strip():
                                    buf.append('\n' + inner.strip() + '\n')
                            # All other tags (script, style, hidden popups) → ignored
                    return ''.join(buf)

                sections = []
                for div in lyrics_divs:
                    section_text = extract_lyrics_from_div(div)

                    # Normalise: single newline between lines, double between stanzas
                    # First collapse 3+ newlines → double newline (stanza break)
                    section_text = re.sub(r'\n{3,}', '\n\n', section_text)
                    # Then collapse any run of 2+ spaces on a single line
                    section_text = re.sub(r'[ \t]{2,}', ' ', section_text)
                    # Strip trailing whitespace from each line
                    section_text = '\n'.join(line.rstrip() for line in section_text.splitlines())
                    # Remove leading/trailing blank lines in this section
                    section_text = section_text.strip()

                    if section_text:
                        sections.append(section_text)

                full_lyrics = '\n\n'.join(sections)

                # Final cleanup
                full_lyrics = full_lyrics.replace('&amp;', '&').replace('&apos;', "'").replace('&#x27;', "'")
                # Remove stray annotation numbers Genius sometimes injects
                full_lyrics = re.sub(r'(?<!\w)\d{4,6}(?!\w)', '', full_lyrics)
                full_lyrics = re.sub(r'\n{3,}', '\n\n', full_lyrics).strip()

                if full_lyrics:
                    result['lyrics'] = full_lyrics

        except Exception as e:
            logger.error(f"Error scraping Genius page: {e}")

        return result

    @staticmethod
    def get_genius_full_data(track: str, artist: str) -> Optional[Dict]:
        """Get song description and lyrics from Genius"""
        try:
            if GENIUS_ACCESS_TOKEN == 'YOUR_GENIUS_ACCESS_TOKEN':
                return None
            
            headers = {'Authorization': f'Bearer {GENIUS_ACCESS_TOKEN}'}
            search_url = f"{GENIUS_API}/search"
            params = {'q': f"{track} {artist}"}
            
            response = requests.get(search_url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('response', {}).get('hits'):
                return None
            
            hits = data['response']['hits']
            song_info = None
            
            for hit in hits[:3]:
                result = hit['result']
                result_artist = result.get('primary_artist', {}).get('name', '').lower()
                if artist.lower() in result_artist or result_artist in artist.lower():
                    song_info = result
                    break
            
            if not song_info:
                song_info = hits[0]['result']
            
            song_id = song_info['id']
            song_url = song_info.get('url', '')
            
            # Get song details from API (for fallback description)
            song_detail_url = f"{GENIUS_API}/songs/{song_id}"
            response = requests.get(song_detail_url, headers=headers, timeout=10)
            response.raise_for_status()
            song_data = response.json()
            song_details = song_data.get('response', {}).get('song', {})
            
            result = {'url': song_url}

            # ── Scrape the full About + clean lyrics from the Genius page ──
            if song_url:
                scraped = SongMetadataFetcher.scrape_genius_page(song_url)
                if scraped.get('about'):
                    result['description'] = scraped['about']
                if scraped.get('lyrics'):
                    result['lyrics'] = scraped['lyrics']

            # ── Fallback: use API description.plain if scraping found nothing ──
            if not result.get('description'):
                api_desc = song_details.get('description', {}).get('plain', '').strip()
                if api_desc and api_desc != '?':
                    result['description'] = api_desc  # Full text, no truncation

            return result if result.get('description') or result.get('lyrics') else None
            
        except Exception as e:
            logger.error(f"Error getting Genius data: {e}")
            return None
    
    @staticmethod
    def get_detailed_metadata(recording_id: str, song_title: str, artist: str) -> Dict:
        """Get detailed metadata - Wikipedia primary, MusicBrainz fallback, Genius for content"""
        metadata = {
            'title': song_title,
            'artist': artist,
            'featured_artists': [],
            'album': 'Unknown',
            'year': 'Unknown',
            'genre': 'Unknown',
            'description': 'No description available',
            'lyrics': None,
            'genius_url': None,
            'wikipedia_url': None
        }
        
        try:
            # PRIMARY: Wikipedia for metadata
            wiki_data = SongMetadataFetcher.get_wikipedia_metadata(song_title, artist)
            if wiki_data:
                if wiki_data.get('artist'):
                    metadata['artist'] = wiki_data['artist']
                if wiki_data.get('featured_artists'):
                    metadata['featured_artists'] = wiki_data['featured_artists']
                if wiki_data.get('album'):
                    metadata['album'] = wiki_data['album']
                if wiki_data.get('year'):
                    metadata['year'] = wiki_data['year']
                if wiki_data.get('genre'):
                    metadata['genre'] = wiki_data['genre']
                if wiki_data.get('url'):
                    metadata['wikipedia_url'] = wiki_data['url']
            
            # FALLBACK: MusicBrainz for missing data
            if metadata['album'] == 'Unknown' or metadata['year'] == 'Unknown' or metadata['genre'] == 'Unknown':
                headers = {'User-Agent': USER_AGENT}
                response = requests.get(
                    f"{MUSICBRAINZ_API}/recording/{recording_id}",
                    params={'inc': 'releases+artist-credits+genres+tags', 'fmt': 'json'},
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                
                if not metadata['featured_artists']:
                    artist_credit = data.get('artist-credit', [])
                    if len(artist_credit) > 1:
                        featured = [credit['name'] for credit in artist_credit[1:] if 'name' in credit]
                        metadata['featured_artists'] = featured
                
                genres = data.get('genres', [])
                tags = data.get('tags', [])
                mb_genres = []
                
                if genres:
                    mb_genres = [g['name'] for g in genres[:3]]
                elif tags:
                    mb_genres = [t['name'] for t in tags[:3]]
                
                if metadata['genre'] != 'Unknown' and mb_genres:
                    wiki_genres = [g.strip().lower() for g in metadata['genre'].split(',')]
                    all_genres = wiki_genres.copy()
                    for g in mb_genres:
                        if g.lower() not in [wg.lower() for wg in wiki_genres]:
                            all_genres.append(g)
                    metadata['genre'] = ', '.join(all_genres[:4])
                elif mb_genres:
                    metadata['genre'] = ', '.join(mb_genres)
                
                releases = data.get('releases', [])
                if releases:
                    first_release = releases[0]
                    if metadata['album'] == 'Unknown':
                        metadata['album'] = first_release.get('title', 'Unknown')
                    if metadata['year'] == 'Unknown':
                        release_date = first_release.get('date', '')
                        if release_date:
                            metadata['year'] = release_date.split('-')[0]
            
            # GENIUS: Get About and Lyrics
            genius_data = SongMetadataFetcher.get_genius_full_data(song_title, artist)
            if genius_data:
                if genius_data.get('description'):
                    metadata['description'] = genius_data['description']
                if genius_data.get('lyrics'):
                    metadata['lyrics'] = genius_data['lyrics']
                if genius_data.get('url'):
                    metadata['genius_url'] = genius_data['url']
            
        except Exception as e:
            logger.error(f"Error getting detailed metadata: {e}")
        
        return metadata


# Bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    welcome_message = (
        "🎵 *Welcome to EchoAtlas!*\n\n"
        "Your free music metadata companion.\n\n"
        "📌 Get song information from Wikipedia\n"
        "📖 Get song meanings from Genius\n"
        "📝 View full lyrics with one tap\n"
        "🔗 Direct links to sources\n\n"
        "✨ *Just type any song name!*\n\n"
        "_Example: \"Stay\" or \"Espresso\"_"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')


async def handle_song_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle song search"""
    song_name = update.message.text.strip()
    
    if not song_name:
        await update.message.reply_text("Please enter a song name!")
        return
    
    searching_msg = await update.message.reply_text(f"🔍 Searching for '{song_name}'...")
    
    results = SongMetadataFetcher.search_songs(song_name)
    
    if not results:
        await searching_msg.edit_text("❌ No results found. Try a different search!")
        return
    
    keyboard = []
    for idx, song in enumerate(results[:10]):
        artist_text = song['artist']
        if song['featured_artists']:
            artist_text += f" ft. {', '.join(song['featured_artists'])}"
        
        button_text = f"🎵 {song['title']} - {artist_text}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_{idx}")])
    
    context.user_data['search_results'] = results
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await searching_msg.edit_text(
        "📋 *Select the correct song:*",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_song_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle song selection and show metadata"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    # Handle lyrics button — sends lyrics as a SEPARATE clean message
    if callback_data == 'show_lyrics':
        metadata = context.user_data.get('current_metadata')
        if metadata and metadata.get('lyrics'):
            lyrics = metadata['lyrics']
            title  = metadata.get('title', 'Lyrics')
            artist = metadata.get('artist', '')

            header = f"📝 *{title}*"
            if artist:
                header += f" — _{artist}_"
            header += "\n\n"

            # Telegram message limit is 4096 chars
            max_body = 4096 - len(header) - 50  # leave room for truncation notice
            body = lyrics
            suffix = ""
            if len(lyrics) > max_body:
                body = lyrics[:max_body]
                # Don't cut mid-line
                body = body[:body.rfind('\n')]
                suffix = "\n\n_…(truncated — see full lyrics on Genius)_"

            # Escape only characters that would break Markdown inside lyrics
            # (Telegram's Markdown v1 is fragile; use MarkdownV2 here)
            safe_lyrics = body.replace('\\', '\\\\').replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
            safe_header = header  # already formatted

            await query.message.reply_text(
                f"{safe_header}{safe_lyrics}{suffix}",
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
        else:
            await query.answer("Lyrics not available", show_alert=True)
        return
    
    if not callback_data.startswith('select_'):
        return
    
    idx = int(callback_data.split('_')[1])
    
    if 'search_results' not in context.user_data:
        await query.edit_message_text("❌ Session expired. Please search again.")
        return
    
    results = context.user_data['search_results']
    if idx >= len(results):
        await query.edit_message_text("❌ Invalid selection.")
        return
    
    selected_song = results[idx]
    await query.edit_message_text("⏳ Fetching metadata...")
    
    metadata = SongMetadataFetcher.get_detailed_metadata(
        selected_song['id'],
        selected_song['title'],
        selected_song['artist']
    )
    
    context.user_data['current_metadata'] = metadata
    
    featured_artists = metadata.get('featured_artists', [])
    artist_line = metadata['artist']
    if featured_artists:
        artist_line += f" ft. {', '.join(featured_artists)}"
    
    message = "🎵 *EchoAtlas — Song Metadata*\n\n"
    message += f"📌 *Title:* {metadata['title']}\n"
    message += f"🎤 *Artist:* {artist_line}\n"
    
    if metadata['album'] != 'Unknown':
        message += f"💿 *Album:* {metadata['album']}\n"
    
    if metadata['year'] != 'Unknown':
        message += f"📅 *Year:* {metadata['year']}\n"
    
    if metadata['genre'] != 'Unknown':
        message += f"🎶 *Genre:* {metadata['genre'].title()}\n"
    
    # About section — full text, no truncation in the card
    if metadata['description'] != 'No description available':
        description = metadata['description']
        # Telegram message cap is 4096; keep card readable but generous
        if len(description) > 700:
            description = description[:697] + "…"
        message += f"\n📖 *About:*\n_{description}_\n"
    
    # Tags
    if metadata['genre'] != 'Unknown':
        genres = [g.strip().lower() for g in metadata['genre'].split(',')]
        tags = ' • '.join(genres[:5])
        message += f"\n🏷 *Tags:*\n{tags}\n"
    
    # Buttons
    buttons = []
    if metadata.get('lyrics'):
        buttons.append([InlineKeyboardButton("📝 View Lyrics", callback_data="show_lyrics")])
    
    more_info_text = "🔗 More: "
    links = []
    if metadata.get('genius_url'):
        links.append(f"[Genius]({metadata['genius_url']})")
    if metadata.get('wikipedia_url'):
        links.append(f"[Wikipedia]({metadata['wikipedia_url']})")
    
    if links:
        message += f"\n{more_info_text}{' • '.join(links)}"
    
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    
    await query.edit_message_text(
        message,
        parse_mode='Markdown',
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help information"""
    help_text = (
        "📖 *How to use EchoAtlas:*\n\n"
        "1️⃣ Send a song name\n"
        "2️⃣ Select the correct match\n"
        "3️⃣ Get metadata + tap for lyrics!\n\n"
        "*Data Sources:*\n"
        "📊 Wikipedia - Metadata\n"
        "📖 Genius - About & Lyrics\n"
        "🎵 MusicBrainz - Fallback\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/help - Show this help"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


def main():
    """Start the bot"""
    if TELEGRAM_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.error("Please set your Telegram bot token!")
        print("\n⚠️  ERROR: Telegram bot token not set!")
        return
    
    import asyncio
    import sys
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_song_search))
    application.add_handler(CallbackQueryHandler(handle_song_selection))
    
    logger.info("Bot started successfully!")
    print("✅ Bot is running... Press Ctrl+C to stop.")
    
    application.run_polling()


if __name__ == '__main__':
    main()