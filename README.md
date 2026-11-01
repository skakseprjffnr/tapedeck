# Tapedeck

A personal YouTube playlist builder for Arch Linux, Windows and Android. Songs
live in categories and play as audio. No Google account needed.

## Install

```sh
cd tapedeck
makepkg -si
```

This pulls in `pyside6`, `yt-dlp`, `yt-dlp-ejs` (which brings `deno`) and
`mpv`, then adds Tapedeck to your app launcher. Remove it later with
`sudo pacman -R tapedeck`.

Optional extras: `ttf-ibm-plex` (the intended typeface), `mpv-mpris`
(media keys and desktop controls), `qt6-wayland` (native Wayland).

To run it without installing:

```sh
sudo pacman -S --needed pyside6 yt-dlp yt-dlp-ejs mpv
python tapedeck.py
```

## Using it

| Do this | To |
|---|---|
| Click a category in the left sidebar | Open its own page with every song |
| Click **All categories** | Go back to the two-column overview |
| Click **+** (bottom right) | Add a song, add a category, or import a playlist. On a category's page, that category is pre-ticked |
| **+** → **Import Playlist** | Paste a YouTube playlist link, a Tapedeck share code, a whole-library code, or YouTube links (one per line); its songs go into a new category or into categories you pick |
| Right-click a category or its tab → **Share or export…** | Copy a share code for other Tapedeck users, or the songs' YouTube links. Category pages also have a share button |
| Double-click a category's **title** | Rename it, on its card or its page (Enter saves, Esc cancels). Its subtitle works the same way |
| Click **▶** on a category | Play it top to bottom (click **■** to stop) |
| Click **shuffle** on a category | Play it in random order, or switch shuffle on or off mid-song |
| Click **▶** on a song | Play just that song. While its category is playing, jump to it instead |
| Use the bar along the bottom | Shuffle, previous, play/pause, next, repeat, volume and mute |
| Click **repeat** in the bottom bar | Play the current song over and over; click again to stop |
| Click the **gear** at the top of the sidebar | Settings: accent color (any hex code) and saving songs for instant play |
| Click the **phone** at the top of the sidebar | Copy your whole library to the Android app (see below) |
| Click or drag the locator bar | Jump anywhere in the song |
| Click the song in the bottom bar | Open its category's page at that song |
| Drag the **☰** handle | Move a song up or down (the play order follows) |
| Double-click a song's **number** | Send it to a position (type the number, press Enter) |
| Double-click a song's **title** | Rename it (clear the text to go back to the video's title) |
| Right-click any song | **Add to categories…**: tick every category it should be in, untick to take it out. Also works on the song in the bottom bar |
| Right-click a song, **Copy YouTube link** or **Copy YouTube Music link** | Put the song's link on the clipboard. Also works on the song in the bottom bar |
| Right-click a song, **Remove from this category** | Take it out of just that category |
| Right-click a category or its tab | Rename or delete the category |

Every category shows its song count and total length ("12 songs, 47 min"), and
the overview shows the whole library's. While some lengths are still being
looked up, it says "at least". Cards on the overview show a category's first
40 songs; **See all** opens its page with the rest.

Keyboard: **Space** play/pause, **←/→** back or forward 5 seconds, **F11**
full screen, **Ctrl+,** settings, **Ctrl+Q** quit (also stops playback).

Zoom: **Ctrl+scroll** or **Ctrl+plus/minus** makes everything bigger or
smaller, from 70% to 180%; **Ctrl+0** goes back to 100%. Tapedeck remembers
your zoom, and music keeps playing while it changes.

## Sharing playlists

**Share or export** a category to get either:

- **A share code** (starts with `tapedeck:1:`). A friend pastes it into
  **+** → **Import Playlist** and gets the same name, subtitle, song order and
  renamed titles. It's about 18 characters per song, so 100 songs come to
  roughly 1,800 characters; very long codes can be sent as a text file.
- **YouTube links**, one per line, for sharing anywhere else. Tapedeck can
  import a list like that too.

**Keeping a playlist in step with a friend.** Copies made from share codes
remember where they came from. When either of you sends a newer code, pasting
it offers to **update** your copy to match: it adds and removes songs, takes
their order, and picks up renames. Changes only travel when someone sends a
new code, and updating replaces your version with theirs, so if you both
edited it, agree on whose copy to keep first.

## Copying to your phone

Click the **phone** button at the top of the sidebar. Tapedeck shows a QR code;
in the Android app, tap **Import**, then **Scan QR code**. The phone downloads
your whole library from this computer over Wi-Fi: every category, song order,
title, length, and your accent color.

- Your phone and computer need to be on the same Wi-Fi. If your computer asks
  whether Tapedeck may use the network, allow it (private networks are enough).
- The QR code only works while that window is open, and it's a one-time
  address that serves nothing else.
- On another network, click **Copy code** instead, send the code to your phone
  (a message to yourself works), and paste it into the app's Import screen.
- Sending again later updates the phone's copies to match instead of adding
  duplicates.
- The same code also works in **+** → **Import Playlist** on another computer,
  for moving your whole library there.

## Importing playlists

Public and unlisted playlists work, including YouTube Music playlists and
mixes. Private playlists and your own lists (Watch later, Liked videos) need
a signed-in account, so they can't be imported. Songs keep the playlist's
order and go to the top of each category you choose. Importing the same
playlist again only adds songs the category doesn't have yet. Deleted and
private videos in a playlist are left out.

## How songs start instantly

YouTube makes every player wait about five seconds before a song's audio is
available, and there's no way around that. So Tapedeck saves each song's
audio to disk once, and plays saved songs straight from there: they start
immediately, and back-to-back songs play without a gap.

Songs are saved as soon as you paste their link, when they're coming up next,
when you open their category's page, and in the background for the rest of
your library. The sidebar's bottom corner shows how many are saved and how
much space they use (about 3 to 5 MB each). Click it to turn saving off or to
delete the saved copies.

A song that isn't saved yet streams after YouTube's wait (the bottom bar says
so), and is saved for next time.

## Windows

The Windows version comes with mpv, yt-dlp and Deno built in, so Windows
users have nothing else to install.

### Making the .exe

The Windows files have to be built on Windows. There are two ways.

**On GitHub, no Windows PC needed.** Put this folder in a GitHub repository,
open its **Actions** tab, choose **Windows build** and click **Run workflow**.
After about ten minutes, download **Tapedeck-windows** from the run's
Artifacts. It contains:

- `Tapedeck-0.8.0-windows-setup.exe`, an installer that needs no admin rights
- `Tapedeck-0.8.0-windows-portable.zip`, which runs from any folder

Pushing a tag such as `v0.8.0` builds the same files (and the Android app) and
attaches them to that release, which makes them easy to share.

**On a Windows PC.** Install Python 3.11 or newer, plus Inno Setup 6 if you
want the installer (without it you get just the zip). Then, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

The files land in `windows\out`.

### Notes for Windows users

- The installer isn't code-signed, so Windows SmartScreen may say it
  "protected your PC". Click **More info**, then **Run anyway**.
- The library and settings live in `%APPDATA%\Tapedeck`; saved songs,
  thumbnails and the log in `%LOCALAPPDATA%\Tapedeck`. Uninstalling keeps
  them, so reinstalling brings everything back.
- Tapedeck updates its copy of yt-dlp about once a day, because YouTube
  changes often.

## Android

The Android app is a separate program in the `android` folder, because Android
doesn't let apps run mpv or yt-dlp. It uses NewPipeExtractor, the library behind
the NewPipe app, which handles YouTube's checks on the phone itself.

It plays what you copy over from your computer: play, shuffle and repeat-one,
in the background with lock-screen and notification controls. Drag the bar
under the player to move back or forward through a song. You can copy a song's
links, remove songs, and delete categories. Adding songs on the phone itself
isn't there yet: add them on your computer, then send the library again.

### Playing offline

Copying the library sends the song list, not the audio. So right after an
import, the phone starts saving every song from YouTube in the background.
A progress bar at the top of the library (and a notification) shows how far
along it is, for example "Saving songs for offline: 37 of 120". You can play
songs meanwhile: saved ones, marked with a check, start instantly and work
offline, and the rest stream.

- Saving uses Wi-Fi only, unless you allow mobile data in the **⋮** menu (each
  song is about 3 to 5 MB). Without Wi-Fi the bar says it's waiting.
- It keeps going with the screen off, and picks up where it left off if the
  app or the phone restarts.
- Songs you remove, here or by sending an updated library, are deleted from
  the phone.
- The **⋮** menu also turns saving off, or deletes every saved song to free
  space.

### Installing it

Download `Tapedeck-<version>-android.apk` from your GitHub release on the
phone and open it. Android asks you to allow installing apps from your browser
or file manager the first time.

### The signing key (one-time setup)

Every APK has to be signed with the same key, or Android won't install an
update over the old version. Your key is in `android/signing/`, which git
ignores. Back that folder up somewhere safe, then give it to GitHub once:

```sh
base64 -w0 android/signing/tapedeck.keystore | gh secret set ANDROID_KEYSTORE
gh secret set ANDROID_KEYSTORE_PASSWORD --body "$(cat android/signing/password.txt)"
```

Without these, builds still work, but each one is signed with a throwaway key,
so updating means uninstalling first, which deletes the phone's library (you
can send it again from your computer).

### Building it

On GitHub: **Actions** → **Android build** → **Run workflow**, then download
**Tapedeck-android** from the run's Artifacts. Tag pushes build it
automatically, alongside the Windows files. To build it yourself, open the
`android` folder in Android Studio.

### Notes

- If YouTube changes something and songs stop playing on the phone, the fix
  is usually a newer NewPipeExtractor: change its version in
  `android/app/build.gradle.kts` and push a new tag.
- NewPipeExtractor is licensed under the GPLv3, so the app built with it is
  too. That's fine for you and your friends; since the source is in your
  public repository, sharing the APK already meets the license's terms.

## Where things are kept (Linux)

- Library: `~/.local/share/tapedeck/library.json` (plain JSON, safe to back up)
- Settings (volume, saving on/off): `~/.local/share/tapedeck/settings.json`
- Saved songs: `~/.cache/tapedeck/audio/` (removed songs are deleted from here)
- Thumbnails: `~/.cache/tapedeck/thumbs/`
- Log: `~/.local/state/tapedeck/tapedeck.log`

## If playback stops working

1. Read the message Tapedeck shows. It names the problem when it can.
2. Update first, since yt-dlp usually fixes YouTube changes within days:
   `sudo pacman -Syu`.
3. Run `tapedeck --debug` (or `python tapedeck.py --debug`) from a terminal
   and try again. Everything yt-dlp and mpv report appears there and in the
   log file, along with their versions.
