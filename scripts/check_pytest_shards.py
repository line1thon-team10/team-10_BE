"""Verify saved collection manifests cover the exact same tests exactly once."""
import argparse
from collections import Counter
import json
from pathlib import Path


def verify(manifests):
    if not manifests:
        raise ValueError('No shard manifests provided')
    count = manifests[0]['count']
    if type(count) is not int or count < 1 or len(manifests) != count:
        raise ValueError('Expected exactly one manifest for every shard')
    if sorted(item['index'] for item in manifests) != list(range(count)):
        raise ValueError('Shard indices are missing or duplicated')
    baseline = Counter(manifests[0]['collected'])
    if not baseline:
        raise ValueError('No tests were collected')
    selected = Counter()
    for manifest in manifests:
        if manifest.get('schemaVersion') != 1 or manifest['count'] != count or Counter(manifest['collected']) != baseline:
            raise ValueError('Shard collections differ')
        values = Counter(manifest['selected'])
        if values - baseline:
            raise ValueError('A shard selected an uncollected test')
        selected.update(values)
    if selected != baseline:
        raise ValueError('Shard selections contain missing or duplicated tests')
    return sum(baseline.values()), [len(item['selected']) for item in sorted(manifests, key=lambda item: item['index'])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifests', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        total, sizes = verify([json.loads(path.read_text(encoding='utf-8')) for path in args.manifests])
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'Shard validation failed: {error}\n')
    print(f'Exact partition verified: {total} tests, shard sizes {sizes}')


if __name__ == '__main__':
    main()
