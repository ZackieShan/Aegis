"""The almanac: exact answers that a language model should not be left to
work out in its head — units, map positions, the sun and the moon, dates.

Everything here is arithmetic on definitions and published formulas. It
needs no network and no model, and each piece is checked against an
independent implementation (see ``tests/test_almanac.py`` and
``docs/almanac.md``).

    units        the same quantity in other units
    coordinates  latitude/longitude, UTM, MGRS; distance and bearing
    gazetteer    town names to positions (an optional 11 MB download)
    places       "what the person wrote" to a position, and their home
    sky          sunrise, sunset, twilight, moonrise, the moon's phase
    dates        days between dates, a date some time on, the weekday
"""
