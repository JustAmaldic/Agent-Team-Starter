"""Turn unittest FAIL/ERROR blocks into GitHub Actions error annotations."""
import re
import sys


def blocks(text):
    parts = re.split(r'^={50,}\n', text, flags=re.MULTILINE)
    for part in parts[1:]:
        header, _, body = part.partition('\n')
        match = re.match(r'(FAIL|ERROR): (\S+) \((.+?)\)', header)
        if match:
            body = body.split('\n' + '-' * 70 + '\nRan ')[0]
            yield f'{match.group(1)} {match.group(3)}', body.strip()


def escape(value):
    return value.replace('%', '%25').replace('\r', '').replace('\n', '%0A')


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8', errors='replace') as handle:
        output = handle.read()
    found = list(blocks(output))
    for title, body in found[:10]:
        print(f'::error title={escape(title)}::{escape(body[-3000:])}')
    if not found:
        print('::error title=Unit tests failed::' + escape(output[-3000:]))
