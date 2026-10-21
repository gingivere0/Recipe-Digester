"""Video links and uploads for the phone inbox (see recipe-inbox.py).

A video job gathers everything a recipe could be read from and leaves it in the
job folder for Claude:

  description.txt  title, channel, length, description, and chapters (links only)
  recipe-page.txt  a recipe page linked from the description, if one has recipe data
  transcript.txt   the captions (manual first, then automatic), or a local
                   faster-whisper transcript when there are none
  frames/          still frames spread across the video, for on-screen text
  cand-<n>.jpg     dish photo candidates: the linked page's photos, the video
                   thumbnail, and frames, on the usual photos.jpg contact sheet

Tools: yt-dlp (run as `python -m yt_dlp`), ffmpeg (found on PATH, under
tools/bin, or where winget puts it), node for YouTube's player scripts, and
faster-whisper. Sites that need a login read cookies from tools/cookies.txt
(Netscape format, gitignored) or else from Edge, Chrome, or Firefox.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIES = os.path.join(HERE, "cookies.txt")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "x.com", "twitter.com", "instagram.com", "tiktok.com",
               "facebook.com", "fb.watch", "vimeo.com", "threads.net", "pinterest.com", "pin.it")
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".3gp"}
MAX_TRANSCRIBE = 30 * 60  # seconds: longer videos skip local transcription
MAX_FRAMES = 10
WHISPER_MODEL = os.environ.get("RECIPE_WHISPER_MODEL", "small")
# description links that are never the recipe
NOT_RECIPE = re.compile(r"youtube\.com|youtu\.be|instagram\.com|tiktok\.com|facebook\.com|fb\.watch|twitter\.com|"
                        r"//(www\.)?x\.com|amazon\.|amzn\.|patreon\.com|spotify\.com|apple\.com|threads\.net|"
                        r"pinterest\.|discord\.|twitch\.tv|snapchat\.com|linkedin\.com|google\.com/maps", re.I)
AUTH_HINTS = re.compile(r"log ?in|sign ?in|cookies|authenticat|private|not available|rate.limit|"
                        r"empty media response|requested content is not available", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- detecting video --------------------------------------------------------

def is_video_url(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS)


def has_extractor(url):
    """True if yt-dlp has a site-specific (not generic) extractor for the link."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        return False
    return any(ie.suitable(url) and ie.ie_key() != "Generic" for ie in gen_extractor_classes())


def upload_ext(body, content_type, filename=""):
    """Returns the file extension if an upload is a video, else None."""
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = os.path.splitext(filename.lower())[1]
    if body[4:8] == b"ftyp":
        brand = body[8:12]
        if brand.startswith((b"hei", b"mif1", b"hev", b"avif", b"msf1")):
            return None  # still images in the same container
        return ".mov" if brand == b"qt  " else ".m4v" if brand.startswith(b"M4V") else ".mp4"
    if body.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv" if b"matroska" in body[:64] else ".webm"
    if ct.startswith("video/") or ext in VIDEO_EXTS:
        return ext if ext in VIDEO_EXTS else "." + (ct.split("/")[1].replace("quicktime", "mov") or "mp4")
    return None


# ---- tools ------------------------------------------------------------------

def ffmpeg():
    """Path of ffmpeg.exe: tools/bin, PATH, or winget's install folder."""
    local = glob.glob(os.path.join(HERE, "bin", "**", "ffmpeg.exe"), recursive=True)
    if local:
        return local[0]
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet")
    for pattern in (os.path.join(winget, "Links", "ffmpeg.exe"),
                    os.path.join(winget, "Packages", "*FFmpeg*", "*", "bin", "ffmpeg.exe")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    raise RuntimeError("ffmpeg not found: run `winget install Gyan.FFmpeg` or put ffmpeg.exe in tools\\bin")


def python():
    # the server runs under python.exe; a pythonw.exe parent still gets a console-less child
    exe = sys.executable
    alt = os.path.join(os.path.dirname(exe), "python.exe")
    return alt if os.path.exists(alt) else exe


def ytdlp_args(cookies=None):
    # --ignore-config: no yt-dlp.conf (from anywhere) can add options such as --exec
    args = [python(), "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-playlist", "--no-progress",
            "--ffmpeg-location", os.path.dirname(ffmpeg())]
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    if os.path.exists(node):  # YouTube needs a JS runtime to read its player
        args += ["--js-runtimes", f"node:{node}", "--remote-components", "ejs:github"]
    if cookies == "file":
        args += ["--cookies", COOKIES]
    elif cookies:
        args += ["--cookies-from-browser", cookies]
    return args


def run(args, timeout=600):
    # always from tools/, never a download folder: `python -m` imports from the
    # working directory, so a downloaded file must never sit there
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=HERE, creationflags=NO_WINDOW)


def _err(proc):
    lines = [l for l in proc.stderr.splitlines() if "ERROR" in l] or proc.stderr.strip().splitlines()[-2:]
    return " ".join(lines)[-400:] or f"exit {proc.returncode}"


def video_info(url, folder):
    """yt-dlp's JSON for the link. Tries without cookies, then tools/cookies.txt and
    the browsers. Returns (info, cookie setting that worked)."""
    tries = [None] + (["file"] if os.path.exists(COOKIES) else []) + ["edge", "chrome", "firefox"]
    errors, needs_login = [], None
    for cookies in tries:
        proc = run(ytdlp_args(cookies) + ["--dump-single-json", url], timeout=180)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                info = json.loads(proc.stdout)
            except ValueError:
                info = None
            # a post with several entries: take the first video
            if info and info.get("_type") == "playlist" and info.get("entries"):
                info = dict(info["entries"][0], description=info.get("description") or
                            info["entries"][0].get("description"))
            if info:
                return info, cookies
        errors.append(f"{cookies or 'without cookies'}: {_err(proc)}")
        if cookies is None:
            needs_login = AUTH_HINTS.search(_err(proc))
            if not needs_login:
                break  # not a login problem; cookies won't help
    with open(os.path.join(folder, "yt-dlp.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(errors))
    if needs_login:
        site = (urlparse(url).hostname or "this site").removeprefix("www.")
        raise RuntimeError(f"{site} needs a login to get this video: export cookies from a browser signed in to "
                           f"{site} to tools\\cookies.txt (Netscape format, e.g. the \"Get cookies.txt LOCALLY\" "
                           f"extension), or share a screenshot. ({errors[-1][:200]})")
    raise RuntimeError("could not read the video: " + errors[0][:300])


# ---- text sources -----------------------------------------------------------

def _clock(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


def write_description(info, url, folder):
    lines = [f"Title: {info.get('title') or ''}",
             f"Channel: {info.get('uploader') or info.get('channel') or info.get('uploader_id') or ''}",
             f"Length: {_clock(info.get('duration'))}", f"Link: {url}", "", "DESCRIPTION:",
             (info.get("description") or "(none)").strip()]
    if info.get("chapters"):
        lines += ["", "CHAPTERS:"] + [f"{_clock(c.get('start_time'))} {c.get('title')}" for c in info["chapters"]]
    with open(os.path.join(folder, "description.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def description_links(info):
    text = (info.get("description") or "") + "\n" + "\n".join(
        c.get("text") or "" for c in info.get("comments") or [] if c.get("author_is_uploader"))
    out = []
    for u in re.findall(r"https?://[^\s<>\"')\]]+", text):
        u = u.rstrip(".,;:!")
        if not NOT_RECIPE.search(u) and u not in out:
            out.append(u)
    return out[:5]


def linked_recipe(info, folder, inbox):
    """Downloads the description's links; the first with schema.org Recipe data is
    saved as recipe-page.txt. Returns (url, page HTML) or (None, None)."""
    log = []
    for u in description_links(info):
        try:
            page = inbox.download(u)
        except Exception as e:  # noqa: BLE001 - a dead link is just skipped
            log.append(f"{u}: {e}")
            continue
        structured = inbox.recipe_json_ld(page)
        log.append(f"{u}: {'recipe data' if structured else 'no recipe data'}")
        if not structured:
            continue
        with open(os.path.join(folder, "recipe-page.txt"), "w", encoding="utf-8") as f:
            f.write(f"LINK: {u}\n\nSTRUCTURED RECIPE DATA FROM THE PAGE (schema.org):\n{structured}\n\n"
                    f"PAGE TEXT:\n{inbox.page_text(page)[:40000]}")
        break
    if log:
        with open(os.path.join(folder, "links.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(log) + "\n")
    return (u, page) if log and log[-1].endswith(": recipe data") else (None, None)


def vtt_text(path):
    """Plain text from a VTT file, without timings, tags, or the repeats of rolling captions."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if (not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                    or line.isdigit()):
                continue
            line = re.sub(r"<[^>]+>", "", line).replace("&nbsp;", " ").replace("&amp;", "&").strip()
            if line and (not out or line != out[-1]) and not (out and out[-1].endswith(line)):
                out.append(line)
    return "\n".join(out)


def _pick_subs(info):
    """(language, manual?) for the best English captions, or (None, None)."""
    for key, manual in (("subtitles", True), ("automatic_captions", False)):
        langs = [l for l in (info.get(key) or {}) if l.split("-")[0] == "en" and l != "live_chat"]
        if not manual:  # automatic: the original track, not a machine translation
            langs = [l for l in langs if l in ("en-orig", "en")]
        for pref in ("en", "en-US", "en-orig", "en-GB"):
            if pref in langs:
                return pref, manual
        if langs:
            return sorted(langs)[0], manual
    # no English: the original-language automatic track
    orig = [l for l in (info.get("automatic_captions") or {}) if l.endswith("-orig")]
    return (orig[0], False) if orig else (None, None)


def captions(url, info, cookies, folder):
    """Saves the captions as transcript.txt. Returns a short note, or None if there are none."""
    lang, manual = _pick_subs(info)
    if not lang:
        return None
    proc = run(ytdlp_args(cookies) + ["--skip-download", "--write-subs" if manual else "--write-auto-subs",
                                      "--sub-langs", lang, "--sub-format", "vtt/best", "--convert-subs", "vtt",
                                      "-P", folder, "-o", "subs.%(ext)s", url], timeout=180)
    files = glob.glob(os.path.join(folder, "subs*.vtt"))
    if not files:
        return None
    text = vtt_text(files[0])
    if len(text) < 40:
        return None
    with open(os.path.join(folder, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return f"{'captions' if manual else 'automatic captions'} ({lang})"


def transcribe(video, folder, duration):
    """Local speech-to-text with faster-whisper, in a child process so a crash
    can't take the server down. Returns a short note, or None."""
    if duration and duration > MAX_TRANSCRIBE:
        return None
    wav = os.path.join(folder, "audio.wav")
    proc = run([ffmpeg(), "-y", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav], timeout=600)
    if proc.returncode or not os.path.exists(wav):
        return None
    out = os.path.join(folder, "transcript.txt")
    proc = run([python(), os.path.abspath(__file__), "--transcribe", wav, out], timeout=1800)
    with open(os.path.join(folder, "whisper.log"), "w", encoding="utf-8") as f:
        f.write(proc.stdout + proc.stderr)
    try:
        os.remove(wav)
    except OSError:
        pass
    if proc.returncode or not os.path.exists(out) or os.path.getsize(out) < 40:
        return None
    return f"speech transcribed on this PC (faster-whisper {WHISPER_MODEL}, may mishear words)"


def _transcribe_main(wav, out):
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(wav, vad_filter=True)
    with open(out, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg.text.strip() + "\n")
    print(f"language {info.language} ({info.language_probability:.2f}), {info.duration:.0f} s")


# ---- pictures ---------------------------------------------------------------

def download_video(url, cookies, folder):
    """A small copy of the video (720p at most). Returns its path or None."""
    proc = run(ytdlp_args(cookies) + ["-f", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b", "-S", "res:720",
                                      "--max-filesize", "600M", "--merge-output-format", "mp4",
                                      "-P", folder, "-o", "video.%(ext)s", url], timeout=900)
    files = [f for f in glob.glob(os.path.join(folder, "video.*")) if not f.endswith((".part", ".ytdl"))]
    if not files:
        with open(os.path.join(folder, "yt-dlp.log"), "a", encoding="utf-8") as f:
            f.write("\nvideo download: " + proc.stderr[-2000:])
    return files[0] if files else None


def probe_duration(video):
    proc = run([ffmpeg(), "-i", video], timeout=60)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0


def grab_frames(video, folder, duration):
    """Saves MAX_FRAMES frames spread across the video as frames/frame-<n>.jpg."""
    out = os.path.join(folder, "frames")
    os.makedirs(out, exist_ok=True)
    names = []
    if not duration:
        return names
    for i in range(MAX_FRAMES):
        t = duration * (i + 0.5) / MAX_FRAMES
        name = f"frame-{i + 1:02d}.jpg"
        run([ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
             "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", os.path.join(out, name)], timeout=120)
        if os.path.exists(os.path.join(out, name)):
            names.append(name)
    return names


def candidates(folder, inbox, page, page_url, thumb_url, frames):
    """Dish photo candidates as cand-<n>.jpg plus the photos.jpg contact sheet:
    the linked recipe page's photos first, then the thumbnail, then frames."""
    Image = inbox._pil()
    if not Image:
        return []
    images = []
    if page:
        for u in inbox.page_image_urls(page, page_url)[:12]:
            data = inbox.download_bytes(u, page_url)
            img = inbox._open_image(data) if data else None
            if img and img.width >= 300 and img.height >= 200:
                images.append(img)
            if len(images) >= 3:
                break
    if thumb_url:
        data = inbox.download_bytes(thumb_url)
        img = inbox._open_image(data) if data else None
        if img:
            images.append(img)
    room = inbox.MAX_CANDIDATES - len(images)
    if frames and room > 0:  # an even spread, favoring the end, where the dish is usually shown
        step = len(frames) / room
        picks = sorted({frames[min(len(frames) - 1, int(len(frames) - 1 - i * step))] for i in range(room)})
        images += [inbox._open_image(os.path.join(folder, "frames", f)) for f in picks]
    names, seen = [], set()
    for img in images:
        if img is None:
            continue
        fp = inbox._fingerprint(img)
        if fp in seen:
            continue
        seen.add(fp)
        names.append(inbox._save_candidate(img, folder, len(names) + 1))
    if names:
        inbox._contact_sheet(folder, names)
    return names


# ---- the whole job ----------------------------------------------------------

def prepare(job, folder, inbox):
    """Gathers the video's text and pictures into the job folder.
    Returns (note for Claude's prompt, dish photo candidate names)."""
    url = job.get("url")
    info, cookies, page, page_url = {}, None, None, None
    found = []
    if url:
        info, cookies = video_info(url, folder)
        write_description(info, url, folder)
        found.append("description.txt (title, channel, description, chapters)")
        page_url, page = linked_recipe(info, folder, inbox)
        if page_url:
            found.append(f"recipe-page.txt (the recipe page linked from the description: {page_url})")
        video = download_video(url, cookies, folder)
    else:
        video = os.path.join(folder, job["source"])
    duration = info.get("duration") or (probe_duration(video) if video else 0)
    job["duration"] = _clock(duration)

    said = captions(url, info, cookies, folder) if url else None
    if not said and video and not page_url:  # a linked recipe page makes the transcript optional
        said = transcribe(video, folder, duration)
        if not said and duration > MAX_TRANSCRIBE:
            job["transcript"] = f"skipped: video is over {MAX_TRANSCRIBE // 60} minutes"
    if said:
        found.append(f"transcript.txt ({said})")
        job["transcript"] = said
    frames = grab_frames(video, folder, duration) if video else []
    if frames:
        found.append(f"frames/frame-01.jpg to frames/{frames[-1]} ({len(frames)} stills spread across the video, "
                     f"in order)")
    elif url:
        job["frames"] = "video download failed; no frames"
    try:
        photos = candidates(folder, inbox, page, page_url, info.get("thumbnail"), frames)
    except Exception as e:  # noqa: BLE001 - the recipe matters more than its photo
        photos, job["photo"] = [], f"failed: {e}"
    if not found:
        raise RuntimeError("nothing to read from the video (no description, captions, speech, or frames)")
    if url:
        where = f"The recipe was shared as a video link: {url}"
    else:
        where = f"The recipe was shared as a video file ({job.get('filename') or job['source']})."
    note = f"""{where}
These files in the current folder hold what was gathered from it. Read all of them:
""" + "".join(f"- {f}\n" for f in found) + """
Look at the frames for on-screen text (ingredient lists, amounts, temperatures,
times) and for what is being done in each step.

Reconstruct the recipe. Sources, most trusted first: the linked recipe page, then the
video description, then the transcript and on-screen text. When the sources disagree,
follow the more trusted one. Only estimate a quantity when the video implies it (for
example "a couple of cloves" or a visible cup), and list each guess in ## Notes as an
estimate. Don't invent ingredients that aren't shown or said. If a time or
temperature isn't given, describe the cue instead (e.g. "until golden").
"""
    if url:
        note += f'The last line of ## Notes must be "Source: {url}"'
        if page_url:
            note += f', with the line "Recipe page: {page_url}" just before it'
        note += ".\n"
    return note, photos


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--transcribe":
        _transcribe_main(sys.argv[2], sys.argv[3])
    else:
        print("usage: python video_intake.py --transcribe <audio.wav> <out.txt>")
