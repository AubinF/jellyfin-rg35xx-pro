#!/usr/bin/python3
import io, json, logging, math, os, subprocess, tempfile, time, uuid
import pygame
import requests
from playback import ControllerInput, PlaybackReporter, PlaybackSession

BUILD = "2.1.3"
CONFIG = os.environ.get("JELLYFIN_CONFIG", "/userdata/roms/ports/jellyfinrg35xx/config.json")
CONFIG_KEY = CONFIG + ".key"
def _ca_bundle():
    # A missing hard-coded bundle makes every request fail on non-KNULLI firmware.
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem"):
        if os.path.exists(path):
            return path
    try:
        import certifi
        return certifi.where()
    except Exception:
        return True

CA_BUNDLE = _ca_bundle()
LOG = logging.getLogger("jellyfin")

def normalize_server_url(value):
    value = str(value or "").strip().rstrip("/")
    if value and not value.lower().startswith(("http://", "https://")):
        host = value.split("/")[0].rsplit("@", 1)[-1]
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        local = name.replace(".", "").isdigit() or name.endswith(".local") or "." not in name
        # Bare LAN addresses and explicit ports are normally plain-HTTP Jellyfin servers.
        value = ("http://" if local or ":8096" in host else "https://") + value
    return value

def authorization_value(token=None):
    value = 'MediaBrowser Client="RG35XX Jellyfin", Device="RG35XX H", DeviceId="rg35xxh", Version="' + BUILD + '"'
    return value + ', Token="' + token + '"' if token else value

def auth_headers(token=None):
    modern = authorization_value(token)
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": modern,
               # Keep the legacy headers for Jellyfin versions that still accept them.
               "X-Emby-Authorization": authorization_value()}
    if token:
        headers["X-Emby-Token"] = token
    return headers

def load_config():
    try:
        with open(CONFIG, "rb") as f: raw = f.read()
        if raw.startswith(b"Salted__"):
            with tempfile.NamedTemporaryFile() as decrypted:
                subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", CONFIG,
                                "-out", decrypted.name, "-pass", "file:" + CONFIG_KEY], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                decrypted.seek(0)
                value = json.loads(decrypted.read().decode("utf-8"))
        else:
            value = json.loads(raw.decode("utf-8"))
        for key in ("serverUrl", "username"):
            if not value.get(key): raise ValueError("config needs serverUrl, username, and password")
        value["serverUrl"] = normalize_server_url(value["serverUrl"])
        if not raw.startswith(b"Salted__"):
            save_config(value)
        return value, ""
    except FileNotFoundError: return None, "Missing config.json"
    except Exception as exc: return None, "Config error: " + str(exc)

def save_config(value):
    """Persist first-run setup encrypted with a device-local AES key."""
    directory = os.path.dirname(CONFIG)
    os.makedirs(directory, exist_ok=True)
    if not os.path.exists(CONFIG_KEY):
        with open(CONFIG_KEY, "wb") as f:
            f.write(os.urandom(32))
        os.chmod(CONFIG_KEY, 0o600)
    plaintext = CONFIG + ".plain.tmp"
    encrypted = CONFIG + ".tmp"
    with open(plaintext, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    try:
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-in", plaintext,
                        "-out", encrypted, "-pass", "file:" + CONFIG_KEY], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(encrypted, 0o600)
        os.replace(encrypted, CONFIG)
    finally:
        try: os.remove(plaintext)
        except FileNotFoundError: pass
        try: os.remove(encrypted)
        except FileNotFoundError: pass

def draw_jellyfin_mark(surface, x, y, scale=1.0):
    """Dependency-free fin mark that remains crisp on the RG35XX display."""
    cyan = (0, 164, 220)
    points = [(x, y + int(29 * scale)), (x + int(7 * scale), y + int(10 * scale)),
              (x + int(16 * scale), y), (x + int(15 * scale), y + int(14 * scale)),
              (x + int(27 * scale), y + int(4 * scale)), (x + int(24 * scale), y + int(18 * scale)),
              (x + int(37 * scale), y + int(11 * scale)), (x + int(30 * scale), y + int(29 * scale))]
    pygame.draw.polygon(surface, cyan, points)
    cut = (12, 16, 25)
    pygame.draw.line(surface, cut, (x + int(10 * scale), y + int(25 * scale)), (x + int(16 * scale), y + int(9 * scale)), max(1, int(3 * scale)))
    pygame.draw.line(surface, cut, (x + int(19 * scale), y + int(26 * scale)), (x + int(26 * scale), y + int(12 * scale)), max(1, int(3 * scale)))

def authenticate(config):
    url = config["serverUrl"].rstrip("/") + "/Users/AuthenticateByName"
    headers = auth_headers()
    response = requests.post(url, headers=headers, json={"Username": config["username"], "Pw": config["password"]}, timeout=20, verify=CA_BUNDLE)
    response.raise_for_status(); value = response.json()
    return value["AccessToken"], value["User"]["Id"]

def fetch_items(config, token, user_id, extra="", start=0):
    url = config["serverUrl"].rstrip("/") + "/Users/" + user_id + "/Items?SortBy=SortName&Recursive=true&StartIndex=" + str(start) + "&Limit=30" + extra
    response = requests.get(url, headers=auth_headers(token), timeout=20, verify=CA_BUNDLE)
    response.raise_for_status()
    return response.json().get("Items", [])

def fetch_views(config, token, user_id):
    url = config["serverUrl"].rstrip("/") + "/Users/" + user_id + "/Views"
    response = requests.get(url, headers=auth_headers(token), timeout=20, verify=CA_BUNDLE)
    response.raise_for_status(); return response.json().get("Items", [])

def fetch_children(config, token, user_id, parent_id):
    return fetch_items(config, token, user_id, "&ParentId=" + parent_id + "&Recursive=false&IncludeItemTypes=Folder,Movie,Series,Season,Episode&SortBy=SortName")

def fetch_seasons(config, token, user_id, show_id):
    url = config["serverUrl"].rstrip("/") + "/Shows/" + show_id + "/Seasons?UserId=" + user_id + "&Fields=PrimaryImageAspectRatio"
    response = requests.get(url, headers=auth_headers(token), timeout=20, verify=CA_BUNDLE)
    response.raise_for_status()
    return response.json().get("Items", [])

def fetch_episodes(config, token, user_id, show_id, season_id):
    url = config["serverUrl"].rstrip("/") + "/Shows/" + show_id + "/Episodes?UserId=" + user_id + "&SeasonId=" + season_id + "&Fields=Overview,RunTimeTicks,PrimaryImageAspectRatio"
    response = requests.get(url, headers=auth_headers(token), timeout=20, verify=CA_BUNDLE)
    response.raise_for_status()
    return response.json().get("Items", [])

def thumbnail(config, token, item, cache, image_cache):
    item_id = item.get("Id")
    if not item_id: return None
    if item_id in image_cache: return image_cache[item_id]
    path = os.path.join(cache, item_id + ".jpg")
    try:
        if not os.path.exists(path):
            url = config["serverUrl"].rstrip("/") + "/Items/" + item_id + "/Images/Primary?maxWidth=120&quality=75"
            response = requests.get(url, headers=auth_headers(token), timeout=15, verify=CA_BUNDLE)
            response.raise_for_status()
            with open(path, "wb") as f: f.write(response.content)
        image_cache[item_id] = pygame.image.load(path).convert()
        return image_cache[item_id]
    except Exception:
        image_cache[item_id] = None
        return None

def report_playing(config, token, user_id, item, session_id, position_ticks=0, event="progress", play_method="DirectPlay", media_source_id=None, paused=False):
    endpoint = {"started": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress", "stopped": "/Sessions/Playing/Stopped"}[event]
    payload = {"ItemId": item.get("Id"), "MediaSourceId": media_source_id or item.get("MediaSourceId", item.get("Id")), "CanSeek": True, "IsPaused": False,
               "PlayMethod": play_method, "PlaySessionId": session_id,
               "PositionTicks": int(position_ticks), "PlaylistIndex": 0, "VolumeLevel": 100,
               "AudioStreamIndex": -1, "SubtitleStreamIndex": -1, "IsMuted": False,
               "RepeatMode": "RepeatNone"}
    payload["IsPaused"] = paused
    with requests.post(config["serverUrl"].rstrip("/") + endpoint,
                       headers=auth_headers(token), json=payload,
                       timeout=(3, 3), verify=CA_BUNDLE) as response:
        LOG.info("Playback %s HTTP %d position_ticks=%d", event, response.status_code, int(position_ticks))
        response.raise_for_status()

def prepare_playback(config, token, user_id, item, audio_index=None, subtitle_index=None):
    item_id = item.get("Id")
    if not item_id: raise ValueError("Missing media ID")
    session_id = str(uuid.uuid4())
    resume_ticks = item.get("UserData", {}).get("PlaybackPositionTicks", 0)
    resume_seconds = resume_ticks / 10000000.0
    quality = config.get("quality", "480p").lower()
    profiles = {"360p": (640, 360, 900000, 1200000), "480p": (854, 480, 1400000, 1800000), "720p": (1280, 720, 2800000, 3400000), "1080p": (1920, 1080, 5000000, 6000000)}
    width, height, video_bitrate, max_bitrate = profiles.get(quality, profiles["480p"])
    url = config["serverUrl"].rstrip("/") + "/Videos/" + item_id + "/stream?static=true&ApiKey=" + token
    play_method = "DirectPlay"
    media_source_id = item.get("MediaSourceId")
    try:
        profile = {"Name": "RG35XX H", "MaxStreamingBitrate": max_bitrate, "MaxStaticBitrate": max_bitrate,
                   "SupportedMediaTypes": "Video", "DirectPlayProfiles": [],
                   "TranscodingProfiles": [{"Container": "ts", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac", "Protocol": "hls", "Context": "Streaming", "MaxAudioChannels": "2", "MinSegments": 1, "SegmentLength": 3}],
                   "SubtitleProfiles": [{"Format": "srt", "Method": "Encode"}]}
        info = requests.post(config["serverUrl"].rstrip("/") + "/Items/" + item_id + "/PlaybackInfo",
                             params={"UserId": user_id, "StartTimeTicks": resume_ticks},
                             headers=auth_headers(token),
                             json={"DeviceProfile": profile}, timeout=6, verify=CA_BUNDLE)
        info.raise_for_status(); info_value = info.json(); sources = info_value.get("MediaSources", [])
        session_id = info_value.get("PlaySessionId") or session_id
        if sources:
            source_id = sources[0].get("Id")
            media_source_id = source_id
            url = (config["serverUrl"].rstrip("/") + "/Videos/" + item_id + "/master.m3u8?ApiKey=" + token
                   + "&MediaSourceId=" + str(source_id) + "&DeviceId=rg35xxh&PlaySessionId=" + session_id
                   + "&VideoCodec=h264&AudioCodec=aac&MaxWidth=" + str(width) + "&MaxHeight=" + str(height)
                   + "&VideoBitrate=" + str(video_bitrate) + "&AudioBitrate=128000&MaxStreamingBitrate=" + str(max_bitrate)
                   + "&TranscodingMaxAudioChannels=2&SegmentContainer=ts"
                   + ("&AudioStreamIndex=" + str(audio_index) if audio_index is not None else "")
                   + ("&SubtitleStreamIndex=" + str(subtitle_index) + "&SubtitleMethod=Encode"
                      if subtitle_index is not None else ""))
            probe = requests.get(url, headers=auth_headers(token), timeout=4, verify=CA_BUNDLE, stream=True)
            invalid = probe.status_code < 200 or probe.status_code >= 300 or "application/json" in probe.headers.get("content-type", "")
            probe.close()
            if invalid:
                url = config["serverUrl"].rstrip("/") + "/Videos/" + item_id + "/stream?static=true&ApiKey=" + token
            else:
                play_method = "Transcode"
    except Exception as exc:
        LOG.warning("Transcode setup failed (%s: %s); using direct stream", type(exc).__name__, exc)
    ipc_path = "/tmp/jellyfin-mpv-" + session_id + ".sock"
    try: os.unlink(ipc_path)
    except FileNotFoundError: pass
    mpv_args = ["mpv", "--fullscreen", "--force-window=yes", "--msg-level=all=v", "--log-file=" + os.path.join(os.path.dirname(CONFIG), "mpv-log.txt"),
                "--input-terminal=no", "--input-default-bindings=no", "--input-vo-keyboard=no",
                "--osd-font=DejaVu Sans", "--osd-font-size=22",
                "--input-ipc-server=" + ipc_path, "--hwdec=auto-safe", "--framedrop=vo", "--cache=yes",
                "--http-header-fields=X-Emby-Token: " + token,
                "--cache-pause=no", "--video-sync=audio", "--audio-buffer=1",
                "--cache-secs=30", "--demuxer-max-bytes=128MiB", "--demuxer-max-back-bytes=64MiB"]
    if resume_seconds > 3: mpv_args.append("--start=" + str(resume_seconds))
    mpv_args.append(url)
    return mpv_args, ipc_path, session_id, resume_ticks, play_method, media_source_id


def play_item(config, token, user_id, item, prepared=None):
    mpv_args, ipc_path, session_id, resume_ticks, play_method, media_source_id = prepared or prepare_playback(config, token, user_id, item)
    controls = None
    reporter = None
    session = None
    status = ""
    try:
        # Verify controls before opening video. SDL joystick indices differ here.
        controls = ControllerInput()
        reporter = PlaybackReporter(
            lambda event, ticks, paused: report_playing(
                config, token, user_id, item, session_id, ticks, event=event,
                play_method=play_method, media_source_id=media_source_id, paused=paused))
        session = PlaybackSession(mpv_args, ipc_path, resume_ticks, reporter, controls)
        # Release the display to mpv; controls use evdev and never need video focus.
        pygame.display.quit()
        session.run()
        item["_PlaybackCompleted"] = session.completed
        status = "Saving progress..."
    except Exception as exc:
        LOG.exception("Playback failed (%s)", type(exc).__name__)
        status = "Playback closed safely. Please try again."
    finally:
        if controls:
            controls.close()
        if session:
            item.setdefault("UserData", {})["PlaybackPositionTicks"] = session.position_ticks
        pygame.display.init()
        screen = pygame.display.set_mode((640, 480))
        pygame.event.clear()
        LOG.info("Library display restored")
    return screen, status, reporter

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import fcntl
    instance_lock = open(os.path.join(os.path.dirname(CONFIG), ".python-instance.lock"), "a")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.info("Jellyfin is already running")
        instance_lock.close()
        return
    LOG.info("Jellyfin build %s starting", BUILD)
    from ui import LibraryUI
    config, status = load_config()
    LibraryUI(config, status, BUILD, authenticate, play_item, prepare_playback, auth_headers, CA_BUNDLE, save_config, normalize_server_url).run()

if __name__ == "__main__": main()
