# Format profiles

One serialisable object, stored **per item** so that re-downloads are exactly
reproducible six months later.

```jsonc
{
  "audio_codec":      "aac",    // "aac" | "mp3"   — default aac
  "audio_bitrate":    192,      // 128 | 192 | 256
  "prefer_copy":      true,     // stream-copy when the source is already acceptable AAC
  "keep_video":       false,    // default OFF
  "video_max_height": 1080,
  "save_artwork":     true,     // default ON
  "artwork_source":   "auto"    // "thumbnail" | "frame" | "auto"
}
```

Store it per item, not only as a global default. Six months from now you will
want to re-pull one long podcast at 128 kbps mono without touching the rest of
the library. With a global-only setting that is a migration; with a per-item
profile it is a button.

---

## Audio

### AAC (default)

```
ffmpeg -i in.webm -vn -c:a aac -b:a 192k -movflags +faststart out.m4a
```

With `prefer_copy` and an already-acceptable source:

```
ffmpeg -i in.m4a -vn -c:a copy -movflags +faststart out.m4a
```

Check before transcoding: if the source audio stream is already AAC, and the
target bitrate is **not lower** than the source's, **stream-copy it**. YouTube's
itag 140 is ~129 kbps AAC and SoundCloud frequently serves AAC over HLS.
Transcoding AAC → AAC costs quality for nothing and burns server time.

The comparison direction matters and the obvious version of it is wrong. "Copy
if the source is at or above the target" sounds right, but with a 192 default
and a 129 kbps source it never fires: every YouTube track would take a lossy
AAC → AAC transcode producing a *larger* file containing *worse* audio. Raising
a bitrate cannot recover information the first encoder discarded. Transcode only
when the user asked for something **smaller** than the source — the "re-pull this
podcast at 128 mono" case that per-item profiles exist for. See `08-decisions.md`
D-014.

`+faststart` moves the moov atom to the front. It costs one extra pass and it is
what lets playback start immediately from a local file.

### MP3

```
ffmpeg -i in.webm -vn -c:a libmp3lame -q:a 2 -id3v2_version 3 out.mp3
```

`-q:a 2` is VBR at roughly 190 kbps and is a better default than CBR. Offer CBR
(`-b:a 192k`) only as an explicit option for stubborn old hardware.

### Format selection handed to yt-dlp

```python
if keep_video:
    fmt = f"bestvideo[height<={video_max_height}]+bestaudio/best[height<={video_max_height}]"
else:
    fmt = "bestaudio[acodec^=mp4a]/bestaudio/best"
```

Preferring an `mp4a` audio stream up front is what makes `prefer_copy` pay off.

### Tags

```
-metadata title="…"  -metadata artist="…"
-metadata album="…"  -metadata date="…"
```

`artist` falls back to the uploader name when the extractor gives no artist.
`album` falls back to the source playlist name, then to "Library".

---

## Artwork

`artwork_source: "auto"` resolves as:

1. Use the extractor's thumbnail if one exists (almost always for both sources).
2. Otherwise, if the source has video, extract a frame:

```
ffmpeg -ss {duration*0.10} -i in.mp4 -frames:v 1 -q:v 3 art.jpg
```

Ten percent in avoids both black lead-ins and title cards, which is where a
naive `-ss 0` lands.

**Embed it** so the file is self-describing outside the app too:

```
ffmpeg -i out.m4a -i art.jpg -map 0 -map 1 -c copy \
       -disposition:v:0 attached_pic tagged.m4a
```

**Keep two crops.** YouTube thumbnails are 16:9 and look wrong in a square grid.
Centre-crop to `art-sq.jpg` for list and grid views; keep the original as
`art.jpg` for the now-playing view.

```
ffmpeg -i art.jpg -vf "crop='min(iw,ih)':'min(iw,ih)',scale=512:512" art-sq.jpg
```

Both are also written to the IndexedDB `artwork` store as small blobs, because
list rendering must never touch OPFS or the network. See
`02-offline-playback.md` FM-7.

---

## Video

When `keep_video` is on, **mux the original video stream** with the transcoded
audio. Never re-encode video unless `video_max_height` actually forces a
downscale:

```
ffmpeg -i video.mp4 -i audio.m4a -c:v copy -c:a copy \
       -movflags +faststart out.mp4
```

Video re-encoding is the difference between a 20-second job and a six-minute
one, and it is almost never what the user meant by "also save the video".

When a downscale genuinely is required:

```
ffmpeg -i video.mp4 -i audio.m4a \
       -vf "scale=-2:{video_max_height}" -c:v libx264 -crf 23 -preset veryfast \
       -c:a copy -movflags +faststart out.mp4
```

Warn in the UI when this path is taken and show the expected duration. A silent
six-minute job reads as a hang.

---

## Output naming

The pipeline always produces the same set inside the job's scratch directory,
and the client mirrors it into OPFS unchanged:

```
audio.m4a   (or audio.mp3)
video.mp4   (only when keep_video)
art.jpg     (original aspect)
art-sq.jpg  (512×512 centre crop)
```

Fixed names mean the client never has to parse or guess, and the OPFS layout in
`03-data-model.md` stays trivially predictable.
