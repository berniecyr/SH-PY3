"""Boolean substring and whole-word queries compiled to SQLite predicates."""
from functools import lru_cache
import re
import math
from decimal import Decimal, InvalidOperation


@lru_cache(maxsize=256)
def _wordPattern(value):
    # Unicode letters/digits form words; underscores separate filename words.
    return re.compile(r'(?<![^\W_])' + re.escape(value.casefold()) + r'(?![^\W_])')


def wholeWordMatch(text, value):
    """Literal, case-insensitive word/phrase matching, never user-supplied regex."""
    return int(bool(value) and bool(_wordPattern(str(value)).search(str(text or '').casefold())))

class SearchSyntaxError(ValueError):
    pass


# Time-of-day fields, stored as HHMMSS integers.  A between range on one of
# these whose start is later than its end, like 190000,060000, spans midnight.
TIME_FIELDS = frozenset(('exifTime',))


def _timeBound(value):
    """'19:00', '19:00:30', '190000' or '60059' -> HHMMSS digits as a string."""
    value = value.strip()
    if ':' in value:
        parts = value.split(':')
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
            raise SearchSyntaxError('Enter a time as HH:MM, HH:MM:SS or HHMMSS.')
        parts = [int(p) for p in parts] + [0] * (3 - len(parts))
    else:
        if not value.isdigit() or len(value) > 6:
            raise SearchSyntaxError('Enter a time as HH:MM, HH:MM:SS or HHMMSS.')
        digits = value.zfill(6)
        parts = [int(digits[0:2]), int(digits[2:4]), int(digits[4:6])]
    if not (parts[0] < 24 and parts[1] < 60 and parts[2] < 60):
        raise SearchSyntaxError('Enter a real time of day (00:00:00 to 23:59:59).')
    return '%02d%02d%02d' % tuple(parts)


def _tokens(query):
    if len(query) > 2048:
        raise SearchSyntaxError('Keep the search under 2048 characters.')
    result, i = [], 0
    while i < len(query):
        char = query[i]
        if char.isspace():
            i += 1
            continue
        if char in '():':
            result.append((char, char))
            i += 1
        elif char == '"':
            i += 1
            value = ''
            while i < len(query) and query[i] != '"':
                if query[i] == '\\' and i + 1 < len(query) and query[i+1] == '"':
                    i += 1
                value += query[i]
                i += 1
            if i == len(query):
                raise SearchSyntaxError('Close the quoted phrase with a double quote.')
            result.append(('quoted', value))
            i += 1
        else:
            start = i
            while i < len(query) and not query[i].isspace() and query[i] not in '():"':
                i += 1
            word = query[start:i]
            upper = word.upper()
            result.append((upper if upper in ('AND', 'OR', 'NOT', 'CONTAINS') else 'text', word))
    if len(result) > 256:
        raise SearchSyntaxError('Use fewer search terms.')
    return result


def parse(query):
    tokens = _tokens(query)
    pos = 0

    def kind():
        return tokens[pos][0] if pos < len(tokens) else None

    def atom(depth=0):
        nonlocal pos
        if depth > 32:
            raise SearchSyntaxError('Too many nested parentheses or NOT operators.')
        if kind() == 'NOT':
            pos += 1
            return ('not', atom(depth+1))
        if kind() == '(':
            pos += 1
            node = either(depth+1)
            if kind() != ')':
                raise SearchSyntaxError('Close the parentheses in your search.')
            pos += 1
            return node
        forceContains = kind() == 'CONTAINS'
        if forceContains:
            pos += 1
        if kind() not in ('text', 'quoted'):
            raise SearchSyntaxError('Expected a word or quoted phrase.')
        quoted = kind() == 'quoted'
        field, value = 'all', tokens[pos][1]
        pos += 1
        if kind() == ':' or kind() == 'CONTAINS':
            field = value.casefold()
            forceContains = forceContains or kind() == 'CONTAINS'
            pos += 1
            if kind() not in ('text', 'quoted'):
                raise SearchSyntaxError('Enter a word or quoted phrase after the field.')
            quoted = kind() == 'quoted'
            value = tokens[pos][1]
            pos += 1
        if not value:
            raise SearchSyntaxError('Empty search phrases are not supported.')
        if field == 'word':
            return ('word', 'all', value)
        if not quoted and kind() == ':' and value.casefold() in ('word', 'exact', 'tag', 'ge', 'gt', 'le', 'lt', 'eq', 'between'):
            mode = value.casefold()
            pos += 1
            if kind() not in ('text', 'quoted') or not tokens[pos][1]:
                raise SearchSyntaxError('Enter a value after %s:.' % mode)
            value = tokens[pos][1]
            pos += 1
            return (mode, field, value)
        return ('word' if quoted and not forceContains and field not in ('has', 'empty') else 'term', field, value)

    def both(depth):
        nonlocal pos
        node = atom(depth)
        while kind() not in (None, ')', 'OR'):
            if kind() == 'AND':
                pos += 1
            node = ('and', node, atom(depth))
        return node

    def either(depth):
        nonlocal pos
        node = both(depth)
        while kind() == 'OR':
            pos += 1
            node = ('or', node, both(depth))
        return node

    if not tokens:
        return None
    node = either(0)
    if pos != len(tokens):
        raise SearchSyntaxError('Unexpected closing parenthesis.')
    return node


def compileQuery(query, fileColumns, detectionColumns, locationColumns=(), columnTypes=None):
    """Each term may match a different detection/location on the same record."""
    tree = parse(query)
    if tree is None:
        return '', []
    parameters = []
    files = {name: 'f.' + name for name in fileColumns}
    detections = {name: 'sd.' + name for name in detectionColumns}
    locations = {name: 'sl.' + name for name in locationColumns}
    locations.setdefault('path', 'sl.path')
    aliases = {'tags': 'description_tags', 'ai': 'description_ai', 'person': 'faceName',
               'name': 'filename', 'folder': 'path', 'contains': 'all'}
    columns = {name.casefold(): name for name in list(files) + list(detections)}

    def contains(expression, value, mode='term'):
        if mode == 'between':
            bounds = value.split(',')
            if len(bounds) != 2:
                raise SearchSyntaxError('A range requires two numbers separated by a comma.')
            lower = contains(expression, bounds[0], 'ge')
            upper = contains(expression, bounds[1], 'le')
            if parameters[-2] > parameters[-1]:
                raise SearchSyntaxError('The range start must not exceed its end.')
            return '(' + lower + ' AND ' + upper + ')'
        if mode in ('ge', 'gt', 'le', 'lt', 'eq'):
            try:
                number = Decimal(value)
                if not number.is_finite():
                    raise InvalidOperation()
                number = int(number) if number == number.to_integral_value() else float(number)
                if isinstance(number, float) and not math.isfinite(number):
                    raise InvalidOperation()
                if isinstance(number, int) and not -(2**63) <= number < 2**63:
                    raise InvalidOperation()
            except (InvalidOperation, ValueError, OverflowError):
                raise SearchSyntaxError('Enter a finite number in the supported range.')
            parameters.append(number)
            return '%s %s ?' % (expression, {'ge': '>=', 'gt': '>', 'le': '<=', 'lt': '<', 'eq': '='}[mode])
        parameters.append(value.casefold())
        if mode == 'word':
            return "UM_WORD(COALESCE(CAST(%s AS TEXT),''), ?) = 1" % expression
        if mode == 'exact':
            return "UM_CASEFOLD(COALESCE(CAST(%s AS TEXT),'')) = ?" % expression
        if mode == 'tag':
            return "UM_TAG(%s, ?) = 1" % expression
        return "instr(UM_CASEFOLD(COALESCE(CAST(%s AS TEXT),'')), ?) > 0" % expression

    def expressions(field):
        qualified = {}
        for table, mapping in [('files', files), ('detections', detections), ('file_locations', locations)]:
            for name, expression in mapping.items():
                qualified[(table + '.' + name).casefold()] = (table, name, expression)
        if field in qualified:
            return [qualified[field]]
        field = aliases.get(field, columns.get(field, field))
        if field == 'filename':
            return [('file_locations', 'filename', 'UM_BASENAME(sl.path)')]
        if field == 'path':
            return [('file_locations', 'path', 'sl.path')]
        if field == 'all':
            return list(qualified.values())
        for table, mapping in [('files', files), ('detections', detections), ('file_locations', locations)]:
            if field in mapping:
                return [(table, field, mapping[field])]
        raise SearchSyntaxError('Unknown field: %s.' % field)

    def grouped(items, predicate):
        clauses = []
        for table in ('files', 'detections', 'file_locations'):
            selected = [predicate(name, expression, table) for t, name, expression in items if t == table]
            if not selected:
                continue
            part = '(' + ' OR '.join(selected) + ')'
            if table != 'files':
                alias = 'sd' if table == 'detections' else 'sl'
                part = 'EXISTS (SELECT 1 FROM %s %s WHERE %s.fileUid=f.uid AND %s)' % (table, alias, alias, part)
            clauses.append(part)
        return '(' + ' OR '.join(clauses) + ')'

    def term(field, value, mode='term'):
        if mode != 'term' and field in ('has', 'empty'):
            raise SearchSyntaxError('Use word: with a text field, such as ai:word:door.')
        if field in ('has', 'empty'):
            def filled(name, expression, table):
                return "length(trim(COALESCE(CAST(%s AS TEXT),''), char(9)||char(10)||char(13)||' ')) > 0" % expression
            predicate = grouped(expressions(value.casefold()), filled)
            return '(' + ('NOT ' if field == 'empty' else '') + predicate + ')'
        items = expressions(field)
        if mode in ('ge', 'gt', 'le', 'lt', 'eq', 'between'):
            if len(items) != 1:
                raise SearchSyntaxError('Choose one numeric field for a comparison.')
            table, name, _ = items[0]
            if columnTypes and columnTypes.get(table + '.' + name, '').upper() not in ('INTEGER', 'REAL', 'NUMERIC'):
                raise SearchSyntaxError('Numeric comparisons require a numeric field.')
        if mode in ('ge', 'gt', 'le', 'lt', 'eq', 'between') and items[0][1] in TIME_FIELDS:
            bounds = [_timeBound(v) for v in value.split(',')] if mode == 'between' else [_timeBound(value)]
            if mode == 'between' and len(bounds) == 2 and bounds[0] > bounds[1]:
                # Overnight: at or after the start, or at or before the end.
                expression = items[0][2]
                return '(%s OR %s)' % (contains(expression, bounds[0], 'ge'),
                                       contains(expression, bounds[1], 'le'))
            value = ','.join(bounds)
        if mode == 'tag' and any(name != 'description_tags' for _, name, _ in items):
            raise SearchSyntaxError('Exact tag matching requires the tags field.')
        return grouped(items, lambda name, expression, table: contains(expression, value, mode))

    def visit(node):
        if node[0] not in ('not', 'and', 'or'):
            return term(node[1], node[2], node[0])
        if node[0] == 'not':
            # A comparison on an empty (NULL) value is unknown, not false, so
            # a bare NOT would drop never-set records from an Exclude too.
            return '(NOT COALESCE(%s, 0))' % visit(node[1])
        return '(%s %s %s)' % (visit(node[1]), node[0].upper(), visit(node[2]))
    return visit(tree), parameters
