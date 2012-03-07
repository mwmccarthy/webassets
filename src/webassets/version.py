"""This module defines the Version classes, and the related Manifest
implementations.
"""

import os
import pickle

from webassets.bundle import has_placeholder, is_url, get_all_bundle_files
from webassets.updater import TimestampUpdater
from webassets.merge import FileHunk
from webassets.utils import md5_constructor


__all__ = ('get_versioner', 'VersionIndeterminableError',
           'Version', 'TimestampVersion',
           'get_manifest', 'HashVersion', 'Manifest', 'FileManifest',)


class VersionIndeterminableError(Exception):
    pass


class VersionRegistry(type):
    """Metaclass that registeres all subclasses."""

    VERSIONERS = {}

    def __new__(mcs, name, bases, attrs):
        new_klass = type.__new__(mcs, name, bases, attrs)
        if hasattr(new_klass, 'id'):
            mcs.VERSIONERS[new_klass.id] = new_klass
        return new_klass

    @classmethod
    def get_versioner(mcs, thing):
        if hasattr(thing, 'get_version_for'):
            if isinstance(thing, type):
                return thing()
            return thing
        if not thing:
            return None
        try:
            return mcs.VERSIONERS[thing]()
        except KeyError:
            raise ValueError('Versioner "%s" is not valid.' % thing)


get_versioner = VersionRegistry.get_versioner


class Version(object):
    """A Version class that can be assigned to the ``Environment.versioner``
    attribute.

    Given a bundle, this must determine it's "version". This version can then
    be used in the output filename of the bundle, or appended to the url as a
    query string, in order to expire cached assets.

    A version could be a timestamp, a content hash, or a git revision etc.

    As a user, all you need to care about, in most cases, is whether you want
    to set the ``Environment.versioner`` attribute to ``hash`` or ``timestamp``.
    """
    
    __metaclass__ = VersionRegistry

    default_updater = TimestampUpdater

    def __eq__(self, other):
        """Return equality with the config values that instantiate this
        instance.
        """
        return (hasattr(self, 'id') and self.id == other) or \
               id(self) == id(other)

    def get_version_for(self, bundle, hunk=None, env=None):
        """Return a string that represents the current version of the given
        bundle.

        This method is called on two separate occasions:

        1) After a bundle has been built and is about to be saved. If the
           output filename contains a placeholder, this method is asked for the
           version. This mode is indicated by the ``hunk`` argument being
           available.

        2) When a version is required for an already built file, either
           because:

              *) An URL needs to be constructed.
              *) It needs to be determined if a bundle needs an update.

           *This will only occur* if *no manifest* is used. If there is a
           manifest, it would be used to determine the version instead.

        Support for option (2) is optional. If not supported, then in those
        cases a manifest needs to be configured. ``VersionIndeterminableError``
        should be raised with a message why.
        """
        raise NotImplementedError()

    def set_version(self, bundle, env, filename, version):
        """Hook called after a bundle has been built. Some version classes
        may need this.
        """

    def _get_updater(self):
        if not hasattr(self, '_updater'):
            self._updater = self.default_updater()
        return self._updater
    def _set_updater(self, value):
        self._updater = value
    updater = property(_get_updater, _set_updater, doc="""
    Updater to use to determine whether a rebuild is required.

    This is an attribute of the Version class, because whether or
    not a rebuild is required is essentially a function of the
    "version" concept.
    """)


class TimestampVersion(Version):
    """Uses the most recent 'last modified' timestamp of all source files
    as the version.

    Uses second-precision.
    """

    id = 'timestamp'

    def determine_version(self, bundle, hunk, env=None):
        # Only look at an existing output file if we are not about to
        # overwrite it with a new version. But if we can, simply using the
        # timestamp of the final file is the fastest way to do this.
        # Note that this works because of our ``save_done`` hook.
        if not hunk:
            if not has_placeholder(bundle.output):
                return self.get_timestamp(bundle.resolve_output())

        # If we need the timestamp for the file we just built (hunk!=None),
        # or if we need the timestamp for a bundle with a placeholder,
        # the way to get it is by looking at the source files.
        try:
            self.find_recent_most_timestamp(bundle)
        except OSError:
            # Source files are missing. Under these circumstances, we cannot
            # return a proper version.
            assert hunk is not None
            raise VersionIndeterminableError(
                'source files are missing and output target has a '
                'placeholder')

    def set_version(self, bundle, env, filename, version):
        # Update the mtime of the newly created file with the version
        print version
        os.utime(filename, (-1, version))

    @classmethod
    def get_timestamp(cls, filename):
        return os.stat(filename).st_mtime    # Let OSError pass

    @classmethod
    def find_recent_most_timestamp(cls, bundle, env=None):
        # Recurse through the bundle hierarchy. Check the timestamp of all
        # the bundle source files, as well as any additional
        # dependencies that we are supposed to watch.
        most_recent = None
        for filename in get_all_bundle_files(bundle, env):
            if is_url(filename):
                continue
            timestamp = cls.get_timestamp(filename)
            if most_recent is None or timestamp > most_recent:
                most_recent = timestamp
        return most_recent


class HashVersion(Version):
    """Uses the MD5 hash of the content as the version.

    By default, only the first 8 characters of the hash are used, which
    should be sufficient. This can be changed by passing the appropriate
    ``length`` value to ``__init__`` (or ``None`` to use the full hash).

    You can also customize the hash used by passing the ``hash`` argument.
    All constructors from ``hashlib`` are supported.
    """

    id = 'hash'

    def __init__(self, length=8, hash=md5_constructor):
        self.length = length
        self.hasher = hash

    def get_version_for(self, bundle, hunk=None, env=None):
        if not hunk:
            if not has_placeholder(bundle.output):
                hunk = FileHunk(bundle.resolve_output())
            else:
                # Can cannot determine the version of placeholder files.
                raise VersionIndeterminableError(
                    'output target has a placeholder')

        hasher = self.hasher()
        hasher.update(hunk.data())
        return hasher.hexdigest()[:self.length]


class Manifest(object):
    """Persists information about the versions bundles are at.

    The Manifest plays a role only if you insert the bundle version in your
    output filenames, or append the version as a querystring to the url (via
    the url_expire option). It serves two purposes:

        - Without a manifest, it may be impossible to determine the version
          at runtime. In a deployed app, the media files may be stored on
          a different server entirely, and be inaccessible from the application
          code. The manifest, if shipped with your application, is what still
          allows to construct the proper URLs.

        - Even if it were possible to determine the version at runtime without
          a manifest, it may be a costly process, and using a manifest may
          give you better performance. If you use a hash-based version for
          example, this hash would need to be recalculated every time a new
          process is started. (*)

    (*) It needs to happen only once per process, because each Bundle is smart
        enough to cache their own version in memory.

    A special case is the ``Environment.auto_build`` option. A manifest
    implementation should re-read it's data from it's out-of-process data
    source on every request, if ``auto_build`` is enabled. Otherwise, if your
    application is served by multiple processes, then after an automatic
    rebuild in one process all other processes would continue to serve an old
    version of the file (or attach an old version to the query string).

    It is important for the manifest to read from it's data source
    on every request if autobuild is enabled (at least if you want to support
    the option). if the data source is
    cached in the process space, and your app is served by multiple
    processes, then you might yield old version information, and you might
    continue to serve the old file, or attach the wrong url expire string.
    """

    def remember(self, bundle, env, version):
        raise NotImplementedError()

    def query(self, bundle, env):
        raise NotImplementedError()


class FileManifest(object):
    """Stores version data in a single file.

    Uses Python's pickle module to stores a dict data structure. You should
    only use this when the manifest is read-only in production, since it is
    not multi-process safe. If you use ``auto_build`` in production, use
    ``CacheManifest`` instead.

    By default, the file is named ".webassets-manifest" and stored in
    ``Environment.directory``.
    """

    def __init__(self, filename):
        self.filename = filename
        self._load_manifest()

    def remember(self, bundle, env, version):
        # TODO: Since it is possible to use the same bundle with multiple
        # environments, some kind of env id should be included here. However,
        # since this is mostly a theoretical possibility, we sort of don't
        # want to bother the user with this.
        self.manifest[bundle.output] = version
        self._save_manifest()

    def query(self, bundle, env):
        if env.auto_build:
            self._load_manifest()
        return self.manifest.get(bundle.output, None)

    def _load_manifest(self):
        if os.path.exists(self.filename):
            with open(self.filename, 'rb') as f:
                self.manifest = pickle.load(f)
        else:
            self.manifest = {}

    def _save_manifest(self):
        with open(self.filename, 'wb') as f:
            pickle.dump(self.manifest, f, protocol=2)


class CacheManifest(object):
    """Stores version data in the webassets cache.

    Since this has bad portability (you hardly want to copy your cache  between
    machines), this only makes sense when you are building on the same machine
    where you're application code runs.

    When you are using ``auto_build`` in production, this is exactly what you
    want to use, since it is multi-process safe.
    """

    # Implementation notes: Do not support MemoryCache.

    def __init__(self):
        raise NotImplementedError()   # TODO


class SymlinkManifest(object):
    """Creates a symlink to the actual file.

    E.g. compressed-current.js -> compressed-1ebcdc5.js
    """

    # Implementation notes: Would presumably be Linux only initially,
    # could clean up after itself, may be hard to implement and maybe
    # shouldn't, would only we usable to resolve placeholders in filenames.

    def __init__(self):
        raise NotImplementedError()   # TODO


def get_manifest(val):
    return val
