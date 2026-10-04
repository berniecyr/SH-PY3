"""Import selected JSON descriptions and run the normal Image-view detectors.

Defaults to a THREE-CONTENT preview database, never the live database.
Run with --apply to execute; otherwise only a JSON plan is written.
For a reviewed full import use --all --apply --database <usermedia.db>.
Distinct AI descriptions for verified content are combined with blank lines.
Missing/ambiguous files are reported.
Exact SHA-256 matches share a content row and keep every physical location.
"""
import argparse
from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backEnd.UserMediaDb import UserMediaDb, fingerprint, mergeTags


def readRows(path):
    with open(path, encoding='utf-8-sig') as stream:
        rows = json.load(stream)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('%s must contain a JSON array of objects' % path)
    return [{key.casefold(): value for key, value in row.items()} for row in rows]


def mergeDescriptions(existing, *descriptions):
    """Preserve existing text and append distinct complete descriptions once."""
    combined = (existing or '').replace('\r\n', '\n').strip()
    for text in descriptions:
        text = (text or '').replace('\r\n', '\n').strip()
        if text and ('\n\n' + text + '\n\n') not in ('\n\n' + combined + '\n\n'):
            combined = combined + '\n\n' + text if combined else text
    return combined


def buildPlan(files, ai, limit=3, names=None, tag=None):
    groups, descriptions = defaultdict(list), defaultdict(list)
    for row in files:
        name = row.get('filename')
        if isinstance(name, str) and name:
            groups[name.casefold()].append(row)
    for row in ai:
        name, text = row.get('filename'), row.get('gemini')
        if isinstance(name, str) and isinstance(text, str) and text.strip():
            if text not in descriptions[name.casefold()]:
                descriptions[name.casefold()].append(text)
    wanted = {name.casefold() for name in names or []}
    keys = [name for name in groups if not wanted or name in wanted]
    if not wanted and limit:
        # Prefer pilot examples that exercise all three requested features.
        keys.sort(key=lambda name: (
            not (len({str(r.get('path')) for r in groups[name]}) > 1 and
                 len({str(r.get('tag')) for r in groups[name] if r.get('tag')}) > 1 and
                 bool(descriptions[name])), name))
    content, issues = {}, []
    for name in keys:
        rows = groups[name]
        if tag and not any(tag.casefold() in str(r.get('tag', '')).casefold() for r in rows):
            continue
        paths = defaultdict(list)
        invalid = False
        for row in rows:
            folder, filename = row.get('path'), row.get('filename')
            if (not isinstance(folder, str) or not isinstance(filename, str)
                    or not os.path.isabs(folder) or os.path.basename(filename) != filename):
                issues.append(dict(filename=name, issue='Invalid absolute directory or filename'))
                invalid = True
                continue
            paths[os.path.normpath(os.path.join(folder, filename))].append(row.get('tag'))
        hashes = defaultdict(list)
        for path, tags in paths.items():
            try:
                digest, stat = fingerprint(path)
            except OSError as exc:
                issues.append(dict(path=path, issue=str(exc)))
                invalid = True
                continue
            hashes[digest].append((path, [t for t in tags if isinstance(t, str)], stat.st_size))
        texts = descriptions.get(name, [])
        ambiguous = len(hashes) != 1 or invalid
        if texts and ambiguous:
            issues.append(dict(filename=name, issue='AI skipped: filename mapping is ambiguous or unverifiable',
                               content_variants=len(hashes), unverified_locations=invalid,
                               ai_options=texts))
        if len(hashes) > 1:
            issues.append(dict(filename=name, issue='Same name, different bytes: kept as separate records'))
        for digest, locations in hashes.items():
            if digest not in content and limit and len(content) >= limit:
                continue
            item = content.setdefault(digest, dict(sha256=digest, locations=[], tags='',
                                                   ai_candidates=[], source_names=[], size=locations[0][2]))
            item['source_names'].append(name)
            for path, tags, size in locations:
                if path not in item['locations']:
                    item['locations'].append(path)
                item['tags'] = mergeTags(item['tags'], *tags)
            if not ambiguous:
                for text in texts:
                    if text not in item['ai_candidates']:
                        item['ai_candidates'].append(text)
        if limit and len(content) >= limit:
            break
    for item in content.values():
        item['description_ai'] = mergeDescriptions('', *item['ai_candidates'])
    return dict(records=list(content.values()), issues=issues,
                input_file_rows=len(files), input_ai_rows=len(ai))


class CheckedClient:
    """Expose attribute-service failures that the GUI intentionally tolerates."""
    def __init__(self, client):
        self.client = client
        self.errors = []

    def __getattr__(self, name):
        target = getattr(self.client, name)
        def call(*args, **kwargs):
            try:
                return target(*args, **kwargs)
            except Exception as exc:
                self.errors.append('%s: %s' % (name, exc))
                raise
        return call


def execute(plan, database, resume=False, checkpoint=None):
    from backEnd import UserMediaAnalysis as analysis
    logger = logging.getLogger('media-import')
    client = analysis.openDetectionClient(logger)
    if client is None:
        raise RuntimeError('Start the application/detection service before importing.')
    checked = CheckedClient(client)
    cfg = analysis.loadConfig()
    database.parent.mkdir(parents=True, exist_ok=True)
    plan['detector_settings'] = {key: cfg.get(key) for key in
                                ('RUN_FACE', 'RUN_NUDITY', 'YOLO_MODEL', 'NUDE_MODEL')}
    # SQLite backup includes WAL state and precedes any schema/data changes.
    if database.exists():
        backup = database.with_name(database.name+'.'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.bak')
        source = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
        dest = sqlite3.connect(backup)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        plan['backup'] = str(backup)
    db = UserMediaDb(logger).open(str(database))
    try:
        for index, item in enumerate(plan['records'], 1):
            path = item['locations'][0]
            print('Analyzing %d/%d: %s' % (index, len(plan['records']), path), flush=True)
            try:
                previous = db.getFile(path)
                reuse = bool(resume and previous and previous['analyzedMs'] is not None
                             and not previous['error']
                             and previous['contentHash'] == item['sha256']
                             and not db.needsAnalysis(path, analysis.modelSignature(cfg)))
                if not reuse:
                    for attempt in range(3):
                        checked.errors.clear()
                        result = analysis.analyzeFile(path, checked, cfg, logger=logger)
                        if not result.get('error') and not checked.errors:
                            break
                        if attempt == 2:
                            raise RuntimeError(result.get('error') or '; '.join(checked.errors))
                        print('Detection incomplete; retrying in 6 seconds...', flush=True)
                        time.sleep(6)
                for location in item['locations']:
                    if fingerprint(location)[0] != item['sha256']:
                        raise RuntimeError('File changed since planning: '+location)
                if reuse:
                    db.registerContent(path)
                    print('Reused verified existing analysis.', flush=True)
                else:
                    db.saveResult(path, result)
                for location in item['locations'][1:]:
                    db.registerContent(location)
                old = db.getDescriptions(path)
                db.saveDescriptions(path, mergeTags(old['description_tags'], item['tags']),
                                    mergeDescriptions(old['description_ai'], *item['ai_candidates']))
                item.update(status='imported', analysis_reused=reuse, uid=db.getFile(path)['uid'],
                            stored=db.getDescriptions(path), locations=db.getLocations(path),
                            detections=[dict(row) for row in db.getDetections(path)])
            except Exception as exc:
                item.update(status='failed', error=str(exc))
                logger.exception('Could not import %s', path)
            if checkpoint is not None and (index % 25 == 0 or item.get('status') == 'failed'):
                try:
                    checkpoint()
                except OSError as exc:
                    logger.warning('Could not update progress report; database work is saved: %s', exc)
            time.sleep(.1)  # Share the inference service with camera processing.
        plan['database_counts'] = dict(zip(('content_records', 'detections'), db.stats()))
    finally:
        db.close()
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Sighthound Video Py3' / 'IMPORT'
    parser.add_argument('--source', type=Path, default=default)
    parser.add_argument('--database', type=Path, help='Default: IMPORT/import-preview.db (separate from live data)')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--all', action='store_true', help='Explicitly select the full input instead of a pilot')
    parser.add_argument('--filename', action='append', help='Select a filename; repeat to select more')
    parser.add_argument('--tag', help='Select filenames with this text in a source tag')
    parser.add_argument('--apply', action='store_true', help='Analyze and import; otherwise only produce a plan')
    parser.add_argument('--resume', action='store_true', help='Reuse successful analysis when bytes, file state and model settings still match')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive; use --all for a full import')
    database = args.database or args.source / 'import-preview.db'
    report = args.report or args.source / 'import-report.json'
    plan = buildPlan(readRows(args.source/'Files.txt'), readRows(args.source/'Ai.txt'),
                     limit=None if args.all else args.limit, names=args.filename, tag=args.tag)
    plan.update(database=str(database.resolve()), mode='apply' if args.apply else 'plan')
    def checkpoint():
        report.parent.mkdir(parents=True, exist_ok=True)
        temporary = report.with_suffix(report.suffix+'.tmp')
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(10):
            try:
                temporary.replace(report)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.2)
    checkpoint()
    try:
        if args.apply:
            execute(plan, database, resume=args.resume, checkpoint=checkpoint)
    finally:
        checkpoint()
        print('Report: '+str(report), flush=True)
    return 1 if any(r.get('status') == 'failed' for r in plan['records']) else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
