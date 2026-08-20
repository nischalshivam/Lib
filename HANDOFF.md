# HANDOFF — Movie Essay Auto-Editor (媒体 media_index)

> Ye file ek naye chat me paste karne ke liye hai. Isme project ka goal,
> architecture, **asli root cause jo abhi tak accuracy kharab kar raha tha**,
> jo **foundation fix abhi lagaya gaya**, aur **aage exactly kya karna hai** —
> sab detail me likha hai. Naya Claude ise padhkar bina context khoye kaam
> continue kar sakta hai.

---

## 0. Ek line me

Main faceless YouTube movie/series video-essays banata hoon. Ye tool ek
**local automation** hai: narration script + clue script + voiceover audio +
local movie/episode files leta hai, aur **automatically** har narration line
ke neeche sahi clip/still lagata hai (sahi character, sahi moment). **Sabse
badi demand: ACCURACY.**

Reference: mere ek dost ka "Westeros Autopilot" (Game of Thrones) system aur
ek course (TECH-SCRAPE BRAIN / Emerge X) — dono Claude se poori pipeline
(library + video) local PC pe banate hain. Wahi quality achieve karni hai.

---

## 1. Architecture — proven 3-stage pipeline

Sab kuch `shared/media_index/` Python package me hai. Koi scraping / YouTube /
upload nahi — sirf local files jo mere paas already hain.

| Stage | File | Kaam |
|------|------|------|
| **1. Catalog** | `catalog.py` | Poori series ko **ek baar** index karo → har shot ka Gemini-written description + tags + **characters** + quality + safe-flag + subtitle dialogue. Output: har episode ke paas `*.catalog.json`. |
| **2. Plan / Retrieval** | `plan.py` | Clue/genspark script ke har shot ko catalog se match karo. Ladder: **dialogue anchor** (exact subtitle line = best) → **description + character** match → **NEEDS VISUAL** (fail-closed). |
| **3. Assemble / Render** | `assemble.py` + `timeline.py` + `render.py` | Matched clips cut karo, voiceover pe time karo (Whisper word-sync), final mp4 render. Beech me **Gemini verify** (reference photos se) — galat person reject. |

CLI: `python -m media_index <command>`. Double-click flows: `catalog.bat`,
`plan.bat`, `build.bat`.

Config: `settings.txt` (gitignored) me Gemini key — **kabhi code me nahi**.
Endpoint OpenAI-compatible (`/v1/chat/completions`), model `gemini-2.5-flash`.

---

## 2. ROOT CAUSE — asli problem (bina sugarcoat)

Kai builds me result inaccurate aaya: narration Victor ki baat kar rahi thi,
screen pe Hank/Skyler ka face aa raha tha. 178 shots verify se reject, 39
gaps, video 6:24 vs audio 11:09 (bahut chhoti). Reference photos **verify-time
pe** lagane se bhi "kuch farak nahi pada."

**Kyun? Ye structural problem thi, chhota bug nahi:**

1. **Catalog banate waqt Gemini ko pata hi nahi tha "Victor" kaun hai.**
   Purana `tag_messages` prompt bolta tha: *"jise pehchano nahi usko `unknown`
   likho, guess mat karo"* — **aur koi reference photo nahi diya jaata tha.**
   Nateeja: sirf famous face (Walter White) label hote the, baaki saare
   action/silent shots ka `characters` field **khaali (`[]`)** reh jaata tha.

2. **Retrieval character se filter karta hai** (`catalog.search`):
   - shot me sahi naam → **+5**
   - shot me galat naam → **−1**
   - shot khaali `[]` → **+0**

   Matlab Victor ke asli shots (characters khaali) ko koi bonus nahi milta —
   wo sirf word-overlap pe compete karte the. **Isliye "jis character ki baat
   ho rahi hai, uski clip hi nahi nikal pa rahi thi"** — kyunki library me wo
   shot Victor ke naam se tagged tha hi nahi.

3. **Verify (reference photos) sirf REJECT kar sakta hai, surface nahi.**
   Wo galat-person shots ko sahi reject karta tha (178 rejections), par
   candidate pool me sahi shot tha hi nahi → gap. **Verify se accuracy nahi
   aa sakti agar candidate pool hi galat ho.**

**Ek line me: identity galat stage pe decide ho rahi thi (verify pe, jahan wo
sirf reject karta hai), library me identity reliable thi hi nahi.**

---

## 3. FOUNDATION FIX (abhi lagaya gaya — is branch me)

**Reference photos ko verify-time se hata kar CATALOG-time pe laaya.** Ab jab
library banti hai, Gemini ko har shot ke saath cast ki reference photos
dikhti hain (naam ke saath). To "ye kaun hai?" (guess, jo mana tha) → "in me
se konsa banda hai, agar koi?" (comparison, jo wo kar sakta hai) ban jaata
hai. **Library ke character labels ab source pe hi reliable ban jaate hain** —
ye wahi ek jagah hai jo har future video ke liye retrieval theek kar deti hai.

Jo koi visible banda kisi reference se match nahi karta, wo abhi bhi
`unknown` — honest-blank rule bana hua hai, bas ab wo main cast ko nahi
nigalta.

**Code changes (branch `claude/video-clip-relevance-issue-khs33k`):**

- `catalog.py::tag_messages(..., refs=None)` — refs ho to reference photos
  pehle dikhata hai + identity rule "match against references, else unknown".
- `catalog.py::build_catalog(..., refs=None)` — har shot ke prompt me refs
  thread karta hai.
- `catalog.py::run(..., cast_dir="", refs=None)` — cast folder load karta hai
  (`assemble.load_refs`), log karta hai kitne characters mile.
- `catalog.py::run_folder(..., cast_dir="")` — poori series ke liye cast
  photos **ek hi baar** load karke reuse.
- `cli.py cmd_catalog` — naya `--cast <folder>` flag.
- `catalog.bat` — cast folder ke liye prompt.
- Tests: `tests/test_catalog.py::TestReferenceIdentity` (4 naye test, pass).

**Verify-time reference photos** (jo pehle se `assemble.py` +
`gemini.confirm_shot` me hain) hataye NAHI — wo ab **second line of defense**
hai. Asli kaam catalog-time identity karti hai; verify sirf border-case galti
pakadta hai.

---

## 4. AB EXACTLY KYA KARNA HAI (next steps, priority order)

### Step 1 — Library ko cast photos ke saath DOBARA banao (ye zaroori hai)

Purani Breaking Bad library (S03/S04) **bina reference photos** ke bani thi —
uske character tags bharosemand nahi. Ise cast folder ke saath re-catalog
karna padega. **Ye Gemini calls kharch karti hai (paisa)** — par ye ek-baar ka
foundation kaam hai, baar-baar nahi.

Cast folder structure (mixed extensions chalti hain — jpg/png/webp):
```
Breaking Bad Cast\
  Victor\    1.jpg 2.png 3.webp ...   (5-8 clear face photos)
  Hank\      1.jpg ...
  Gus\       ...
  Walter White\ ...
  ...
```

Command (ek episode pe pehle test — sasta):
```
python -m media_index catalog "E:\Movies\Breaking Bad\S04E01.mkv" ^
    --cast "C:\Users\Dell\Desktop\Characters\Breaking Bad Cast" --minutes 15
```
Ya double-click `catalog.bat` → cast folder wale prompt me path daalo.

Poori series:
```
python -m media_index catalog "E:\Movies\Breaking Bad" ^
    --cast "C:\Users\Dell\Desktop\Characters\Breaking Bad Cast"
```

**Verify karo fix kaam kar rahi hai (paisa lagane se pehle):** 15-min test ke
baad us episode ki `catalog.json` kholo — dekho `characters` fields me ab
`"Victor"`, `"Hank"` etc. bhare hue hain ya nahi (pehle zyada `[]` the). Agar
bhar rahe hain → fix kaam kar rahi hai, poori library banao. **Agar abhi bhi
khaali ya galat → mujhe (naye chat me) us catalog.json ka sample bhejo, hum
prompt tune karenge. Sugarcoat mat hone dena — actual JSON dekho.**

### Step 2 — Clue script (already sahi disha me)

`PROMPT_CLUE_SCRIPT.md` pehle se dialogue-first hai — yani deterministic path
(dialogue anchor) pe bana, jo measured 352/354 shots place karta hai. Isme
`characters_on_screen` bhi hai jo reference photos se match hota hai. **Ye
already library se connected hai.** Jab tak Step 1 ki library reliable nahi
banti, clue script ki `characters_on_screen` bhi theek match nahi karegi — to
Step 1 pehle.

Clue script Claude se banwao (Genspark se nahi), phir wahi Genspark ko do
taaki wo asli quotes use kare. Visual script **halves me** maango (beats 1-30,
phir 31-60) — warna budget khatam ho jaata hai.

### Step 3 — Video banao aur naap-tol karo
```
python -m media_index makevideo "genspark.json" "E:\Movies\Breaking Bad" ^
    "voiceover.mp3" --narration "clean.txt" ^
    --cast "C:\Users\Dell\Desktop\Characters\Breaking Bad Cast" --scope S04E01
```
Dekho: `NNN shots cut · NNN rejected · NNN gaps`. Ab **rejections aur gaps
kaafi kam** hone chahiye kyunki candidate pool me sahi character ke shots
surface honge. Agar abhi bhi bahut gaps → `manifest.json` + `timeline.json`
share karo, hum dekhenge kaun se beats fail ho rahe.

---

## 5. Full PC access ke baare me — honest baat

Main jab **cloud (claude.ai/code web)** pe chal raha hoon, tab main **tere PC
ke files (E:\Movies, cast photos) tak nahi pahunch sakta** — isliye main
end-to-end (asli movie + photos ke saath) test nahi kar sakta. Ye ek honest
technical limit hai, sugarcoat nahi.

Tera dost / course wale **local Claude Code (desktop app / terminal)** chalate
hain jo unke apne PC pe full filesystem access ke saath chalta hai. Ye setup
alag hai. **Agar tujhe chahiye ki main khud teri movies pe catalog banau aur
accuracy tab tak test karu jab tak perfect na ho — to:**

1. Apne PC pe **Claude Code** install kar (terminal ya desktop app —
   `claude.ai/download` ya `npm i -g @anthropic-ai/claude-code`).
2. Us folder me kholo jaha `shared/` repo hai.
3. Ye `HANDOFF.md` paste kar / reference de.
4. Wahan mujhe `E:\Movies`, cast folder, sab dikhega — tab main khud catalog
   bana ke, `catalog.json` padh ke, character tags verify kar ke, jab tak
   accurate na ho iterate kar sakta hoon.

Cloud session me main sirf **code likh/fix kar sakta hoon aur unit-test kar
sakta hoon** (jo maine kiya) — asli footage pe validation local session me
hi ho sakti hai.

---

## 6. Codebase map (jaldi reference)

```
shared/
  media_index/
    catalog.py     Stage 1: video → tagged shot library. **[FIX yaha]**
    plan.py        Stage 2: script → matched shots (dialogue anchor ladder)
    assemble.py    Stage 3: matched shots → cut clips + manifest (+verify)
    gemini.py      Gemini calls: describe, confirm_shot (verify), config
    timeline.py    manifest + voiceover → timed timeline
    render.py      timeline → final mp4 (ffmpeg)
    narration.py   Whisper word-sync (align_audio)
    subtitles.py   .srt find + parse (bracket-folder glob.escape fix)
    naming.py      episode/title parsing, walk_media
    cli.py         all commands (catalog/plan/makevideo/gemini/...)
    ...
  catalog.bat / plan.bat / build.bat   double-click flows
  settings.txt   (gitignored) Gemini key — NEVER commit
  PROMPT_CLUE_SCRIPT.md    clue script prompt (dialogue-first) — Claude ko do
  PROMPT_VISUAL_SCRIPT.md  genspark/visual script prompt
  tests/         unittest (no network/ffmpeg needed for core)
```

Retrieval ladder (plan.py::match), precision-first:
1. **dialogue anchor** — request ka quote kisi shot ke subtitle me → exact
   timestamp. Deterministic. **Ye WORK karta hai.**
2. **description + character** — quote nahi ho to visual sentence + character
   filter. **Ye Step-1 fix ke bina fragile tha; ab reliable character tags se
   strong.**
3. **NEEDS VISUAL** — kuch nahi mila → honest gap, galat guess nahi.

---

## 7. Constraints (naye Claude ke liye — zaroor follow karo)

- **Gemini key sirf `settings.txt` (gitignored) ya env var me** — kabhi code
  me hardcode/commit nahi. Ek test assert karta hai `gemini.py` me koi
  `sk-` literal na ho.
- Kaam sirf branch **`claude/video-clip-relevance-issue-khs33k`** pe.
- Commit/PR/code me model identifier mat likho.
- Commit footer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` +
  Claude-Session line.
- User se **Hinglish** me baat karo.
- **Sugarcoat mat karo.** "Ho gaya fix" tabhi bolo jab actual data
  (catalog.json ke character fields, manifest ke cut/gap counts) se verify
  ho jaye. Chhote-chhote fix baar-baar mat karo — root pe jao.

---

## 8. Abhi ki state (is handoff ke time)

- Foundation fix (catalog-time reference identity) **code me lag gaya,
  unit-tested (49+ tests pass), commit + push ho gaya** is branch pe.
- **Pending (user ke PC pe):** cast photos ke saath library re-catalog (Step
  1), phir video re-build (Step 3), aur actual accuracy verify.
- Cloud session se aage ki asli-footage validation nahi ho sakti — local
  Claude Code session me continue karo (Section 5).
