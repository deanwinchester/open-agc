"""Quick verification for core.security (temporary script)."""
from core.security import resolve_under, is_safe_name

fails = []


def expect_raises(base, name, label):
    try:
        r = resolve_under(base, name)
        fails.append(f'{label}: expected ValueError, got {r!r}')
    except ValueError:
        print(f'OK   {label}: ValueError raised')


def expect_ok(base, name, label):
    try:
        r = resolve_under(base, name)
        print(f'OK   {label}: -> {r}')
    except ValueError as e:
        fails.append(f'{label}: unexpected ValueError: {e}')


expect_raises('/a/b', '../../etc', 'posix traversal')
expect_ok('/a/b', 'ok.md', 'normal file')
expect_raises('/a/b', '..\\x', 'windows traversal')
expect_raises('/a/b', '..', 'bare dotdot')
expect_raises('/a/b', 'sub/../../x', 'nested traversal')
expect_raises('/a/b', 'C:/Windows/x', 'drive absolute')
expect_raises('/a/b', 'C:\\Windows\\x', 'drive absolute backslash')
expect_raises('/a/b', '/etc/passwd', 'posix absolute')
expect_raises('/a/b', '\\\\server\\share', 'UNC')
expect_raises('/a/b', '', 'empty')
expect_ok('/a/b', 'sub/dir/file.md', 'nested subdir allowed')
expect_ok('skills', 'api_tester.md', 'relative base')

cases = [('ok.md', True), ('a-b_c.1', True), ('..', False), ('a/b', False),
         ('a\\b', False), ('', False), ('a b', False), ('.hidden', True), ('a:b', False)]
for name, want in cases:
    got = is_safe_name(name)
    mark = 'OK  ' if got == want else 'FAIL'
    if got != want:
        fails.append(f'is_safe_name({name!r}) = {got}, want {want}')
    print(f'{mark} is_safe_name({name!r}) = {got} (want {want})')

print()
if fails:
    print('FAILURES:')
    for f in fails:
        print(' -', f)
    raise SystemExit(1)
print('ALL TESTS PASSED')
