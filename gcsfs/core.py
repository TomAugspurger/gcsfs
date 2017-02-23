# -*- coding: utf-8 -*-
"""
Google Cloud Storage pythonic interface
"""
from __future__ import print_function

import io
import json
from json.decoder import JSONDecodeError
import logging
import oauth2client.client   # version 1.5.2
import os
import pickle
import re
import requests
import sys
import time
import warnings
import webbrowser

from .utils import read_block
PY2 = sys.version_info.major == 2

logger = logging.getLogger(__name__)

not_secret = {"client_id": "586241054156-is96mugvl2prnj0ib5gsg1l3q9m9jp7p."
                           "apps.googleusercontent.com",
              "client_secret": "_F-W4r2HzuuoPvi6ROeaUB6o"}
tfile = os.path.join(os.path.expanduser("~"), '.gcs_tokens')
ACLs = {"authenticatedread", "bucketownerfullcontrol", "bucketownerread",
        "private", "projectprivate", "publicread"}
bACLs = {"authenticatedRead", "private", "projectPrivate", "publicRead",
         "publicReadWrite"}
DEFAULT_PROJECT = os.environ.get('GCSFS_DEFAULT_PROJECT', '')


def quote_plus(s):
    """
    Convert some URL elements to be HTTP-safe.

    Not the same as in urllib, because, for instance, parentheses and commas
    are passed through.

    Parameters
    ----------
    s: input URL/portion

    Returns
    -------
    corrected URL
    """
    s = s.replace('/', '%2F')
    s = s.replace(' ', '%20')
    return s

def split_path(path):
    """
    Normalise S3 path string into bucket and key.

    Parameters
    ----------
    path : string
        Input path, like `s3://mybucket/path/to/file`

    Examples
    --------
    >>> split_path("s3://mybucket/path/to/file")
    ['mybucket', 'path/to/file']
    """
    if path.startswith('gcs://'):
        path = path[6:]
    if path.startswith('gs://'):
        path = path[5:]
    if '/' not in path:
        return path, ""
    else:
        return path.split('/', 1)


class GCSFileSystem(object):
    """
    Connect to Google Cloud Storage.

    Two modes of authentication are supported:
    - if ``token=None``, you will be given a "device code", which you must
      enter into a browser where you are logged in with your Google identity.
    - you may supply a token generated by the
      [gcloud](https://cloud.google.com/sdk/docs/)
      utility; this is either a python dictionary, or the name of a file
      containing the JSON returned by logging in with the gcloud CLI tool. On
      a posix system this may be at
      ``~/.config/gcloud/application_default_credentials.json``

    We maintain a cache of refresh tokens in the file ~/.gcs_tokens, so for any
    pair of (project, access), you will not need to log in once your credentials
    are verified.

    Parameters
    ----------
    project : string
        GCS users may only access to contents of one project in a single
        instance of GCSFileSystem. This is required in order
        to list all the buckets you have access to within a project.
    access : one of {'read_only', 'read_write', 'full_control'}
        Full control implies read/write as well as modifying metadata,
        e.g., access control.
    token: None, dict or string
        (see description of authentication methods, above)
    """
    scopes = {'read_only', 'read_write', 'full_control'}
    retries = 10
    base = "https://www.googleapis.com/storage/v1/"
    _singleton = [None]

    def __init__(self, project=DEFAULT_PROJECT, access='full_control',
                 token=None):
        if access not in self.scopes:
            raise ValueError('access must be one of {}', self.scopes)
        if project is None:
            warnings.warn('GCS project not set - cannot list or create buckets')
        if token is not None:
            if 'type' in token or isinstance(token, str):
                token = self._parse_gtoken(token)
            self.tokens[(project, access)] = token
        self.project = project
        self.access = access
        self.dirs = {}
        self.connect()
        self._singleton[0] = self

    @classmethod
    def current(cls):
        """ Return the most recently created S3FileSystem

        If no S3FileSystem has been created, then create one
        """
        if not cls._singleton[0]:
            return GCSFileSystem()
        else:
            return cls._singleton[0]

    @staticmethod
    def _parse_gtoken(t):
        if isinstance(t, str):
            t = json.load(open(t))
        else:
            t = t.copy()
        typ = t.pop('type')
        if typ != "authorized_user":
            raise ValueError("Only 'authorized_user' tokens accepted, got: %s"
                             % typ)
        t['grant_type'] = 'refresh_token'
        t['timestamp'] = time.time()
        t['expires_in'] = 0
        return t

    @staticmethod
    def load_tokens():
        try:
            with open(tfile, 'rb') as f:
                tokens = pickle.load(f)
        except Exception:
            tokens = {}
        GCSFileSystem.tokens = tokens

    def connect(self, refresh=False):
        """
        Establish session token. A new token will be requested if the current
        one is within 100s of expiry.

        Parameters
        ----------
        refresh: bool (False)
            Force refresh, even if the token is expired.
        """
        project, access = self.project, self.access
        if (project, access) in self.tokens:
            # cached credentials
            data = self.tokens[(project, access)]
        else:
            # no credentials - try to ask google in the browser
            scope = "https://www.googleapis.com/auth/devstorage." + access
            r = requests.post('https://accounts.google.com/o/oauth2/device/code',
                              params={'client_id': not_secret['client_id'],
                                      'scope': scope})
            if r.status_code != 200:
                raise RuntimeError
            data = json.loads(r.content.decode())
            print('Enter the following code when prompted in the browser:')
            print(data['user_code'])
            webbrowser.open(data['verification_url'])
            for i in range(self.retries):
                time.sleep(2)
                r = requests.post(
                        "https://www.googleapis.com/oauth2/v4/token",
                        params={'client_id': not_secret['client_id'],
                                'client_secret': not_secret['client_secret'],
                                'code': data['device_code'],
                                'grant_type':
                                    "http://oauth.net/grant_type/device/1.0"})
                data2 = json.loads(r.content.decode())
                if 'error' in data2:
                    if i == self.retries - 1:
                        raise RuntimeError("Waited too long for browser"
                                           "authentication.")
                    continue
                data = data2
                break
            data['timestamp'] = time.time()
            data.update(not_secret)
        if refresh or time.time() - data['timestamp'] > data['expires_in'] - 100:
            # token has expired, or is about to - call refresh
            r = requests.post(
                    "https://www.googleapis.com/oauth2/v4/token",
                    params={'client_id': data['client_id'],
                            'client_secret': data['client_secret'],
                            'refresh_token': data['refresh_token'],
                            'grant_type': "refresh_token"})
            if r.status_code != 200:
                raise RuntimeError
            data['timestamp'] = time.time()
            data['access_token'] = json.loads(r.content.decode())['access_token']

        self.tokens[(project, access)] = data
        self.header = {'Authorization': 'Bearer ' + data['access_token']}
        self._save_tokens()

    def _save_tokens(self):
        try:
            with open(tfile, 'wb') as f:
                pickle.dump(self.tokens, f, 2)
        except Exception:
            pass

    def _call(self, method, path, *args, **kwargs):
        for k, v in list(kwargs.items()):
            # only pass parameters that have values
            if v is None:
                del kwargs[k]
        json = kwargs.pop('json', None)
        meth = getattr(requests, method)
        if args:
            path = path.format(*[quote_plus(p) for p in args])
        r = meth(self.base + path, headers=self.header, params=kwargs,
                 json=json)
        try:
            out = r.json()
        except JSONDecodeError:
            out = r.content
        if not r.ok:
            if "Not Found" in str(out):
                raise FileNotFoundError(path)
            else:
                raise RuntimeError(str(out))
        return out

    def _list_buckets(self):
        if '' not in self.dirs:
            out = self._call('get', 'b/', project=self.project)
            dirs = out.get('items', [])
            self.dirs[''] = dirs
        return self.dirs['']

    def _list_bucket(self, bucket):
        if bucket not in self.dirs:
            out = self._call('get', 'b/{}/o/', bucket)
            dirs = out.get('items', [])
            for f in dirs:
                f['name'] = '%s/%s' % (bucket, f['name'])
                f['size'] = int(f.get('size'), 0)
            self.dirs[bucket] = dirs
        return self.dirs[bucket]

    def mkdir(self, bucket, acl='projectPrivate',
              default_acl='bucketOwnerFullControl'):
        """
        New bucket

        Parameters
        ----------
        bucket: str
            bucket name
        acl: string, one of bACLs
            access for the bucket itself
        default_acl: str, one of ACLs
            default ACL for objects created in this bucket
        """
        self._call('post', 'b/', predefinedAcl=acl, project=self.project,
                   json={"name": bucket})
        self.invalidate_cache(bucket)
        self.invalidate_cache('')

    def rmdir(self, bucket):
        """Delete an empty bucket"""
        self._call('delete', 'b/' + bucket)
        if '' in self.dirs:
            for v in self.dirs[''].copy():
                if v['name'] == bucket:
                    self.dirs[''].remove(v)
        self.invalidate_cache(bucket)

    def invalidate_cache(self, bucket=None):
        """
        Mark files cache as dirty, so that it is reloaded on next use.

        Parameters
        ----------
        bucket: string or None
            If None, clear all files cached; if a string, clear the files
            corresponding to that bucket.
        """
        if bucket in {'/', '', None}:
            self.dirs.clear()
        else:
            self.dirs.pop(bucket, None)

    def ls(self, path, detail=False):
        if path in ['', '/']:
            out = self._list_buckets()
        else:
            bucket, prefix = split_path(path)
            path = '/'.join([bucket, prefix])
            files = self._list_bucket(bucket)
            seek, l, bit = (path, len(path), '') if path.endswith('/') else (
                path+'/', len(path)+1, '/')
            out = []
            for f in files:
                if (f['name'].startswith(seek) and '/' not in f['name'][l:] or
                        f['name'] == path):
                    out.append(f)
                elif f['name'].startswith(seek) and '/' in f['name'][l:]:
                    directory = {
                        'bucket': bucket, 'kind': 'storage#object',
                        'size': 0, 'storageClass': 'DIRECTORY',
                        'name': path+bit+f['name'][l:].split('/', 1)[0]+'/'}
                    if directory not in out:
                        out.append(directory)
        if detail:
            return out
        else:
            return [f['name'] for f in out]

    def walk(self, path, detail=False):
        bucket, prefix = split_path(path)
        path = '/'.join([bucket, prefix])
        files = self._list_bucket(bucket)
        if path.endswith('/'):
            files = [f for f in files if f['name'].startswith(path) or
                     f['name'] == path]
        else:
            files = [f for f in files if f['name'].startswith(path+'/') or
                     f['name'] == path]
        if detail:
            return files
        else:
            return [f['name'] for f in files]

    def du(self, path, total=False, deep=False):
        if deep:
            files = self.walk(path, True)
        else:
            files = [f for f in self.ls(path, True)]
        if total:
            return sum(f['size'] for f in files)
        return {f['name']: f['size'] for f in files}

    def glob(self, path):
        """
        Find files by glob-matching.

        Note that the bucket part of the path must not contain a "*"
        """
        path = path.rstrip('/')
        bucket, key = split_path(path)
        path = '/'.join([bucket, key])
        if "*" in bucket:
            raise ValueError('Bucket cannot contain a "*"')
        if '*' not in path:
            path = path.rstrip('/') + '/*'
        if '/' in path[:path.index('*')]:
            ind = path[:path.index('*')].rindex('/')
            root = path[:ind + 1]
        else:
            root = ''
        allfiles = self.walk(root)
        pattern = re.compile("^" + path.replace('//', '/')
                             .rstrip('/')
                             .replace('*', '[^/]*')
                             .replace('?', '.') + "$")
        out = [f for f in allfiles if re.match(pattern,
               f.replace('//', '/').rstrip('/'))]
        return out

    def exists(self, path):
        bucket, key = split_path(path)
        try:
            if key:
                return bool(self.info(path))
            else:
                return bucket in self.ls('')
        except FileNotFoundError:
            return False

    def info(self, path):
        path = '/'.join(split_path(path))
        files = self.ls(path, True)
        out = [f for f in files if f['name'] == path]
        if out:
            return out[0]
        else:
            raise FileNotFoundError(path)

    def cat(self, path):
        """ Simple one-shot get of file data """
        details = self.info(path)
        return _fetch_range(self.header, details)

    def get(self, rpath, lpath, blocksize=5 * 2 ** 20):
        with self.open(rpath, 'rb', block_size=0), open(lpath, 'wb') as f1, f2:
            while True:
                d = f1.read(blocksize)
                if not d:
                    break
                f2.write(d)

    def head(self, path, size=1024):
        with self.open(path, 'rb') as f:
            return f.read(size)

    def tail(self, path, size=1024):
        if size > self.info(path)['size']:
            return self.cat(path)
        with self.open(path, 'rb') as f:
            f.seek(-size, 2)
            return f.read()

    def merge(self, path, paths, acl=None):
        """Concatenate objects within a single bucket"""
        bucket, key = split_path(path)
        source = [{'name': split_path(p)[1]} for p in paths]
        self._call('post', 'b/{}/o/{}/compose', bucket, key,
                   destinationPredefinedAcl=acl,
                   json={'sourceObjects': source,
                         "kind": "storage#composeRequest",
                         'destination': {'name': key, 'bucket': bucket}})

    def copy(self, path1, path2, acl=None):
        b1, k1 = split_path(path1)
        b2, k2 = split_path(path2)
        self._call('post', 'b/{}/o/{}/copyTo/b/{}/o/{}', b1, k1, b2, k2,
                   destinationPredefinedAcl=acl)

    def rm(self, path):
        bucket, path = split_path(path)
        self._call('delete', "b/{}/o/{}", bucket, path)
        self.invalidate_cache(bucket)

    def open(self, path, mode='rb', block_size=5 * 2 ** 20, acl=None):
        return GCSFile(self, path, mode, block_size)

    def touch(self, path):
        with self.open(path, 'wb'):
            pass

    def read_block(self, fn, offset, length, delimiter=None):
        """ Read a block of bytes from a GCS file

        Starting at ``offset`` of the file, read ``length`` bytes.  If
        ``delimiter`` is set then we ensure that the read starts and stops at
        delimiter boundaries that follow the locations ``offset`` and ``offset
        + length``.  If ``offset`` is zero then we start at zero.  The
        bytestring returned WILL include the end delimiter string.

        If offset+length is beyond the eof, reads to eof.

        Parameters
        ----------
        fn: string
            Path to filename on S3
        offset: int
            Byte offset to start read
        length: int
            Number of bytes to read
        delimiter: bytes (optional)
            Ensure reading starts and stops at delimiter bytestring

        Examples
        --------
        >>> gcs.read_block('data/file.csv', 0, 13)  # doctest: +SKIP
        b'Alice, 100\\nBo'
        >>> gcs.read_block('data/file.csv', 0, 13, delimiter=b'\\n')  # doctest: +SKIP
        b'Alice, 100\\nBob, 200\\n'

        Use ``length=None`` to read to the end of the file.
        >>> gcs.read_block('data/file.csv', 0, None, delimiter=b'\\n')  # doctest: +SKIP
        b'Alice, 100\\nBob, 200\\nCharlie, 300'

        See Also
        --------
        distributed.utils.read_block
        """
        with self.open(fn, 'rb') as f:
            size = f.size
            if length is None:
                length = size
            if offset + length > size:
                length = size - offset
            bytes = read_block(f, offset, length, delimiter)
        return bytes

GCSFileSystem.load_tokens()


class GCSFile:

    def __init__(self, gcsfs, path, mode='rb', block_size=5 * 2 ** 20,
                 acl=None):
        bucket, key = split_path(path)
        self.gcsfs = gcsfs
        self.bucket = bucket
        self.key = key
        if mode not in {'rb', 'wb'}:
            raise ValueError('File mode not supported')
        self.mode = mode
        self.blocksize = block_size
        self.cache = b""
        self.loc = 0
        self.acl = acl
        self.end = None
        self.start = None
        self.closed = False
        self.trim = True
        if mode == 'rb':
            self.details = gcsfs.info(path)
            self.size = self.details['size']
        else:
            self.buffer = io.BytesIO()
            self.offset = 0
            self.forced = False

    def info(self):
        """ File information about this path """
        return self.details

    def tell(self):
        """ Current file location """
        return self.loc

    def seek(self, loc, whence=0):
        """ Set current file location

        Parameters
        ----------
        loc : int
            byte location
        whence : {0, 1, 2}
            from start of file, current location or end of file, resp.
        """
        if not self.mode == 'rb':
            raise ValueError('Seek only available in read mode')
        if whence == 0:
            nloc = loc
        elif whence == 1:
            nloc = self.loc + loc
        elif whence == 2:
            nloc = self.size + loc
        else:
            raise ValueError(
                "invalid whence (%s, should be 0, 1 or 2)" % whence)
        if nloc < 0:
            raise ValueError('Seek before start of file')
        self.loc = nloc
        return self.loc

    def readline(self, length=-1):
        """
        Read and return a line from the stream.

        If length is specified, at most size bytes will be read.
        """
        self._fetch(self.loc, self.loc + 1)
        while True:
            found = self.cache[self.loc - self.start:].find(b'\n') + 1
            if length > 0 and found > length:
                return self.read(length)
            if found:
                return self.read(found)
            if self.end > self.size:
                return self.read(length)
            self._fetch(self.start, self.end + self.blocksize)

    def __next__(self):
        data = self.readline()
        if data:
            return data
        else:
            raise StopIteration

    next = __next__

    def __iter__(self):
        return self

    def readlines(self):
        """ Return all lines in a file as a list """
        return list(self)

    def write(self, data):
        """
        Write data to buffer.

        Buffer only sent to S3 on flush() or if buffer is greater than or equal to blocksize.

        Parameters
        ----------
        data : bytes
            Set of bytes to be written.
        """
        if self.mode not in {'wb', 'ab'}:
            raise ValueError('File not in write mode')
        if self.closed:
            raise ValueError('I/O operation on closed file.')
        out = self.buffer.write(ensure_writable(data))
        self.loc += out
        if self.buffer.tell() >= self.blocksize:
            self.flush()
        return out

    def flush(self, force=False):
        """
        Write buffered data to S3.

        Uploads the current buffer, if it is larger than the block-size, or if
        the file is being closed.

        Parameters
        ----------
        force : bool
            When closing, write the last block even if it is smaller than
            blocks are allowed to be.
        """
        if self.mode not in {'wb', 'ab'}:
            raise ValueError('Flush on a file not in write mode')
        if self.closed:
            raise ValueError('Flush on closed file')
        if self.buffer.tell() == 0 and not force:
            # no data in the buffer to write
            return
        if force and self.forced:
            raise ValueError("Force flush cannot be called more than once")
        if force:
            self.forced = True
        if force and not self.offset and self.buffer.tell() <= 5 * 2**20:
            self._simple_upload()
            return

        if not self.offset:
            self._initiate_upload()
        self._upload_chunk(final=force)

    def _upload_chunk(self, final=False):
        self.buffer.seek(0)
        data = self.buffer.read()
        head = self.gcsfs.header.copy()
        l = len(data)
        if final:
            if l:
                head['Content-Range'] = 'bytes %i-%i/%i' % (
                    self.offset, self.offset + l - 1, self.offset + l)
            else:
                # closing when buffer is empty
                head['Content-Range'] = 'bytes */%i' % self.offset
                data = None
        else:
            head['Content-Range'] = 'bytes %i-%i/*' % (
                self.offset, self.offset + l - 1)
        head.update({'Content-Type': 'application/octet-stream',
                     'Content-Length': str(l)})
        r = requests.post(self.location, params={'uploadType': 'resumable'},
                          headers=head, data=data)
        if not r.ok:
            raise RuntimeError(r.text)
        if 'Range' in r.headers:
            shortfall = (self.offset + l - 1) - int(
                    r.headers['Range'].split('-')[1])
            if shortfall:
                self.buffer = io.BytesIO(data[-shortfall:])
                self.buffer.seek(shortfall)
            else:
                self.buffer = io.BytesIO()
            import pdb
            # pdb.set_trace()
            self.offset += l - shortfall
        else:
            self.buffer = io.BytesIO()
            self.offset += l

    def _initiate_upload(self):
        r = requests.post('https://www.googleapis.com/upload/storage/v1/b/%s/o'
                          % quote_plus(self.bucket),
                          params={'uploadType': 'resumable'},
                          headers=self.gcsfs.header, json={'name': self.key})
        self.location = r.headers['Location']

    def _simple_upload(self):
        """One-shot upload, less than 5MB"""
        head = self.gcsfs.header.copy()
        self.buffer.seek(0)
        data = self.buffer.read()
        r = requests.post('https://www.googleapis.com/upload/storage/v1/b/%s/o'
                          % quote_plus(self.bucket),
                          params={'uploadType': 'media', 'name': self.key},
                          headers=head, data=data)
        if not r.ok:
            raise RuntimeError(r.text)

    def _fetch(self, start, end):
        if self.start is None and self.end is None:
            # First read
            self.start = start
            self.end = end + self.blocksize
            self.cache = _fetch_range(self.gcsfs.header, self.details,
                                      start, self.end)
        if start < self.start:
            new = _fetch_range(self.gcsfs.header, self.details,
                               start, self.start)
            self.start = start
            self.cache = new + self.cache
        if end > self.end:
            if self.end > self.size:
                return
            new = _fetch_range(self.gcsfs.header, self.details,
                               self.end, end + self.blocksize)
            self.end = end + self.blocksize
            self.cache = self.cache + new

    def read(self, length=-1):
        """
        Return data from cache, or fetch pieces as necessary

        Parameters
        ----------
        length : int (-1)
            Number of bytes to read; if <0, all remaining bytes.
        """
        if self.mode != 'rb':
            raise ValueError('File not in read mode')
        if length < 0:
            length = self.size
        if self.closed:
            raise ValueError('I/O operation on closed file.')
        self._fetch(self.loc, self.loc + length)
        out = self.cache[self.loc - self.start:
                         self.loc - self.start + length]
        self.loc += len(out)
        if self.trim:
            num = (self.loc - self.start) // self.blocksize - 1
            if num > 0:
                self.start += self.blocksize * num
                self.cache = self.cache[self.blocksize * num:]
        return out

    def close(self):
        """ Close file """
        if self.closed:
            return
        if self.mode == 'rb':
            self.cache = None
        else:
            self.flush(force=True)
            self.gcsfs.invalidate_cache(self.bucket)
        self.closed = True

    def readable(self):
        """Return whether the GCSFile was opened for reading"""
        return self.mode == 'rb'

    def seekable(self):
        """Return whether the GCSFile is seekable (only in read mode)"""
        return self.readable()

    def writable(self):
        """Return whether the GCSFile was opened for writing"""
        return self.mode in {'wb', 'ab'}

    def __del__(self):
        self.close()

    def __str__(self):
        return "<GCSFile %s/%s>" % (self.bucket, self.key)

    __repr__ = __str__

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _fetch_range(head, obj_dict, start=None, end=None):
    """ Get data from GCS

    head : dict
        Contains authorization header
    obj_dict : an entry from ls() or info()
    start, end : None or integers
        if not both None, fetch only given range
    """
    logger.debug("Fetch: {}/{}, {}-{}", obj_dict['name'], start, end)
    if start is not None or end is not None:
        start = start or 0
        end = end or -0
        head = head.copy()
        head['Range'] = 'bytes=%i-%i' % (start, end - 1)
    back = requests.get(obj_dict['mediaLink'], headers=head)
    data = back.content
    return data


def put_object(credentials, bucket, name, data):
    """ Simple put, up to 5MB of data

    credentials : from auth()
    bucket : string
    name : object name
    data : binary
    """
    out = requests.post('https://www.googleapis.com/upload/storage/'
                        'v1/b/%s/o?uploadType=media&name=%s' % (
                            quote_plus(bucket), quote_plus(name)),
                        headers={'Authorization': 'Bearer '+credentials.access_token,
                                 'Content-Type': 'application/octet-stream',
                                 'Content-Length': len(data)}, data=data)
    assert out.status_code == 200


def ensure_writable(b):
    if PY2 and isinstance(b, array.array):
        return b.tostring()
    return b
