#!/usr/bin/env python

import os, sys
import logging
import argparse
import xxhash
import paramiko
import threading

from errno import ENOENT
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn

from oxfs.cache import MemoryCache
from oxfs.task_executor import TaskExecutorService, Task

class OXFS(LoggingMixIn, Operations):
    '''
    A simple sftp filesystem with powerfull cache. Requires paramiko: http://www.lag.net/paramiko/

    You need to be able to login to remote host without entering a password.
    '''

    def __init__(self, host, user, cache_path, port=22):
        self.logger = logging.getLogger('oxfs')
        self.host = host
        self.port = port
        self.user = user
        self.cache_path = cache_path
        self.client, self.sftp = self.open_sftp()
        self.taskpool = TaskExecutorService(2)
        self.attributes = MemoryCache(prefix='attributes')
        self.directories = MemoryCache(prefix='directories')

        if not os.path.exists(self.cache_path):
            os.makedirs(self.cache_path)

    def open_sftp(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.load_system_host_keys()
        client.connect(self.host, port=self.port, username=self.user)
        return client, client.open_sftp();

    def current_thread_sftp(self, thread_local_data):
        sftp = thread_local_data.get('sftp')
        if sftp is not None:
            return sftp

        client, sftp = self.open_sftp()
        thread_local_data['sftp'] = sftp
        thread_local_data['client'] = client
        # thread terminate hook
        thread_local_data['exit_hook'] = self.sftp_destroy
        return thread_local_data['sftp']

    def sftp_destroy(self, thread_local_data):
        client = thread_local_data.get('client')
        sftp = thread_local_data.get('sftp')
        if sftp is not None:
            sftp.close()
            client.close()

    def cachefile(self, path):
        return os.path.join(self.cache_path, xxhash.xxh64_hexdigest(path))

    def trylock(self, path):
        lockfile = self.cachefile(path) + '.lockfile'
        if os.path.exists(lockfile):
            return False
        open(lockfile, 'wb').close()
        return True

    def unlock(self, path):
        lockfile = self.cachefile(path) + '.lockfile'
        os.remove(lockfile)

    def getfile(self, thread_local_data, path):
        if not self.trylock(path):
            self.logger.info('getfile lock failed {}'.format(path))
            return False

        self.logger.info('getfile {}'.format(path))
        sftp = self.current_thread_sftp(thread_local_data)
        cachefile = self.cachefile(path)
        tmpfile = cachefile + '.tmpfile'
        with open(tmpfile, 'wb') as outfile:
            with sftp.open(path, 'rb') as infile:
                outfile.write(infile.read())

        os.rename(tmpfile, cachefile)
        self.unlock(path)
        return True

    def extract(self, attr):
        return dict((key, getattr(attr, key)) for key in (
            'st_atime', 'st_gid', 'st_mode', 'st_mtime', 'st_size', 'st_uid'))

    def _chmod(self, path, mode):
        self.logger.info('sftp chmod {}'.format(path))
        return self.sftp.chmod(path, mode)

    def chmod(self, path, mode):
        cachefile = self.cachefile(path)
        if os.path.exists(cachefile):
            os.chmod(self.cachefile(path), mode)
            self.attributes.insert(path, self.extract(os.lstat(cachefile)))
            return self._chmod(path, mode)
        else:
            status = self._chmod(path, mode)
            self.attributes.remove(path)
            return status

    def chown(self, path, uid, gid):
        return self.sftp.chown(path, uid, gid)

    def create(self, path, mode):
        self.logger.info('create {}'.format(path))
        cachefile = self.cachefile(path)
        open(cachefile, 'wb').close()
        os.chmod(cachefile, mode)
        f = self.sftp.open(path, 'wb')
        f.chmod(mode)
        f.close()

        self.attributes.remove(path)
        self.directories.remove(os.path.dirname(path))
        return 0

    def getattr(self, path, fh=None):
        attr = self.attributes.fetch(path)
        if attr is not None:
            if 'filenotexist' == attr:
                raise FuseOSError(ENOENT)
            return attr

        self.logger.info('sftp getattr {}'.format(path))
        try:
            attr = self.extract(self.sftp.lstat(path))
            self.attributes.insert(path, attr)
            return attr
        except:
            self.attributes.insert(path, 'filenotexist')
            raise FuseOSError(ENOENT)

    def mkdir(self, path, mode):
        self.logger.info('mkdir {}'.format(path))
        status = self.sftp.mkdir(path, mode)
        self.attributes.remove(path)
        self.directories.remove(os.path.dirname(path))
        return status

    def read(self, path, size, offset, fh):
        cachefile = self.cachefile(path)
        if os.path.exists(cachefile):
            with open(cachefile, 'rb') as infile:
                infile.seek(offset, 0)
                return infile.read(size)

        task = Task(xxhash.xxh64(path).intdigest(), self.getfile, path)
        self.taskpool.submit(task)
        with self.sftp.open(path, 'rb') as infile:
            infile.seek(offset, 0)
            return infile.read(size)

    def readdir(self, path, fh=None):
        entries = self.directories.fetch(path)
        if entries is None:
            entries = self.sftp.listdir(path)
            self.directories.insert(path, entries)
            self.logger.info('sftp readdir {} = {}'.format(path, entries))

        return entries + ['.', '..']

    def readlink(self, path):
        return self.sftp.readlink(path)

    def rename(self, old, new):
        self.logger.info('sftp rename {} {}'.format(old, new))
        status = self.sftp.rename(old, new)
        self.attributes.remove(old)
        self.attributes.remove(new)
        self.directories.remove(os.path.dirname(old))
        self.directories.remove(os.path.dirname(new))

        cachefile = self.cachefile(old)
        if os.path.exists(cachefile):
            os.unlink(cachefile)

        cachefile = self.cachefile(new)
        if os.path.exists(cachefile):
            os.unlink(cachefile)

        return status

    def rmdir(self, path):
        self.logger.info('rmdir {}'.format(path))
        status = self.sftp.rmdir(path)
        self.attributes.remove(path)
        self.directories.remove(os.path.dirname(path))
        return status

    def symlink(self, target, source):
        'creates a symlink `target -> source` (e.g. ln -sf source target)'
        self.logger.info('sftp symlink {} {}'.format(source, target))
        self.sftp.symlink(source, target)
        self.attributes.remove(target)
        self.directories.remove(os.path.dirname(target))
        return 0

    def _truncate(self, thread_local_data, path, length):
        self.logger.info('sftp truncate {}'.format(path))
        sftp = self.current_thread_sftp(thread_local_data)
        return sftp.truncate(path, length)

    def truncate(self, path, length, fh=None):
        self.logger.info('truncate {}'.format(path))
        cachefile = self.cachefile(path)
        if not os.path.exists(cachefile):
            raise FuseOSError(ENOENT)

        status = os.truncate(cachefile, length)
        self.logger.info(self.extract(os.lstat(cachefile)))
        self.attributes.insert(path, self.extract(os.lstat(cachefile)))
        task = Task(xxhash.xxh64(path).intdigest(), self._truncate, path, length)
        self.taskpool.submit(task)
        return status

    def unlink(self, path):
        self.logger.info('unlink {}'.format(path))
        cachefile = self.cachefile(path)
        if os.path.exists(cachefile):
            os.unlink(cachefile)

        self.sftp.unlink(path)
        self.attributes.remove(path)
        self.directories.remove(os.path.dirname(path))
        return 0

    def utimens(self, path, times=None):
        self.logger.info('utimens {}'.format(path))
        status = self.sftp.utime(path, times)
        self.attributes.remove(path)
        return status

    def _write(self, thread_local_data, path, data, offset):
        self.logger.info('sftp write {}'.format(path))
        sftp = self.current_thread_sftp(thread_local_data)
        with sftp.open(path, 'rb+') as outfile:
            outfile.seek(offset, 0)
            outfile.write(data)

        return len(data)

    def write(self, path, data, offset, fh):
        self.logger.info('write : {}'.format(data))
        cachefile = self.cachefile(path)
        if not os.path.exists(cachefile):
            raise FuseOSError(ENOENT)

        with open(cachefile, 'rb+') as outfile:
            outfile.seek(offset, 0)
            outfile.write(data)

        self.attributes.insert(path, self.extract(os.lstat(cachefile)))
        task = Task(xxhash.xxh64(path).intdigest(),
                    self._write, path, data, offset)
        self.taskpool.submit(task)
        return len(data)

    def destroy(self, path):
        self.taskpool.shutdown()
        self.sftp.close()
        self.client.close()

def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', dest='host')
    parser.add_argument('-m', dest='mount_point')
    parser.add_argument('-p', dest='cache_path')
    args = parser.parse_args()

    if not args.host:
        sys.exit()
    if not args.mount_point:
        sys.exit()
    if not args.cache_path:
        sys.exit()

    if '@' not in args.host:
        logging.error('invalid host arguments.')
        sys.exit()

    user, _, args.host = args.host.partition('@')
    fuse = FUSE(OXFS(args.host, user=user, cache_path=args.cache_path),
                args.mount_point,
                foreground=True,
                nothreads=True,
                allow_other=True)

if __name__ == '__main__':
    main()