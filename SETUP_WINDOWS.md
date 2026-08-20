# Setting this up on Windows

The error `No module named media_index` means exactly one thing: the code is
on GitHub, not on this PC yet. Nothing is broken. These four steps fix it.

---

## Step 1 — get the code onto the PC

Open this link and the download starts:

```
https://github.com/nischalshivam/Claude/archive/refs/heads/claude/video-clip-relevance-issue-khs33k.zip
```

Then:

1. Right-click the downloaded `.zip` → **Extract All…**
2. Extract it somewhere easy, e.g. `D:\VideoTool`
3. Inside you will find a folder ending in `…-khs33k`, and inside **that** a
   folder called **`shared`**

**`shared` is the folder you work in.** Everything below happens there.

> Prefer git? `git clone -b claude/video-clip-relevance-issue-khs33k https://github.com/nischalshivam/Claude.git`
> — then updates are one `git pull` instead of a fresh download.

---

## Step 2 — run `setup.bat`

Open the `shared` folder and **double-click `setup.bat`**.

It checks for Python, installs an optional speed-up, checks for ffmpeg, and
runs the test suite. It tells you what is missing and how to get it.

### If it says Python was not found

Install from <https://www.python.org/downloads/>.
**Tick "Add python.exe to PATH"** on the first install screen — that box is
the whole difference between it working and not.

### If it says ffmpeg was not found

ffmpeg is required — it is what reads inside video files and cuts clips.
`setup.bat` will offer to install it with winget. If you prefer to do it
yourself, open PowerShell and run:

```
winget install Gyan.FFmpeg
```

Then **close the window, open a new one**, and run `setup.bat` again. A new
window is needed because PATH changes only apply to newly opened windows.

You should end with:

```
[OK] Python 3.x
[OK] rapidfuzz installed
[OK] ffmpeg 7.x
[OK] all tests passed
```

---

## Step 3 — double-click `start.bat`

Everything after setup happens from one menu. No commands to remember, no
Command Prompt needed.

```
 ==========================================================
   media_index
 ==========================================================

   media folder : D:\Breaking Bad Season 2
   index file   : library.db

 ----------------------------------------------------------
   1.  Check a media folder      - is my download usable?
   2.  Make subtitles from audio - when a folder has none
   3.  Build the library index
   4.  Search for a line         - prove it works
   5.  Show what is in the index

   6.  Set the media folder
   7.  Run a job queue (jobs.json)
   0.  Exit
 ----------------------------------------------------------

   Pick a number:
```

Pick **6** once to set your media folder — you can **drag the folder into the
window** instead of typing it. It is remembered from then on.

Then work down the list: **1** to check, **2** if subtitles are missing, **3**
to build, **4** to prove it works.

Option **1** gives a verdict per episode:

```
MEDIA CHECK — 13 file(s)

  ✅ Breaking Bad S02E01   47m  1920x1080  subs: embedded    684
  ⚠️  Breaking Bad S02E04   47m  1920x1080  subs: none
        no subtitles at all
        → download an English .srt named 'Breaking Bad Season 2 Episode 4.en.srt'

  12 ready · 1 need subtitles · 0 unreadable
```

For any `⚠️`, download the subtitle from <https://www.opensubtitles.org>, save
it **next to the video with exactly the filename shown**, and run `check.bat`
again.

---

## Step 4 — build and test

Menu option **3** builds the index, option **4** searches for a line you
remember. If option 4 returns the right episode and timestamp, the whole
system is proven on real footage.

---

## The files in `shared`

| File | What it does |
|---|---|
| **`start.bat`** | **the menu — start here every time** |
| `setup.bat` | one-time setup and health check |
| `check.bat` | inspect a folder by dragging it onto the file |
| `mi.bat` | type a command directly, if you prefer that |

Double-clicking `mi.bat` shows its usage rather than doing anything — it is
built to take arguments. `start.bat` is the one to double-click.

---

## Two things worth knowing

**Everything is resumable.** Transcribing, indexing and the job queue all skip
work that is already finished, so closing the window and starting again later
costs nothing.

**You never need to type a path.** In the menu, drag the folder into the
window instead — Windows fills in the path, quotes and all.
