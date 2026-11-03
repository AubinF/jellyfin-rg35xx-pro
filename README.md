# Jellyfin RG35XX H

Watch your Jellyfin library on an Anbernic RG35XX H running KNULLI. This is an unofficial, controller-first app built for the handheld's 640×480 screen.

**Tested on the latest KNULLI Scarab release.**

It has a Jellyfin-style home screen with **My Media**, **Next Up**, **Continue Watching**, and **Recently Added** shelves. Folder artwork and media thumbnails load from your own server. Watched titles display a check badge and partially watched titles show their progress bar.

## Jellyfin server compatibility

Client build `2.1.3` targets **Jellyfin Server 12.0** and uses its current authorization format. Jellyfin Server **10.11.x** is the older compatibility line. Other versions may work, but are not currently verified; include the exact server version when reporting a problem.

## See it in action

This frame comes directly from the recorded RG35XX H demo, with artwork loaded from Jellyfin. Select it to play the full video in your browser.

[![Jellyfin RG35XX H home screen — select to play the demo](docs/screenshots/navigation-demo-preview.jpg)](https://github.com/aG00Dtime/jellyfin-rg35xxh/raw/refs/heads/master/docs/screenshots/navigation-demo.mp4)

[Watch the controller navigation demo (MP4, 6 MB)](https://github.com/aG00Dtime/jellyfin-rg35xxh/raw/refs/heads/master/docs/screenshots/navigation-demo.mp4)

## What you need

- An **Anbernic RG35XX H** with **KNULLI** installed.
- **PortMaster** installed on KNULLI. In KNULLI, press **Start** → **Device Settings** → **Install PortMaster** if you do not already have it. [KNULLI's PortMaster guide](https://knulli.org/systems/portmaster/) explains the standard setup.
- A Jellyfin server you can already sign into.
- Wi-Fi on the handheld. Streaming and artwork both come from your server.

## Install — no terminal needed

1. Download `jellyfinrg35xx.zip` from this project's latest release.
2. Extract the ZIP on your computer. You will get a file named `Jellyfin RG35XX.sh` and a folder named `jellyfinrg35xx`.
3. Open the KNULLI data card, then open `roms/ports`.
4. Copy both the file and the folder into `roms/ports`.
5. Put the card back in the handheld. In KNULLI, open **Ports** and launch **Jellyfin RG35XX**.
6. On first launch, use the built-in login screen to enter your server URL, username, and password with the controller keyboard. A bare domain is automatically treated as HTTPS; explicit `http://` and `https://` URLs are respected.

For a later update, copy the new file and folder into the same place and allow your computer to merge the folder. Do **not** delete `config.json`; release packages deliberately do not contain it.

## Controls

| Where you are | Controls |
| --- | --- |
| Home and folders | D-pad moves, **A** opens, **B** goes back, **Y** searches, **R1** opens Settings, **Start** exits |
| Settings | Change quality, edit connection details, or choose Logout; Logout asks for confirmation and keeps the server and username |
| Login keyboard | D-pad chooses, **A** types, **R1** toggles caps/symbols, **X** inserts a space, **Y** deletes, **Start** saves the field, **B** cancels |
| Search keyboard | D-pad chooses, **A** types, **R1** toggles caps/symbols, **X** inserts a space, **Y** deletes, **Start** searches, **B** cancels |
| Video | **A** pauses or resumes; **B**, **Start**, or **Select** returns to the app |
| Video seeking | D-pad left/right skips 10 seconds; L1/R1 skips 30 seconds; L2/R2 skips 60 seconds |
| Audio and subtitles | **X** cycles audio tracks; **Y** cycles subtitle tracks (including Off) |
| Music | **A** pauses; D-pad left/right seeks 10 seconds; up/down changes track; **X** shows the queue; **Y** shuffles; **R1** opens Music options; **B** returns to the library while music keeps playing |
| Music queue | D-pad chooses, **A** plays the chosen track, **Y** shuffles, **X** cycles repeat (off / all / one), **B** closes |
| Music options (R1) | Favourite the track, set a sleep timer (D-pad left/right changes it), open Saved queues, or turn the screen off |
| Headset buttons | Play/pause, next and previous work from any screen, even with the screen off. A single-button wired remote works like a phone: press once to play/pause, twice for next, three times for previous. Play with nothing playing resumes the most recent saved queue |
| Home | While music plays, **B** returns to Now Playing; otherwise **B** opens Saved queues |
| Anywhere | **R2** opens Saved queues (press again to close) |
| Books | From a Bookshelf library, **A** on a book's page reads it: EPUB as reflowed text, CBZ/CBR comics and PDFs page by page. While reading: **A** next page, **Y** previous, **R1** menu (chapters, text size, theme, zoom, rotate, close). EPUB: **X** text size, D-pad turns pages, **B** closes. Comics/PDF: **X** zoom in, **B** zoom out (press B twice at the whole page to close), D-pad pans (and turns pages only where there's nothing to pan); the menu rotates the page for reading with the handheld upright. Your place is remembered; **Continue reading** on Home reopens it; **R1** on a book's page downloads it for offline reading. CBR needs `unrar`, `7z`, `bsdtar` or `unar` on the device, PDF needs `mutool` or `pdftoppm` |
| Albums and playlists | Shown as a track list (✓ marks tracks on the card); D-pad left/right jumps a screen; **A** plays from that track; **X** shuffles the whole album |
| Shows | **R1** on a show's page: keep its next 3 unwatched episodes downloaded (watched ones are deleted and the next ones fetched whenever you're online), or download all / only unwatched episodes. **R1** on a season asks whether to skip episodes you've already watched |
| Removing downloads | **R1** again on anything downloaded offers to remove it; or **X** twice in Settings → Downloads. Files deleted by hand show as missing (**Y** gets them back) instead of downloading again on their own |
| Offline | If the server can't be reached at startup, the app opens in offline mode: Home shows your Downloads, which play from the card, and Saved queues still work. **X** tries the server again; music keeps playing when it comes back. Needs one online launch first so the app knows your account |
| Downloads | **R1** downloads what the page shows: an album, playlist or audiobook, a film (on its details page), or a whole season (on its episode list). Videos are converted by the server to your Quality setting by default (Settings → Video downloads switches to original files); text subtitles are saved alongside and can be toggled with **Y** during playback, and an image subtitle chosen in Play options is burned in. A size estimate is checked against free space first. The first time, choose where to keep downloads (a second SD card, internal storage, or any folder: browse to it, create a new one, or type its path). Downloaded tracks always play from the card. **Settings → Downloads** shows progress and lets you play (**A**), retry (**Y**) or remove (**X** twice) |
| Saved queues | Up to 10 queues (albums, playlists, audiobooks) keep their track and position. D-pad chooses, **A** resumes, **X** twice removes one, **B** goes back |
| Volume | The handheld's volume buttons still control KNULLI volume |

Selecting a song in an album or playlist queues the whole list and plays it gaplessly. While music plays, the screen turns off after a period without input (30 seconds by default, change it under **Settings → Music screen off**). Any button turns it back on; that press does nothing else.

On a movie or episode details page, press **X** to open Play Options. Choose an audio track or subtitle track with the D-pad, then press **A** to play with those selections. During playback, **X** and **Y** can also cycle the active audio and subtitle tracks.

The app requests server-side H.264/AAC HLS transcoding by default because that is the most reliable format for the RG35XX H. The quality setting controls the requested stream size and bitrate, so playback uses server CPU and may create temporary transcoding files. If the server cannot provide the requested HLS stream, the app can fall back to a direct stream. The app reports playback to Jellyfin while you watch and when you stop, so **Continue Watching**, watched indicators, and resume position stay in sync. Selecting an unfinished item resumes it from its saved position.

When an episode finishes successfully, the next episode starts automatically with a short loading screen. Library folders fetch additional pages automatically as you approach the end of the current page.

Jellyfin temporarily pauses KNULLI's automatic idle sleep while the app is open, then restores it when you exit.

## Early-release note

This is a community project and it is still evolving. It has been tested on an RG35XX H running KNULLI Scarab, but some media formats, server configurations, artwork, or controller situations may still reveal bugs. If something behaves oddly, restart the app first and then [open an issue](https://github.com/aG00Dtime/jellyfin-rg35xxh/issues) with the Jellyfin version, KNULLI version, and a short description of what happened. Never include your server address, username, password, or access token.

## If something goes wrong

- **No connection:** confirm the handheld is on Wi-Fi and that the server URL is correct. A bare domain uses HTTPS by default.
- **Login failed:** open Settings and re-enter the password.
- **Jellyfin 12 login or playback failure:** update to client build `2.1.3` or newer. Older client builds use authorization forms that Jellyfin 12 disables by default.
- **Video is choppy:** open Settings and choose `360p` or `480p`. The RG35XX H is happiest with a modest H.264/AAC stream.
- **The server is transcoding:** this is expected. The app requests H.264/AAC HLS for handheld compatibility; lower the quality in Settings if the server struggles.
- **No artwork yet:** give the home screen a moment. Small thumbnails download in the background and are cached on the SD card.
- **A new release does not appear:** exit back to the KNULLI menu and launch it again.

## Keep your account private

The app encrypts the saved connection data on the handheld using a device-local AES key. The key and encrypted config are excluded from Git and release ZIPs. This protects the SD-card files if copied, but cannot protect credentials from someone with root access to the running device.

## License and attribution

This project is licensed under the [MIT License](LICENSE). If you copy, modify,
or redistribute the software, you must retain the copyright and license notice
included in `LICENSE`.

The project is unofficial and is not affiliated with Jellyfin or KNULLI. The
Jellyfin icon and DejaVu Sans font are third-party assets with their own notices
and licensing requirements; see the attribution notes below and the files in
`app/assets/`.

The app is Python and pygame. On Windows, run:

```powershell
.\package.ps1
```

This creates `jellyfinrg35xx.zip`. Automated checks are in `tests/`; the device-specific verification notes are in [tests/VERIFICATION-1.3.md](tests/VERIFICATION-1.3.md).

The Jellyfin icon is from [Jellyfin UX](https://github.com/jellyfin/jellyfin-ux), used under Jellyfin's [branding guidance](https://jellyfin.org/docs/project/branding/). The bundled DejaVu Sans font includes its required license in [app/assets/DejaVuSans-LICENSE.txt](app/assets/DejaVuSans-LICENSE.txt).
