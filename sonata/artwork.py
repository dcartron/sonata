import functools
import logging
import queue
import os
import queue
import re
import shutil
import threading

from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject

from sonata import img, ui, misc, consts, mpdhelper as mpdh
from sonata.song import SongRecord
from sonata.pluginsystem import pluginsystem


COVERS_DIR = os.path.expanduser("~/.covers")
COVERS_TEMP_DIR = os.path.join(COVERS_DIR, 'temp')
logger = logging.getLogger(__name__)


def artwork_path_from_song(song, config, location_type=None):
    return artwork_path_from_data(song.artist, song.album,
                                  os.path.dirname(song.file), config,
                                  location_type)


def artwork_path_from_data(artist, album, song_dir, config, location_type=None):
    """Return the artwork path for the specified data"""

    artist = (artist or "").replace("/", "")
    album = (album or "").replace("/", "")
    song_folder = os.path.join(
        config.current_musicdir,
        get_multicd_album_root_dir(song_dir)
    )

    if location_type is None:
        location_type = config.art_location

    if location_type == consts.ART_LOCATION_HOMECOVERS:
        paths = (COVERS_DIR, "%s-%s.jpg" % (artist, album))
    elif location_type == consts.ART_LOCATION_COVER:
        paths = (song_folder, "cover.jpg")
    elif location_type == consts.ART_LOCATION_FOLDER:
        paths = (song_folder, "folder.jpg")
    elif location_type == consts.ART_LOCATION_ALBUM:
        paths = (song_folder, "album.jpg")
    elif location_type == consts.ART_LOCATION_CUSTOM:
        paths = (song_folder, config.art_location_custom_filename)

    return os.path.join(*paths)

def get_multicd_album_root_dir(albumpath):
    """Go one dir upper for multicd albums

    >>> from sonata.artwork import get_multicd_album_root_dir as f
    >>> f('Moonspell/1995 - Wolfheart/cd 2')
    'Moonspell/1995 - Wolfheart'
    >>> f('2007 - Dark Passion Play/CD3')
    '2007 - Dark Passion Play'
    >>> f('Ayreon/2008 - 01011001/CD 1 - Y')
    'Ayreon/2008 - 01011001'

    """

    if re.compile(r'(?i)cd\s*\d+').match(os.path.split(albumpath)[1]):
        albumpath = os.path.split(albumpath)[0]
    return albumpath


def artwork_path(song, config):
    if song.name is not None:
        f = os.path.join(COVERS_DIR, "%s.jpg" % song.name.replace("/", ""))
    else:
        f = artwork_path_from_song(song, config)
    return f


# XXX check name
def artwork_get_misc_img_in_path(musicdir, songdir):
    path = os.path.join(musicdir, songdir)
    if os.path.exists(path):
        for name in consts.ART_LOCATIONS_MISC:
            filename = os.path.join(path, name)
            if os.path.exists(filename):
                return filename
    return False


def find_local_image(config, artist, album, song_dir):
    """Returns a tuple (location_type, filename) or (None, None).

    Only pass a artist, album and song directory if we don't want to use info
    from the currently playing song.
    """

    assert song_dir is not None, (artist, album, song_dir)

    get_artwork_path_from_song = functools.partial(artwork_path_from_data,
        artist, album, song_dir, config)

    # Give precedence to images defined by the user's current
    # art_location config (in case they have multiple valid images
    # that can be used for cover art).
    testfile = get_artwork_path_from_song()
    if os.path.exists(testfile):
        return config.art_location, testfile

    # Now try all local possibilities...
    simplelocations = [consts.ART_LOCATION_HOMECOVERS,
               consts.ART_LOCATION_COVER,
               consts.ART_LOCATION_ALBUM,
               consts.ART_LOCATION_FOLDER]
    for location in simplelocations:
        testfile = get_artwork_path_from_song(location)
        if os.path.exists(testfile):
            return location, testfile

    testfile = get_artwork_path_from_song(consts.ART_LOCATION_CUSTOM)
    if config.art_location == consts.ART_LOCATION_CUSTOM and \
       len(config.art_location_custom_filename) > 0 and \
       os.path.exists(testfile):
        return consts.ART_LOCATION_CUSTOM, testfile

    musicdir = config.current_musicdir
    if artwork_get_misc_img_in_path(musicdir, song_dir):
        return consts.ART_LOCATION_MISC, \
                artwork_get_misc_img_in_path(musicdir, song_dir)

    path = os.path.join(config.current_musicdir, song_dir)
    testfile = img.single_image_in_dir(path)
    if testfile is not None:
        return consts.ART_LOCATION_SINGLE, testfile

    return None, None


class Artwork(GObject.GObject):

    __gsignals__ = {
        'artwork-changed': (GObject.SIGNAL_RUN_FIRST, None,
                            (GdkPixbuf.Pixbuf,)),
        'artwork-reset': (GObject.SIGNAL_RUN_FIRST, None, ()),
    }

    def __init__(self, config, is_lang_rtl, schedule_gc_collect,
                 status_is_play_or_pause,
                 album_image, tray_image):
        super().__init__()

        self.config = config
        self.album_filename = 'sonata-album'

        # constants from main
        self.is_lang_rtl = is_lang_rtl

        # callbacks to main XXX refactor to clear this list
        self.schedule_gc_collect = schedule_gc_collect
        self.status_is_play_or_pause = status_is_play_or_pause

        # local pixbufs, image file names
        self.sonatacd = Gtk.IconFactory.lookup_default('sonata-cd')
        self.sonatacd_large = Gtk.IconFactory.lookup_default('sonata-cd-large')
        self.albumpb = None
        self.currentpb = None

        # local UI widgets provided to main by getter methods
        self.albumimage = album_image
        self.albumimage.set_from_icon_set(self.sonatacd, -1)

        self.tray_album_image = tray_image

        # local version of Main.songinfo mirrored by update_songinfo
        self.songinfo = None

        # local state
        self.lastalbumart = None
        self.single_img_in_dir = None
        self.misc_img_in_dir = None

        # local artwork, cache for library
        self.lib_model = None
        self.lib_art_rows_local = []
        self.lib_art_rows_remote = []
        self.lib_art_pb_size = 0

        self.cache = ArtworkCache(self.config)
        self.cache.load()

        self.jobs_queue = queue.Queue()
        self.results_queue = queue.Queue()
        self.x = 0

    def update_songinfo(self, songinfo):
        self.songinfo = songinfo

    def on_reset_image(self, _action):
        if self.songinfo:
            if 'name' in self.songinfo:
                # Stream, remove file:
                misc.remove_file(artwork_stream(self.songinfo.name))
            else:
                # Normal song:
                misc.remove_file(artwork_path_from_song(self.songinfo, self.config))
                misc.remove_file(artwork_path_from_song(
                    self.songinfo, self.config, consts.ART_LOCATION_HOMECOVERS))
                # Use blank cover as the artwork
                dest_filename = artwork_path_from_song(self.songinfo, self.config,
                                                 consts.ART_LOCATION_HOMECOVERS)
                try:
                    emptyfile = open(dest_filename, 'w')
                    emptyfile.close()
                except IOError:
                    pass
            self.artwork_update(True)

    def artwork_set_tooltip_art(self, pix):
        # Set artwork
        pix = pix.new_subpixbuf(0, 0, 77, 77)
        self.tray_album_image.set_from_pixbuf(pix)
        del pix

    def library_artwork_init(self, model, pb_size):

        self.lib_model = model
        self.lib_art_pb_size = pb_size

        # Launch thread
        ArtworkUpdateWorker(self.jobs_queue, self.results_queue, pb_size,
                            self.config).start()

    def library_artwork_update(self, model, start_row, end_row, albumpb):
        self.albumpb = albumpb

        # Update self.lib_art_rows_local with new rows followed
        # by the rest of the rows.
        start = start_row.get_indices()[0]
        end = end_row.get_indices()[0]
        test_rows = list(range(start, end + 1)) + list(range(len(model)))

        def loop(x):
            logger.error("%d starting with %d rows", x, len(test_rows))
            for i, row in enumerate(test_rows):
                iter = model.get_iter((row,))
                icon = model.get_value(iter, 0)
                if icon == self.albumpb:
                    data = model.get_value(iter, 1)
                    #logger.info("%d: pushing %s", x, data)
                    self.jobs_queue.put((iter, data, icon))

                if i % 50:
                    yield True
            logger.error("%d finishing", x)
            yield False
        self.x += 1
        l = loop(self.x)
        GLib.idle_add(next, l)

        GLib.timeout_add(500, self.check_results_queue)

    def check_results_queue(self):
        for i in range(20):
            try:
                (iter, pixbuf, data) = self.results_queue.get(False)
            except queue.Empty:
                break

            if self.lib_model.iter_is_valid(iter):
                if self.lib_model.get_value(iter, 1) == data:
                    logger.info("Setting library pixbuf for %s", data)
                    self.lib_model.set_value(iter, 0, pixbuf)

        GLib.timeout_add(500, self.check_results_queue)


    def library_set_image_for_current_song(self, cache_key):
        # Search through the rows in the library to see
        # if we match the currently playing song:
        if cache_key.artist is None and cache_key.album is None:
            return
        for row in self.lib_model:
            if str(cache_key.artist).lower() == str(row[1].artist).lower() \
            and str(cache_key.album).lower() == str(row[1].album).lower():
                pb = self.cache.get_pixbuf(cache_key, self.lib_art_pb_size)
                if pb:
                    self.lib_model.set_value(row.iter, 0, pb)

    def artwork_update(self, force=False):
        if force:
            self.lastalbumart = None

        if not self.config.show_covers:
            return
        if not self.songinfo:
            self.artwork_set_default_icon()
            return

        if self.status_is_play_or_pause():
            thread = threading.Thread(target=self._artwork_update,
                                      name="ArtworkUpdate")
            thread.daemon = True
            thread.start()
        else:
            self.artwork_set_default_icon()

    def _artwork_update(self):
        if 'name' in self.songinfo:
            # Stream
            streamfile = artwork_stream(self.songinfo.name)
            if os.path.exists(streamfile):
                GLib.idle_add(self.artwork_set_image, streamfile, None, None,
                              None)
            else:
                self.artwork_set_default_icon()
        else:
            # Normal song:
            artist = self.songinfo.artist or ""
            album = self.songinfo.album or ""
            path = os.path.dirname(self.songinfo.file)
            if len(artist) == 0 and len(album) == 0:
                self.artwork_set_default_icon(artist, album, path)
                return
            filename = artwork_path_from_song(self.songinfo, self.config)
            if filename == self.lastalbumart:
                # No need to update..
                return
            self.lastalbumart = None
            imgfound = self.artwork_check_for_local(artist, album, path)
            # XXX
            #if not imgfound:
                #if self.config.covers_pref == consts.ART_LOCAL_REMOTE:
                    #imgfound = self.artwork_check_for_remote(artist, album,
                                                             #path, filename)

    def artwork_check_for_local(self, artist, album, path):
        self.artwork_set_default_icon(artist, album, path)
        self.misc_img_in_dir = None
        self.single_img_in_dir = None
        location_type, filename = find_local_image(
            self.config, self.songinfo.artist, self.songinfo.album,
            os.path.dirname(self.songinfo.file))

        if location_type is not None and filename:
            if location_type == consts.ART_LOCATION_MISC:
                self.misc_img_in_dir = filename
            elif location_type == consts.ART_LOCATION_SINGLE:
                self.single_img_in_dir = filename
            GLib.idle_add(self.artwork_set_image, filename, artist, album, path)
            return True

        return False

    def artwork_check_for_remote(self, artist, album, path, filename):
        self.artwork_set_default_icon(artist, album, path)
        RemoteArtworkDownloader(self.config, artist, album, filename)
        if os.path.exists(filename):
            GLib.idle_add(self.artwork_set_image, filename, artist, album, path)
            return True
        return False

    def artwork_set_default_icon(self, artist=None, album=None, path=None):
        GLib.idle_add(self.albumimage.set_from_icon_set,
                      self.sonatacd, -1)
        self.emit('artwork-reset')
        GLib.idle_add(self.tray_album_image.set_from_icon_set,
                      self.sonatacd, -1)

        self.lastalbumart = None

        # Also, update row in library:
        if artist is not None:
            cache_key = SongRecord(artist=artist, album=album, path=path)
            self.cache.set(cache_key, self.album_filename)
            GLib.idle_add(self.library_set_image_for_current_song, cache_key)

    def artwork_set_image(self, filename, artist, album, path,
                          info_img_only=False):
        # Note: filename arrives here is in FILESYSTEM_CHARSET, not UTF-8!
        if self.artwork_is_for_playing_song(filename):
            if os.path.exists(filename):

                # We use try here because the file might exist, but might
                # still be downloading or corrupt:
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file(filename)
                except:
                    # If we have a 0-byte file, it should mean that
                    # sonata reset the image file. Otherwise, it's a
                    # bad file and should be removed.
                    if os.stat(filename).st_size != 0:
                        misc.remove_file(filename)
                    return

                self.currentpb = pix

                if not info_img_only:
                    # Store in cache
                    cache_key = SongRecord(artist=artist, album=album,
                                           path=path)
                    self.cache.set(cache_key, filename)

                    # Artwork for tooltip, left-top of player:
                    (pix1, w, h) = img.get_pixbuf_of_size(pix, 75)
                    pix1 = img.do_style_cover(self.config, pix1, w, h)
                    pix1 = img.pixbuf_add_border(pix1)
                    pix1 = img.pixbuf_pad(pix1, 77, 77)
                    self.albumimage.set_from_pixbuf(pix1)
                    self.artwork_set_tooltip_art(pix1)
                    del pix1

                    # Artwork for library, if current song matches:
                    self.library_set_image_for_current_song(cache_key)

                self.emit('artwork-changed', pix)
                del pix

                self.lastalbumart = filename

                self.schedule_gc_collect()

    def artwork_set_image_last(self):
        self.artwork_set_image(self.lastalbumart, None, None, None, True)

    def artwork_is_for_playing_song(self, filename):
        # Since there can be multiple threads that are getting album art,
        # this will ensure that only the artwork for the currently playing
        # song is displayed
        if self.status_is_play_or_pause() and self.songinfo:
            if 'name' in self.songinfo:
                streamfile = artwork_stream(self.songinfo.name)
                if filename == streamfile:
                    return True
            else:
                # Normal song:
                if (filename in [artwork_path_from_song(self.songinfo, self.config, l)
                                 for l in [consts.ART_LOCATION_HOMECOVERS,
                                           consts.ART_LOCATION_COVER,
                                           consts.ART_LOCATION_ALBUM,
                                           consts.ART_LOCATION_FOLDER,
                                           consts.ART_LOCATION_CUSTOM]] or
                    (self.misc_img_in_dir and \
                     filename == self.misc_img_in_dir) or
                    (self.single_img_in_dir and filename == \
                     self.single_img_in_dir)):
                    return True
        # If we got this far, no match:
        return False

    def have_last(self):
        if self.lastalbumart is not None:
            return True
        return False


class RemoteArtworkDownloaderWorker(threading.Thread):
    def __init__(self, config, artist, album,
                 destination, all_images=False):

        self.output_queue = queue.Queue()
        self._stop_event = threading.Event()
        args = (config, artist, album, destination,
                self.output_queue.put if all_images else None,
                self._stop_event.is_set)

        super().__init__(name="RemoteArtworkDownloaderThread", args=args,
                         target=RemoteArtworkDownloader)
        self.daemon = True

    def stop(self):
        self._stop_event.set()


class RemoteArtworkDownloader:
    def __init__(self, config, artist, album, dest_filename,
                 add_cb=None, should_stop_cb=lambda: False):
        self.config = config
        self.artist = artist
        self.album = album
        self.path = dest_filename
        self.max_images = 50 if add_cb else 1
        self.current = 0
        self.add_cb = add_cb if add_cb else lambda x: None
        self.should_stop_cb = should_stop_cb

        # Fetch covers from covers websites or such...
        cover_fetchers = pluginsystem.get('cover_fetching')
        for plugin, plugin_callback in cover_fetchers:
            logger.info("Looking for covers for %r from %r (using %s)",
                        self.album, self.artist, plugin.name)

            try:
                plugin_callback(self.artist, self.album,
                                self.on_save_callback, self.on_err_callback)
            except Exception as e:
                if logger.isEnabledFor(logging.DEBUG):
                    log = logger.exception
                else:
                    log = logger.warning

                log("Error while downloading covers from %s: %s",
                    plugin.name, e)

            if self.current > 0 or self.should_stop_cb():
                # The plugin founds images, no need to call the other plugins
                break

        logger.debug("Finished!")

    def on_save_callback(self, content_fp):
        """Return True to continue finding covers, False to stop finding
        covers."""

        self.current += 1
        if self.max_images > 1:
            path = self.path.replace("<imagenum>", str(self.current))
        else:
            path = self.path

        with open(path, 'wb') as fp:
            shutil.copyfileobj(content_fp, fp)

        pix = GdkPixbuf.Pixbuf.new_from_file(path)
        pix = pix.scale_simple(148, 148, GdkPixbuf.InterpType.HYPER)
        pix = img.do_style_cover(self.config, pix, 148, 148)
        pix = img.pixbuf_add_border(pix)
        logger.debug("Found artwork %s", path)
        self.add_cb((path, pix))
        del pix # XXX why?
        return not self.should_stop_cb()

    def on_err_callback(self, reason=None):
        """Return True to stop finding, False to continue finding covers."""
        return self.should_stop_cb()


class ArtworkCache:
    def __init__(self, config, path=None):
        self.logger = logging.getLogger('sonata.artwork.cache')
        self._cache = {}
        self.config = config
        self.path = path if path is not None else \
                os.path.expanduser("~/.config/sonata/art_cache")

    def set(self, key, value):
        self.logger.debug("Setting %r to %r", key, value)
        self._cache[key] = value

    def get(self, key):
        self.logger.debug("Requesting for %r", key)
        return self._cache.get(key)

    def get_pixbuf(self, key, size, default=None):
        self.logger.debug("Requesting pixbuf for %r", key)
        try:
            path = self._cache[key]
        except KeyError:
            return default

        if not os.path.exists(path):
            self._cache.pop(key, None)
            return default

        try:
            p = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
        except:
            self.logger.exception("Unable to load %r at size (%d, %d)",
                                  path, size, size)
            raise
        return img.do_style_cover(self.config, p, size, size)

    def save(self):
        self.logger.debug("Saving to %s", self.path)
        misc.create_dir(os.path.dirname(self.path))
        try:
            with open(self.path, 'w', encoding="utf8") as f:
                f.write(repr(self._cache))
        except IOError as e:
            self.logger.info("Unable to save: %s", e)

    def load(self):
        self.logger.debug("Loading from %s", self.path)
        self._cache = {}
        try:
            with open(self.path, 'r', encoding="utf8") as f:
                self._cache = eval(f.read())
        except (IOError, SyntaxError) as e:
            self.logger.info("Unable to load: %s", e)


class ArtworkUpdateWorker(threading.Thread):
    def __init__(self, job_queue, output_queue, size, config):
        self.queue = job_queue
        self.output_queue = output_queue # XXX
        self.size = size # XXX
        self.config = config
        super().__init__(name="LibArtworkUpdateWorker")
        self.daemon = True
        self.in_process = set()
        self._remote_queue = queue.Queue()

    def run(self):
        while True:
            i, data, icon = self.queue.get()
            if data in self.in_process or data.path is None:
                # "path is None" happens for 'Untagged' songs, maybe streams?
                continue
            else:
                self.in_process.add(data)

            if self.find_local(i, data):
                self._remote_queue.put(i, data, icon)

        #self.in_process.clear()

        #while True:
            #e, data, icon = self._remote_queue.get()
            #if data in self.in_process:
                #continue
            #else:
                #self.in_process.add(data)
            #self.find_remote(i, data):

    def find_local(self, i, data):
        cover_file = find_local_image(self.config, data.artist, data.album,
                                      data.path)[1]
        if not cover_file:
            logger.debug("No local artwork for %s", data)
            return False

        pb = self.build_pixbuf(cover_file)
        if pb is None:
            return False

        logger.debug("Found local artwork '%s' for %s", cover_file, data)
        self.output_queue.put((i, pb, data)) # XXX need to put data ?
        return True

    def find_remote(self, i, data):
        cover_file = artwork_path_from_data(data.artist, data.album, data.path,
                                            self.config)
        RemoteArtworkDownloader(config, data.artist, data.album, cover_file)

        pb = self.build_pixbuf(cover_file)
        if pb is None:
            logger.debug("No remote artwork for %s", data)
            return False

        logger.debug("Found remote artwork '%s' for %s", cover_file, data)
        self.output_queue.put((i, pb, data)) # XXX need to put data ?
        return True

    def build_pixbuf(self, cover_file):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(cover_file,
                                                        self.size, self.size)
        except Exception as e:
            # Delete bad image
            logger.warning("Unable to load image from '%s': %s", cover_file, e)
            misc.remove_file(cover_file)
            return

        w = pb.get_width()
        h = pb.get_height()
        return img.do_style_cover(self.config, pb, w, h)
