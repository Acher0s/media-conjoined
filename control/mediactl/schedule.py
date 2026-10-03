"""Feed time, the casters' lineup and what a delay change would do.

Feed time = now - delay: what the delayed feeds are showing right now. A slot shows whichever
team was assigned to it at feed time, so an assignment reaches the feeds exactly one delay later.
"""
from . import config, db


def feed_time(now: float, delay_minutes: float) -> float:
    return now - delay_minutes * 60


def lineup(conn, now: float, delay_minutes: float) -> dict:
    names = db.team_names(conn)
    ft = feed_time(now, delay_minutes)
    slots = []
    for slot in config.SLOTS:
        team = db.assignment_at(conn, slot, ft)
        upcoming = [{"team": row["team_path"], "team_name": names.get(row["team_path"]) if row["team_path"] else None,
                     "assigned_at": row["at"], "reaches_feed_at": row["at"] + delay_minutes * 60}
                    for row in db.assignments_between(conn, slot, ft, now)]
        slots.append({"slot": slot, "team": team, "team_name": names.get(team) if team else None,
                      "upcoming": upcoming})
    return {"now": now, "feed_time": ft, "delay_minutes": delay_minutes, "slots": slots}


def preview(conn, now: float, current: float, new: float) -> dict:
    """What changing the delay from `current` to `new` minutes does to the feeds.

    Longer delay: the feeds hold (slate) for the difference, then carry on where they were.
    Shorter delay: the feeds jump forward, skipping the window in between.
    """
    result = {"current_minutes": current, "new_minutes": new}
    if abs(new - current) < 1e-9:
        return {**result, "effect": "none"}
    if new > current:
        return {**result, "effect": "hold", "hold_minutes": new - current}
    names = db.team_names(conn)
    skip_from, skip_to = feed_time(now, current), feed_time(now, new)
    skipped = []
    for slot in config.SLOTS:
        teams = []
        first = db.assignment_at(conn, slot, skip_from)
        if first:
            teams.append(first)
        teams += [r["team_path"] for r in db.assignments_between(conn, slot, skip_from, skip_to) if r["team_path"]]
        for team in dict.fromkeys(teams):
            skipped.append({"slot": slot, "team": team, "team_name": names.get(team)})
    return {**result, "effect": "skip", "skip_from": skip_from, "skip_to": skip_to, "skipped": skipped}
