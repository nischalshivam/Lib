# Design brief — Movie Editor

Ye file **Claude Design** ko dene ke liye hai.

Neeche `>>>` se shuru hone wale blocks wo hai jo tum seedha copy-paste karoge.
Baaki text tumhare liye hai — usko paste karne ki zaroorat nahi.

---

## Kaise use karna hai

1. Claude Design kholo, naya project banao — naam: **Movie Editor**
2. **Message 1** paste karo (neeche wala) → design aayega
3. Dekho, badlav bolo ("sidebar patla karo", "badge ka colour halka karo")
4. Jab pehli screen theek lage → **Message 2** paste karo → agli screen
5. Phir **Message 3**
6. Ho jaaye to mujhe do (tarika sabse neeche)

Ek message me ek screen. Teeno ek saath maangoge to teeno average aayenge.

---

## Message 1 — foundation + Library screen

>>>
Main ek desktop app ka UI design karwa raha hu jiska naam **Movie Editor** hai.
Ye ek video-editing tool hai jo YouTube video essays banata hai: user ek script
aur voiceover deta hai, tool uske paas already downloaded movies/series me se
sahi-sahi scenes dhoondh ke video bana deta hai.

## Technical constraints — ye tod nahi sakte

- **Ek single self-contained HTML file.** CSS aur JS usi file ke andar.
- **Koi external resource nahi** — no CDN, no Google Fonts, no external images,
  no icon library. Icons inline SVG me banao. Fonts system fonts
  (`-apple-system, "Segoe UI", Roboto, sans-serif`) aur monospace ke liye
  `"Cascadia Code", Consolas, monospace`.
- **Koi build step nahi** — plain HTML + CSS + vanilla JS. React/Tailwind/Vite
  nahi. Ye app ek offline Windows machine pe chalti hai jahan node nahi hai.
- **Dark theme** primary. Ek professional editing tool jaisa —
  DaVinci Resolve / Premiere / Linear jaisa gehra, shaant, kam colour.
- Screen 1280px se 1920px tak theek dikhna chahiye.
- Interactive dikhao (hover, click, selected state) — par logic mat likho,
  dummy data hardcode kar do.

## App ka shape

Left me ek permanent sidebar hai. Uske andar do hisse:

**MOVIE EDITOR** (abhi active)
- New Video
- Queue
- Editor
- Library
- Settings

**Coming soon** (dabe hue, disabled, par dikhne chahiye)
- Documentary
- Cartoon Essay
- Anime

Sidebar ke neeche ek chhoti status line: `library: E:\Libraries · 4 titles`

## Ab is message me sirf ek screen banao: **Library**

Library wo jagah hai jahan user apni movies/series ek baar "index" karta hai,
taaki tool unke andar se scene dhoondh sake. Ye ek baar ka kaam hai.

Screen pe ye ho:

**Header:** "Library" + ek subtitle "Har title ek baar index hota hai" +
right side pe primary button `+ Add title`

**Titles ki list** — cards ya rows, har ek me:
- Title ka naam (bada)
- Kind badge: `series` ya `movie`
- Episodes ki ginti (movie ke liye chhupa do)
- Size (jaise `271 MB`)
- Status: `ready` (green), `4 without subtitles` (amber),
  `indexing 23/62` (blue, progress bar ke saath), `not indexed` (grey)
- Media folder ka path chhote monospace text me
- Row pe hover karne pe: `Check`, `Rebuild`, `Change folder`, `Remove`

Ye dummy data use karo:
- Breaking Bad · series · 62 episodes · 271 MB · ready ·
  `E:\Media\Breaking Bad`
- Game of Thrones · series · 73 episodes · 318 MB ·
  indexing 23/73 (progress bar) · `E:\Media\Game of Thrones`
- Titanic · movie · 14 MB · ready · `E:\Media\Titanic`
- The Shawshank Redemption · movie · — · 4 without subtitles ·
  `E:\Media\The Shawshank Redemption`

**"+ Add title" ka panel** (right side se slide ho ke aaye, ya modal):
- Title (text)
- Video folder (text + Browse button)
- Library folder — auto-filled, greyed out: `E:\Libraries\<title>`
- Do buttons: `Check` (secondary) aur `Build index` (primary, disabled jab tak
  Check na ho)

**Check ka result** usi panel me dikhao — ek list jisme tick/warning/cross:
```
✓  62 video files mile
✓  58 episodes — subtitles theek
⚠  2 episodes — subtitle image-based hai (.srt chahiye)
✗  2 episodes — subtitle hai hi nahi  ·  S02E07, S05E03
✓  disk pe 340 GB jagah hai
   picture index me lagega: ~4 ghante
```

**Indexing chal rahi ho to** — ek progress area: bada progress bar,
`Episode 23 / 62`, `3h 10m bacha`, aur `Pause` button.

## Foundation bhi banao (baaki screens isi pe banengi)

- Colour tokens: background, surface, border, text, muted text, aur
  4 status colours (ok/green, warn/amber, bad/red, busy/blue)
- Type scale
- Buttons: primary, secondary, ghost, danger — normal/hover/disabled
- Badge component (status ke liye)
- Input, select, checkbox, toggle
- Progress bar
- Empty state (jab ek bhi title na ho)
- Toast / inline error

Sab kuch ek HTML file me. Screen ke neeche ek chhota "components" section bhi
rakh do jisme ye sab elements alag se dikhe.
>>>

---

## Message 2 — New Video screen

Message 1 ka natija theek lagne ke baad ye bhejo.

>>>
Ab **New Video** screen banao — wahi foundation, wahi colours, wahi components.

Ye wo form hai jahan user ek video ke saare inputs deta hai. Design ka lakshya:
**30 second me bhara jaaye**. Zaroori cheezein upar, optional cheezein niche
ek collapsed "Advanced" section me.

**Header:** "New Video" + right side `Check` (secondary) aur
`Build → Editor` (primary) aur `Build → Export` (chhota secondary)

### Section 1 — Source (hamesha khula)
- **Title** — dropdown, library se. Options: Breaking Bad, Game of Thrones,
  Titanic, The Shawshank Redemption. Har option ke saath chhota status dot.
  Selected: `Breaking Bad · 62 episodes · ready`
- **Script** — file picker (`.json`). File chunne ke baad **turant** ek chhoti
  summary strip dikhe:
  `55 scenes · 132 shots · episodes: S04E01, S03E13, S04E08, S04E10`
  Ye sabse important detail hai — user ko turant dikhna chahiye ki tool ne
  script samajh li.
- **Voiceover** — file picker (`.m4a` / `.mp3` / `.wav`).
  Chunne ke baad: `11m 09s` aur ek chhota waveform preview
- **Video title** — text input
- **Output folder** — text + Browse

### Section 2 — Look (khula, par chhota)
- **Style preset** — 4 bade clickable cards, ek chunna hai:
  `Auto` (recommended) · `Cinematic` · `Tense` · `Documentary`
  Har card me 2-3 shabd ka description
- **Quality** — segmented control: `1080p` (default) · `4K (upscaled)`
  4K ke neeche chhota amber note: "source 1080p hai — file 4x badi hogi"
- **Captions** — toggle, default off

### Section 3 — Advanced (collapsed, default band)
Khulne pe:
- **Transitions** — `Auto` / `Manual` radio. Manual pe ek dropdown khule
  jisme 20 options ho (Cut, Dissolve, Fade to black, Wipe left, Slide up,
  Zoom in, Whip pan, Glitch, Film burn, ... — 20 dikha do)
- **Filters** — `None` / `Auto` / `Manual`. Manual pe dropdown:
  Contrast+, Warm, Cool, Desaturate, Vignette, Film grain, Letterbox,
  Faded, Teal-orange, High contrast BW, ... (20)
- **Animation** — `Auto` / `Manual`. Manual pe: Ken Burns in, Ken Burns out,
  Pan left, Pan right, Pan up, Pan down, Slow push, Static, ... (20)
- **Pace** — `Calm` / `Normal` / `Quick` / `Rapid` segmented
- **Clip length** — number input, default 4.0 seconds

### Section 4 — Queue
Sabse neeche ek ghost button: `+ Add another video`
Dabane pe upar wala poora form dobara, collapsed card ki tarah, upar
"Video 2" likha hua. Design me 2 videos ki queue dikha do — ek khuli,
ek band (band waali me sirf title + status).

### Check ka result panel
`Check` dabane pe right side me ek panel:
```
GAPS   ·  83% shots resolve honge

✓  script padh li — 55 scenes, 132 shots
✓  Breaking Bad — saare episodes indexed
✓  voiceover mil gaya — 11m 09s
✓  disk pe jagah hai
⚠  8 scenes me koi quoted line nahi — footage anumaan hoga
      scene 12, 19, 24, 31, 38, 44, 49, 52
```
Verdict badge ke teen roop dikhao: `OK` (green), `GAPS` (amber),
`BLOCKED` (red).
>>>

---

## Message 3 — Editor screen

>>>
Ab **Editor** screen banao — sabse important screen. Wahi foundation.

Ye wo jagah hai jahan build hone ke baad user video theek karta hai.
Reference: Premiere / DaVinci ka timeline, par bahut simple.

### Layout — teen hisse

**Upar: preview**
- Beech me bada video player (16:9), transport controls
- Uske left me chhota sa info: video ka naam, kitni der ki hai,
  kitne shots hai
- Right me: `Export` primary button

**Beech: timeline** (poori chaudai, horizontal scroll)
Chaar track, upar se neeche:
1. **SHOTS** — sabse mota track. Har shot ek block hai jisme uska thumbnail
   dikhta hai. Block ki chaudai uski duration ke hisaab se. Har block ke
   upar-left ek chhota badge:
   - `anchor` (green) — dialogue se exact mila
   - `verified` (blue) — picture se confirm hua
   - `interpolated` (amber) — beech me anumaan
   - `filler` (grey) — sahi episode, par koi khaas moment nahi
2. **VOICE** — waveform
3. **TEXT** — captions ke blocks (khaali ho to "no captions")
4. **MUSIC** — khaali track, "+ add music" ghost

Playhead (patli lines) poore timeline ke aar-paar. Upar time ruler.
Right-bottom me zoom slider.
Ek block **selected** state me dikhao (bright border).

**Right panel: selected shot**
- Bada preview thumbnail
- `Scene 12` heading + us scene ki narration ka text (2-3 lines, italic)
- Source: `Breaking Bad S04E01` + timestamp `37:12`
- Badge: `interpolated`
- Duration: number input + chhota slider
- Transition dropdown, Filter dropdown, Animation dropdown
- Buttons, do rows me:
  `Find another` (primary) · `Use previous`
  `Split` · `Duplicate` · `Delete`

**"Find another" ka modal** — ye alag se design karo:
- Heading: "Scene 12 ke liye dusra shot"
- Us scene ki narration upar
- 10 thumbnails ka grid (2 rows × 5)
- Har thumbnail pe: timestamp, aur ek match score (jaise `0.41`),
  aur ek chhota bar jo score dikhaye
- Pehla wala "current" mark ho
- Hover pe border, click pe select
- Neeche: `Use this shot` primary + `Cancel`

**Export ka modal**
- Output folder (text + Browse)
- Quality segmented (1080p / 4K)
- Estimated: `~2.1 GB · ~18 minutes`
- `Export` primary

### Ye states bhi dikhao
- Ek scene jisme koi shot nahi — timeline me ek khaali dashed block
  jisme likha ho "nothing here" aur ek `+ add shot` button
- Loading state — timeline ki jagah shimmer
- Empty state — koi build hi nahi hua, "Build a video first" + link
>>>

---

## Design ke baad — future me edit ho jaayega?

Haan. Do wajah:

1. **Claude Design me project rehta hai.** Wapas jaake "sidebar me ek naya
   item jodo" bol sakte ho, wo usi design ko update karega.
2. **Meri taraf se bhi.** Jab main isko app me jod dunga, uske baad chhote
   badlav (colour, spacing, ek naya button, ek naya field) main yahin
   Claude Code me kar dunga — dobara Claude Design me jaane ki zaroorat nahi.

Bada redesign hi Claude Design pe wapas le jaana. Rozmarra ke badlav yahin.

---

## Mujhe kaise doge — teen tarike

### Tarika 1 — seedha (agar chal jaaye)
Agar tum Claude Design me isko ek **design-system project** ki tarah banate ho,
to main yahin se us project ko **padh sakta hu** — tumhe kuch bhejna hi nahi
padega. Bas mujhe project ka naam bata dena, main check kar lunga ki dikh
raha hai ya nahi.

### Tarika 2 — file (sabse pakka)
Claude Design se HTML file download karo, aur
`shared\design\` folder me daal do. Phir mujhe bol do:
"design file `shared/design/library.html` me padi hai".

### Tarika 3 — paste
Design ka code copy karke seedha chat me paste kar do. Lamba hoga par
kaam karega. Ek screen ek message me.

**Mera sujhaav: Tarika 2.** File rehti hai, git me commit ho jaati hai, aur
baad me compare bhi kar sakte hai ki kya badla.

---

## Ek chetavni

Agar Claude Design React + Tailwind ke saath design de de (wo aksar deta hai),
to **wo tumhari machine pe nahi chalega** — kyunki wahan node/npm nahi hai.

Isliye Message 1 me constraints wala hissa **hataana mat**. Aur agar output me
`import React` ya `className=` dikhe, to wapas bolo:

> "Ye plain HTML + CSS + vanilla JS me chahiye, ek single file me.
> React aur Tailwind nahi."

---
---

# Round 2 — jo pehle round me reh gaya

Pehla round achha aaya: sidebar, foundation/components, Library ki list,
New Video ka poora form, aur Editor ka timeline — sab ban gaya.

Paanch cheezein reh gayi. Neeche har ek ka alag message hai. **Ek message me
ek cheez** bhejna, aur har baar shuru me ye line zaroor likhna:

> Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
> plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

---

## Message 4 — "Find another" modal (sabse zaroori)

>>>
Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

Editor screen ke liye **"Find another" modal** banao. Ye poore tool ka sabse
zyada istemaal hone wala screen hai — jab tool galat shot chun leta hai, user
yahin se sahi wala chunta hai.

Modal (center, backdrop ke saath, ~900px chaura):

- Heading: `Scene 12 ke liye dusra shot`
- Neeche us scene ki narration italic me:
  *"By the fourth season, the mask is gone. What's left isn't a man protecting
  his family — it's a man protecting his empire."*
- Ek line: `Breaking Bad S04E01 me se — 10 sabse milte-julte frames`

- **Grid: 2 rows × 5 thumbnails.** Har thumbnail 16:9. Har ek ke neeche:
  - timestamp monospace me (`37:12`, `41:02`, `22:18`, ...)
  - match score (`0.41`) aur uske saath ek patla horizontal bar jo score
    dikhaye (sabse upar wale ka bar sabse lamba)
  - pehle wale pe ek chhota `current` badge
- Hover pe border highlight, click pe selected state (accent border + tick)
- Ek thumbnail selected dikha do

- Neeche left me ek chhoti line: `score jitna zyada, match utna pakka`
- Neeche right me: `Cancel` (ghost) + `Use this shot` (primary)

Ek doosra variant bhi banao: **koi alternative nahi mila** —
grid ki jagah empty state: "Is episode me aur koi milta-julta frame nahi
mila" + `Cancel`.
>>>

---

## Message 5 — Library ka "+ Add title" aur "Check" panel

>>>
Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

Library screen ka **"+ Add title" panel** banao. Ye right side se slide hoke
aane wala panel hai (~480px chaura), backdrop ke saath.

**Panel ke teen roop banao, teeno dikhao:**

### Roop 1 — khaali form
- Heading `Add title` + close (×)
- **Title** — text input, placeholder `The Shawshank Redemption`
- **Video folder** — text input + `Browse` button,
  placeholder `E:\Media\The Shawshank Redemption`
- **Library folder** — disabled input, auto-filled:
  `E:\Libraries\The Shawshank Redemption`, neeche chhoti muted line
  "apne aap ban jaayega"
- Neeche: `Check` (secondary) + `Build index` (primary, **disabled**)
- Disabled button ke neeche muted line: "pehle Check chalao"

### Roop 2 — Check ka result
Wahi form upar, neeche ek result card jisme icon + text ki list:
```
✓  62 video files mile
✓  58 episodes — subtitles theek
⚠  2 episodes — subtitle image-based hai (.srt chahiye)
      S03E09, S04E02
✗  2 episodes — subtitle hai hi nahi
      S02E07, S05E03
✓  disk pe 340 GB jagah hai
```
Aur neeche ek muted estimate line:
`picture index banne me lagega ~4 ghante`
Ab `Build index` **enabled** hai.

### Roop 3 — indexing chal rahi hai
Form chhup jaata hai. Uski jagah:
- Bada progress bar
- `Episode 23 / 62`
- monospace line: `picture index · S03E10`
- `3h 10m bacha`
- `Pause` button (secondary) aur `Cancel` (ghost, danger colour me text)
- Neeche muted line: "band ho jaaye to koi baat nahi — dobara chalane pe
  yahin se shuru hoga"
>>>

---

## Message 6 — New Video ka "Check" result panel

>>>
Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

New Video screen pe jab user `Check` dabata hai, to right side se ek panel
aata hai (~440px). Wo banao — **teen roop, teeno dikhao:**

Har roop me sabse upar ek bada verdict badge aur ek line:

### Roop 1 — OK (green)
```
OK  ·  94% shots resolve honge
```
```
✓  script padh li — 55 scenes, 132 shots
✓  Breaking Bad — saare episodes indexed
✓  voiceover mil gaya — 11m 09s
✓  disk pe jagah hai — 12 GB chahiye, 340 GB hai
✓  ffmpeg mil gaya
```
Neeche: `Build → Editor` primary button

### Roop 2 — GAPS (amber)
```
GAPS  ·  83% shots resolve honge
```
Wahi list, par ek amber row extra:
```
⚠  8 scenes me koi quoted line nahi — footage anumaan hoga
      scene 12, 19, 24, 31, 38, 44, 49, 52
```
Wo scene numbers **clickable chips** ki tarah dikhne chahiye.
Neeche: `Build anyway → Editor` primary + `Script theek karo` ghost

### Roop 3 — BLOCKED (red)
```
BLOCKED  ·  build shuru nahi hoga
```
```
✓  script padh li — 55 scenes, 132 shots
✗  S05E14 library me nahi hai
      Library me jaake Breaking Bad rebuild karo
✗  voiceover file nahi mili
      E:\Voice\vo-final.m4a
✓  disk pe jagah hai
```
Neeche: `Build` button **disabled**, aur ek `Library kholo` link button
>>>

---

## Message 7 — Queue aur Settings screens

>>>
Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

Do screen banao.

### Screen 1 — Queue
Yahan saari videos ki list hai jo ban rahi hai ya ban chuki hai.

Header: `Queue` + subtitle "Jo ban rahi hai aur jo ban chuki hai" +
right me `+ New Video` button aur ek `Pause all` ghost button.

Rows, har ek me:
- Video ka naam (bada) + neeche muted: `Breaking Bad · 55 scenes · 11m 09s`
- Status badge
- Right me action buttons

Ye paanch rows dikhao, paanch alag status ke saath:

1. `Why Walter White Broke Bad` — **done** (green) — `12m 04s me bani` —
   buttons: `Move to editor` (primary) + `Folder kholo` (ghost)
2. `Gus Fring — The Quiet Villain` — **building** (blue) — progress bar +
   `scene 31 / 38` + `6m bacha` — button: `Pause`
3. `The Cousins` — **queued** (grey) — `#3 line me` — buttons: `Upar karo`,
   `Hatao`
4. `Jesse's Last Ride` — **needs attention** (amber) —
   `GAPS · 78% shots` — button: `Check dekho`
5. `Mike's Rules` — **failed** (red) — `voiceover file nahi mili` —
   buttons: `Theek karo`, `Dobara chalao`

Neeche ek summary strip: `2 done · 1 building · 1 queued · 1 failed`

### Screen 2 — Settings
Simple, ek column, sections me:

**Folders**
- Libraries folder — `E:\Libraries` + Browse
- Default output folder — `E:\Exports` + Browse
- Scratch / temp folder — `E:\Temp` + Browse

**Defaults** (nayi video pe ye pehle se bhare aayenge)
- Quality — 1080p / 4K segmented
- Pace — Calm / Normal / Quick / Rapid segmented
- Style preset — dropdown
- Captions — toggle

**Advanced**
- ffmpeg path — text input, uske aage green tick + `mil gaya`
- Model — disabled dropdown `siglip-base-patch16-224`
- Frames per second (indexing) — number, `0.5`, neeche muted warning
  "isse badlane pe saari libraries dobara banani padengi"

**Danger zone** — ek red-border card:
- `Saare temp files hatao` (danger ghost button)
- `Settings reset karo` (danger ghost button)
>>>

---

## Message 8 — Export modal + timeline ki khaali jagah

>>>
Wahi Movie Editor design, wahi components, wahi colours, wahi single-file
plain HTML + CSS + vanilla JS wale constraints. React/Tailwind nahi.

Do chhoti cheezein.

### 1. Export modal
Editor ke `Export` button se khulta hai. Center modal, ~520px:
- Heading `Export`
- Output folder — text + Browse, `E:\Exports\walter-essay`
- File name — text, `why-walter-white-broke-bad.mp4`
- Quality — segmented `1080p` / `4K (upscaled)`
- Ek muted estimate strip: `~2.1 GB  ·  ~18 minute lagenge`
- Neeche: `Cancel` ghost + `Export` primary

Ek **doosra roop** bhi banao — export chal raha hai:
- Progress bar, `segment 84 / 132`, `11m bacha`
- `Cancel` button
- Neeche muted: "isko band karke doosra kaam kar sakte ho"

### 2. Timeline me khaali scene
Editor ke SHOTS track me ek aisa block dikhao jisme kuch hai hi nahi:
- Dashed border, halka background
- Andar chhote text me `nothing here`
- Hover pe ek chhota `+ add shot` button
Uske aas-paas normal bhare hue blocks bhi rahein taaki farak dikhe.
>>>

---

## Dark theme

Design me Light/Dark toggle bana hua hai. **Dark pe switch karke ek baar
saari screens dekh lena.** Agar kahin text padhne me dikkat ho ya koi cheez
gayab lage, to ek message bhej dena:

>>>
Dark theme me <screen ka naam> theek nahi dikh raha — <kya dikkat hai>.
Sirf dark theme ke colours theek karo, layout mat badalna.
>>>
