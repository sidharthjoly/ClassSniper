"""Center name -> id map, shared by the scraper and the striker.

The striker needs these at strike time without spending a round-trip on a
/centers lookup (they're physical gym locations — effectively static). The
scraper checks the live API against this map every run, so a location that
opens later can't quietly show up in the class list with no id here and send
every booking for it down the slow browser path.
"""

CENTER_IDS = {
    "Surry Hills": 101,
    "Bunker": 102,
    "Marrickville": 103,
    "Newtown": 104,
    "Haymarket": 105,
    "Merrylands": 106,
    "North Sydney": 107,
    "Zetland": 108,
    # Not yet trading — both carry a startup_date of 2027-02-10. Listed now so the
    # fast path works the day they open rather than silently falling back.
    "Chatswood": 109,
    "Rockdale": 110,
}
