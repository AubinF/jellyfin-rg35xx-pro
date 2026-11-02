"""Jellyfin home feeds and asynchronous work for the handheld UI."""
import collections
import hashlib
import logging
import os
import queue
import threading
from urllib.parse import urlencode

import requests

LOG = logging.getLogger("jellyfin.library")
FIELDS = "Overview,RunTimeTicks,PrimaryImageAspectRatio,DateCreated,UserData"


class Jobs:
    def __init__(self, workers=3):
        self.work = queue.Queue()
        self.done = queue.Queue()
        self.serial = 0
        self.callbacks = {}
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    def submit(self, function, callback):
        self.serial += 1
        self.callbacks[self.serial] = callback
        self.work.put((self.serial, function))
        return self.serial

    def cancel(self, job):
        self.callbacks.pop(job, None)

    def _run(self):
        while True:
            serial, function = self.work.get()
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, type(exc).__name__
                LOG.warning("Library request failed (%s)", error)
            self.done.put((serial, result, error))

    def poll(self):
        for _ in range(32):
            try:
                serial, result, error = self.done.get_nowait()
            except queue.Empty:
                break
            callback = self.callbacks.pop(serial, None)
            if callback:
                callback(result, error)


class LibraryAPI:
    def __init__(self, config, token, user_id, headers, verify):
        self.server = config["serverUrl"].rstrip("/")
        self.token, self.user_id = token, user_id
        self.headers, self.verify = headers, verify

    def get(self, path, **params):
        with requests.get(self.server + path, params=params, headers=self.headers,
                          timeout=(4, 12), verify=self.verify) as response:
            response.raise_for_status()
            return response.json()

    def views(self):
        return self.get("/Users/" + self.user_id + "/Views").get("Items", [])

    def next_up(self):
        return self.get("/Shows/NextUp", UserId=self.user_id, Limit=24,
                        Fields=FIELDS, EnableResumable="false",
                        EnableRewatching="false", EnableImageTypes="Primary,Thumb,Backdrop").get("Items", [])

    def resume(self):
        return self.get("/Users/" + self.user_id + "/Items/Resume", Limit=24,
                        MediaTypes="Video", Fields=FIELDS,
                        EnableImageTypes="Primary,Thumb,Backdrop").get("Items", [])

    def latest(self, parent):
        result = self.get("/Users/" + self.user_id + "/Items/Latest", ParentId=parent,
                          Limit=24, IncludeItemTypes="Movie,Episode", Fields=FIELDS,
                          EnableImageTypes="Primary,Thumb,Backdrop")
        return result if isinstance(result, list) else result.get("Items", [])

    def children(self, item, start=0, limit=30):
        kind = item.get("Type")
        params = dict(UserId=self.user_id, Fields=FIELDS, StartIndex=start, Limit=limit,
                      EnableImageTypes="Primary,Thumb,Backdrop")
        if kind == "Series":
            path = "/Shows/" + item["Id"] + "/Seasons"
        elif kind == "Season":
            path = "/Shows/" + item.get("SeriesId", item["Id"]) + "/Episodes"
            params["SeasonId"] = item["Id"]
        elif kind == "Playlist":
            # Keep the playlist's own order; the generic items endpoint re-sorts it.
            path = "/Playlists/" + item["Id"] + "/Items"
        else:
            path = "/Users/" + self.user_id + "/Items"
            # Albums list in disc/track order so the play queue follows the record.
            order = "ParentIndexNumber,IndexNumber,SortName" if kind == "MusicAlbum" else "SortName"
            params.update(ParentId=item["Id"], Recursive="false", SortBy=order,
                          SortOrder="Ascending")
        result = self.get(path, **params)
        items = result.get("Items", [])
        if kind == "Series":
            for season in items:
                season.setdefault("SeriesId", item["Id"])
        total = result.get("TotalRecordCount")
        more = start + len(items) < total if isinstance(total, int) else len(items) == limit
        return items, total, more

    def album_tracks(self, parent):
        """Every track of an album or playlist, in play order: the music queue."""
        tracks, start, more = [], 0, True
        while more and start < 1000:
            items, _, more = self.children(parent, start, limit=200)
            if not items:
                break
            tracks.extend(items)
            start += len(items)
        return [track for track in tracks if track.get("Type") == "Audio"]

    def stream_url(self, item):
        # Same static audio stream as single-track playback; mpv decodes it directly.
        return self.server + "/Audio/" + item["Id"] + "/stream?static=true&ApiKey=" + self.token

    def report_playing(self, event, item, session_id, ticks, paused=False):
        endpoint = {"started": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                    "stopped": "/Sessions/Playing/Stopped"}[event]
        payload = {"ItemId": item["Id"], "MediaSourceId": item.get("MediaSourceId") or item["Id"],
                   "PlaySessionId": session_id, "PositionTicks": int(ticks), "IsPaused": paused,
                   "CanSeek": True, "PlayMethod": "DirectPlay"}
        with requests.post(self.server + endpoint, headers=self.headers, json=payload,
                           timeout=(3, 3), verify=self.verify) as response:
            LOG.info("Music %s HTTP %d", event, response.status_code)
            response.raise_for_status()

    def search(self, term, parent=None):
        """Search the signed-in user's library, optionally within one folder."""
        params = dict(UserId=self.user_id, SearchTerm=term, Recursive="true", Limit=30,
                      Fields=FIELDS, SortBy="SortName", SortOrder="Ascending",
                      IncludeItemTypes="Movie,Series,Episode,Video,MusicVideo,Audio,MusicAlbum,MusicArtist,BoxSet,Folder",
                      EnableImageTypes="Primary,Thumb,Backdrop")
        if parent:
            params["ParentId"] = parent["Id"]
        result = self.get("/Users/" + self.user_id + "/Items", **params)
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def detail(self, item):
        return self.get("/Users/" + self.user_id + "/Items/" + item["Id"], Fields="SeriesId,SeasonId,IndexNumber,ParentIndexNumber,MediaSources,MediaStreams,Overview,RunTimeTicks,UserData")

    def set_played(self, item, played):
        path = "/Users/" + self.user_id + "/PlayedItems/" + item["Id"]
        method = requests.post if played else requests.delete
        with method(self.server + path, headers=self.headers, timeout=(4, 12), verify=self.verify) as response:
            response.raise_for_status()
        item.setdefault("UserData", {})["Played"] = bool(played)
        if played:
            item["UserData"]["PlaybackPositionTicks"] = 0
        return item

    def set_favourite(self, item, favourite):
        path = "/Users/" + self.user_id + "/FavoriteItems/" + item["Id"]
        method = requests.post if favourite else requests.delete
        with method(self.server + path, headers=self.headers, timeout=(4, 12), verify=self.verify) as response:
            response.raise_for_status()
        item.setdefault("UserData", {})["IsFavorite"] = bool(favourite)
        return item

    def favourite_songs(self):
        return self.get("/Users/" + self.user_id + "/Items", Filters="IsFavorite",
                        IncludeItemTypes="Audio", Recursive="true", Limit=200,
                        SortBy="AlbumArtist,Album,ParentIndexNumber,IndexNumber", SortOrder="Ascending",
                        Fields=FIELDS, EnableImageTypes="Primary,Thumb,Backdrop").get("Items", [])

    def next_episode(self, item):
        series_id = item.get("SeriesId")
        season_id = item.get("SeasonId")
        index = item.get("IndexNumber")
        if not series_id or index is None:
            return None
        params = dict(UserId=self.user_id, Fields=FIELDS + ",SeriesId,SeasonId,IndexNumber,ParentIndexNumber",
                      Limit=200, SortBy="ParentIndexNumber,IndexNumber", SortOrder="Ascending")
        if season_id:
            params["SeasonId"] = season_id
        result = self.get("/Shows/" + series_id + "/Episodes", **params)
        episodes = result.get("Items", [])
        current = next((pos for pos, episode in enumerate(episodes)
                        if episode.get("Id") == item.get("Id")), None)
        if current is not None and current + 1 < len(episodes):
            return episodes[current + 1]
        return next((episode for episode in episodes
                     if episode.get("IndexNumber") == index + 1), None)


def image_candidates(item, landscape):
    """Use server library artwork and episode/series artwork like jellyfin-web."""
    identity = item.get("Id")
    tags = item.get("ImageTags") or {}
    candidates = []
    if landscape and tags.get("Thumb"):
        candidates.append((identity, "Thumb", tags["Thumb"]))
    if landscape and item.get("Type") == "Episode" and item.get("ParentThumbItemId"):
        candidates.append((item["ParentThumbItemId"], "Thumb", item.get("ParentThumbImageTag")))
    candidates.append((identity, "Primary", tags.get("Primary")))
    if landscape and item.get("BackdropImageTags"):
        candidates.append((identity, "Backdrop/0", item["BackdropImageTags"][0]))
    if item.get("SeriesId"):
        candidates.append((item["SeriesId"], "Primary", item.get("SeriesPrimaryImageTag")))
    if item.get("AlbumId"):
        # Tracks rarely have their own image; show the album cover instead.
        candidates.append((item["AlbumId"], "Primary", item.get("AlbumPrimaryImageTag")))
    if item.get("ParentBackdropItemId"):
        candidates.append((item["ParentBackdropItemId"], "Backdrop/0", None))
    return [v for v in candidates if v[0]]


class Artwork:
    """Download/cache bytes on workers, decode/scale surfaces only on the UI thread."""
    PROFILE = "thumb-v8-120q75"
    def __init__(self, api, cache):
        self.api, self.cache = api, cache
        self.jobs = Jobs(2)
        self.pending = set()
        self.images = collections.OrderedDict()
        self.scaled = collections.OrderedDict()
        self.revision = 0
        os.makedirs(cache, exist_ok=True)

    def key(self, item, landscape):
        value = repr((self.PROFILE, self.api.server, image_candidates(item, landscape)))
        return hashlib.sha256(value.encode()).hexdigest()

    def _download(self, item, landscape, key):
        path = os.path.join(self.cache, key + ".img")
        if os.path.exists(path):
            with open(path, "rb") as source:
                return source.read()
        for identity, kind, tag in image_candidates(item, landscape):
            # The RG35XX display is only 640x480.  Keep network images tiny;
            # pygame scales them to the card size after they arrive.
            params = {"maxWidth": 120 if landscape else 96, "quality": 75}
            if tag:
                params["tag"] = tag
            with requests.get(self.api.server + "/Items/" + identity + "/Images/" + kind,
                              params=params, headers=self.api.headers, timeout=(3, 6),
                              verify=self.api.verify) as response:
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    continue
                content = response.content
                if len(content) > 4000000:
                    continue
                with open(path + ".tmp", "wb") as target:
                    target.write(content)
                os.replace(path + ".tmp", path)
                return content
        return None

    def get(self, item, size, landscape=True):
        import io
        import pygame
        self.jobs.poll()
        key = self.key(item, landscape)
        if key not in self.images and key not in self.pending:
            self.pending.add(key)
            def complete(value, error):
                self.pending.discard(key)
                try:
                    surface = pygame.image.load(io.BytesIO(value)).convert() if value else None
                except pygame.error:
                    surface = None
                self.images[key] = surface
                self.revision += 1
            while len(self.images) > 32:
                    self.images.popitem(last=False)
            self.jobs.submit(lambda: self._download(item, landscape, key), complete)
        source = self.images.get(key)
        if source is None:
            return None, key in self.pending
        self.images.move_to_end(key)
        scale_key = (key, tuple(size))
        if scale_key not in self.scaled:
            width, height = source.get_size()
            factor = min(size[0] / width, size[1] / height)
            scaled = pygame.transform.smoothscale(source, (max(1, round(width * factor)), max(1, round(height * factor))))
            self.scaled[scale_key] = scaled
            while len(self.scaled) > 24:
                self.scaled.popitem(last=False)
        self.scaled.move_to_end(scale_key)
        return self.scaled[scale_key], False

    def prefetch(self, items, landscape=True):
        """Start visible shelf downloads before the user scrolls to them."""
        for item in items[:2]:
            self.get(item, (188, 113) if landscape else (140, 220), landscape)


class Rows:
    """Independent horizontal positions; up/down moves between real sections."""
    def __init__(self, values):
        self.values = values
        self.row = 0
        self.columns = [0] * len(values)

    def move(self, x, y):
        if not self.values:
            return
        if y:
            self.row = max(0, min(len(self.values) - 1, self.row - y))
        if x:
            maximum = max(0, len(self.values[self.row].get("items", [])) - 1)
            self.columns[self.row] = max(0, min(maximum, self.columns[self.row] + x))

    def selected(self):
        if not self.values:
            return None
        items = self.values[self.row].get("items", [])
        if not items:
            return None
        self.columns[self.row] = min(self.columns[self.row], len(items) - 1)
        return items[self.columns[self.row]]
