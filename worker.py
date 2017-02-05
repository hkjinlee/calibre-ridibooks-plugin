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

from lxml.html import fromstring, tostring

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.localization import canonicalize_lang

import calibre_plugins.ridibooks.config as cfg

class Worker(Thread): # Get details

    '''
    Get book details from Goodreads book page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.ridibooks_id = self.isbn = None

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
            self.get_details()
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def get_details(self):
        try:
            self.log.info('Ridibooks url: %r' % self.url)
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r'%self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Ridibooks timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r'%self.url
                self.log.exception(msg)
            return

        raw = raw.decode('utf-8', errors='replace')
        #open('c:\\ridibooks.html', 'wb').write(raw)

        try:
            root = fromstring(clean_ascii_chars(raw))
        except:
            msg = 'Failed to parse Ridibooks details page: %r'%self.url
            self.log.exception(msg)
            return

        try:
            # Look at the <title> attribute for page to make sure that we were actually returned
            # a details page for a book. If the user had specified an invalid ISBN, then the results
            # page will just do a textual search.
            title = root.xpath('//meta[@property="og:title"]/@content')
            if title:
                if title is None:
                    self.log.error('Failed to see search results in page title: %r'%self.url)
                    return
        except:
            msg = 'Failed to read ridibooks page title: %r'%self.url
            self.log.exception(msg)
            return

        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse ridibooks details page: %r'%self.url
            msg += tostring(errmsg, method='text', encoding=unicode).strip()
            self.log.error(msg)
            return

        self.parse_details(root)

    def parse_details(self, root):
        try:
            ridibooks_id = self.parse_ridibooks_id(self.url)
        except:
            self.log.exception('Error parsing ridibooks id for url: %r'%self.url)
            ridibooks_id = None

        try:
            (title, series, series_index) = self.parse_title_series(root)
        except:
            self.log.exception('Error parsing title and series for url: %r'%self.url)
            title = series = series_index = None

        try:
            authors = self.parse_authors(root)
        except:
            self.log.exception('Error parsing authors for url: %r'%self.url)
            authors = []

        if not title or not authors or not ridibooks_id:
            self.log.error('Could not find title/authors/ridibooks id for %r'%self.url)
            self.log.error('Ridibooks: %r Title: %r Authors: %r'%(ridibooks_id, title,
                authors))
            return

        mi = Metadata(title, authors)
        if series:
            mi.series = series
            mi.series_index = series_index
        mi.set_identifier('ridibooks', ridibooks_id)
        self.ridibooks_id = ridibooks_id

        try:
            isbn = self.parse_isbn(root)
            if isbn:
                self.isbn = mi.isbn = isbn
        except:
            self.log.exception('Error parsing ISBN for url: %r'%self.url)

        try:
            mi.rating = self.parse_rating(root)
        except:
            self.log.exception('Error parsing ratings for url: %r'%self.url)

        try:
            (mi.publisher, mi.pubdate) = self.parse_publisher_date(root)
        except:
            self.log.exception('Error parsing publisher/date for url: %r'%self.url)

        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception('Error parsing comments for url: %r'%self.url)

        try:
            self.cover_url = self.parse_cover(root)
        except:
            self.log.exception('Error parsing cover for url: %r'%self.url)
        mi.has_cover = bool(self.cover_url)

        try:
            tags = self.parse_tags(root)
            if tags:
                mi.tags = tags
            else:
                mi.tags = []
        except:
            self.log.exception('Error parsing tags for url: %r'%self.url)

        try:
            lang = self._parse_language(root)
            if lang:
                mi.language = lang
        except:
            self.log.exception('Error parsing language for url: %r'%self.url)

        mi.source_relevance = self.relevance

        if self.ridibooks_id:
            if self.isbn:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.ridibooks_id)
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(self.ridibooks_id,
                        self.cover_url)

        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_ridibooks_id(self, url):
        return re.search('/v2/Detail\?id=(\d+)', url).groups(0)[0]

    def parse_title_series(self, root):
        title_node = root.xpath('//meta[@property="og:title"]/@content')
        if not title_node:
            return (None, None, None)
        title_text = title_node[0].strip()
        if title_text.find('(') == -1:
            return (title_text, None, None)
        # Contains a Title and possibly a series. Possible values currently handled:
        # "Some title (Omnibus)"
        # "Some title (#1-3)"
        # "Some title (Series #1)"
        # "Some title (Series (digital) #1)"
        # "Some title (Series #1-5)"
        # "Some title (NotSeries #2008 Jan)"
        # "Some title (Omnibus) (Series #1)"
        # "Some title (Omnibus) (Series (digital) #1)"
        # "Some title (Omnibus) (Series (digital) #1-5)"
        text_split = title_text.rpartition('(')
        title = text_split[0]
        series_info = text_split[2]
        hash_pos = series_info.find('#')
        if hash_pos <= 0:
            # Cannot find the series # in expression or at start like (#1-7)
            # so consider whole thing just as title
            title = title_text
            series_info = ''
        else:
            # Check to make sure we have got all of the series information
            series_info = series_info[:len(series_info)-1] #Strip off trailing ')'
            while series_info.count(')') != series_info.count('('):
                title_split = title.rpartition('(')
                title = title_split[0].strip()
                series_info = title_split[2] + '(' + series_info
        if series_info:
            series_partition = series_info.rpartition('#')
            series_name = series_partition[0].strip()
            if series_name.endswith(','):
                series_name = series_name[:-1]
            series_index = series_partition[2].strip()
            if series_index.find('-'):
                # The series is specified as 1-3, 1-7 etc.
                # In future we may offer config options to decide what to do,
                # such as "Use start number", "Use value xxx" like 0 etc.
                # For now will just take the start number and use that
                series_index = series_index.partition('-')[0].strip()
            try:
                return (title.strip(), series_name, float(series_index))
            except ValueError:
                # We have a series index which isn't really a series index
                title = title_text
        return (title.strip(), None, None)

    def parse_authors(self, root):
        base_node = root.xpath('//li[@class="metadata_writer"]')[0]
        if not base_node:
            return

        authors = base_node.xpath('./span/a/text()')
        self.log.debug(authors)
        author_roles = map(lambda x: re.sub(',', '', x.strip()), base_node.xpath('./span/text()'))
        self.log.debug(author_roles)
        author_index_max = author_roles.index(u'저')
        translator_index_max = author_roles.index(u'역')
        for i in range(author_index_max + 1, translator_index_max + 1):
            self.log.info(i)
            authors[i] = authors[i] + u'(역자)'
        self.log.info(authors)
        get_all_authors = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GET_ALL_AUTHORS]
        if get_all_authors:
            return authors
        else:
            return authors[0:author_index_max]   

    def parse_publisher_date(self, root):
        # Build a dict of authors with their contribution if any in values
        base_node = root.xpath('//ul[@class="info_metadata02_wrap"]')[0]
        if not base_node:
            return

        publisher_text = base_node.xpath('.//span[@itemprop="publisher"]//span/text()')[0]
        pubdate_text = base_node.xpath('.//span[@itemprop="datePublished"]/@content')[0]

        pubdate = self._convert_date_text(pubdate_text)
        return (publisher_text, pubdate)

    def parse_rating(self, root):
        rating_node = root.xpath('//meta[@itemprop="ratingValue"]/@content')
        if rating_node:
            rating_text = rating_node[0]
            # 네이버 평점은 10점 만점임
            rating_value = float(rating_text)
            return rating_value

    def parse_comments(self, root):
        # Look for description in a second span that gets expanded when interactively displayed [@id="display:none"]
        description_node = root.xpath('//div[@id="introduce_book"]')
        if description_node:
            desc = description_node[0] if len(description_node) == 1 else description_node[1]
            less_link = desc.xpath('button[@class="view_more"]')
            if less_link is not None and len(less_link):
                desc.remove(less_link[0])
            comments = tostring(desc, method='html', encoding=unicode).strip()
            while comments.find('  ') >= 0:
                comments = comments.replace('  ',' ')
            comments = sanitize_comments_html(comments)
            return comments

    def parse_cover(self, root):
        imgcol_node = root.xpath('//meta[@property="og:image"]/@content')
        if imgcol_node:
            img_url = imgcol_node[0]
            # Unfortunately Goodreads sometimes have broken links so we need to do
            # an additional request to see if the URL actually exists
            info = self.browser.open_novisit(img_url, timeout=self.timeout).info()
            if int(info.getheader('Content-Length')) > 1000:
                return img_url
            else:
                self.log.warning('Broken image for url: %s'%img_url)

    def parse_isbn(self, root):
        isbn_nodes = root.xpath('//meta[@property="books:isbn"]/@content')
        for node in isbn_nodes:
            text = node.strip()
            match = re.search('([0-9A-Z]{10,})', text)
            if match:
                isbn_text = match.group(1)
                self.log.info('ISBN is %s'%isbn_text)
                
        return isbn_text.strip()

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

    def _convert_date_text(self, date_text):
        year = int(date_text[0:4])
        month = int(date_text[4:6]) 
        day = int(date_text[6:])
        from calibre.utils.date import utc_tz
        return datetime.datetime(year, month, day, tzinfo=utc_tz)

    def _parse_language(self, root):
        return "Korean"
