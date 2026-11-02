# Jellyfin RG35XX H

An unofficial controller-first Jellyfin client for the Anbernic RG35XX H running KNULLI.

Client build `2.1.3` targets **Jellyfin Server 12.0**. Jellyfin Server **10.11.x** is the older compatibility line. Other server versions are not currently verified.

## First-time setup

1. Copy the `Jellyfin RG35XX.sh` file and the `jellyfinrg35xx` folder into `roms/ports` on the KNULLI data card.
2. Launch **Jellyfin RG35XX** from KNULLI's **Ports** section.
3. On the first launch, use the controller keyboard to enter your Jellyfin server URL, username, and password.

The app accepts a bare domain and adds `https://` automatically. It also accepts explicit `http://` and `https://` URLs. After login, open Settings with **R1** to change video quality, edit connection details, or log out. Logging out asks for confirmation and keeps the server URL and username while requiring the password again.

Credentials are encrypted on the device using a device-local key. Never share the config or key files. Updates do not include or replace your saved connection data.

## Controls

- Home and library: D-pad moves, **A** opens, **B** goes back, **Y** searches, **R1** opens Settings, **Start** exits.
- Search keyboard: D-pad chooses, **A** types, **R1** toggles caps/symbols, **X** inserts a space, **Y** deletes, **Start** searches, **B** cancels.
- Details: **A** plays or resumes; **X** opens audio/subtitle Play Options; **Y** toggles watched status; **B** goes back.
- Video: **A** pauses, **B**, **Start**, or **Select** returns to the app.
- Video seeking: D-pad left/right skips 10 seconds; L1/R1 skips 30 seconds; L2/R2 skips 60 seconds.
- Audio and subtitles: **X** cycles audio tracks; **Y** cycles subtitle tracks, including Off.
- Music: **A** pauses; D-pad left/right seeks; up/down changes track; **X** shows the queue (there **X** cycles repeat); **Y** shuffles; **R1** opens Music options (favourite, sleep timer, saved queues, screen off); **B** returns to the library while music keeps playing.
- Headset (wired or Bluetooth): play/pause, next and previous control the music from any screen, even with the screen off. Single-button remotes: 1 press play/pause, 2 next, 3 previous.
- **R2** opens Saved queues from anywhere. Home: **B** returns to Now Playing, or opens Saved queues: up to 10 queues (e.g. audiobooks) each keep their track and position. **A** resumes, **X** twice removes.
- Downloads: **R1** downloads an album, playlist, audiobook, film (details page) or season (episode list); videos are converted to your quality setting unless Settings → Video downloads says original; choose the location the first time (second SD card, internal, or browse to / create any folder). Manage in Settings → Downloads.
- Offline: without a server the app opens with your Downloads (and Saved queues); **X** tries to go online again.
- Albums are a track list: **A** plays, **X** shuffles all. **R1** on a show keeps its next 3 episodes downloaded; **R1** again on anything downloaded removes it.
- The screen turns off while music plays without input (Settings → Music screen off); any button wakes it.
- Volume buttons remain controlled by KNULLI.

Automatic KNULLI idle sleep is paused while Jellyfin is open and returns to normal when you exit.

Unfinished items resume from Jellyfin's saved position. Episode playback advances automatically to the next episode after a short loading screen. Library folders load more results automatically as you reach the end of the current page.

The app requests server-side H.264/AAC HLS transcoding by default for reliable RG35XX H playback. The quality setting controls the requested stream size and bitrate, so this uses server CPU and may create temporary transcoding files. A direct-stream fallback may be used if the requested HLS stream cannot be provided.
