#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = 'Jin, Heonkyu <heonkyu.jin@gmail.com>'
__docformat__ = 'restructuredtext en'

import socket, re, datetime
from collections import OrderedDict
from threading import Thread

import lxml.html
import json

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars, unescape
from calibre.utils.localization import canonicalize_lang
from calibre.utils.date import utc_tz

import calibre_plugins.ridibooks.config as cfg

class Worker(Thread): # Get details

    '''
    Get book details from Ridibooks page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()

        lm = {
                'eng': ('English', 'Englisch'),
                'fra': ('French', 'Français'),
                'ita': ('Italian', 'Italiano'),
                'dut': ('Dutch',),
                'deu': ('German', 'Deutsch'),
                'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol'),
                'jpn': ('Japanese', u'日本語'),
                'kor': ('Korean', u'한국어'),
                'por': ('Portuguese', 'Português'),
                }
        self.lang_map = {}
        for code, names in lm.iteritems():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        try:
            self.load_details(self.url, self.timeout)
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def load_details(self, url, timeout):
        def _format_item(str):
            return re.sub('^"(.*)"$', '\\1', unescape(str))

        def _format_list(str):
            return [_.strip() for _ in _format_item(str).split(',')]

        def _find_meta(node, property):
            return [_.get('content') for _ in node if _.get('property') == property][0]

        def _format_date(date_text):
            year = int(date_text[0:4])
            month = int(date_text[4:6]) 
            day = int(date_text[6:])
            return datetime.datetime(year, month, day, tzinfo=utc_tz)

        try:
            response = self.browser.open(url, timeout=timeout)
            root = lxml.html.fromstring(response.read())

            # <meta> tag에서 불러오는 항목
            # 책ID, 제목, ISBN, 이미지URL, 평점
            meta = root.xpath('//meta[starts-with(@property, "og") or starts-with(@property, "books")]')

            # schema.org JSON에서 불러오는 항목
            # 제목, 저자, 책소개, 출판사
            ld_json = root.xpath('//script[@type="application/ld+json"]/text()')
            ld = [json.loads(_) for _ in ld_json]
            book_info = [_ for _ in ld if _['@type'] == 'Book'][0]
        except Exception as e:
            self.log.exception(e)

        ridibooks_id = re.search('id=([0-9]+)', url).group(1)
        isbn = _find_meta(meta, 'books:isbn')
        cover_url = _find_meta(meta, 'og:image')

        title = _find_meta(meta, 'og:title')
        authors = _format_list(book_info['author']['name'])
        if book_info.has_key('translator'):
            authors.extend([_ + u'(역자)' for _ in _format_list(book_info['translator']['name'])])

        mi = Metadata(title, authors)
        mi.set_identifier('ridibooks', ridibooks_id)

        mi.cover_url = cover_url
        mi.has_cover = bool(cover_url)

        mi.publisher = _format_item(book_info['publisher']['name'])
        mi.pubdate = _format_date(book_info['datePublished'])

        mi.comments = _format_item(book_info['description'])
        mi.rating = float(_find_meta(meta, 'books:rating:normalized_value'))

        series = re.search(u'(.*)\s*(\d+)권', title)
        if series:
            mi.series = series.group(1)
            mi.series_index = float(series.group(2))

        mi.language = 'Korean'
        mi.source_relevance = self.relevance

        if ridibooks_id:
            if isbn:
                self.plugin.cache_isbn_to_identifier(isbn, ridibooks_id)
            if cover_url:
                self.plugin.cache_identifier_to_cover_url(ridibooks_id, cover_url)

        self.plugin.clean_downloaded_metadata(mi)
        self.result_queue.put(mi)

    def parse_tags(self, root):
        # Goodreads does not have "tags", but it does have Genres (wrapper around popular shelves)
        # We will use those as tags (with a bit of massaging)
        genres_node = root.xpath('//div[@class="stacked"]/div/div/div[contains(@class, "bigBoxContent")]/div/div[@class="left"]')
        #self.log.info("Parsing tags")
        if genres_node:
            #self.log.info("Found genres_node")
            genre_tags = list()
            for genre_node in genres_node:
                sub_genre_nodes = genre_node.xpath('a')
                genre_tags_list = [sgn.text_content().strip() for sgn in sub_genre_nodes]
                #self.log.info("Found genres_tags list:", genre_tags_list)
                if genre_tags_list:
                    genre_tags.append(' > '.join(genre_tags_list))
            calibre_tags = self._convert_genres_to_calibre_tags(genre_tags)
            if len(calibre_tags) > 0:
                return calibre_tags

    def _convert_genres_to_calibre_tags(self, genre_tags):
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(),v) for (k,v) in calibre_tag_lookup.iteritems())
        tags_to_add = list()
        for genre_tag in genre_tags:
            tags = calibre_tag_map.get(genre_tag.lower(), None)
            if tags:
                for tag in tags:
                    if tag not in tags_to_add:
                        tags_to_add.append(tag)
        return list(tags_to_add)
