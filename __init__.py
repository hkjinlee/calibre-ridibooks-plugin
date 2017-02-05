#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = 'Jin, Heonkyu <heonkyu.jin@gmail.com>'
__docformat__ = 'restructuredtext en'

import time
from urllib import quote
from Queue import Queue, Empty

from lxml.html import fromstring, tostring

from calibre import as_unicode
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import Source
from calibre.utils.icu import lower
from calibre.utils.cleantext import clean_ascii_chars

try:
    load_translations()
except NameError:
    pass

class RidiBooks(Source):
    name = 'RidiBooks'
    description = _('Downloads metadata and covers from ridibooks.com')
    author = 'Jin, Heonkyu <heonkyu.jin@gmail.com>'
    version = (0, 0, 1)
    minimum_calibre_version = (0, 8, 0)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['title', 'authors', 'identifier:ridibooks',
        'identifier:isbn', 'rating', 'comments', 'publisher', 'pubdate',
        'tags', 'series', 'languages'])
    has_html_comments = True
    supports_gzip_transfer_encoding = True

    BASE_URL = 'http://ridibooks.com'
    MAX_EDITIONS = 5

    def config_widget(self):
        '''
        Overriding the default configuration screen for our own custom configuration
        '''
        from calibre_plugins.ridibooks.config import ConfigWidget
        return ConfigWidget(self)

    def get_book_url(self, identifiers):
        ridibooks_id = identifiers.get('ridibooks', None)
        if ridibooks_id:
            return ('ridibooks', ridibooks_id,
                    '%s/v2/Detail?id=%s' % (RidiBooks.BASE_URL, ridibooks_id))

    def create_query(self, log, title=None, authors=None, identifiers={}):
        isbn = check_isbn(identifiers.get('isbn', None))
        url = ''
        if title or authors:
            title_tokens = list(self.get_title_tokens(title,
                                strip_joiners=False, strip_subtitle=True))
            author_tokens = self.get_author_tokens(authors, only_first_author=True)

            tokens = [quote(t.encode('utf-8') if isinstance(t, unicode) else t) 
                for t in title_tokens]
            tokens += [quote(t.encode('utf-8') if isinstance(t, unicode) else t) 
                for t in author_tokens]
            url = '/search/?q=' + '+'.join(tokens)

        if not url:
            return None

        log.info('Search from %s' %(url))
        return RidiBooks.BASE_URL + url

    def get_cached_cover_url(self, identifiers):
        url = None
        ridibooks_id = identifiers.get('ridibooks', None)
        if ridibooks_id is None:
            isbn = identifiers.get('isbn', None)
            if isbn is not None:
                ridibooks_id = self.cached_isbn_to_identifier(isbn)
        if ridibooks_id is not None:
            url = self.cached_identifier_to_cover_url(ridibooks_id)

        return url

    def identify(self, log, result_queue, abort, title=None, authors=None,
            identifiers={}, timeout=30):
        '''
        Note this method will retry without identifiers automatically if no
        match is found with identifiers.
        '''
        matches = []
        # Unlike the other metadata sources, if we have a goodreads id then we
        # do not need to fire a "search" at Goodreads.com. Instead we will be
        # able to go straight to the URL for that book.
        ridibooks_id = identifiers.get('ridibooks', None)
        isbn = check_isbn(identifiers.get('isbn', None))
        br = self.browser
        if ridibooks_id:
            matches.append('%s/v2/Detail?id=%s' % (RidiBooks.BASE_URL, ridibooks_id))
        else:
            query = self.create_query(log, title=title, authors=authors,
                    identifiers=identifiers)
            if query is None:
                log.error('Insufficient metadata to construct query')
                return
            try:
                log.info('Querying: %s' % query)
                response = br.open_novisit(query, timeout=timeout)
            except Exception as e:
                err = 'Failed to make identify query: %r' % query
                log.exception(err)
                return as_unicode(e)

            try:
                raw = response.read().strip()
                #open('E:\\t.html', 'wb').write(raw)
                raw = raw.decode('utf-8', errors='replace')
                if not raw:
                    log.error('Failed to get raw result for query: %r' % query)
                    return
                root = fromstring(clean_ascii_chars(raw))
            except:
                msg = 'Failed to parse goodreads page for query: %r' % query
                log.exception(msg)
                return msg
            # Now grab the first value from the search results, provided the
            # title and authors appear to be for the same book
            self._parse_search_results(log, isbn, title, authors, root, matches, timeout)

        if abort.is_set():
            return

        if not matches:
            if identifiers and title and authors:
                log.info('No matches found with identifiers, retrying using only'
                        ' title and authors')
                return self.identify(log, result_queue, abort, title=title,
                        authors=authors, timeout=timeout)
            log.error('No matches found with query: %r' % query)
            return

        from calibre_plugins.ridibooks.worker import Worker
        workers = [Worker(url, result_queue, br, log, i, self) for i, url in
                enumerate(matches)]

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None

    def _parse_search_results(self, log, isbn, orig_title, orig_authors, root, matches, timeout):
        search_result = root.xpath('//div[@class="book_metadata_wrapper"]')
        if not search_result:
            return
        log.info(search_result[0])
        title_tokens = list(self.get_title_tokens(orig_title))
        author_tokens = list(self.get_author_tokens(orig_authors, True))

        import difflib
        similarities = []
        for i in range(len(search_result)):
            title = search_result[i].xpath('.//span[@class="title_text"]')[0].text_content().strip()
            author = search_result[i].xpath('.//p[@class="book_metadata author "]/a')[0].text_content().strip()
            log.info('Compare %s (%s) with %s (%s)' % (title, author, 
                        ' '.join(title_tokens), 
                        ' '.join(author_tokens)))
            title_similarity = difflib.SequenceMatcher(None, 
                    title.replace(' ', ''), ''.join(title_tokens)).ratio()
            author_similarity = difflib.SequenceMatcher(None, 
                    author.replace(' ', ''), ''.join(author_tokens)).ratio()
            similarities.append(title_similarity * author_similarity)

        matched_node = search_result[similarities.index(max(similarities))]

        if matched_node is None:
            log.error('Rejecting as not close enough match: %s %s' % (title, authors))
            return

        first_result_url = matched_node.xpath('.//a[@class="title_link "]/@href')[0]
        if first_result_url:
            import calibre_plugins.ridibooks.config as cfg
            c = cfg.plugin_prefs[cfg.STORE_NAME]
            matches.append('%s%s' % (RidiBooks.BASE_URL, first_result_url))

    def download_cover(self, log, result_queue, abort,
            title=None, authors=None, identifiers={}, timeout=30):
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                    identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        br = self.browser
        log('Downloading cover from:', cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)


if __name__ == '__main__': # tests
    # To run these test use:
    # calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin,
            title_test, authors_test, series_test)

    test_identify_plugin(RidiBooks.name, [
        (# 정의란 무엇인가
            {
                'identifiers': {'ridibooks': '593000535'}
            },
            [
                title_test(u'정의란 무엇인가', exact=True),
                authors_test([u'마이클 샌델', u'김명철(역자)'])
            ]
        ),

        (# 세상에서 제일 쉬운 회계학
            {
                'title':u'회계학', 
                'authors':[u'구보 유키야']
            },
            [
                title_test(u'세상에서 가장 쉬운 회계학', exact=True),
                authors_test([u'구보 유키야', u'안혜은(역자)'])
            ]
        ),

        (# 테메레르 6권
            {
                'title':u"테메레르 큰바다뱀", 
            },
            [
                title_test(u"테메레르 6권 - 큰바다뱀들의 땅", exact=True),
                authors_test([u'나오미 노빅', u'공보경(역자)']),
                series_test('테메레르', 6.0)
            ]
        ),
    ], fail_missing_meta=False)


