"""Versioned, recoverable multi-file exports with an OS-held writer lock.

CURRENT is the sole commit point. Immutable generations include every export
and hashes. Root files are compatibility projections, repaired on the next run
after an interrupted publication. No existing generation is deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


def clean_temps(out: Path, root: Path):
    """Delete owned temp files only; keep complete and incomplete generations."""
    folders = [out, root]
    if root.exists():
        folders += [p for p in root.iterdir() if re.fullmatch(r'[0-9a-f]{32}', p.name)
                    and p.is_dir() and not p.is_symlink()]
    for folder in folders:
        if folder.exists():
            for path in folder.glob('.integration-*.tmp'):
                if path.parent.resolve() == folder.resolve() and not path.is_symlink():
                    path.unlink()


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.integration-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_staged(folder: Path, files: dict[str, bytes]):
    for name, expected in files.items():
        if hashlib.sha256((folder/name).read_bytes()).digest() != hashlib.sha256(expected).digest():
            raise ValueError('Staging checksum mismatch; generation was not committed')


@contextmanager
def writer_lock(out: Path):
    if out.is_symlink():
        raise ValueError('Output directory cannot be a symbolic link')
    out.mkdir(parents=True, exist_ok=True)
    if (out / '.integration.lock').is_symlink():
        raise ValueError('Integration lock cannot be a symbolic link')
    with (out / '.integration.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another integration holds the output lock') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Store:
    FILES = ('state.json', 'candidate_review.csv', 'integration_report.json')

    def __init__(self, out: Path):
        self.out = out
        self.root = out / '.integration'
        self.current = self.root / 'CURRENT'

    def load(self):
        if self.root.is_symlink() or self.current.is_symlink():
            raise ValueError('Integration state cannot use symbolic links')
        if not self.current.exists():
            report = self.out / 'integration_report.json'
            if report.exists():
                try:
                    metadata = json.loads(report.read_text(encoding='utf-8-sig'))
                    if isinstance(metadata, dict) and metadata.get('generation'):
                        raise ValueError('CURRENT is missing; restore the integration state before continuing')
                except json.JSONDecodeError:
                    pass  # The migration validator supplies a clearer legacy error.
            return None
        generation = self.current.read_text(encoding='ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{32}', generation):
            raise ValueError('Invalid integration CURRENT pointer; no files replaced')
        folder = self.root / generation
        if folder.is_symlink():
            raise ValueError('Integration generation cannot be a symbolic link')
        try:
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict) or set(manifest) != set(self.FILES):
                raise ValueError('Invalid generation manifest')
            data = {name: (folder / name).read_bytes() for name in self.FILES}
            if any(hashlib.sha256(data[name]).hexdigest() != manifest[name] for name in data):
                raise ValueError('Integration generation checksum mismatch')
            state = json.loads(data['state.json'].decode('utf-8'))
            if not isinstance(state, dict) or state.get('schema') != 1:
                raise ValueError('Unsupported integration state schema')
            # Report-last publication is the recovery marker.
            report = self.out / 'integration_report.json'
            if not report.exists() or report.read_bytes() != data['integration_report.json'] or not (self.out / 'candidate_review.csv').exists():
                # Do not silently discard an externally edited CSV after a crash.
                target = self.out / 'candidate_review.csv'
                if target.exists() and target.read_bytes() != data['candidate_review.csv']:
                    previous = state.get('previous_csv_hash')
                    if hashlib.sha256(target.read_bytes()).hexdigest() != previous:
                        raise ValueError('Interrupted export and CSV edits detected; preserve edits before recovery')
                self.publish(data)
            return state
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('Integration state incomplete or unreadable; no fallback to stale CSV') from exc

    def publish(self, data):
        atomic_bytes(self.out / 'candidate_review.csv', data['candidate_review.csv'])
        atomic_bytes(self.out / 'integration_report.json', data['integration_report.json'])

    def commit(self, state, csv_data, report):
        generation = uuid.uuid4().hex
        report = {**report, 'generation': generation}
        folder = self.root / generation
        folder.mkdir(parents=True)
        data = {'state.json': json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'),
                'candidate_review.csv': csv_data,
                'integration_report.json': json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8-sig')}
        for name, content in data.items():
            atomic_bytes(folder / name, content)
        manifest = {name: hashlib.sha256(content).hexdigest() for name, content in data.items()}
        manifest_data = json.dumps(manifest).encode('ascii')
        atomic_bytes(folder / 'manifest.json', manifest_data)
        verify_staged(folder, {**data, 'manifest.json': manifest_data})
        # Commit after the entire generation has been persisted and checked.
        atomic_bytes(self.current, generation.encode('ascii'))
        self.publish(data)
        return generation

    def clean_temps(self):
        clean_temps(self.out, self.root)


class ExportBundle:
    """Recoverable publication of discovery or audit files, with prior-byte backups.

    Caller holds writer_lock. CURRENT commits a complete generation; PUBLISHED
    records completion of the compatibility copies. No snapshots are pruned.
    """
    def __init__(self, out: Path, name: str):
        if not re.fullmatch(r'[a-z-]+', name):
            raise ValueError('Invalid export namespace')
        self.out, self.root = out, out / '.exports' / name

    def recover(self):
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError('Export state cannot use symbolic links')
        current = self.root / 'CURRENT'
        clean_temps(self.out, self.root)
        if not current.exists():
            if (self.root / 'PUBLISHED').exists():
                raise ValueError('Export CURRENT is missing; restore the export state')
            return
        generation = current.read_text(encoding='ascii')
        if not re.fullmatch(r'[0-9a-f]{32}', generation):
            raise ValueError('Invalid export generation pointer')
        folder = self.root / generation
        if folder.is_symlink():
            raise ValueError('Export generation cannot use symbolic links')
        meta = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if not isinstance(meta, dict) or not isinstance(meta.get('hashes'), dict) or not isinstance(meta.get('previous'), dict):
            raise ValueError('Invalid export manifest')
        data = {}
        for name, digest in meta['hashes'].items():
            if Path(name).name != name or name.startswith('.'):
                raise ValueError('Invalid export filename')
            content = (folder / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('Export checksum mismatch')
            data[name] = content
        published = self.root / 'PUBLISHED'
        if published.exists() and published.read_text(encoding='ascii') == generation:
            return
        for name, previous_hash in meta['previous'].items():
            if name not in data:
                raise ValueError('Invalid published export set')
            target = self.out / name
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() not in {previous_hash, meta['hashes'][name]}:
                raise ValueError('Export interrupted and external edits detected; no files overwritten')
        for name in meta['previous']:
            atomic_bytes(self.out / name, data[name])
        atomic_bytes(published, generation.encode('ascii'))

    def commit(self, files: dict[str, bytes]):
        self.recover()
        if not files or any(Path(name).name != name or name.startswith('.') for name in files):
            raise ValueError('Invalid export filenames')
        generation = uuid.uuid4().hex
        folder = self.root / generation
        folder.mkdir(parents=True)
        previous = {}
        payload = dict(files)
        for name in files:
            old = self.out / name
            if old.exists():
                content = old.read_bytes()
                previous[name] = hashlib.sha256(content).hexdigest()
                payload['before-' + name] = content
            else:
                previous[name] = None
        for name, data in payload.items():
            atomic_bytes(folder / name, data)
        manifest = {'hashes': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}, 'previous': previous}
        manifest_data = json.dumps(manifest).encode('utf-8')
        atomic_bytes(folder / 'manifest.json', manifest_data)
        verify_staged(folder, {**payload, 'manifest.json': manifest_data})
        atomic_bytes(self.root / 'CURRENT', generation.encode('ascii'))
        self.recover()
