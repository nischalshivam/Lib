Claude Design se aayi files. Ye "design" hai, tool nahi.

  Movie Editor.dc.html            <- asli design (5 screens + saare modals)
  Movie Editor v1 (dark).dc.html  <- pehla dark version
  Movie Editor standalone-src.html
  support.js                      <- Claude Design ka apna runtime (React chahiye)

.dc.html ko seedha browser me khologe to KHAALI dikhega -- ye Claude Design ke
andar hi chalta hai. Tool ise apne tareeke se padhta hai:

  media_index/ui/dcx.js      {{ }}, sc-if, sc-for, onClick, style-hover
  media_index/ui/screens.html  screens, isi design ki markup se
  media_index/ui/app.js      asli data

Is file ke colours (--bg, --accent, --ok ...) seedha yahan se padhe jaate hai,
copy nahi kiye gaye -- isliye design badla to app ke colours khud badal jaayenge.

Naya design aaye to inhi naamo se overwrite kar dena.

Brief: ../DESIGN_BRIEF.md
