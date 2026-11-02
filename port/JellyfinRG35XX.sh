#!/bin/bash
# PORTMASTER: jellyfinrg35xx.zip, Jellyfin RG35XX.sh
XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
if [ -d "/opt/system/Tools/PortMaster/" ]; then controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then controlfolder="$XDG_DATA_HOME/PortMaster"
else controlfolder="/roms/ports/PortMaster"; fi
source "$controlfolder/control.txt"
get_controls
GAMEDIR="/$directory/ports/jellyfinrg35xx"
CONFDIR="$GAMEDIR/conf"
GAMELIST="/$directory/ports/gamelist.xml"
PM_IMAGES="$XDG_DATA_HOME/PortMaster/config/images_pm"
# Existing installs keep their old game-list entry when updated. Add the cover
# reference once without replacing play time or other user-maintained metadata.
if [ -f "$GAMELIST" ] && ! sed -n '/<path>\.\/Jellyfin RG35XX\.sh<\/path>/,/<\/game>/p' "$GAMELIST" | grep -q '<image>'; then
  sed -i '/<path>\.\/Jellyfin RG35XX\.sh<\/path>/,/<\/game>/ { /<genre>Media<\/genre>/a\            <image>./jellyfinrg35xx/cover.png</image>
}' "$GAMELIST"
fi
# Repair the temporary JPEG diagnostic cover used by development builds.
sed -i '/<path>\.\/Jellyfin RG35XX\.sh<\/path>/,/<\/game>/ s#\./jellyfinrg35xx/cover\.jpg#./jellyfinrg35xx/cover.png#' "$GAMELIST"
# The manifest and game info belong in the app folder, just like PortMaster's
# own ports. Only the old nested launcher created a second Ports entry.
rm -f "$GAMEDIR/JellyfinRG35XX.sh"
if [ -f "$GAMEDIR/cover.png" ]; then
  mkdir -p "$PM_IMAGES"
  cp -f "$GAMEDIR/cover.png" "$PM_IMAGES/jellyfinrg35xx.screenshot.png"
  PM_INDEX="$PM_IMAGES/images.json"
  if [ -f "$PM_INDEX" ] && ! grep -q 'jellyfinrg35xx.screenshot.png' "$PM_INDEX"; then
    sed -i '/"gmu.music.player.screenshot.jpg",/a\            "jellyfinrg35xx.screenshot.png",' "$PM_INDEX"
  fi
fi
# Python bytecode from a manually copied update can have the same timestamp as
# its source. Regenerate this tiny cache each launch so KNULLI always uses the
# current app files.
rm -rf "$GAMEDIR/__pycache__"
mkdir -p "$CONFDIR"
# A second launch otherwise invalidates the first login and competes for video.
exec 9>"$GAMEDIR/.launcher.lock"
flock -n 9 || exit 0
cd "$GAMEDIR" || exit 1
> "$GAMEDIR/log.txt" && exec > >(tee "$GAMEDIR/log.txt") 2>&1
export XDG_CONFIG_HOME="$CONFDIR"
export XDG_DATA_HOME="$CONFDIR"
export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
BATTSAVER_PAUSE="/var/run/battery-saver/jellyfinrg35xx.pause"
mkdir -p "${BATTSAVER_PAUSE%/*}"
: > "$BATTSAVER_PAUSE"
cleanup() {
  rm -f "$BATTSAVER_PAUSE"
  pm_finish
}
trap cleanup EXIT
$GPTOKEYB "jellyfin_rg35xx.py" &
command -v pm_platform_helper >/dev/null && pm_platform_helper "$GAMEDIR/jellyfin_rg35xx.py"
python3 ./jellyfin_rg35xx.py
