# Copyright 2010, Kovid Goyal <kovid@kovidgoyal.net>
# Copyright 2013 Hardcoded Software (http://www.hardcoded.net)
# 
# This software is licensed under the "GPL v3" License as described in the "LICENSE" file, 
# which should be included with this package. The terms are also available at 
# http://www.hardcoded.net/licenses/gplv3_license

import posixpath
from urllib.parse import urldefrag, urlparse
import logging
from pprint import pformat

import cssutils
from lxml import etree

from ..base import rewrite_links, urlnormalize

class RenameFiles:

    '''
    Rename files and adjust all links pointing to them. Note that the spine
    and manifest are not touched by this transform.
    '''

    def __init__(self, rename_map, renamed_items_map = None):
        self.rename_map = rename_map
        self.renamed_items_map = renamed_items_map

    def __call__(self, oeb):
        self.oeb = oeb

        for item in oeb.manifest.items:
            self.current_item = item
            if etree.iselement(item.data):
                rewrite_links(self.current_item.data, self.url_replacer)
            elif hasattr(item.data, 'cssText'):
                cssutils.replaceUrls(item.data, self.url_replacer)

        if self.oeb.guide:
            for ref in list(self.oeb.guide.values()):
                href = urlnormalize(ref.href)
                href, frag = urldefrag(href)
                replacement = self.rename_map.get(href, None)
                if replacement is not None:
                    nhref = replacement
                    if frag:
                        nhref += '#' + frag
                    ref.href = nhref

        if self.oeb.toc:
            self.fix_toc_entry(self.oeb.toc)

    def fix_toc_entry(self, toc):
        if toc.href:
            href = urlnormalize(toc.href)
            href, frag = urldefrag(href)
            replacement = self.rename_map.get(href, None)

            if replacement is not None:
                nhref = replacement
                if frag:
                    nhref = '#'.join((nhref, frag))
                toc.href = nhref

        for x in toc:
            self.fix_toc_entry(x)

    def url_replacer(self, orig_url):
        url = urlnormalize(orig_url)
        parts = urlparse(url)
        if parts.scheme:
            # Only rewrite local URLs
            return orig_url
        path, frag = urldefrag(url)
        if self.renamed_items_map:
            orig_item = self.renamed_items_map.get(self.current_item.href, self.current_item)
        else:
            orig_item = self.current_item

        href = orig_item.abshref(path)
        replacement = self.current_item.relhref(self.rename_map.get(href, href))
        if frag:
            replacement += '#' + frag
        return replacement

class UniqueFilenames:

    'Ensure that every item in the manifest has a unique filename'

    def __call__(self, oeb):
        self.oeb = oeb

        self.seen_filenames = set([])
        self.rename_map = {}

        for item in list(oeb.manifest.items):
            fname = posixpath.basename(item.href)
            if fname in self.seen_filenames:
                suffix = self.unique_suffix(fname)
                data = item.data
                base, ext = posixpath.splitext(item.href)
                nhref = base + suffix + ext
                nhref = oeb.manifest.generate(href=nhref)[1]
                nitem = oeb.manifest.add(item.id, nhref, item.media_type, data=data,
                        fallback=item.fallback)
                self.seen_filenames.add(posixpath.basename(nhref))
                self.rename_map[item.href] = nhref
                if item.spine_position is not None:
                    oeb.spine.insert(item.spine_position, nitem, item.linear)
                    oeb.spine.remove(item)
                oeb.manifest.remove(item)
            else:
                self.seen_filenames.add(fname)

        if self.rename_map:
            logging.info('Found non-unique filenames, renaming to support broken'
                    ' EPUB readers like FBReader, Aldiko and Stanza...')
            logging.debug(pformat(self.rename_map))

            renamer = RenameFiles(self.rename_map)
            renamer(oeb)


    def unique_suffix(self, fname):
        base, ext = posixpath.splitext(fname)
        c = 0
        while True:
            c += 1
            suffix = '_u%d'%c
            candidate = base + suffix + ext
            if candidate not in self.seen_filenames:
                return suffix

class FlatFilenames:

    'Ensure that every item in the manifest has a unique filename without subdirectories.'

    def __call__(self, oeb):
        self.oeb = oeb

        self.rename_map = {}
        self.renamed_items_map = {}

        for item in list(oeb.manifest.items):
            # Flatten URL by removing directories.
            # Example: a/b/c/index.html -> a_b_c_index.html
            nhref = item.href.replace("/", "_")

            if item.href == nhref:
                # URL hasn't changed, skip item.
                continue

            data = item.data
            nhref = oeb.manifest.generate(href=nhref)[1]
            nitem = oeb.manifest.add(item.id, nhref, item.media_type, data=data,
                                     fallback=item.fallback)
            self.rename_map[item.href] = nhref
            self.renamed_items_map[nhref] = item
            if item.spine_position is not None:
                oeb.spine.insert(item.spine_position, nitem, item.linear)
                oeb.spine.remove(item)
            oeb.manifest.remove(item)

        if self.rename_map:
            logging.info('Found non-flat filenames, renaming to support broken'
                    ' EPUB readers like FBReader...')
            logging.debug(pformat(self.rename_map))
            logging.debug(pformat(self.renamed_items_map))

            renamer = RenameFiles(self.rename_map, self.renamed_items_map)
            renamer(oeb)

