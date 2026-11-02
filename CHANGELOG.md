# Changelog

## [Unreleased]

### Added

- Music player: picking a track in an album or playlist queues the whole list and keeps playing while you browse.
- Now Playing screen with cover art, progress, pause, seek (left/right), and previous/next track (up/down).
- Queue view to see and jump to any track, plus shuffle and repeat (off / all / one).
- Albums list in track order and playlists keep their own order.
- Gapless playback: the next track is lined up and pre-buffered so albums flow without a pause.
- Saved queues: up to 10 queues (albums, playlists, audiobooks) keep their own track and position on the device, even after a restart. Switch between them from **B** on Home or **R1** → Saved queues; picking the chapter you were on in the library resumes mid-chapter. Finished queues leave the list.
- Favourites: mark the playing track from Music options; a Favourite Songs row appears on Home for music libraries.
- Sleep timer (15–90 minutes or end of track) that fades the music out.
- Screen turns off during music playback after a configurable idle time (Settings → Music screen off). On the RG35XX H this sets the panel brightness to 0 through the Allwinner `/dev/disp` driver (KNULLI has no `/sys/class/backlight` there); the launcher restores the brightness on exit.
- Headset media buttons (wired remotes and Bluetooth AVRCP headphones) control music from any screen without waking it; single-button remotes use 1/2/3 presses for play-pause/next/previous.

### Fixed

- The release package now includes `music.py`.

## [2.1.3] - 2026-09-08

### Fixed

- Added Jellyfin 12-compatible `Authorization: MediaBrowser` request headers.
- Switched authenticated playback URLs from legacy `api_key=` to `ApiKey=`.
- Updated mpv playback authentication so transcoded streams work with Jellyfin 12.

### Documentation

- Documented Jellyfin Server 12.0 as the target server version.
- Documented Jellyfin Server 10.11.x as the older compatibility line.
- Added troubleshooting guidance for Jellyfin 12 authorization failures.

## [2.1.2] - 2026-09-05

- Reduced artwork download sizes and memory cache limits for faster browsing on the RG35XX H.
