# Copyright 2008, Marshall T. Vandegrift <llasram@gmail.cam> and Kovid Goyal <kovid@kovidgoyal.net>
# Copyright 2013 Hardcoded Software (http://www.hardcoded.net)
# 
# This software is licensed under the "GPL v3" License as described in the "LICENSE" file, 
# which should be included with this package. The terms are also available at 
# http://www.hardcoded.net/licenses/gplv3_license

from collections import defaultdict
import random
import re
from struct import pack
import time
from urllib.parse import urldefrag
from io import BytesIO
import logging
import unicodedata
from uuid import uuid4

from .langcodes import iana2mobi
from .mobiml import MBP_NS
from ..oeb.base import OEB_DOCS
from ..oeb.base import OEB_RASTER_IMAGES
from ..oeb.base import XHTML
from ..oeb.base import XHTML_NS
from ..oeb.base import XML_NS
from ..oeb.base import namespace
from ..oeb.base import prefixname
from ..oeb.base import urlnormalize
from ..compression.palmdoc import compress_doc

INDEXING = True
FCIS_FLIS = True
WRITE_PBREAKS = True

# TODO:
# - Optionally rasterize tables

EXTH_CODES = {
    'creator': 100,
    'publisher': 101,
    'description': 103,
    'identifier': 104,
    'subject': 105,
    'pubdate': 106,
    'date': 106,
    'review': 107,
    'contributor': 108,
    'rights': 109,
    'type': 111,
    'source': 112,
    'title': 503,
    }

RECORD_SIZE = 0x1000

UNCOMPRESSED = 1
PALMDOC = 2
HUFFDIC = 17480

PALM_MAX_IMAGE_SIZE = 63 * 1024
OTHER_MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_THUMB_SIZE = 16 * 1024
MAX_THUMB_DIMEN = (180, 240)


TAGX = {
    'chapter': b'\x00\x00\x00\x01\x01\x01\x01\x00\x02\x01\x02\x00\x03\x01\x04\x00\x04\x01\x08\x00\x00\x00\x00\x01',
    'subchapter': b'\x00\x00\x00\x01\x01\x01\x01\x00\x02\x01\x02\x00\x03\x01\x04\x00\x04\x01\x08\x00\x05\x01\x10\x00\x15\x01\x10\x00\x16\x01\x20\x00\x17\x01\x40\x00\x00\x00\x00\x01',
    'periodical' : b'\x00\x00\x00\x02\x01\x01\x01\x00\x02\x01\x02\x00\x03\x01\x04\x00\x04\x01\x08\x00\x05\x01\x10\x00\x15\x01\x20\x00\x16\x01\x40\x00\x17\x01\x80\x00\x00\x00\x00\x01\x45\x01\x01\x00\x46\x01\x02\x00\x47\x01\x04\x00\x00\x00\x00\x01',
    'secondary_book': b'\x00\x00\x00\x01\x01\x01\x01\x00\x00\x00\x00\x01',
    'secondary_periodical': b'\x00\x00\x00\x01\x01\x01\x01\x00\x0b\x03\x02\x00\x00\x00\x00\x01'
}

INDXT = {
    'chapter': b'\x0f',
    'subchapter': b'\x1f',
    'article': b'\x3f',
    'chapter with subchapters': b'\x6f',
    'periodical': b'\xdf',
    'section': b'\xff',
}

def encode(data):
    return data.encode('utf-8')

def normalize(x):
    if isinstance(x, str):
        x = unicodedata.normalize('NFKC', x)
    return x

# Almost like the one for MS LIT, but not quite.
DECINT_FORWARD = 0
DECINT_BACKWARD = 1
def decint(value, direction):
    # Encode vwi
    result = []
    while True:
        b = value & 0x7f
        value >>= 7
        result.append(b)
        if value == 0:
            break
    if direction == DECINT_FORWARD:
        result[0] |= 0x80
    elif direction == DECINT_BACKWARD:
        result[-1] |= 0x80
    return bytes(reversed(result))

def align_block(raw, multiple=4, pad=b'\0'):
    extra = len(raw) % multiple
    if extra == 0: return raw
    return raw + pad*(multiple - extra)

class Serializer:
    NSRMAP = {'': None, XML_NS: 'xml', XHTML_NS: '', MBP_NS: 'mbp'}

    def __init__(self, oeb, images, write_page_breaks_after_item=True):
        self.oeb = oeb
        self.images = images
        self.write_page_breaks_after_item = write_page_breaks_after_item
        self.id_offsets = {}
        self.href_offsets = defaultdict(list)
        self.breaks = []
        # We need to write to a BytesIO because if we write to a StringIO, our filepos offset will
        # be wrong because of multi-bytes characters.
        self.buffer = BytesIO()
        self._write('<html>')
        self.serialize_head()
        self.serialize_body()
        self._write('</html>')
        self.fixup_links()
    
    def _write(self, s):
        self.buffer.write(encode(s))
    
    def serialize_head(self):
        self._write('<head>')
        if len(self.oeb.guide) > 0:
            self.serialize_guide()
        self._write('</head>')

    def serialize_guide(self):
        hrefs = self.oeb.manifest.hrefs
        self._write('<guide>')
        for ref in list(self.oeb.guide.values()):
            # The Kindle decides where to open a book based on the presence of
            # an item in the guide that looks like
            # <reference type="text" title="Start" href="chapter-one.xhtml"/>
            path = urldefrag(ref.href)[0]
            if path not in hrefs or hrefs[path].media_type not in OEB_DOCS:
                continue

            self._write('<reference type="')
            if ref.type.startswith('other.') :
                self.serialize_text(ref.type.replace('other.',''), quot=True)
            else :
                self.serialize_text(ref.type, quot=True)
            self._write('" ')
            if ref.title is not None:
                self._write('title="')
                self.serialize_text(ref.title, quot=True)
                self._write('" ')
            self.serialize_href(ref.href)
            # Space required or won't work, I kid you not
            self._write(' />')

        self._write('</guide>')

    def serialize_href(self, href, base=None):
        hrefs = self.oeb.manifest.hrefs
        path, frag = urldefrag(urlnormalize(href))
        if path and base:
            path = base.abshref(path)
        if path and path not in hrefs:
            return False
        item = hrefs[path] if path else None
        if item and item.spine_position is None:
            return False
        path = item.href if item else base.href
        href = '#'.join((path, frag)) if frag else path
        self._write('filepos=')
        self.href_offsets[href].append(self.buffer.tell())
        self._write('0000000000')
        return True

    def serialize_body(self):
        self.anchor_offset = self.buffer.tell()
        self._write('<body>')
        self.anchor_offset_kindle = self.buffer.tell()
        spine = [item for item in self.oeb.spine if item.linear]
        spine.extend([item for item in self.oeb.spine if not item.linear])
        for item in spine:
            self.serialize_item(item)
        self._write('</body>')

    def serialize_item(self, item):
        if not item.linear:
            self.breaks.append(self.buffer.tell() - 1)
        self.id_offsets[urlnormalize(item.href)] = self.buffer.tell()
        # Kindle periodical articles are contained in a <div> tag
        self._write('<div>')
        for elem in item.data.find(XHTML('body')):
            self.serialize_elem(elem, item)
        # Kindle periodical article end marker
        self._write('<div></div>')
        if self.write_page_breaks_after_item:
            self._write('<mbp:pagebreak/>')
        self._write('</div>')
        self.anchor_offset = None

    def serialize_elem(self, elem, item, nsrmap=NSRMAP):
        if not isinstance(elem.tag, str) \
            or namespace(elem.tag) not in nsrmap:
                return
        tag = prefixname(elem.tag, nsrmap)
        # Previous layers take care of @name
        id = elem.attrib.pop('id', None)
        if id:
            href = '#'.join((item.href, id))
            offset = self.anchor_offset or self.buffer.tell()
            self.id_offsets[urlnormalize(href)] = offset
        if self.anchor_offset is not None and \
            tag == 'a' and not elem.attrib and \
            not len(elem) and not elem.text:
                return
        self.anchor_offset = self.buffer.tell()
        self._write('<')
        self._write(tag)
        if elem.attrib:
            for attr, val in list(elem.attrib.items()):
                if namespace(attr) not in nsrmap:
                    continue
                attr = prefixname(attr, nsrmap)
                self._write(' ')
                if attr == 'href':
                    if self.serialize_href(val, item):
                        continue
                elif attr == 'src':
                    href = urlnormalize(item.abshref(val))
                    if href in self.images:
                        index = self.images[href]
                        self._write('recindex="%05d"' % index)
                        continue
                self._write(attr)
                self._write('="')
                self.serialize_text(val, quot=True)
                self._write('"')
        self._write('>')
        if elem.text or len(elem) > 0:
            if elem.text:
                self.anchor_offset = None
                self.serialize_text(elem.text)
            for child in elem:
                self.serialize_elem(child, item)
                if child.tail:
                    self.anchor_offset = None
                    self.serialize_text(child.tail)
        self._write('</%s>' % tag)

    def serialize_text(self, text, quot=False):
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('\u00AD', '') # Soft-hyphen
        if quot:
            text = text.replace('"', '&quot;')
        self._write(text)

    def fixup_links(self):
        id_offsets = self.id_offsets
        for href, hoffs in list(self.href_offsets.items()):
            if href not in id_offsets:
                logging.warn('Hyperlink target %r not found', href)
                href, _ = urldefrag(href)
            if href in self.id_offsets:
                ioff = self.id_offsets[href]
                for hoff in hoffs:
                    self.buffer.seek(hoff)
                    self._write('%010d' % ioff)

class MobiWriter:
    COLLAPSE_RE = re.compile(r'[ \t\r\n\v]+')

    def __init__(self, write_page_breaks_after_item=True, dont_compress=False,
            prefer_author_sort=False, mobi_periodical=False, verbose=0):
        self.write_page_breaks_after_item = write_page_breaks_after_item
        self._compression = UNCOMPRESSED if dont_compress else PALMDOC
        # self._imagemax = (PALM_MAX_IMAGE_SIZE if getattr(opts,
        #     'rescale_images', False) else OTHER_MAX_IMAGE_SIZE)
        self._prefer_author_sort = prefer_author_sort
        self._mobi_periodical = mobi_periodical
        self._verbose = verbose
        self._primary_index_record = None
        self._conforming_periodical_toc = False
        self._indexable = False
        self._ctoc = b""
        self._ctoc_records = []
        self._ctoc_offset = 0
        self._ctoc_largest = 0
        self._HTMLRecords = []
        self._tbSequence = b""
        self._MobiDoc = None
        self._anchor_offset_kindle = 0
        self._initialIndexRecordFound = False
        self._firstSectionConcluded = False
        self._currentSectionIndex = 0

    def __call__(self, oeb, path):
        if hasattr(path, 'write'):
            return self._dump_stream(oeb, path)
        with open(path, 'w+b') as stream:
            return self._dump_stream(oeb, stream)

    def _write(self, * data):
        for datum in data:
            self._stream.write(datum)

    def _tell(self):
        return self._stream.tell()

    def _dump_stream(self, oeb, stream):
        self._oeb = oeb
        self._stream = stream
        self._records = [None]
        self._generate_content()
        self._generate_record0()
        self._write_header()
        self._write_content()

    def _generate_content(self):
        self._map_image_names()
        self._generate_text()

        if INDEXING and self._indexable :
            try:
                self._generate_index()
            except:
                logging.exception('Failed to generate index')

        # XXX
        self._first_image_record = []
        # self._generate_images()

    def _map_image_names(self):
        index = 1
        self._images = images = {}
        mh_href = None

        if 'masthead' in self._oeb.guide:
            mh_href = self._oeb.guide['masthead'].href
            images[mh_href] = 1
            index += 1

        for item in list(self._oeb.manifest.values()):
            if item.media_type in OEB_RASTER_IMAGES:
                if item.href == mh_href: continue
                images[item.href] = index
                index += 1

    def _read_text_record(self, text):
        pos = text.tell()
        text.seek(0, 2)
        npos = min((pos + RECORD_SIZE, text.tell()))
        last = b''
        while not last.decode('utf-8', 'ignore'):
            size = len(last) + 1
            text.seek(npos - size)
            last = text.read(size)
        extra = 0
        try:
            last.decode('utf-8')
        except UnicodeDecodeError:
            prev = len(last)
            while True:
                text.seek(npos - prev)
                last = text.read(len(last) + 1)
                try:
                    last.decode('utf-8')
                except UnicodeDecodeError:
                    pass
                else:
                    break
            extra = len(last) - prev
        text.seek(pos)
        data = text.read(RECORD_SIZE)
        overlap = text.read(extra)
        text.seek(npos)
        return data, overlap

    def _generate_flat_indexed_navpoints(self):
        # Assemble a HTMLRecordData instance for each HTML record
        # Return True if valid, False if invalid
        logging.info('Indexing flat navPoints ...')

        numberOfHTMLRecords = ( self._content_length // RECORD_SIZE ) + 1

        # Create a list of HTMLRecordData class instances
        x = numberOfHTMLRecords
        while x:
            self._HTMLRecords.append(HTMLRecordData())
            x -= 1

        toc = self._oeb.toc
        myIndex = 0
        myEndingRecord = 0
        previousOffset = 0
        previousLength = 0
        offset = 0
        length = 0
        entries = list(toc.iter())[1:]

        # Get offset, length per entry
        for (i, child) in enumerate(entries):
            if not child.title or not child.title.strip():
                child.title = "(none)"

            if not child.title or not child.title.strip():
                child.title = "(none)"

            h = child.href
            if h not in self._id_offsets:
                logging.warning('  Could not find TOC entry "%s", aborting indexing ...'% child.title)
                return False
            offset = self._id_offsets[h]

            length = None

            for sibling in entries[i+1:]:
                h2 = sibling.href
                if h2 in self._id_offsets:
                    offset2 = self._id_offsets[h2]
                    if offset2 > offset:
                        length = offset2 - offset
                        break

            if length is None:
                length = self._content_length - offset

            if self._verbose > 3 :
                logging.info("child %03d: %s" % (i, child))
                logging.info("    title: %s" % child.title)
                logging.info("    depth: %d" % child.depth())
                logging.info("   offset: 0x%06X \tlength: 0x%06X \tnext: 0x%06X" % (offset, length, offset + length))

            # Look a gap between chapter nodes.  Don't evaluate periodical or section nodes
            if (i and child.depth() == 1 and entries[i-1].depth() == 1) :
                if offset != previousOffset + previousLength :
                    logging.warning("*** TOC discontinuity ***")
                    logging.warning(" node %03d: '%s' offset: 0x%X length: 0x%X" % \
                        (i-1, entries[i-1].title, previousOffset, previousLength) )
                    logging.warning(" node %03d: '%s' offset: 0x%X != 0x%06X" % \
                        (i, child.title, offset, previousOffset + previousLength) )
                    logging.warning('_generate_flat_indexed_navpoints: Failed to generate index')
                    # Zero out self._HTMLRecords, return False
                    self._HTMLRecords = []
                    #last_name = None
                    return False

            previousOffset = offset
            previousLength = length

            # Calculate the HTML record for this entry
            myStartingRecord = offset // RECORD_SIZE

            # If no one has taken the openingNode slot, it must be us
            if self._HTMLRecords[myStartingRecord].openingNode == -1 :
                self._HTMLRecords[myStartingRecord].openingNode = myIndex

            # Bump the node count for this HTML record
            # Special case if we're the first so we get a true node count
            if self._HTMLRecords[myStartingRecord].currentSectionNodeCount == -1:
                self._HTMLRecords[myStartingRecord].currentSectionNodeCount = 1
            else:
                self._HTMLRecords[myStartingRecord].currentSectionNodeCount += 1

            # Calculate the ending HTMLRecord of this entry
            myEndingRecord = (offset + length) // RECORD_SIZE

            if myEndingRecord > myStartingRecord :
                interimSpanRecord = myStartingRecord + 1
                while interimSpanRecord <= myEndingRecord :
                    self._HTMLRecords[interimSpanRecord].continuingNode = myIndex
                    self._HTMLRecords[interimSpanRecord].currentSectionNodeCount = 1
                    interimSpanRecord += 1
                if self._verbose > 3 : logging.info(" node %03d: %-15.15s... spans HTML records %03d - %03d \t offset: 0x%06X length: 0x%06X" % \
                    (myIndex, child.title if child.title.strip() > "" else "(missing)", myStartingRecord, interimSpanRecord, offset, length) )
            else :
                if self._verbose > 3 : logging.info(" node %03d: %-15.15s... spans HTML records %03d - %03d \t offset: 0x%06X length: 0x%06X" % \
                    (myIndex, child.title if child.title.strip() > "" else "(missing)", myStartingRecord, myStartingRecord, offset, length) )

            myIndex += 1

        # Successfully parsed the entries
        return True

    def _generate_indexed_navpoints(self):
        # Assemble a HTMLRecordData instance for each HTML record
        # Return True if valid, False if invalid
        logging.info('Indexing navPoints ...')

        numberOfHTMLRecords = ( self._content_length // RECORD_SIZE ) + 1

        # Create a list of HTMLRecordData class instances
        x = numberOfHTMLRecords
        while x:
            self._HTMLRecords.append(HTMLRecordData())
            x -= 1

        toc = self._oeb.toc
        myIndex = 0
        myEndingRecord = 0
        previousOffset = 0
        previousLength = 0
        offset = 0
        length = 0
        sectionChangedInRecordNumber = -1
        sectionChangesInThisRecord = False
        entries = list(toc.iter())[1:]

        # Get offset, length per entry
        for (firstSequentialNode, node) in enumerate(list(self._ctoc_map)) :
            if node['klass'] != 'article' and node['klass'] != 'chapter' :
                # Skip periodical and section entries
                continue
            else :
                if self._verbose > 3 :logging.info("\tFirst sequential node: %03d" % firstSequentialNode)
                break

        for i, child in enumerate(entries):
            # Entries continues with a stream of section+articles, section+articles ...
            h = child.href
            if h not in self._id_offsets:
                logging.warning('  Could not find TOC entry "%s", aborting indexing ...'% child.title)
                return False
            offset = self._id_offsets[h]

            length = None

            for sibling in entries[i+1:]:
                h2 = sibling.href
                if h2 in self._id_offsets:
                    offset2 = self._id_offsets[h2]
                    if offset2 > offset:
                        length = offset2 - offset
                        break

            if length is None:
                length = self._content_length - offset

            if self._verbose > 3 :
                logging.info("child %03d: %s" % (i, child))
                logging.info("    title: %s" % child.title)
                logging.info("    depth: %d" % child.depth())
                logging.info("   offset: 0x%06X \tlength: 0x%06X \tnext: 0x%06X" % (offset, length, offset + length))

            # Look a gap between nodes, articles/chapters only, as
            # periodical and section lengths cover spans of articles
            if (i>firstSequentialNode) and self._ctoc_map[i-1]['klass'] != 'section':
                if offset != previousOffset + previousLength :
                    logging.warning("*** TOC discontinuity: nodes are not sequential ***")
                    logging.info(" node %03d: '%s' offset: 0x%X length: 0x%X" % \
                        (i-1, entries[i-1].title, previousOffset, previousLength) )
                    logging.warning(" node %03d: '%s' offset: 0x%X != 0x%06X" % \
                        (i, child.title, offset, previousOffset + previousLength) )
                    # Dump the offending entry
                    logging.info("...")
                    for z in range(i-6 if i-6 > 0 else 0, i+6 if i+6 < len(entries) else len(entries)):
                        if z == i:
                            logging.warning("child %03d: %s" % (z, entries[z]))
                        else:
                            logging.info("child %03d: %s" % (z, entries[z]))
                    logging.info("...")

                    logging.warning('_generate_indexed_navpoints: Failed to generate index')
                    # Zero out self._HTMLRecords, return False
                    self._HTMLRecords = []
                    return False

            previousOffset = offset
            previousLength = length

            # Calculate the HTML record for this entry
            thisRecord = offset // RECORD_SIZE

            # Store the current continuingNodeParent and openingNodeParent
            if self._ctoc_map[i]['klass'] == 'article':
                if thisRecord > 0 :
                    if sectionChangesInThisRecord :         # <<<
                        self._HTMLRecords[thisRecord].continuingNodeParent = self._currentSectionIndex - 1
                    else :
                        self._HTMLRecords[thisRecord].continuingNodeParent = self._currentSectionIndex

            # periodical header?
            if self._ctoc_map[i]['klass'] == 'periodical' :
                # INCREMENT currentSectionNode count
                # Commented out because structured docs don't count section changes in nodeCount
                # compensation at 948 for flat periodicals
                # self._HTMLRecords[thisRecord].currentSectionNodeCount = 1
                continue

            # Is this node a new section?
            if self._ctoc_map[i]['klass'] == 'section' :
                # INCREMENT currentSectionNode count
                # Commented out because structured docs don't count section changes in nodeCount
                # self._HTMLRecords[thisRecord].currentSectionNodeCount += 1

                # *** This should check currentSectionNumber, because content could start late
                if thisRecord > 0:
                    sectionChangesInThisRecord = True
                    #sectionChangesInRecordNumber = thisRecord
                    self._currentSectionIndex += 1
                    self._HTMLRecords[thisRecord].nextSectionNumber = self._currentSectionIndex
                    # The following node opens the nextSection
                    self._HTMLRecords[thisRecord].nextSectionOpeningNode = myIndex
                    continue
                else :
                    continue


            # If no one has taken the openingNode slot, it must be us
            # This could happen before detecting a section change
            if self._HTMLRecords[thisRecord].openingNode == -1 :
                self._HTMLRecords[thisRecord].openingNode = myIndex
                self._HTMLRecords[thisRecord].openingNodeParent = self._currentSectionIndex

            # Bump the nextSection node count while we're in the same record
            if sectionChangedInRecordNumber == thisRecord :
                if self._ctoc_map[i]['klass'] == 'article' :
                    if self._HTMLRecords[thisRecord].nextSectionNodeCount == -1:
                        self._HTMLRecords[thisRecord].nextSectionNodeCount = 1
                    else:
                        self._HTMLRecords[thisRecord].nextSectionNodeCount += 1
                else :
                        # Bump the currentSectionNodeCount one last time
                        self._HTMLRecords[thisRecord].currentSectionNodeCount += 1

            else :
                # Reset the change record
                # sectionChangedInRecordNumber = -1
                sectionChangesInThisRecord = False
                if self._HTMLRecords[thisRecord].currentSectionNodeCount == -1:
                    self._HTMLRecords[thisRecord].currentSectionNodeCount = 1
                else:
                    self._HTMLRecords[thisRecord].currentSectionNodeCount += 1

            # Fill in the spanning records
            myEndingRecord = (offset + length) // RECORD_SIZE
            if myEndingRecord > thisRecord :
                sectionChangesInThisRecord = False
                interimSpanRecord = thisRecord + 1
                while interimSpanRecord <= myEndingRecord :
                    self._HTMLRecords[interimSpanRecord].continuingNode = myIndex

                    self._HTMLRecords[interimSpanRecord].continuingNodeParent = self._currentSectionIndex
                    self._HTMLRecords[interimSpanRecord].currentSectionNodeCount = 1
                    interimSpanRecord += 1

                if self._verbose > 3 :logging.info("     node: %03d %-10.10s %-15.15s... spans HTML records %03d-%03d \t offset: 0x%06X length: 0x%06X" % \
                    (myIndex, self._ctoc_map[i]['klass'], child.title if child.title.strip() > "" else "(missing)", thisRecord, interimSpanRecord, offset, length) )
            elif thisRecord == numberOfHTMLRecords-1:
                # Check for short terminating record (GR provisional)
                if self._HTMLRecords[thisRecord].continuingNode == -1:
                    self._HTMLRecords[thisRecord].continuingNode = self._HTMLRecords[thisRecord].openingNode - 1
            else :
                if self._verbose > 3 : logging.info("     node: %03d %-10.10s %-15.15s... spans HTML records %03d-%03d \t offset: 0x%06X length: 0x%06X" % \
                    (myIndex, self._ctoc_map[i]['klass'], child.title if child.title.strip() > "" else "(missing)", thisRecord, thisRecord, offset, length) )

            myIndex += 1

        # Successfully parsed the entries
        return True

    def _generate_tbs_book(self, nrecords, lastrecord):
        if self._verbose > 3 :logging.info("Assembling TBS for Book: HTML record %03d of %03d" % \
                                    (nrecords, lastrecord) )
        # Variables for trailing byte sequence
        tbsType = 0x00
        tbSequence = b""

        # Generate TBS for type 0x002 - mobi_book
        if self._initialIndexRecordFound == False :

            # Is there any indexed content yet?
            if self._HTMLRecords[nrecords].currentSectionNodeCount == -1 :
                # No indexing data - write vwi length of 1 only
                tbSequence = decint(len(tbSequence) + 1, DECINT_FORWARD)

            else :
                # First indexed HTML record is a special case
                # One or more nodes
                self._initialIndexRecordFound = True
                if self._HTMLRecords[nrecords].currentSectionNodeCount == 1 :
                    tbsType = 2
                else :
                    tbsType = 6

                tbSequence = decint(tbsType, DECINT_FORWARD)
                tbSequence += decint(0x00, DECINT_FORWARD)
                # Don't write a nodecount for opening type 2 record
                if tbsType != 2 :
                    # Check that <> -1
                    tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)

        else :
            # Determine tbsType for indexed HTMLRecords
            if nrecords == lastrecord and self._HTMLRecords[nrecords].currentSectionNodeCount == 1 :
                # Ending record with singleton node
                tbsType = 2

            elif self._HTMLRecords[nrecords].continuingNode > 0 and self._HTMLRecords[nrecords].openingNode == -1 :
                # This is a span-only record
                tbsType = 3
                # Zero out the nodeCount with a pre-formed vwi
                self._HTMLRecords[nrecords].currentSectionNodeCount = 0x80

            else :
                tbsType = 6


            # Shift the openingNode index << 3
            shiftedNCXEntry = self._HTMLRecords[nrecords].continuingNode << 3
            # Add the TBS type
            shiftedNCXEntry |= tbsType

            # Assemble the TBS
            tbSequence = decint(shiftedNCXEntry, DECINT_FORWARD)
            tbSequence += decint(0x00, DECINT_FORWARD)
            # Don't write a nodecount for terminating type 2 record
            if tbsType != 2 :
                tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])
            tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)

        self._tbSequence = tbSequence

    def _generate_tbs_flat_periodical(self, nrecords, lastrecord):
        # Flat periodicals <0x102> have a single section for all articles
        # Structured periodicals <0x101 | 0x103> have one or more sections with articles
        # The first section TBS sequence is different for Flat and Structured
        # This function is called once per HTML record

        # Variables for trailing byte sequence
        tbsType = 0x00
        tbSequence = ""

        # Generate TBS for type 0x102 - mobi_feed - flat periodical
        if self._initialIndexRecordFound == False :
            # Is there any indexed content yet?
            if self._HTMLRecords[nrecords].currentSectionNodeCount == -1 :
                # No indexing data - write vwi length of 1 only
                tbSequence = decint(len(tbSequence) + 1, DECINT_FORWARD)

            else :
                # First indexed record: Type 6 with nodeCount only
                self._initialIndexRecordFound = True
                tbsType = 6
                tbSequence = decint(tbsType, DECINT_FORWARD)
                tbSequence += decint(0x00, DECINT_FORWARD)
                # nodeCount = 0xDF + 0xFF + n(0x3F) - need to add 2 because we didn't count them earlier
                tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount + 2])
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)
                if self._verbose > 2 :
                    logging.info("\nAssembling TBS for Flat Periodical: HTML record %03d of %03d, section %d" % \
                        (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent ) )
                    self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

        else :
            # An HTML record with nextSectionNumber = -1 has no section change in this record
            # Default for flat periodicals with only one section
            if self._verbose > 2 :
                logging.info("\nAssembling TBS for Flat Periodical: HTML record %03d of %03d, section %d" % \
                    (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent ) )
                self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

            # First section has different Type values
            # Determine tbsType for HTMLRecords > 0
            if nrecords == lastrecord and self._HTMLRecords[nrecords].currentSectionNodeCount == 1 :
                # Ending record with singleton node
                tbsType = 6

                # Assemble the Type 6 TBS
                tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                tbSequence += bytes([2])                                                    # arg2 = 0x02

                # Assemble arg3 - (article index +1) << 4  +  flag: 1 = article spans this record
                arg3 = self._HTMLRecords[nrecords].continuingNode
                arg3 += 1
                arg3 <<= 4
                arg3 |= 0x0                                                             #flags = 0
                tbSequence += decint(arg3, DECINT_FORWARD)                              # arg3


                # tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

            elif self._HTMLRecords[nrecords].continuingNode > 0 and self._HTMLRecords[nrecords].openingNode == -1 :
                # This is a span-only record
                tbsType = 6
                # Zero out the nodeCount with a pre-formed vwi
                self._HTMLRecords[nrecords].currentSectionNodeCount = 0x80

                # Assemble the Type 6 TBS
                tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                tbSequence += bytes([2])                                                    # arg2 = 0x02
                # Assemble arg3 - article index << 3  +  flag: 1 = article spans this record
                arg3 = self._HTMLRecords[nrecords].continuingNode
                # Add the index of the openingNodeParent to get the offset start
                # We know that section 0 is at position 1, section 1 at index 2, etc.
                arg3 += self._HTMLRecords[nrecords].continuingNodeParent + 1
                arg3 <<= 4
                arg3 |= 0x01
                tbSequence += decint(arg3, DECINT_FORWARD)                              # arg3
                tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

            else :
                tbsType = 7
                # Assemble the Type 7 TBS
                tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                tbSequence += bytes([2])                                                    # arg2 = 0x02
                tbSequence += decint(0x00, DECINT_FORWARD)                              # arg3 = 0x80
                # Assemble arg4 - article index << 4  +  flag: 1 = article spans this record
                arg4 = self._HTMLRecords[nrecords].continuingNode
                # Add the index of the openingNodeParent to get the offset start
                # We know that section 0 is at position 1, section 1 at index 2, etc.
                arg4 += self._HTMLRecords[nrecords].continuingNodeParent + 1
                arg4 <<= 4
                arg4 |= 0x04                                                            # 4: multiple nodes
                tbSequence += decint(arg4, DECINT_FORWARD)                              # arg4
                tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

        self._tbSequence = tbSequence

    def _generate_tbs_structured_periodical(self, nrecords, lastrecord):
        # Structured periodicals <0x101 | 0x103> have one or more sections for all articles
        # The first section TBS sequences is different for Flat and Structured
        # This function is called once per HTML record

        # Variables for trailing byte sequence
        tbsType = 0x00
        tbSequence = ""

        # Generate TBS for type 0x101/0x103 - structured periodical
        if self._initialIndexRecordFound == False :
            # Is there any indexed content yet?
            if self._HTMLRecords[nrecords].currentSectionNodeCount == -1 :
                # No indexing data - write vwi length of 1 only
                tbSequence = decint(len(tbSequence) + 1, DECINT_FORWARD)

            else :
                self._initialIndexRecordFound = True

                if self._verbose > 2 :
                    logging.info("\nAssembling TBS for Structured Periodical: HTML record %03d of %03d, section %d" % \
                        (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent ) )
                    self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

                # First record only
                tbsType = 6
                # Assemble the Type 6 TBS
                tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                tbSequence += bytes([2])                                                    # arg2 = 0x02
                # Assemble arg3: (section jump + article index) << 4  +  flag: 1 = article spans this record
                arg3 = self._sectionCount                      # Jump over the section group
                arg3 += 0                                      # First article index = 0
                arg3 <<= 4
                arg3 |= 0x04
                tbSequence += decint(arg3, DECINT_FORWARD)                              # arg3

                # Structured periodicals don't count periodical, section in nodeCount
                tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len
        else :
            if self._firstSectionConcluded == False :
                # Use type 6 & 7 until first section switch, then 2

                if self._HTMLRecords[nrecords].nextSectionNumber == -1 :
                    # An HTML record with nextSectionNumber = -1 has no section change in this record
                    if self._verbose > 2 :
                        logging.info("\nAssembling TBS for Structured Periodical: HTML record %03d of %03d, section %d" % \
                            (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent ) )
                        self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

                    # First section has different Type values
                    # Determine tbsType for HTMLRecords > 0
                    if nrecords == lastrecord and self._HTMLRecords[nrecords].currentSectionNodeCount == 1 :
                        # Ending record with singleton node
                        tbsType = 6

                        # Assemble the Type 6 TBS
                        tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                        tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                        tbSequence += bytes([2])                                                    # arg2 = 0x02
                        # Assemble arg3: (section jump + article index) << 4  +  flag: 1 = article spans this record
                        arg3 = self._sectionCount
                        arg3 += self._HTMLRecords[nrecords].continuingNode
                        arg3 <<= 4
                        arg3 |= 0x04
                        tbSequence += decint(arg3, DECINT_FORWARD)                              # arg3
                        tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                        tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

                    elif self._HTMLRecords[nrecords].continuingNode > 0 and self._HTMLRecords[nrecords].openingNode == -1 :
                        # This is a span-only record
                        tbsType = 6
                        # Zero out the nodeCount with a pre-formed vwi
                        self._HTMLRecords[nrecords].currentSectionNodeCount = 0x80

                        # Assemble the Type 6 TBS
                        tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                        tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                        tbSequence += bytes([2])                                                    # arg2 = 0x02
                        # Assemble arg3: (section jump + article index) << 4  +  flag: 1 = article spans this record
                        arg3 = self._sectionCount
                        arg3 += self._HTMLRecords[nrecords].continuingNode
                        arg3 <<= 4
                        arg3 |= 0x01
                        tbSequence += decint(arg3, DECINT_FORWARD)                              # arg3
                        tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                        tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

                    else :
                        tbsType = 7
                        # Assemble the Type 7 TBS
                        tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                        tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                        tbSequence += bytes([2])                                                    # arg2 = 0x02
                        tbSequence += decint(0x00, DECINT_FORWARD)                              # arg3 = 0x80
                        # Assemble arg4: (section jump + article index) << 4  +  flag: 1 = article spans this record
                        arg4 = self._sectionCount
                        arg4 += self._HTMLRecords[nrecords].continuingNode
                        arg4 <<= 4
                        arg4 |= 0x04                                                            # 4: multiple nodes
                        tbSequence += decint(arg4, DECINT_FORWARD)                              # arg4
                        tbSequence += bytes([self._HTMLRecords[nrecords].currentSectionNodeCount])  # nodeCount
                        tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len


                # Initial section switch from section 1
                elif self._HTMLRecords[nrecords].nextSectionNumber > 0 :
                    tbsType = 3

                    if self._verbose > 2 :
                        logging.info("\nAssembling TBS for Structured Periodical: HTML record %03d of %03d, switching sections %d-%d" % \
                        (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent, self._HTMLRecords[nrecords].nextSectionNumber) )
                        self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

                    tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                    tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                    tbSequence += decint(0x00, DECINT_FORWARD)                              # arg2 = 0x80

                    # Assemble arg3: Upper nybble: ending section index
                    #                Lower nybble = flags for next section - 0 or 1
                    arg3 = (self._HTMLRecords[nrecords].continuingNodeParent + 1) << 4
                    arg3Flags = 0               # 0: has nodes?
                    arg3 |= arg3Flags
                    tbSequence += decint(arg3, DECINT_FORWARD)

                    # Assemble arg4: Upper nybble: continuingNode << 4
                    #                Lower nybble: flag: 0 = no starting nodes from previous section
                    #                              flag: 4 = starting nodes from previous section

                    sectionBase = self._HTMLRecords[nrecords].continuingNodeParent
                    sectionDelta = self._sectionCount - sectionBase - 1
                    articleOffset = self._HTMLRecords[nrecords].continuingNode + 1
                    arg4 = (sectionDelta + articleOffset) << 4

                    arg4Flags = 0
                    if self._HTMLRecords[nrecords].currentSectionNodeCount > 1 :
                        arg4Flags = 4
                    else :
                        arg4Flags = 0
                    arg4 |= arg4Flags
                    tbSequence += decint(arg4, DECINT_FORWARD)                              # arg4

                    # Write optional 4a if previous section node count > 1
                    if arg4Flags == 4 :                                                      # arg4a
                        nodeCountValue = self._HTMLRecords[nrecords].currentSectionNodeCount
                        nodeCountValue = 0x80 if nodeCountValue == 0 else nodeCountValue
                        tbSequence += bytes([nodeCountValue])

                    # Write article2: not completely understood
                    arg5 = sectionDelta + articleOffset
                    if self._HTMLRecords[nrecords].currentSectionNodeCount < 2:
                        arg5 -= 1
                    arg5 <<= 4
                    arg5Flags = 8
                    arg5 |= arg5Flags
                    tbSequence += decint(arg5, DECINT_FORWARD)                              # arg5

                    # Write first article of new section
                    #arg6 = self._sectionCount - 1                   # We're now into the following section
                    #arg6 = self._HTMLRecords[nrecords].nextSectionNumber
                    arg6 = sectionDelta + self._HTMLRecords[nrecords].nextSectionOpeningNode
                    arg6 <<= 4
                    if self._HTMLRecords[nrecords].nextSectionNodeCount > 1 :
                        arg6Flags = 4
                    else :
                        arg6Flags = 0
                    arg6 |= arg6Flags
                    tbSequence += decint(arg6, DECINT_FORWARD)                              # arg5

                    # Write optional 6a if previous section node count > 1
                    if arg6Flags == 4 :                                                      # arg4a
                        nodeCountValue = self._HTMLRecords[nrecords].nextSectionNodeCount
                        nodeCountValue = 0x80 if nodeCountValue == 0 else nodeCountValue
                        tbSequence += bytes([nodeCountValue])

                    tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

                    self._firstSectionConcluded = True
            else :
                # After first section switch, use types 2 and 3
                if self._HTMLRecords[nrecords].nextSectionNumber == -1 :
                    if self._verbose > 2 :
                        logging.info("\nAssembling TBS for Structured Periodical: HTML record %03d of %03d, section %d" % \
                            (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent ) )
                        self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

                    tbsType = 2
                    tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                    tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                    arg2 = self._HTMLRecords[nrecords].continuingNodeParent + 1
                    arg2 <<= 4
                    # Add flag = 1 if there are multiple nodes in this record
                    arg2Flags = 0
                    if self._HTMLRecords[nrecords].currentSectionNodeCount > 0 :
                        arg2Flags = 1
                        arg2 |= arg2Flags
                    tbSequence += decint(arg2, DECINT_FORWARD)

                    if arg2Flags :
                        # Add an extra vwi 0x00
                        tbSequence += decint(0x00, DECINT_FORWARD)                          # arg2Flags = 0x80

                    # arg3 - offset of continuingNode from sectionParent
                    arg3 =  self._sectionCount - self._HTMLRecords[nrecords].continuingNodeParent        # Total guess
                    arg3 += self._HTMLRecords[nrecords].continuingNode
                    arg3 <<= 4
                    arg3Flags = 1
                    if self._HTMLRecords[nrecords].currentSectionNodeCount > 0 :
                        arg3Flags = 4
                    arg3 |= arg3Flags
                    tbSequence += decint(arg3, DECINT_FORWARD)

                    if arg3Flags == 4 :
                        nodeCountValue = self._HTMLRecords[nrecords].currentSectionNodeCount
                        nodeCountValue = 0x80 if nodeCountValue == 0 else nodeCountValue
                        tbSequence += bytes([nodeCountValue])
                    else :
                        tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80

                    tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

                else :
                    # Section switch when section > 1
                    tbsType = 3

                    if self._verbose > 2 :
                        logging.info("\nAssembling TBS for Structured Periodical: HTML record %03d of %03d, switching sections %d-%d" % \
                        (nrecords, lastrecord, self._HTMLRecords[nrecords].continuingNodeParent, self._HTMLRecords[nrecords].nextSectionNumber) )
                        self._HTMLRecords[nrecords].dumpData(nrecords, self._oeb)

                    tbSequence = decint(tbsType, DECINT_FORWARD)                            # Type
                    tbSequence += decint(0x00, DECINT_FORWARD)                              # arg1 = 0x80
                    tbSequence += decint(0x00, DECINT_FORWARD)                              # arg2 = 0x80

                    # arg3: continuingNodeParent section
                    #   Upper nybble: ending section index
                    #   Lower nybble = flags for next section - 0 or 1
                    arg3 = (self._HTMLRecords[nrecords].continuingNodeParent + 1) << 4
                    arg3Flags = 0               # 0: has nodes?
                    arg3 |= arg3Flags
                    tbSequence += decint(arg3, DECINT_FORWARD)

                    # Assemble arg4: Upper nybble: continuingNode << 4
                    #                Lower nybble: flag: 0 = no starting nodes from previous section
                    #                              flag: 4 = starting nodes from previous section
                    sectionBase = self._HTMLRecords[nrecords].continuingNodeParent
                    sectionDelta = self._sectionCount - sectionBase - 1
                    articleOffset = self._HTMLRecords[nrecords].continuingNode + 1
                    arg4 = (sectionDelta + articleOffset) << 4

                    arg4Flags = 0
                    if self._HTMLRecords[nrecords].currentSectionNodeCount > 1 :
                        arg4Flags = 4
                    else :
                        arg4Flags = 0
                    arg4 |= arg4Flags
                    tbSequence += decint(arg4, DECINT_FORWARD)                              # arg4

                    # Write optional 4a if previous section node count > 1
                    if arg4Flags == 4 :                                                      # arg4a
                        nodeCountValue = self._HTMLRecords[nrecords].currentSectionNodeCount
                        nodeCountValue = 0x80 if nodeCountValue == 0 else nodeCountValue
                        tbSequence += bytes([nodeCountValue])

                    # Write article2: not completely understood
                    arg5 = sectionDelta + articleOffset
                    if self._HTMLRecords[nrecords].currentSectionNodeCount < 2:
                        arg5 -= 1
                    arg5 <<= 4
                    arg5Flags = 8
                    arg5 |= arg5Flags
                    tbSequence += decint(arg5, DECINT_FORWARD)                              # arg5

                    # Write first article of new section
                    arg6 = sectionDelta + self._HTMLRecords[nrecords].nextSectionOpeningNode
                    arg6 <<= 4
                    if self._HTMLRecords[nrecords].nextSectionNodeCount > 1 :
                        arg6Flags = 4
                    else :
                        arg6Flags = 0
                    arg6 |= arg6Flags
                    tbSequence += decint(arg6, DECINT_FORWARD)                              # arg5

                    # Write optional 6a if previous section node count > 1
                    if arg6Flags == 4 :                                                      # arg4a
                        nodeCountValue = self._HTMLRecords[nrecords].nextSectionNodeCount
                        nodeCountValue = 0x80 if nodeCountValue == 0 else nodeCountValue
                        tbSequence += bytes([nodeCountValue])

                    tbSequence += decint(len(tbSequence) + 1, DECINT_FORWARD)               # len

        self._tbSequence = tbSequence

    def _evaluate_periodical_toc(self):
        '''
        Periodical:
        <navMap>                            depth=4
          <navPoint class="periodical">     depth=3     1
            <navPoint class="section">      depth=2     1 or more
              <navPoint class="article">    depth=1     multiple
        Book:
        <navMap>                            depth=2
          <navPoint [class="chapter"|None]> depth=1     multiple
        '''
        toc = self._oeb.toc
        nodes = list(toc.iter())[1:]
        toc_conforms = True
        for child in nodes:
            if child.klass == "periodical" and child.depth() != 3 or    \
               child.klass == "section" and child.depth() != 2 or       \
               child.klass == "article" and child.depth() != 1 :

                logging.warn('Nonconforming TOC entry: "%s" found at depth %d' % \
                        (child.klass, child.depth()) )
                logging.warn("  <title>: '%-25.25s...' \t\tklass=%-15.15s \tdepth:%d  \tplayOrder=%03d" % \
                        (child.title, child.klass, child.depth(), child.play_order) )
                toc_conforms = False

        # We also need to know that we have a pubdate or timestamp in the metadata, which the Kindle needs
        if self._oeb.metadata['date'] == [] and self._oeb.metadata['timestamp'] == [] :
            logging.info('metadata missing date/timestamp')
            toc_conforms = False

        if not 'masthead' in self._oeb.guide :
            logging.info('mastheadImage missing from manifest')
            toc_conforms = False

        logging.info("  TOC structure conforms" if toc_conforms else "  TOC structure non-conforming")
        return toc_conforms

    def _generate_text(self):
        logging.info('Serializing markup content...')
        serializer = Serializer(self._oeb, self._images,
                write_page_breaks_after_item=self.write_page_breaks_after_item)
        breaks = serializer.breaks
        text = serializer.buffer.getvalue()
        self._anchor_offset_kindle = serializer.anchor_offset_kindle
        self._id_offsets = serializer.id_offsets
        self._content_length = len(text)
        self._text_length = len(text)
        text = BytesIO(text)
        buf = []
        nrecords = 0
        lastrecord = (self._content_length // RECORD_SIZE )
        offset = 0

        if self._compression != UNCOMPRESSED:
            logging.info('  Compressing markup content...')
        data, overlap = self._read_text_record(text)

        if not self._mobi_periodical:
            self._flatten_toc()

        # Evaluate toc for conformance
        if self._mobi_periodical:
            logging.info('  MOBI periodical specified, evaluating TOC for periodical conformance ...')
            self._conforming_periodical_toc = self._evaluate_periodical_toc()

        # This routine decides whether to build flat or structured based on self._conforming_periodical_toc
        # self._ctoc = self._generate_ctoc()

        # There may be multiple CNCX records built below, but the last record is returned and should be stored
        self._ctoc_records.append(self._generate_ctoc())

        # Build the HTMLRecords list so we can assemble the trailing bytes sequences in the following while loop
        toc = self._oeb.toc
        entries = list(toc.iter())[1:]

        if len(entries):
            self._indexable = self._generate_indexed_navpoints()
        else:
            logging.info('  No entries found in TOC ...')
            self._indexable = False

        if not self._indexable:
            logging.info('  Writing unindexed mobi ...')

        while len(data) > 0:
            if self._compression == PALMDOC:
                data = compress_doc(data)
            record = BytesIO()
            record.write(data)
            # Write trailing muti-byte sequence if any
            record.write(overlap)
            record.write(pack(b'>B', len(overlap)))

            if WRITE_PBREAKS :
                nextra = 0
                pbreak = 0
                running = offset
                while breaks and (breaks[0] - offset) < RECORD_SIZE:
                    # .pop returns item, removes it from list
                    pbreak = (breaks.pop(0) - running) >> 3
                    if self._verbose > 2 :
                        logging.info('pbreak = 0x%X at 0x%X' % (pbreak, record.tell()) )
                    encoded = decint(pbreak, DECINT_FORWARD)
                    record.write(encoded)
                    running += pbreak << 3
                    nextra += len(encoded)
                lsize = 1
                while True:
                    size = decint(nextra + lsize, DECINT_BACKWARD)
                    if len(size) == lsize:
                        break
                    lsize += 1
                record.write(size)

            # Write Trailing Byte Sequence
            if INDEXING and self._indexable:
                # Dispatch to different TBS generators based upon publication type
                booktype = self._MobiDoc.mobiType
                if booktype == 0x002 :
                    self._generate_tbs_book(nrecords, lastrecord)
                elif booktype == 0x102 :
                    self._generate_tbs_flat_periodical(nrecords, lastrecord)
                elif booktype == 0x101 or booktype == 0x103 :
                    self._generate_tbs_structured_periodical(nrecords, lastrecord)
                else :
                    raise NotImplementedError('Indexing for mobitype 0x%X not implemented' % booktype)

                # Write the sequence
                record.write(self._tbSequence)

            self._records.append(record.getvalue())
            buf.append(self._records[-1])
            nrecords += 1
            offset += RECORD_SIZE
            data, overlap = self._read_text_record(text)

        if INDEXING:
            extra = sum(map(len, buf))%4
            if extra == 0:
                extra = 4
            self._records.append(b'\0'*(4-extra))
            nrecords += 1
        self._text_nrecords = nrecords

    # def _generate_images(self):
    #     logging.info('Serializing images...')
    #     images = [(index, href) for href, index in self._images.items()]
    #     images.sort()
    #     self._first_image_record = None
    #     for _, href in images:
    #         item = self._oeb.manifest.hrefs[href]
    #         try:
    #             data = rescale_image(item.data, self._imagemax)
    #         except:
    #             self._oeb.logger.warn('Bad image file %r' % item.href)
    #             continue
    #         finally:
    #             item.unload_data_from_memory()
    #         self._records.append(data)
    #         if self._first_image_record is None:
    #             self._first_image_record = len(self._records)-1

    def _generate_end_records(self):
        if FCIS_FLIS:
            # This adds the binary blobs of FLIS and FCIS, which don't seem to be necessary
            self._flis_number = len(self._records)
            self._records.append(
            b'FLIS\0\0\0\x08\0\x41\0\0\0\0\0\0\xff\xff\xff\xff\0\x01\0\x03\0\0\0\x03\0\0\0\x01'+
            b'\xff'*4)
            fcis = b'FCIS\x00\x00\x00\x14\x00\x00\x00\x10\x00\x00\x00\x01\x00\x00\x00\x00'
            fcis += pack(b'>I', self._text_length)
            fcis += b'\x00\x00\x00\x00\x00\x00\x00\x20\x00\x00\x00\x08\x00\x01\x00\x01\x00\x00\x00\x00'
            self._fcis_number = len(self._records)
            self._records.append(fcis)
            self._records.append(b'\xE9\x8E\x0D\x0A')
        else:
            self._flis_number = len(self._records)
            self._records.append(b'\xE9\x8E\x0D\x0A')

    def _generate_record0(self):
        metadata = self._oeb.metadata
        exth = self._build_exth()
        last_content_record = len(self._records) - 1

        # if INDEXING and self._indexable:
        #     self._generate_end_records()
        self._generate_end_records()

        record0 = BytesIO()
        # The PalmDOC Header
        record0.write(pack(b'>HHIHHHH', self._compression, 0,
            self._text_length,
            self._text_nrecords-1, RECORD_SIZE, 0, 0)) # 0 - 15 (0x0 - 0xf)
        uid = random.randint(0, 0xffffffff)
        title = normalize(str(metadata.title[0])).encode('utf-8')
        # The MOBI Header

        # 0x0 - 0x3
        record0.write(b'MOBI')

        # 0x4 - 0x7   : Length of header
        # 0x8 - 0x11  : MOBI type
        #   type    meaning
        #   0x002   MOBI book (chapter - chapter navigation)
        #   0x101   News - Hierarchical navigation with sections and articles
        #   0x102   News feed - Flat navigation
        #   0x103   News magazine - same as 0x101
        # 0xC - 0xF   : Text encoding (65001 is utf-8)
        # 0x10 - 0x13 : UID
        # 0x14 - 0x17 : Generator version

        btype = self._MobiDoc.mobiType

        record0.write(pack(b'>IIIII',
            0xe8, btype, 65001, uid, 6))

        # 0x18 - 0x1f : Unknown
        record0.write(b'\xff' * 8)


        # 0x20 - 0x23 : Secondary index record
        if btype < 0x100 :
            record0.write(pack(b'>I', 0xffffffff))
        elif btype > 0x100 and self._indexable :
            if self._primary_index_record is None:
                record0.write(pack(b'>I', 0xffffffff))
            else:
                record0.write(pack(b'>I', self._primary_index_record + 2 + len(self._ctoc_records)))
        else :
            record0.write(pack(b'>I', 0xffffffff))

        # 0x24 - 0x3f : Unknown
        record0.write(b'\xff' * 28)

        # 0x40 - 0x43 : Offset of first non-text record
        record0.write(pack(b'>I',
            self._text_nrecords + 1))

        # 0x44 - 0x4b : title offset, title length
        record0.write(pack(b'>II',
            0xe8 + 16 + len(exth), len(title)))

        # 0x4c - 0x4f : Language specifier
        record0.write(iana2mobi(
            str(metadata.language[0])))

        # 0x50 - 0x57 : Unknown
        record0.write(b'\0' * 8)

        # 0x58 - 0x5b : Format version
        # 0x5c - 0x5f : First image record number
        record0.write(pack(b'>II',
            6, self._first_image_record if self._first_image_record else 0))

        # 0x60 - 0x63 : First HUFF/CDIC record number
        # 0x64 - 0x67 : Number of HUFF/CDIC records
        # 0x68 - 0x6b : First DATP record number
        # 0x6c - 0x6f : Number of DATP records
        record0.write(b'\0' * 16)

        # 0x70 - 0x73 : EXTH flags
        record0.write(pack(b'>I', 0x50))

        # 0x74 - 0x93 : Unknown
        record0.write(b'\0' * 32)

        # 0x94 - 0x97 : DRM offset
        # 0x98 - 0x9b : DRM count
        # 0x9c - 0x9f : DRM size
        # 0xa0 - 0xa3 : DRM flags
        record0.write(pack(b'>IIII',
            0xffffffff, 0xffffffff, 0, 0))


        # 0xa4 - 0xaf : Unknown
        record0.write(b'\0'*12)

        # 0xb0 - 0xb1 : First content record number
        # 0xb2 - 0xb3 : last content record number
        # (Includes Image, DATP, HUFF, DRM)
        record0.write(pack(b'>HH', 1, last_content_record))

        # 0xb4 - 0xb7 : Unknown
        record0.write(b'\0\0\0\x01')

        # 0xb8 - 0xbb : FCIS record number
        if FCIS_FLIS:
            # Write these if FCIS/FLIS turned on
            # 0xb8 - 0xbb : FCIS record number
            record0.write(pack(b'>I', self._fcis_number))

            # 0xbc - 0xbf : Unknown (FCIS record count?)
            record0.write(pack(b'>I', 1))

            # 0xc0 - 0xc3 : FLIS record number
            record0.write(pack(b'>I', self._flis_number))

            # 0xc4 - 0xc7 : Unknown (FLIS record count?)
            record0.write(pack(b'>I', 1))
        else:
            # 0xb8 - 0xbb : FCIS record number
            record0.write(pack(b'>I', 0xffffffff))

            # 0xbc - 0xbf : Unknown (FCIS record count?)
            record0.write(pack(b'>I', 0xffffffff))

            # 0xc0 - 0xc3 : FLIS record number
            record0.write(pack(b'>I', 0xffffffff))

            # 0xc4 - 0xc7 : Unknown (FLIS record count?)
            record0.write(pack(b'>I', 1))

        # 0xc8 - 0xcf : Unknown
        record0.write(b'\0'*8)

        # 0xd0 - 0xdf : Unknown
        record0.write(pack(b'>IIII', 0xffffffff, 0, 0xffffffff, 0xffffffff))

        # 0xe0 - 0xe3 : Extra record data
        # Extra record data flags:
        #   - 0x1: <extra multibyte bytes><size> (?)
        #   - 0x2: <TBS indexing description of this HTML record><size> GR
        #   - 0x4: <uncrossable breaks><size>
        # GR: Use 7 for indexed files, 5 for unindexed
        # Setting bit 2 (0x4) disables <guide><reference type="start"> functionality

        trailingDataFlags = 1
        if self._indexable:
            trailingDataFlags |= 2
        if WRITE_PBREAKS:
            trailingDataFlags |= 4
        record0.write(pack(b'>I', trailingDataFlags))

        # 0xe4 - 0xe7 : Primary index record
        record0.write(pack(b'>I', 0xffffffff if self._primary_index_record is
            None else self._primary_index_record))

        record0.write(exth)
        record0.write(title)
        record0 = record0.getvalue()
        self._records[0] = record0 + (b'\0' * (1024*8))

    def _build_exth(self):
        oeb = self._oeb
        exth = BytesIO()
        nrecs = 0
        for term in oeb.metadata:
            if term not in EXTH_CODES:
                continue
            code = EXTH_CODES[term]
            items = oeb.metadata[term]
            if term == 'creator':
                if self._prefer_author_sort:
                    creators = [normalize(str(c.file_as or c)) for c in items]
                else:
                    creators = [normalize(str(c)) for c in items]
                items = ['; '.join(creators)]
            for item in items:
                data = self.COLLAPSE_RE.sub(' ', normalize(str(item)))
                if term == 'identifier':
                    if data.lower().startswith('urn:isbn:'):
                        data = data[9:]
                    elif item.scheme.lower() == 'isbn':
                        pass
                    else:
                        continue
                data = data.encode('utf-8')
                exth.write(pack(b'>II', code, len(data) + 8))
                exth.write(data)
                nrecs += 1
            if term == 'rights' :
                try:
                    rights = normalize(str(oeb.metadata.rights[0])).encode('utf-8')
                except:
                    rights = 'Unknown'
                exth.write(pack(b'>II', EXTH_CODES['rights'], len(rights) + 8))
                exth.write(rights)
                nrecs += 1

        # Write UUID as ASIN
        uuid = None
        from ..oeb.base import OPF
        for x in oeb.metadata['identifier']:
            if x.get(OPF('scheme'), None).lower() == 'uuid' or str(x).startswith('urn:uuid:'):
                uuid = str(x).split(':')[-1]
                break
        if uuid is None:
            uuid = str(uuid4())

        if isinstance(uuid, str):
            uuid = uuid.encode('utf-8')
        exth.write(pack(b'>II', 113, len(uuid) + 8))
        exth.write(uuid)
        nrecs += 1

        # Write cdetype
        if not self._mobi_periodical:
            data = b'EBOK'
            exth.write(pack(b'>II', 501, len(data)+8))
            exth.write(data)
            nrecs += 1

        # Add a publication date entry
        if oeb.metadata['date'] != [] :
            datestr = encode(str(oeb.metadata['date'][0]))
        elif oeb.metadata['timestamp'] != [] :
            datestr = encode(str(oeb.metadata['timestamp'][0]))

        if datestr is not None:
            exth.write(pack(b'>II',EXTH_CODES['pubdate'], len(datestr) + 8))
            exth.write(datestr)
            nrecs += 1
        else:
            raise NotImplementedError("missing date or timestamp needed for mobi_periodical")

        if oeb.metadata.cover and str(oeb.metadata.cover[0]) in oeb.manifest.ids:
            id = str(oeb.metadata.cover[0])
            item = oeb.manifest.ids[id]
            href = item.href
            # if href in self._images:
            #     index = self._images[href] - 1
            #     exth.write(pack(b'>III', 0xc9, 0x0c, index))
            #     exth.write(pack(b'>III', 0xcb, 0x0c, 0))
            #     nrecs += 2
            #     index = self._add_thumbnail(item)
            #     if index is not None:
            #         exth.write(pack(b'>III', 0xca, 0x0c, index - 1))
            #         nrecs += 1

        exth = exth.getvalue()
        trail = len(exth) % 4
        pad = b'\0' * (4 - trail) # Always pad w/ at least 1 byte
        exth = [b'EXTH', pack(b'>II', len(exth) + 12, nrecs), exth, pad]
        return b''.join(exth)

    # def _add_thumbnail(self, item):
    #     try:
    #         data = rescale_image(item.data, MAX_THUMB_SIZE, MAX_THUMB_DIMEN)
    #     except IOError:
    #         logging.warn('Bad image file %r' % item.href)
    #         return None
    #     manifest = self._oeb.manifest
    #     id, href = manifest.generate('thumbnail', 'thumbnail.jpeg')
    #     manifest.add(id, href, 'image/jpeg', data=data)
    #     index = len(self._images) + 1
    #     self._images[href] = index
    #     self._records.append(data)
    #     return index

    def _write_header(self):
        title = encode(str(self._oeb.metadata.title[0]))
        title = re.sub(b'[^-A-Za-z0-9]+', b'_', title)[:31]
        title = title + (b'\0' * (32 - len(title)))
        now = int(time.time())
        nrecords = len(self._records)
        self._write(title, pack(b'>HHIIIIII', 0, 0, now, now, 0, 0, 0, 0),
            b'BOOK', b'MOBI', pack(b'>IIH', (2*nrecords)-1, 0, nrecords))
        offset = self._tell() + (8 * nrecords) + 2
        for i, record in enumerate(self._records):
            self._write(pack(b'>I', offset), b'\0', pack(b'>I', 2*i)[1:])
            offset += len(record)
        self._write(b'\0\0')

    def _write_content(self):
        for record in self._records:
            self._write(record)

    def _clean_text_value(self, text):
        if text is not None and text.strip() :
            text = text.strip()
            if not isinstance(text, str):
                text = text.decode('utf-8', 'replace')
            text = normalize(text).encode('utf-8')
        else :
            text = "(none)".encode('utf-8')
        return text

    def _compute_offset_length(self, i, node, entries) :
        h = node.href
        if h not in self._id_offsets:
            logging.warning('Could not find TOC entry:', node.title)
            return -1, -1

        offset = self._id_offsets[h]
        length = None
        # Calculate length based on next entry's offset
        for sibling in entries[i+1:]:
            h2 = sibling.href
            if h2 in self._id_offsets:
                offset2 = self._id_offsets[h2]
                if offset2 > offset:
                    length = offset2 - offset
                    break
        if length is None:
            length = self._content_length - offset
        return offset, length

    def _establish_document_structure(self) :
        documentType = None
        try :
            klass = self._ctoc_map[0]['klass']
        except :
            klass = None

        if klass == 'chapter' or klass == None :
            documentType = 'book'
            if self._verbose > 2 :
                logging.info("Adding a MobiBook to self._MobiDoc")
            self._MobiDoc.documentStructure = MobiBook()

        elif klass == 'periodical' :
            documentType = klass
            if self._verbose > 2 :
                logging.info("Adding a MobiPeriodical to self._MobiDoc")
            self._MobiDoc.documentStructure = MobiPeriodical(self._MobiDoc.getNextNode())
            self._MobiDoc.documentStructure.startAddress = self._anchor_offset_kindle
        else :
            raise NotImplementedError('_establish_document_structure: unrecognized klass: %s' % klass)
        return documentType

    # Index {{{

    def _flatten_toc(self):
        '''
        Flatten and re-order entries in TOC so that chapter to chapter jumping
        never fails on the Kindle.
        '''
        from ..oeb.base import TOC
        items = list(self._oeb.toc.iterdescendants())
        offsets = {i:self._id_offsets.get(i.href, -1) for i in items if i.href}
        items = [i for i in items if offsets[i] > -1]
        items.sort(key=lambda i:offsets[i])
        filt = []
        seen = set()
        for i in items:
            off = offsets[i]
            if off in seen: continue
            seen.add(off)
            filt.append(i)
        items = filt
        newtoc = TOC()
        for c, i in enumerate(items):
            newtoc.add(i.title, i.href, play_order=c+1, id=str(c),
                    klass='chapter')
        self._oeb.toc = newtoc

    def _generate_index(self):
        logging.info('Generating INDX ...')
        self._primary_index_record = None

        # Build the NCXEntries and INDX
        indxt, indxt_count, indices, last_name = self._generate_indxt()

        if last_name is None:
            logging.warn('Input document has no TOC. No index generated.')
            return

        # Assemble the INDX0[0] and INDX1[0] output streams
        indx1 = BytesIO()
        indx1.write(b'INDX'+pack(b'>I', 0xc0)) # header length

        # 0x8 - 0xb : Unknown
        indx1.write(b'\0'*4)

        # 0xc - 0xf : Header type
        indx1.write(pack(b'>I', 1))

        # 0x10 - 0x13 : Unknown
        indx1.write(b'\0'*4)

        # 0x14 - 0x17 : IDXT offset
        # 0x18 - 0x1b : IDXT count
        indx1.write(pack(b'>I', 0xc0+len(indxt)))
        indx1.write(pack(b'>I', indxt_count + 1))

        # 0x1c - 0x23 : Unknown
        indx1.write(b'\xff'*8)

        # 0x24 - 0xbf
        indx1.write(b'\0'*156)
        indx1.write(indxt)
        indx1.write(indices)
        indx1 = indx1.getvalue()

        idxt0 = bytes([len(last_name)]) + encode(last_name) + pack(b'>H', indxt_count + 1)
        idxt0 = align_block(idxt0)
        indx0 = BytesIO()

        if self._MobiDoc.mobiType == 0x002 :
            tagx = TAGX['chapter']
        else :
            tagx = TAGX['periodical']

        tagx = align_block(b'TAGX' + pack(b'>I', 8 + len(tagx)) + tagx)
        indx0_indices_pos = 0xc0 + len(tagx) + len(idxt0)
        indx0_indices = align_block(b'IDXT' + pack(b'>H', 0xc0 + len(tagx)))
        # Generate record header
        header = BytesIO()

        header.write(b'INDX')
        header.write(pack(b'>I', 0xc0)) # header length

        # 0x08 - 0x0b : Unknown
        header.write(b'\0'*4)

        # 0x0c - 0x0f : Header type
        header.write(pack(b'>I', 0))

        # 0x10 - 0x13 : Generator ID
        # This value may impact the position of flagBits written in
        # write_article_node().  Change with caution.
        header.write(pack(b'>I', 6))

        # 0x14 - 0x17 : IDXT offset
        header.write(pack(b'>I', indx0_indices_pos))

        # 0x18 - 0x1b : IDXT count
        header.write(pack(b'>I', 1))

        # 0x1c - 0x1f : Text encoding ?
        # header.write(pack('>I', 650001))
        # GR: This needs to be either 0xFDE9 or 0x4E4
        header.write(pack(b'>I', 0xFDE9))

        # 0x20 - 0x23 : Language code?
        header.write(iana2mobi(str(self._oeb.metadata.language[0])))

        # 0x24 - 0x27 : Number of TOC entries in INDX1
        header.write(pack(b'>I', indxt_count + 1))

        # 0x28 - 0x2b : ORDT Offset
        header.write(b'\0'*4)

        # 0x2c - 0x2f : LIGT offset
        header.write(b'\0'*4)

        # 0x30 - 0x33 : Number of LIGT entries
        header.write(b'\0'*4)

        # 0x34 - 0x37 : Number of ctoc[] blocks
        header.write(pack(b'>I', len(self._ctoc_records)))

        # 0x38 - 0xb3 : Unknown (pad?)
        header.write(b'\0'*124)

        # 0xb4 - 0xb7 : TAGX offset
        header.write(pack(b'>I', 0xc0))

        # 0xb8 - 0xbf : Unknown
        header.write(b'\0'*8)

        header = header.getvalue()

        indx0.write(header)
        indx0.write(tagx)
        indx0.write(idxt0)
        indx0.write(indx0_indices)
        indx0 = indx0.getvalue()

        self._primary_index_record = len(self._records)

        # GR: handle multiple ctoc records
        self._records.extend([indx0, indx1 ])
        for (i,ctoc_record) in enumerate(self._ctoc_records):
            self._records.append(ctoc_record)
            # print "adding %d of %d ctoc records" % (i+1, len(self._ctoc_records))

        # Indexing for author/description fields in summary section
        # Test for indexed periodical - only one that needs secondary index
        if self._MobiDoc.mobiType > 0x100 :
            # Write secondary index records
            #tagx = TAGX['secondary_'+\
            #        ('periodical' if self._mobi_periodical else 'book')]
            tagx = TAGX['secondary_'+'periodical']
            tagx_len = 8 + len(tagx)

            # generate secondary INDX0
            indx0 = BytesIO()
            indx0.write('INDX'+pack(b'>I', 0xc0)+b'\0'*8)            # header + 8x00
            indx0.write(pack(b'>I', 0x06))                          # generator ID
            indx0.write(pack(b'>I', 0xe8))                          # IDXT offset
            indx0.write(pack(b'>I', 1))                             # IDXT entries
            indx0.write(pack(b'>I', 65001))                         # encoding
            indx0.write(b'\xff'*4)                                  # language
            indx0.write(pack(b'>I', 4))                             # IDXT Entries in INDX1
            indx0.write(b'\0'*4)                                    # ORDT Offset
            indx0.write(b'\0'*136)                                  # everything up to TAGX offset
            indx0.write(pack(b'>I', 0xc0))                          # TAGX offset
            indx0.write(b'\0'*8)                                    # unknowns
            indx0.write(b'TAGX'+pack(b'>I', tagx_len)+tagx)          # TAGX
            indx0.write(b'\x0D'+b'mastheadImage' + '\x00\x04')       # mastheadImage
            indx0.write(b'IDXT'+b'\x00\xd8\x00\x00')                 # offset plus pad

            # generate secondary INDX1
            indx1 = BytesIO()
            indx1.write(b'INDX' + pack(b'>I', 0xc0) + b'\0'*4)         # header + 4x00
            indx1.write(pack(b'>I', 1))                              # blockType 1
            indx1.write(pack(b'>I', 0x00))                           # unknown
            indx1.write(b'\x00\x00\x00\xF0')                         # IDXT offset
            indx1.write(pack(b'>I', 4))                              # num of IDXT entries
            indx1.write(b'\xff'*8)                                   # encoding, language
            indx1.write(b'\0'*(0xc0-indx1.tell()))                   # 00 to IDXT Entries @ 0xC0
            indx1.write(b'\0\x01\x80')                               # 1 - null
            indx1.write(b'\x06'+b'author' + b'\x02\x80\x80\xc7')           # author
            indx1.write(b'\x0B'+b'description' + b'\x02\x80\x80\xc6')    # description
            indx1.write(b'\x0D'+b'mastheadImage' + b'\x02\x85\x80\xc5')  # mastheadImage
            indx1.write(b'IDXT'+b'\x00\xc0\x00\xc3\x00\xce\x00\xde')      # IDXT header

            # Write INDX0 and INDX1 to the stream
            indx0, indx1 = indx0.getvalue(), indx1.getvalue()
            self._records.extend((indx0, indx1))
            if self._verbose > 3:
                from tempfile import mkdtemp
                import os
                t = mkdtemp()
                for i, n in enumerate(['sindx1', 'sindx0', 'ctoc', 'indx0', 'indx1']):
                    open(os.path.join(t, n+'.bin'), 'wb').write(self._records[-(i+1)])
                logging.debug('Index records dumped to', t)

    # Index nodes {{{
    def _write_periodical_node(self, indxt, indices, index, offset, length, count, firstSection, lastSection) :
        pos = 0xc0 + indxt.tell()
        indices.write(pack(b'>H', pos))								# Save the offset for IDXTIndices
        name = "%04X"%count
        indxt.write(bytes([len(name)]) + name)							# Write the name
        indxt.write(INDXT['periodical'])                            # entryType [0x0F | 0xDF | 0xFF | 0x3F]
        indxt.write(bytes([1]))                                         # subType 1
        indxt.write(decint(offset, DECINT_FORWARD))					# offset
        indxt.write(decint(length, DECINT_FORWARD))					# length
        indxt.write(decint(self._ctoc_map[index]['titleOffset'], DECINT_FORWARD))	# vwi title offset in CNCX

        indxt.write(decint(0, DECINT_FORWARD))						# unknown byte

        indxt.write(decint(self._ctoc_map[index]['classOffset'], DECINT_FORWARD))	# vwi title offset in CNCX
        indxt.write(decint(firstSection, DECINT_FORWARD))           # first section in periodical
        indxt.write(decint(lastSection, DECINT_FORWARD))            # first section in periodical

        indxt.write(decint(0, DECINT_FORWARD))						# 0x80

    def _write_section_node(self, indxt, indices, myCtocMapIndex, index, offset, length, count, firstArticle, lastArticle, parentIndex) :
        pos = 0xc0 + indxt.tell()
        indices.write(pack(b'>H', pos))								# Save the offset for IDXTIndices
        name = "%04X"%count
        indxt.write(bytes([len(name)]) + name.encode('ascii'))							# Write the name
        indxt.write(INDXT['section'])                               # entryType [0x0F | 0xDF | 0xFF | 0x3F]
        indxt.write(bytes([0]))                                         # subType 0
        indxt.write(decint(offset, DECINT_FORWARD))					# offset
        indxt.write(decint(length, DECINT_FORWARD))					# length
        indxt.write(decint(self._ctoc_map[myCtocMapIndex]['titleOffset'], DECINT_FORWARD))	# vwi title offset in CNCX

        indxt.write(decint(1, DECINT_FORWARD))						# unknown byte

        indxt.write(decint(self._ctoc_map[myCtocMapIndex]['classOffset'], DECINT_FORWARD))	# vwi title offset in CNCX
        indxt.write(decint(parentIndex, DECINT_FORWARD))			# index of periodicalParent
        indxt.write(decint(firstArticle, DECINT_FORWARD))           # first section in periodical
        indxt.write(decint(lastArticle, DECINT_FORWARD))            # first section in periodical

    def _write_article_node(self, indxt, indices, index, offset, length, count, parentIndex) :
        pos = 0xc0 + indxt.tell()
        indices.write(pack(b'>H', pos))								# Save the offset for IDXTIndices
        name = "%04X"%count
        indxt.write(bytes([len(name)]) + name.encode('ascii'))							# Write the name
        indxt.write(INDXT['article'])                               # entryType [0x0F | 0xDF | 0xFF | 0x3F]

        hasAuthor = True if self._ctoc_map[index]['authorOffset'] else False
        hasDescription = True if self._ctoc_map[index]['descriptionOffset']  else False

        # flagBits may be dependent upon the generatorID written at 0x10 in generate_index().
        # in INDX0.  Mobigen uses a generatorID of 2 and writes these bits at positions 1 & 2;
        # calibre uses a generatorID of 6 and writes the bits at positions 2 & 3.
        flagBits = 0
        if hasAuthor : flagBits |= 0x4
        if hasDescription : flagBits |= 0x2
        indxt.write(pack(b'>B',flagBits))                            # Author/description flags
        indxt.write(decint(offset, DECINT_FORWARD))					# offset


        indxt.write(decint(length, DECINT_FORWARD))					# length
        indxt.write(decint(self._ctoc_map[index]['titleOffset'], DECINT_FORWARD))	# vwi title offset in CNCX

        indxt.write(decint(2, DECINT_FORWARD))						# unknown byte

        indxt.write(decint(self._ctoc_map[index]['classOffset'], DECINT_FORWARD))	# vwi title offset in CNCX
        indxt.write(decint(parentIndex, DECINT_FORWARD))			# index of periodicalParent

        # Optionally write the author and description fields
        descriptionOffset = self._ctoc_map[index]['descriptionOffset']
        if descriptionOffset :
            indxt.write(decint(descriptionOffset, DECINT_FORWARD))

        authorOffset = self._ctoc_map[index]['authorOffset']
        if authorOffset :
            indxt.write(decint(authorOffset, DECINT_FORWARD))

    def _write_chapter_node(self, indxt, indices, index, offset, length, count):
        # Writes an INDX1 NCXEntry of entryType 0x0F - chapter
        if self._verbose > 2:
            pass

        pos = 0xc0 + indxt.tell()
        indices.write(pack(b'>H', pos))								# Save the offset for IDXTIndices
        name = "%04X"%count
        indxt.write(bytes([len(name)]) + name.encode('ascii'))							# Write the name
        indxt.write(INDXT['chapter'])								# entryType [0x0F | 0xDF | 0xFF | 0x3F]
        indxt.write(decint(offset, DECINT_FORWARD))					# offset
        indxt.write(decint(length, DECINT_FORWARD))					# length
        indxt.write(decint(self._ctoc_map[index]['titleOffset'], DECINT_FORWARD))	# vwi title offset in CNCX
        indxt.write(decint(0, DECINT_FORWARD))						# unknown byte

    # }}}


    def _generate_section_indices(self, child, currentSection, myPeriodical, myDoc ) :
        sectionTitles = list(child.iter())[1:]
        sectionIndices = []
        sectionParents = []
        for (j, section) in enumerate(sectionTitles):
            # iterate over just the sections

            if section.klass == 'periodical' :
                # Write our index to the list
                sectionIndices.append(currentSection)

                if self._verbose > 3 :
                    logging.info("Periodical: %15.15s \tkls:%s \tdpt:%d  ply:%03d" % \
                        (section.title, section.klass, section.depth(), section.play_order) )

            elif section.klass == 'section' :
                # Add sections, save in list with original sequence number
                myNewSection = myPeriodical.addSectionParent(myDoc, j)
                sectionParents.append(myNewSection)

                # Bump the section #
                currentSection += 1
                # Write our index to the list
                sectionIndices.append(currentSection)

                if self._verbose > 3 :
                    logging.info("   Section: %15.15s \tkls:%s \tdpt:%d  ply:%03d \tindex:%d" % \
                        (section.title, section.klass, section.depth(), section.play_order,j) )

            elif section.klass == 'article' :
                # Write our index to the list
                sectionIndices.append(currentSection)

            else :
                if self._verbose > 3 :
                    logging.info( " Unrecognized class %s in structured document" % section.klass)
        return sectionIndices, sectionParents

    def _generate_section_article_indices(self, i, section, entries, sectionIndices, sectionParents):
        sectionArticles = list(section.iter())[1:]
        # Iterate over the section's articles

        for (j, article) in enumerate(sectionArticles):
            # Recompute offset and length for each article
            offset, length = self._compute_offset_length(i, article, entries)
            if self._verbose > 2 :
                logging.info( "article %02d: offset = 0x%06X length = 0x%06X" % (j, offset, length) )

            ctoc_map_index = i + j + 1

            #hasAuthor = self._ctoc_map[ctoc_map_index].get('authorOffset')
            #hasDescription = self._ctoc_map[ctoc_map_index].get('descriptionOffset')
            mySectionParent = sectionParents[sectionIndices[i-1]]
            myNewArticle = MobiArticle(mySectionParent, offset, length, ctoc_map_index )
            mySectionParent.addArticle( myNewArticle )

    def _add_book_chapters(self, myDoc, indxt, indices):
        chapterCount = myDoc.documentStructure.chapterCount()
        if self._verbose > 3 :
            logging.info("Writing %d chapters for mobitype 0x%03X" % (chapterCount, myDoc.mobiType))

        for (c, chapter) in enumerate(list(myDoc.documentStructure.chapters)) :
            index = chapter.myCtocMapIndex
            self._write_chapter_node(indxt, indices, index, chapter.startAddress, chapter.length, c)

            last_name = "%04X"%c                                    # Returned when done
        return last_name, c

    def _add_periodical_flat_articles(self, myDoc, indxt, indices):
        sectionParent = myDoc.documentStructure.sectionParents[0]
        articleCount = len(sectionParent.articles)
        if self._verbose > 3 :
            logging.info("Writing %d articles for mobitype 0x%03X" % (articleCount, myDoc.mobiType))

        # Singleton periodical
        index = 0
        offset = myDoc.documentStructure.startAddress
        length = myDoc.documentStructure.length
        c = 0
        firstSection = myDoc.documentStructure.firstSectionIndex
        lastSection = myDoc.documentStructure.lastSectionIndex
        self._write_periodical_node(indxt, indices, index, offset, length, c, firstSection, lastSection)

        # Singleton section
        index += 1
        offset = sectionParent.startAddress
        length = sectionParent.sectionLength
        c += 1
        firstArticle = sectionParent.firstArticleIndex
        lastArticle = sectionParent.lastArticleIndex
        parentIndex = sectionParent.parentIndex
        self._write_section_node(indxt, indices, sectionParent.myCtocMapIndex, index, offset, length, c, firstArticle, lastArticle, parentIndex)

        # articles
        for article in list(sectionParent.articles):
            index = article.myCtocMapIndex
            offset = article.startAddress
            length = article.articleLength
            c += 1
            parentIndex = article.sectionParentIndex
            self._write_article_node(indxt, indices, index, offset, length, c, parentIndex)

        last_name = "%04X" % c
        return last_name, c

    def _add_periodical_structured_articles(self, myDoc, indxt, indices):
        # Write NCXEntries for Structured Periodical
        # <periodical>
        #   <section>
        #   <section> ...
        #       <article>
        #       <article> ...

        if self._verbose > 2 :
            logging.info( "Writing NCXEntries for mobiType 0x%03X" % myDoc.mobiType)

        sectionParent = myDoc.documentStructure.sectionParents[0]
        #articleCount = len(sectionParent.articles)

        # Write opening periodical 0xDF entry
        index = 0
        offset = myDoc.documentStructure.startAddress
        length = myDoc.documentStructure.length
        c = 0
        firstSection = myDoc.documentStructure.firstSectionIndex
        lastSection = myDoc.documentStructure.lastSectionIndex
        self._write_periodical_node(indxt, indices, index, offset, length, c, firstSection, lastSection)

        # Write each section 0xFF entry
        sectionCount = firstSection
        while sectionCount <= lastSection :
            # section
            sectionParent = myDoc.documentStructure.sectionParents[sectionCount - 1]
            #articleCount = len(sectionParent.articles)
            #index += 1
            offset = sectionParent.startAddress
            length = sectionParent.sectionLength
            c += 1
            firstArticle = sectionParent.firstArticleIndex
            lastArticle = sectionParent.lastArticleIndex
            parentIndex = sectionParent.parentIndex
            self._write_section_node(indxt, indices, sectionParent.myCtocMapIndex, sectionCount, offset, length, c, firstArticle, lastArticle, parentIndex)
            sectionCount += 1

        # Write each article 0x3F entry
        sectionCount = firstSection
        while sectionCount <= lastSection :
            # section
            sectionParent = myDoc.documentStructure.sectionParents[sectionCount - 1]
#                 articleCount = len(sectionParent.articles)
#                 index += 1
#                 offset = sectionParent.startAddress
#                 length = sectionParent.sectionLength
#                 c += 1
#                 firstArticle = sectionParent.firstArticleIndex
#                 lastArticle = sectionParent.lastArticleIndex
#                 parentIndex = sectionParent.parentIndex
#                 add_section_node(index, offset, length, c, firstArticle, lastArticle, parentIndex)

            last_name = "%04X"%c

            # articles
            for (i, article) in enumerate(list(sectionParent.articles)) :
                if self._verbose > 3 :
                    logging.info( "Adding section:article %d:%02d" % \
                        (sectionParent.myIndex, i))
                index = article.myCtocMapIndex
                offset = article.startAddress
                length = article.articleLength
                c += 1
                parentIndex = article.sectionParentIndex
                self._write_article_node(indxt, indices, index, offset, length, c, parentIndex)

                last_name = "%04X"%c

            sectionCount += 1

        return last_name, c

    def _generate_indxt(self):
        # Assumption: child.depth() represents nestedness of the TOC.
        # A flat document (book) has a depth of 2:
        # <navMap>					child.depth() = 2
        #	<navPoint>	Chapter		child.depth() = 1
        #	<navPoint>	Chapter		etc
        # -or-
        # A structured document (periodical) has a depth of 4 (Mobigen-prepped)
        # <navMap>					child.depth() = 4
        #  <navPoint>	Periodical	child.depth() = 3
        #   <navPoint>	Section	1	child.depth() = 2
        #    <navPoint> Article		child.depth() = 1
        #	 <navPoint> Article(s)	child.depth() = 1
        #   <navpoint>	Section 2

        sectionIndices = []
        sectionParents = []
        currentSection = 0      # Starting section number
        toc = self._oeb.toc
        indxt, indices, c = BytesIO(), BytesIO(), 0

        indices.write(b'IDXT')
        last_name = None

        # 'book', 'periodical' or None
        documentType = self._establish_document_structure()
        myDoc = self._MobiDoc

        nodes = list(toc.iter())[0:1]
        for (i, child) in enumerate(nodes) :

            if documentType == "periodical" :
                myPeriodical = myDoc.documentStructure
                if self._verbose > 3 :
                    logging.info("\nDocument: %s \tkls:%s \tdpt:%d  ply:%03d" % \
                        (child.title, child.klass, child.depth(), child.play_order) )
                sectionIndices, sectionParents = \
                    self._generate_section_indices(child, currentSection, myPeriodical, myDoc)

            elif documentType == "book" :
                myBook = myDoc.documentStructure

                if self._verbose > 3 :
                    logging.info("\nBook: %-19.19s \tkls:%s \tdpt:%d  ply:%03d" % \
                    (child.title, child.klass, child.depth(), child.play_order) )
            else :
                if self._verbose > 3 :
                    logging.info("unknown document type %12.12s \tdepth:%d" % (child.title, child.depth()) )

        # Original code starts here
        # test first node for depth/class
        entries = list(toc.iter())[1:]
        for (i, child) in enumerate(entries):
            if not child.title or not child.title.strip():
                continue

            offset, length = self._compute_offset_length(i, child, entries)

            if child.klass == 'chapter' or (not self._mobi_periodical and child.klass == 'article'):
                # create chapter object - confirm i + 0 is correct!!
                myNewChapter = MobiChapter(myDoc.getNextNode(), offset, length, i)
                myBook.addChapter(myNewChapter)

                # Diagnostic
                try :
                    if self._verbose > 3 :
                        logging.info( "  Chapter: %-14.14s \tcls:%s \tdpt:%d  ply:%03d \toff:0x%X \t:len0x%X" % \
                        (child.title, child.klass, child.depth(), child.play_order, offset, length) )
                except :
                    if self._verbose > 3 :
                        logging.info( "  Chapter: %-14.14s \tclass:%s \tdepth:%d  playOrder:%03d \toff:0x%X \t:len0x%X" % \
                        ("(bad string)", child.klass, child.depth(), child.play_order, offset, length))

            elif child.klass == 'section' and self._mobi_periodical :
                if self._verbose > 3 :
                    logging.info("\n  Section: %-15.15s \tkls:%s \tdpt:%d  ply:%03d" % \
                        (child.title, child.klass, child.depth(), child.play_order))
                self._generate_section_article_indices(i, child, entries, sectionIndices, sectionParents)

        if self._verbose > 3 :
            logging.info("")

        mobiType = myDoc.mobiType
        if self._verbose > 3 :
            self._MobiDoc.dumpInfo()

        if mobiType == 0x02 :
            last_name, c = self._add_book_chapters(myDoc, indxt, indices)

        elif mobiType == 0x102 and myDoc.documentStructure.sectionCount() == 1 :
            last_name, c = self._add_periodical_flat_articles(myDoc, indxt, indices)

        else :
            last_name, c = self._add_periodical_structured_articles(myDoc, indxt, indices)

        return align_block(indxt.getvalue()), c, align_block(indices.getvalue()), last_name
    # }}}

    # CTOC {{{
    def _add_to_ctoc(self, ctoc_str, record_offset):
        # Write vwilen + string to ctoc
        # Return offset
        # Is there enough room for this string in the current ctoc record?
        if 0xfbf8 - self._ctoc.tell() < 2 + len(ctoc_str):
            # flush this ctoc, start a new one
            # print "closing ctoc_record at 0x%X" % self._ctoc.tell()
            # print "starting new ctoc with '%-50.50s ...'" % ctoc_str
            # pad with 00
            pad = 0xfbf8 - self._ctoc.tell()
            # print "padding %d bytes of 00" % pad
            self._ctoc.write(b'\0' * (pad))
            self._ctoc_records.append(self._ctoc.getvalue())
            self._ctoc.truncate(0)
            self._ctoc_offset += 0x10000
            record_offset = self._ctoc_offset

        offset = self._ctoc.tell() + record_offset
        self._ctoc.write(decint(len(ctoc_str), DECINT_FORWARD) + ctoc_str)
        return offset

    def _add_flat_ctoc_node(self, node, ctoc, title=None):
        # Process 'chapter' or 'article' nodes only, force either to 'chapter'
        t = node.title if title is None else title
        t = self._clean_text_value(t)
        self._last_toc_entry = t

        # Create an empty dictionary for this node
        ctoc_name_map = {}

        # article = chapter
        if node.klass == 'article' :
            ctoc_name_map['klass'] = 'chapter'
        else :
            ctoc_name_map['klass'] = node.klass

        # Add title offset to name map
        ctoc_name_map['titleOffset'] = self._add_to_ctoc(t, self._ctoc_offset)
        self._chapterCount += 1

        # append this node's name_map to map
        self._ctoc_map.append(ctoc_name_map)

        return

    def _add_structured_ctoc_node(self, node, ctoc, title=None):
        # Process 'periodical', 'section' and 'article'

        # Fetch the offset referencing the current ctoc_record
        if node.klass is None :
            return
        t = node.title if title is None else title
        t = self._clean_text_value(t)
        self._last_toc_entry = t

        # Create an empty dictionary for this node
        ctoc_name_map = {}

        # Add the klass of this node
        ctoc_name_map['klass'] = node.klass

        if node.klass == 'chapter':
            # Add title offset to name map
            ctoc_name_map['titleOffset'] = self._add_to_ctoc(t, self._ctoc_offset)
            self._chapterCount += 1

        elif node.klass == 'periodical' :
            # Add title offset
            ctoc_name_map['titleOffset'] = self._add_to_ctoc(t, self._ctoc_offset)

            # Look for existing class entry 'periodical' in _ctoc_map
            for entry in self._ctoc_map:
                if entry['klass'] == 'periodical':
                    # Use the pre-existing instance
                    ctoc_name_map['classOffset'] = entry['classOffset']
                    break
                else :
                    continue
            else:
                # class names should always be in CNCX 0 - no offset
                ctoc_name_map['classOffset'] = self._add_to_ctoc(node.klass, 0)

            self._periodicalCount += 1

        elif node.klass == 'section' :
            # Add title offset
            ctoc_name_map['titleOffset'] = self._add_to_ctoc(t, self._ctoc_offset)

            # Look for existing class entry 'section' in _ctoc_map
            for entry in self._ctoc_map:
                if entry['klass'] == 'section':
                    # Use the pre-existing instance
                    ctoc_name_map['classOffset'] = entry['classOffset']
                    break
                else :
                    continue
            else:
                # class names should always be in CNCX 0 - no offset
                ctoc_name_map['classOffset'] = self._add_to_ctoc(node.klass, 0)

            self._sectionCount += 1

        elif node.klass == 'article' :
            # Add title offset/title
            ctoc_name_map['titleOffset'] = self._add_to_ctoc(t, self._ctoc_offset)

            # Look for existing class entry 'article' in _ctoc_map
            for entry in self._ctoc_map:
                if entry['klass'] == 'article':
                    ctoc_name_map['classOffset'] = entry['classOffset']
                    break
                else :
                    continue
            else:
                # class names should always be in CNCX 0 - no offset
                ctoc_name_map['classOffset'] = self._add_to_ctoc(node.klass, 0)

            # Add description offset/description
            if node.description :
                d = self._clean_text_value(node.description)
                ctoc_name_map['descriptionOffset'] = self._add_to_ctoc(d, self._ctoc_offset)
            else :
                ctoc_name_map['descriptionOffset'] = None

            # Add author offset/attribution
            if node.author :
                a = self._clean_text_value(node.author)
                ctoc_name_map['authorOffset'] = self._add_to_ctoc(a, self._ctoc_offset)
            else :
                ctoc_name_map['authorOffset'] = None

            self._articleCount += 1

        else :
            raise NotImplementedError( \
            'writer._generate_ctoc.add_node: title: %s has unrecognized klass: %s, playOrder: %d' % \
            (node.title, node.klass, node.play_order))

        # append this node's name_map to map
        self._ctoc_map.append(ctoc_name_map)

    def _generate_ctoc(self):
        # Generate the compiled TOC strings
        # Each node has 1-4 CTOC entries:
        #	Periodical (0xDF)
        #		title, class
        #	Section (0xFF)
        #		title, class
        #	Article (0x3F)
        #		title, class, description, author
        #	Chapter (0x0F)
        #		title, class
        #   nb: Chapters don't actually have @class, so we synthesize it
        #   in reader._toc_from_navpoint

        toc = self._oeb.toc
        reduced_toc = []
        self._ctoc_map = []				# per node dictionary of {class/title/desc/author} offsets
        self._last_toc_entry = None
        #ctoc = BytesIO()
        self._ctoc = BytesIO()

        # Track the individual node types
        self._periodicalCount = 0
        self._sectionCount = 0
        self._articleCount = 0
        self._chapterCount = 0

        #first = True

        if self._conforming_periodical_toc :
            logging.info('Generating structured CTOC ...')
            for (child) in toc.iter():
                if self._verbose > 2 :
                    logging.info("  %s" % child)
                self._add_structured_ctoc_node(child, self._ctoc)
                #first = False

        else :
            logging.info('Generating flat CTOC ...')
            previousOffset = -1
            currentOffset = 0
            for (i, child) in enumerate(toc.iterdescendants()):
                # Only add chapters or articles at depth==1
                # no class defaults to 'chapter'
                if child.klass is None : child.klass = 'chapter'
                if (child.klass == 'article' or child.klass == 'chapter') and child.depth() == 1 :
                    if self._verbose > 2 :
                        logging.info("adding (klass:%s depth:%d) %s to flat ctoc" % \
                                              (child.klass, child.depth(), child) )

                    # Test to see if this child's offset is the same as the previous child's
                    # offset, skip it
                    h = child.href

                    if h is None:
                        logging.warn('  Ignoring TOC entry with no href:',
                                child.title)
                        continue
                    if h not in self._id_offsets:
                        logging.warn('  Ignoring missing TOC entry:',
                                str(child))
                        continue

                    currentOffset = self._id_offsets[h]
                    # print "_generate_ctoc: child offset: 0x%X" % currentOffset

                    if currentOffset != previousOffset :
                        self._add_flat_ctoc_node(child, self._ctoc)
                        reduced_toc.append(child)
                        previousOffset = currentOffset
                    else :
                        logging.warn("  Ignoring redundant href: %s in '%s'" % (h, child.title))

                else :
                    if self._verbose > 2 :
                        logging.info("skipping class: %s depth %d at position %d" % \
                                              (child.klass, child.depth(),i))

            # Update the TOC with our edited version
            self._oeb.toc.nodes = reduced_toc

        # Instantiate a MobiDocument(mobitype)
        if (not self._periodicalCount and not self._sectionCount and not self._articleCount) or \
            not self._mobi_periodical :
            mobiType = 0x002
        elif self._periodicalCount:
            pt = None
            if self._oeb.metadata.publication_type:
                x = str(self._oeb.metadata.publication_type[0]).split(':')
                if len(x) > 1:
                    pt = x[1]
            mobiType = {'newspaper':0x101}.get(pt, 0x103)
        else :
            raise NotImplementedError('_generate_ctoc: Unrecognized document structured')

        self._MobiDoc = MobiDocument(mobiType)

        if self._verbose > 2 :
            structType = 'book'
            if mobiType > 0x100 :
                structType = 'flat periodical' if mobiType == 0x102 else 'structured periodical'
            logging.info("Instantiating a %s MobiDocument of type 0x%X" % (structType, mobiType ) )
            if mobiType > 0x100 :
                logging.info("periodicalCount: %d  sectionCount: %d  articleCount: %d"% \
                                    (self._periodicalCount, self._sectionCount, self._articleCount) )
            else :
                logging.info("chapterCount: %d" % self._chapterCount)

        # Apparently the CTOC must end with a null byte
        self._ctoc.write(b'\0')

        ctoc = self._ctoc.getvalue()
        rec_count = len(self._ctoc_records)
        logging.info("  CNCX utilization: %d %s %.0f%% full" % \
            (rec_count + 1, 'records, last record' if rec_count else 'record,',
                len(ctoc)/655) )

        return align_block(ctoc)

class HTMLRecordData:
    """ A data structure containing indexing/navigation data for an HTML record """
    def __init__(self):
        self._continuingNode = -1
        self._continuingNodeParent = -1
        self._openingNode = -1
        self._openingNodeParent = -1
        self._currentSectionNodeCount = -1
        self._nextSectionNumber = -1
        self._nextSectionOpeningNode = -1
        self._nextSectionNodeCount = -1

    def getContinuingNode(self):
        return self._continuingNode
    def setContinuingNode(self, value):
        self._continuingNode = value
    continuingNode = property(getContinuingNode, setContinuingNode, None, None)

    def getContinuingNodeParent(self):
        return self._continuingNodeParent
    def setContinuingNodeParent(self, value):
        self._continuingNodeParent = value
    continuingNodeParent = property(getContinuingNodeParent, setContinuingNodeParent, None, None)

    def getOpeningNode(self):
        return self._openingNode
    def setOpeningNode(self, value):
        self._openingNode = value
    openingNode = property(getOpeningNode, setOpeningNode, None, None)

    def getOpeningNodeParent(self):
        return self._openingNodeParent
    def setOpeningNodeParent(self, value):
        self._openingNodeParent = value
    openingNodeParent = property(getOpeningNodeParent, setOpeningNodeParent, None, None)

    def getCurrentSectionNodeCount(self):
        return self._currentSectionNodeCount
    def setCurrentSectionNodeCount(self, value):
        self._currentSectionNodeCount = value
    currentSectionNodeCount = property(getCurrentSectionNodeCount, setCurrentSectionNodeCount, None, None)

    def getNextSectionNumber(self):
        return self._nextSectionNumber
    def setNextSectionNumber(self, value):
        self._nextSectionNumber = value
    nextSectionNumber = property(getNextSectionNumber, setNextSectionNumber, None, None)

    def getNextSectionOpeningNode(self):
        return self._nextSectionOpeningNode
    def setNextSectionOpeningNode(self, value):
        self._nextSectionOpeningNode = value
    nextSectionOpeningNode = property(getNextSectionOpeningNode, setNextSectionOpeningNode, None, None)

    def getNextSectionNodeCount(self):
        return self._nextSectionNodeCount
    def setNextSectionNodeCount(self, value):
        self._nextSectionNodeCount = value
    nextSectionNodeCount = property(getNextSectionNodeCount, setNextSectionNodeCount, None, None)

    def dumpData(self, recordNumber, oeb):
        oeb.logger.info( "---  Summary of HTML Record 0x%x [%d] indexing  ---" % (recordNumber, recordNumber) )
        oeb.logger.info( "            continuingNode: %03d" % self.continuingNode )
        oeb.logger.info( "      continuingNodeParent: %03d" % self.continuingNodeParent )
        oeb.logger.info( "               openingNode: %03d" % self.openingNode )
        oeb.logger.info( "         openingNodeParent: %03d" % self.openingNodeParent )
        oeb.logger.info( "   currentSectionNodeCount: %03d" % self.currentSectionNodeCount )
        oeb.logger.info( "         nextSectionNumber: %03d" % self.nextSectionNumber )
        oeb.logger.info( "    nextSectionOpeningNode: %03d" % self.nextSectionOpeningNode )
        oeb.logger.info( "      nextSectionNodeCount: %03d" % self.nextSectionNodeCount )

class MobiDocument:
    """ Hierarchical description of a Mobi document """

    # Counter to assign index values as new nodes are created
    _nextNode = -1

    def __init__(self, mobitype):
        self._mobitype = mobitype
        self._documentStructure = None              # Assigned in _generate_indxt

    def getMobiType(self):
        return self._mobitype
    def setMobiType(self, value):
        self._mobitype = value
    mobiType = property(getMobiType, setMobiType, None, None)

    def getDocumentStructure(self):
        return self._documentStructure
    def setDocumentStructure(self, value):
        self._documentStructure = value
    documentStructure = property(getDocumentStructure, setDocumentStructure, None, None)

    def getNextNode(self):
        self._nextNode += 1
        return self._nextNode

    def dumpInfo(self):
        self._documentStructure.dumpInfo()

class MobiBook:
    """ A container for a flat chapter-to-chapter Mobi book """
    def __init__(self):
        self._chapters = []

    def chapterCount(self):
        return len(self._chapters)

    def getChapters(self):
        return self._chapters
    def setChapters(self, value):
        self._chapters = value
    chapters = property(getChapters, setChapters, None, None)

    def addChapter(self, value):
        self._chapters.append(value)

    def dumpInfo(self):
        print("%20s:" % ("Book"))
        print("%20s: %d" % ("Number of chapters", len(self._chapters)))
        for (count, chapter) in enumerate(self._chapters):
            print("%20s: %d"    % ("myCtocMapIndex",chapter.myCtocMapIndex))
            print("%20s: %d"    % ("Chapter",count))
            print("%20s: 0x%X"  % ("startAddress", chapter.startAddress))
            print("%20s: 0x%X"  % ("length", chapter.length))
            print()

class MobiChapter:
    """ A container for Mobi chapters """
    def __init__(self, myIndex, startAddress, length, ctoc_map_index):
        self._myIndex = myIndex
        self._startAddress = startAddress
        self._length = length
        self._myCtocMapIndex = ctoc_map_index

    def getMyCtocMapIndex(self):
        return self._myCtocMapIndex
    def setMyCtocMapIndex(self, value):
        self._myCtocMapIndex = value
    myCtocMapIndex = property(getMyCtocMapIndex, setMyCtocMapIndex, None, None)

    def getMyIndex(self):
        return self._myIndex
    myIndex = property(getMyIndex, None, None, None)

    def getStartAddress(self):
        return self._startAddress
    def setStartAddress(self, value):
        self._startAddress = value
    startAddress = property(getStartAddress, setStartAddress, None, None)

    def getLength(self):
        return self._length
    def setLength(self, value):
        self._length = value
    length = property(getLength, setLength, None, None)

class MobiPeriodical:
    """ A container for a structured periodical """
    def __init__(self, myIndex):
        self._myIndex = myIndex
        self._sectionParents = []
        self._startAddress = 0xFFFFFFFF
        self._length = 0xFFFFFFFF
        self._firstSectionIndex = 0xFFFFFFFF
        self._lastSectionIndex = 0xFFFFFFFF
        self._myCtocMapIndex = 0    # Always first entry

    def getMyIndex(self):
        return self._myIndex
    def setMyIndex(self, value):
        self._myIndex = value
    myIndex = property(getMyIndex, setMyIndex, None, None)

    def getSectionParents(self):
        return self._sectionParents
    def setSectionParents(self, value):
        self._sectionParents = value
    sectionParents = property(getSectionParents, setSectionParents, None, None)

    def sectionCount(self):
        return len(self._sectionParents)

    def getStartAddress(self):
        return self._startAddress
    def setStartAddress(self, value):
        self._startAddress = value
    startAddress = property(getStartAddress, setStartAddress, None, None)

    def getLength(self):
        return self._length
    def setLength(self, value):
        self._length = value
    length = property(getLength, setLength, None, None)

    def getFirstSectionIndex(self):
        return self._firstSectionIndex
    def setFirstSectionIndex(self, value):
        self._firstSectionIndex = value
    firstSectionIndex = property(getFirstSectionIndex, setFirstSectionIndex, None, None)

    def getLastSectionIndex(self):
        return self._lastSectionIndex
    def setLastSectionIndex(self, value):
        self._lastSectionIndex = value
    lastSectionIndex = property(getLastSectionIndex, setLastSectionIndex, None, None)

    def getMyCtocMapIndex(self):
        return self._myCtocMapIndex
    def setMyCtocMapIndex(self, value):
        self._myCtocMapIndex = value
    myCtocMapIndex = property(getMyCtocMapIndex, setMyCtocMapIndex, None, None)

    def addSectionParent(self, myIndex, ctoc_map_index):
        # Create a new section parent
        newSection = MobiSection(myIndex)
        # Assign our index to the section
        newSection.parentIndex = self._myIndex
        # Assign section number
        newSection.sectionIndex = len(self._sectionParents)
        # Assign ctoc_map_index
        newSection.myCtocMapIndex = ctoc_map_index
        # Add it to the list
        self._sectionParents.append(newSection)
        return newSection

    def dumpInfo(self):
        print("%20s:" % ("Periodical"))
        print("%20s: 0x%X" % ("myIndex", self.myIndex))
        print("%20s: 0x%X" % ("startAddress", self.startAddress))
        print("%20s: 0x%X" % ("length", self.length))
        print("%20s: 0x%X" % ("myCtocMapIndex", self.myCtocMapIndex))
        print("%20s: 0x%X" % ("firstSectionIndex", self.firstSectionIndex))
        print("%20s: 0x%X" % ("lastSectionIndex", self.lastSectionIndex))
        print("%20s: %d" % ("Number of Sections", len(self._sectionParents)))
        for (count, section) in enumerate(self._sectionParents):
            print("\t%20s: %d"    % ("Section",count))
            print("\t%20s: 0x%X"  % ("startAddress", section.startAddress))
            print("\t%20s: 0x%X"  % ("length", section.sectionLength))
            print("\t%20s: 0x%X"  % ("parentIndex", section.parentIndex))
            print("\t%20s: 0x%X"  % ("myIndex", section.myIndex))
            print("\t%20s: 0x%X"  % ("firstArticleIndex", section.firstArticleIndex))
            print("\t%20s: 0x%X"  % ("lastArticleIndex", section.lastArticleIndex))
            print("\t%20s: 0x%X"  % ("articles", len(section.articles) ))
            print("\t%20s: 0x%X"  % ("myCtocMapIndex", section.myCtocMapIndex ))
            print()
            for (artCount, article) in enumerate(section.articles) :
                print("\t\t%20s: %d"    % ("Article",artCount))
                print("\t\t%20s: 0x%X"  % ("startAddress", article.startAddress))
                print("\t\t%20s: 0x%X"  % ("length", article.articleLength))
                print("\t\t%20s: 0x%X"  % ("sectionIndex", article.sectionParentIndex))
                print("\t\t%20s: 0x%X"  % ("myIndex", article.myIndex))
                print("\t\t%20s: 0x%X"  % ("myCtocMapIndex", article.myCtocMapIndex))
                print()

class MobiSection:
    """ A container for periodical sections """
    def __init__(self, myMobiDoc):
        self._myMobiDoc = myMobiDoc
        self._myIndex = myMobiDoc.getNextNode()
        self._parentIndex = 0xFFFFFFFF
        self._firstArticleIndex = 0x00
        self._lastArticleIndex = 0x00
        self._startAddress = 0xFFFFFFFF
        self._sectionLength = 0xFFFFFFFF
        self._articles = []
        self._myCtocMapIndex = -1

    def getMyMobiDoc(self):
        return self._myMobiDoc
    def setMyMobiDoc(self, value):
        self._myMobiDoc = value
    myMobiDoc = property(getMyMobiDoc, setMyMobiDoc, None, None)

    def getMyIndex(self):
        return self._myIndex
    def setMyIndex(self, value):
        self._myIndex = value
    myIndex = property(getMyIndex, setMyIndex, None, None)

    def getParentIndex(self):
        return self._parentIndex
    def setParentIndex(self, value):
        self._parentIndex = value
    parenIndex = property(getParentIndex, setParentIndex, None, None)

    def getFirstArticleIndex(self):
        return self._firstArticleIndex
    def setFirstArticleIndex(self, value):
        self._firstArticleIndex = value
    firstArticleIndex = property(getFirstArticleIndex, setFirstArticleIndex, None, None)

    def getLastArticleIndex(self):
        return self._lastArticleIndex
    def setLastArticleIndex(self, value):
        self._lastArticleIndex = value
    lastArticleIndex = property(getLastArticleIndex, setLastArticleIndex, None, None)

    def getStartAddress(self):
        return self._startAddress
    def setStartAddress(self, value):
        self._startAddress = value
    startAddress = property(getStartAddress, setStartAddress, None, None)

    def getSectionLength(self):
        return self._sectionLength
    def setSectionLength(self, value):
        self._sectionLength = value
    sectionLength = property(getSectionLength, setSectionLength, None, None)

    def getArticles(self):
        return self._articles
    def setArticles(self, value):
        self._articles = value
    articles = property(getArticles, setArticles, None, None)

    def getMyCtocMapIndex(self):
        return self._myCtocMapIndex
    def setMyCtocMapIndex(self, value):
        self._myCtocMapIndex = value
    myCtocMapIndex = property(getMyCtocMapIndex, setMyCtocMapIndex, None, None)

    def addArticle(self, article):
        self._articles.append(article)

        # Adjust the Periodical parameters
        # If this is the first article of the first section, init the values
        if self.myIndex == 1 and len(self.articles) == 1 :
            self.myMobiDoc.documentStructure.firstSectionIndex = self.myIndex
            self.myMobiDoc.documentStructure.lastSectionIndex = self.myIndex
            self.myMobiDoc.documentStructure.length = article.articleLength + \
                ( article.startAddress - self.myMobiDoc.documentStructure.startAddress)
        else:
            self.myMobiDoc.documentStructure.length += article.articleLength

        # Always set the highest section index to myIndex
        self.myMobiDoc.documentStructure.lastSectionIndex = self.myIndex

        # Adjust the Section parameters
        if len(self.articles) == 1 :
            self.firstArticleIndex = article.myIndex

            if len(self.myMobiDoc.documentStructure.sectionParents) == 1 :
                self.startAddress = self.myMobiDoc.documentStructure.startAddress
                self.sectionLength = article.articleLength + \
                    ( article.startAddress - self.myMobiDoc.documentStructure.startAddress )

            else :
                self.startAddress = article.startAddress
                self.sectionLength = article.articleLength

            self.lastArticleIndex = article.myIndex
        else :
            self.lastArticleIndex = article.myIndex

        # Adjust the Section length
        if len(self.articles) > 1 :
            self.sectionLength += article.articleLength

class MobiArticle:
    """ A container for periodical articles """
    def __init__(self, sectionParent, startAddress, length, ctocMapIndex):
        self._mySectionParent = sectionParent
        self._myMobiDoc = sectionParent.myMobiDoc
        self._myIndex = sectionParent.myMobiDoc.getNextNode()
        self._myCtocMapIndex = ctocMapIndex
        self._sectionParentIndex = sectionParent.myIndex
        self._startAddress = startAddress
        self._articleLength = length

    def getMySectionParent(self):
        return self._mySectionParent
    def setMySectionParent(self, value):
        self._mySectionParent = value
    mySectionParent = property(getMySectionParent, setMySectionParent, None, None)

    def getMyMobiDoc(self):
        return self._myMobiDoc
    def setMyMobiDoc(self, value):
        self._myMobiDoc = value
    myMobiDoc = property(getMyMobiDoc, setMyMobiDoc, None, None)

    def getMyIndex(self):
        return self._myIndex
    def setMyIndex(self, value):
        self._sectionIndex = value
    myIndex = property(getMyIndex, setMyIndex, None, None)

    def getSectionParentIndex(self):
        return self._sectionParentIndex
    def setSectionParentIndex(self, value):
        self._sectionParentIndex = value
    sectionParentIndex = property(getSectionParentIndex, setSectionParentIndex, None, None)

    def getStartAddress(self):
        return self._startAddress
    def setStartAddress(self, value):
        self._startAddress = value
    startAddress = property(getStartAddress, setStartAddress, None, None)

    def getArticleLength(self):
        return self._articleLength
    def setArticleLength(self, value):
        self._articleLength = value
    articleLength = property(getArticleLength, setArticleLength, None, None)

    def getMyCtocMapIndex(self):
        return self._myCtocMapIndex
    def setMyCtocMapIndex(self, value):
        self._myCtocMapIndex = value
    myCtocMapIndex = property(getMyCtocMapIndex, setMyCtocMapIndex, None, None)

