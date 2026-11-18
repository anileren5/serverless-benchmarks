"""Generate the document collection of the search benchmarks from CPython's docstrings.

One document per public module, function, class and method of the standard library:
its qualified name, its signature and its docstring. Common modules come first, so
that the smaller sizes hold the same core documents as the large one.

Usage: python3.11 make_corpus.py <output directory>
"""
import gzip
import importlib
import inspect
import json
import os
import sys
import warnings

SIZES = {'test': 500, 'small': 2500}
CORE = ['json', 'math', 're', 'os', 'posixpath', 'string', 'random', 'datetime', 'collections',
        'itertools', 'functools', 'pathlib', 'csv', 'statistics', 'time', 'shutil']
SKIPPED = {'antigravity', 'this', 'idlelib', 'tkinter', 'turtle', 'turtledemo', 'test', 'lib2to3',
           'ensurepip', 'pydoc_data', 'xxsubtype', 'xxlimited', 'xxlimited_35'}
MAX_TEXT = 2000


def signature(obj, name):
    try:
        return f'{name}{inspect.signature(obj)}'
    except (TypeError, ValueError):
        return name


def documents(module):
    doc = inspect.getdoc(module)
    if doc:
        yield {'id': module.__name__, 'title': module.__name__, 'text': doc[:MAX_TEXT]}
    for name, obj in sorted(vars(module).items()):
        if name.startswith('_') or getattr(obj, '__module__', None) != module.__name__:
            continue
        if not (inspect.isfunction(obj) or inspect.isbuiltin(obj) or inspect.isclass(obj)):
            continue
        qualified = f'{module.__name__}.{name}'
        doc = inspect.getdoc(obj)
        if doc:
            yield {'id': qualified, 'title': signature(obj, qualified), 'text': doc[:MAX_TEXT]}
        if inspect.isclass(obj):
            for attr, member in sorted(vars(obj).items()):
                if attr.startswith('_') or not callable(member):
                    continue
                doc = inspect.getdoc(member)
                if doc:
                    yield {'id': f'{qualified}.{attr}', 'title': signature(member, f'{qualified}.{attr}'),
                           'text': doc[:MAX_TEXT]}


def main(out_dir):
    warnings.simplefilter('ignore')
    names = sorted(n for n in sys.stdlib_module_names if not n.startswith('_') and n not in SKIPPED)
    order = CORE + [n for n in names if n not in CORE]
    docs, seen = [], set()
    for name in order:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        for d in documents(module):
            if d['id'] not in seen:
                seen.add(d['id'])
                docs.append(d)

    os.makedirs(out_dir, exist_ok=True)
    for size, count in [*SIZES.items(), ('large', len(docs))]:
        path = os.path.join(out_dir, f'corpus-{size}.jsonl.gz')
        # mtime=0 keeps the archive identical between runs
        with gzip.GzipFile(path, 'wb', mtime=0) as f:
            for d in docs[:count]:
                f.write((json.dumps(d) + '\n').encode('utf-8'))
        print(size, min(count, len(docs)), 'documents', os.path.getsize(path), 'bytes')


if __name__ == '__main__':
    main(sys.argv[1])
