import datetime
import hashlib
import json

from check_subscriptions import (
    ARCHIVE_DAYS, SCHEDULE_TZ, UNAVAILABLE_PREFIX, base_source_name, parse_date, parse_time, read_archive,
)


CLIENT_CHECK_HTTP = {"HTTP 401", "HTTP 403", "HTTP 407", "HTTP 429", "HTTP 451"}


def unavailable_last(sites):
    return dict(sorted(sites.items(), key=lambda item: item[1]["name"].startswith(UNAVAILABLE_PREFIX)))


def source_status(row):
    catalog = row["catalog"]
    playback = row["playback"]
    if catalog.status == "fail":
        return "client" if catalog.message in CLIENT_CHECK_HTTP else "unavailable"
    if catalog.status != "ok" or playback.status != "ok":
        return "client"
    return "ok"


def update_sources(root, documents, rows, checked_at):
    state = read_archive(root)
    checked = parse_time(checked_at)
    if state["last_checked_at"] is not None and checked <= parse_time(state["last_checked_at"]):
        raise ValueError("Health check must be newer than the published result")
    day = checked.astimezone(SCHEDULE_TZ).date()
    yesterday = (day - datetime.timedelta(days=1)).isoformat()
    today = day.isoformat()

    by_source = {(row["group"], row["key"]): row for row in rows}
    expected = {(group, key) for group, document in documents.items() for key in document["api_site"]}
    if len(by_source) != len(rows) or by_source.keys() != expected:
        raise ValueError("Health results do not match the subscription inventory")

    pending, archived = {}, state["archived"]
    archived_now = []
    for group, document in documents.items():
        sites = document["api_site"]
        for key, site in list(sites.items()):
            row = by_source[group, key]
            status = source_status(row)
            name = base_source_name(site["name"])
            site["name"] = name if status == "ok" else UNAVAILABLE_PREFIX + name
            row["name"] = site["name"]
            if status != "unavailable":
                continue
            config = {**site, "name": name}
            identity = json.dumps([group, {field: value for field, value in config.items()
                                           if field != "name"}], sort_keys=True, ensure_ascii=False)
            fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            previous = state["pending"].get(key)
            first = today
            if (previous and previous["fingerprint"] == fingerprint
                    and previous["last_failed"] in {today, yesterday}):
                first = previous["first_failed"]
            period = {"first_failed": first, "last_failed": today}
            if (day - parse_date(first)).days + 1 < ARCHIVE_DAYS:
                pending[key] = {"fingerprint": fingerprint, **period}
                continue
            archived_now.append({"group": group, "key": key, "site": config, **period})
            del sites[key]
        document["api_site"] = unavailable_last(sites)
    archived.extend(archived_now)
    return {"last_checked_at": checked_at, "pending": pending, "archived": archived}, archived_now
