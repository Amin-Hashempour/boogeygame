#!/usr/bin/env python3
"""Local news reader. Python 3.10+, no third-party dependencies."""
import concurrent.futures
import hashlib
import json
import argparse
import socket
import threading
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCES = {
    'Z': ('زومجی', 'https://www.zoomg.ir', ['/feed/', '/rss/']),
    'V': ('ویجیاتو', 'https://vigiato.net', ['/feed/']),
    'G': ('گیمفا', 'https://gamefa.com', ['/feed/']),
}
CACHE_DIR = ROOT / '.cache' / 'images'
INDEX_PATH = ROOT / '.cache' / 'image-index.json'
INDEX_LOCK = threading.Lock()
TOKEN_LOCKS = {}

def load_image_index():
    try:
        saved = json.loads(INDEX_PATH.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            return {token: value for token, value in saved.items()
                    if len(token) == 64 and isinstance(value, dict)
                    and value.get('kind') in ('image', 'article')
                    and safe_url(value.get('url', ''), '')}
    except (OSError, ValueError, TypeError):
        pass
    return {}

def save_image_index():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = INDEX_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(IMAGE_URLS, ensure_ascii=False), encoding='utf-8')
    temporary.replace(INDEX_PATH)

IMAGE_URLS = {}

class Images(HTMLParser):
    def __init__(self):
        super().__init__(); self.image = ''
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.image:
            return
        if tag == 'meta' and attrs.get('property') in ('og:image', 'og:image:secure_url'):
            self.image = attrs.get('content', '')
        elif tag == 'meta' and attrs.get('name') in ('twitter:image', 'twitter:image:src'):
            self.image = attrs.get('content', '')
        elif tag == 'link' and attrs.get('rel') == 'image_src':
            self.image = attrs.get('href', '')
        elif tag == 'img':
            self.image = (attrs.get('data-src') or attrs.get('data-lazy-src') or
                          attrs.get('data-original') or attrs.get('src', ''))
            if not self.image and attrs.get('srcset'):
                self.image = attrs['srcset'].split(',')[0].strip().split(' ')[0]

def safe_url(value, base):
    value = urljoin(base, value or '')
    return value if urlsplit(value).scheme in ('http', 'https') else ''

IMAGE_URLS.update(load_image_index())

def request_bytes(url, *, accept, referer='', limit=8 * 1024 * 1024, timeout=18):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36',
        'Accept': accept,
        'Accept-Language': 'fa-IR,fa;q=0.9,en-US;q=0.7,en;q=0.6',
    }
    if referer:
        headers['Referer'] = referer
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        data = response.read(limit + 1)
        content_type = response.headers.get_content_type()
    if len(data) > limit:
        raise ValueError('Response too large')
    return data, content_type

def image_content_type(data, reported):
    if reported.startswith('image/'):
        return reported
    signatures = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
                  (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif'), (b'RIFF', 'image/webp'))
    for signature, content_type in signatures:
        if data.startswith(signature):
            return content_type
    return ''

def find_article_image(url, timeout=6):
    try:
        data, content_type = request_bytes(url, accept='text/html,application/xhtml+xml', limit=2 * 1024 * 1024, timeout=timeout)
        if content_type not in ('text/html', 'application/xhtml+xml'):
            return ''
        parser = Images()
        parser.feed(data.decode('utf-8', errors='replace'))
        return safe_url(parser.image, url)
    except Exception:
        return ''

def register_image_sources(items):
    changed = False
    for item in items:
        if item['image']:
            identity = item['image']
            record = {'kind': 'image', 'url': item['image'], 'referer': item['url']}
        else:
            identity = 'article:' + item['url']
            record = {'kind': 'article', 'url': item['url'], 'referer': item['url']}
        token = hashlib.sha256(identity.encode()).hexdigest()
        if IMAGE_URLS.get(token) != record:
            IMAGE_URLS[token] = record
            changed = True
        item['image'] = '/api/image/' + token
    if changed:
        with INDEX_LOCK:
            save_image_index()
    return items

def fetch_image(token):
    record = IMAGE_URLS.get(token)
    if not record:
        raise FileNotFoundError('Unknown image token')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_path = CACHE_DIR / (token + '.bin')
    type_path = CACHE_DIR / (token + '.type')
    if data_path.is_file() and type_path.is_file():
        content_type = type_path.read_text(encoding='ascii').strip()
        if content_type.startswith('image/'):
            return data_path.read_bytes(), content_type

    with INDEX_LOCK:
        lock = TOKEN_LOCKS.setdefault(token, threading.Lock())
    with lock:
        if data_path.is_file() and type_path.is_file():
            content_type = type_path.read_text(encoding='ascii').strip()
            if content_type.startswith('image/'):
                return data_path.read_bytes(), content_type

        if record['kind'] == 'article':
            article_url = record['url']
            image_url = find_article_image(article_url)
            if not image_url:
                raise ValueError('Article has no usable image')
            record = {'kind': 'image', 'url': image_url, 'referer': article_url}
            IMAGE_URLS[token] = record
            with INDEX_LOCK:
                save_image_index()

        try:
            data, reported_type = request_bytes(
                record['url'], accept='image/avif,image/webp,image/*,*/*;q=0.8',
                referer=record.get('referer', ''), limit=10 * 1024 * 1024, timeout=8)
        except Exception:
            data, reported_type = request_bytes(
                record['url'], accept='image/avif,image/webp,image/*,*/*;q=0.8',
                limit=10 * 1024 * 1024, timeout=6)
        content_type = image_content_type(data, reported_type)
        if not content_type:
            raise ValueError('Remote response is not an image')
        temporary = data_path.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(data_path)
        type_path.write_text(content_type, encoding='ascii')
        return data, content_type

def describe_error(error):
    if isinstance(error, HTTPError):
        return f'HTTP {error.code}'
    if isinstance(error, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return 'timeout'
        return f'network: {str(reason)[:80]}'
    if isinstance(error, ET.ParseError):
        return 'invalid RSS/XML'
    return type(error).__name__

def parse_feed(data, base):
    root = ET.fromstring(data)
    def local(tag): return tag.rsplit('}', 1)[-1]
    entries = [e for e in root.iter() if local(e.tag) in ('item', 'entry')]
    result = []
    seen = set()
    for entry in entries:
        title = ''; link = ''; published = ''; image = ''
        for node in entry.iter():
            tag = local(node.tag)
            if tag == 'title' and not title:
                title = unescape(''.join(node.itertext())).strip()
            elif tag == 'link' and not link:
                if node.get('rel', 'alternate') == 'alternate':
                    link = node.get('href') or node.text or ''
            elif tag in ('pubDate', 'published', 'updated') and not published:
                published = node.text or ''
            if tag in ('thumbnail', 'content', 'enclosure'):
                candidate = (node.get('url') or node.get('href') or node.get('src') or
                             node.get('file') or '')
                if candidate and (tag != 'enclosure' or node.get('type', '').startswith('image/')):
                    image = image or candidate
            if tag in ('description', 'encoded', 'content') and node.text:
                parser = Images(); parser.feed(node.text)
                image = image or parser.image
        link = safe_url(link.strip(), base)
        if not title or not link or link in seen: continue
        seen.add(link)
        result.append({'id': link, 'url': link, 'title': title,
                       'image': safe_url(image, base) if image else '', 'published': published})
    if not result: raise ValueError('فهرست مطالب قابل خواندن نیست.')
    # RSS/Atom publishers normally supply newest entries first. Dates are sorted
    # when all entries have parseable timestamps; otherwise retain publisher order.
    from email.utils import parsedate_to_datetime
    from datetime import datetime
    def timestamp(item):
        value = item['published']
        try: return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, OverflowError):
            try: return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, OverflowError): return None
    if all(timestamp(item) is not None for item in result):
        result.sort(key=timestamp, reverse=True)
    return result

def fetch_source(code):
    name, base, paths = SOURCES[code]
    failures = []
    for path in paths:
        try:
            data, _ = request_bytes(
                base + path,
                accept='application/rss+xml, application/atom+xml, application/xml, text/xml',
                timeout=7)
            return code, {'items': register_image_sources(parse_feed(data, base)), 'error': None}
        except Exception as error:
            failure = f'{path}: {describe_error(error)}'
            failures.append(failure)
            print(f'[{code}] {failure}', flush=True)
    detail = '؛ '.join(failures)
    return code, {'items': [], 'error': f'دریافت مطالب {name} ممکن نشد ({detail}).'}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT), **kwargs)
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/news':
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                result = dict(pool.map(fetch_source, SOURCES))
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif path.startswith('/api/image/'):
            token = path.rsplit('/', 1)[-1]
            if len(token) != 64 or any(character not in '0123456789abcdef' for character in token):
                self.send_error(404); return
            try:
                data, content_type = fetch_image(token)
            except FileNotFoundError:
                self.send_error(404); return
            except Exception:
                self.send_error(502); return
            self.send_response(200); self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'private, max-age=604800'); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
        elif path in ('/', '/index.html', '/favicon.svg', '/fonts.css', '/styles.css', '/app.js') or path.startswith('/fonts/'):
            super().do_GET()
        else:
            self.send_error(404)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    print(f'News reader: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
