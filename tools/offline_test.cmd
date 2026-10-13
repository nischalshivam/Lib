@echo off
rem phase-2 offline proof: the model proxy and every HTTP(S) request point at a dead port
set MODEL_ROUTER_URL=http://127.0.0.1:9
set HTTP_PROXY=http://127.0.0.1:9
set HTTPS_PROXY=http://127.0.0.1:9
set ALL_PROXY=http://127.0.0.1:9
set NO_PROXY=
set PY=C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe
set S=D:\Movies\English\Reacher\02 Why Reacher Season 4 Feels So Different\Script.txt
echo === until online ===
"%PY%" -u -m editv2 build "%S%" --lang en --look auto --until online
echo === full (offline) ===
"%PY%" -u -m editv2 build "%S%" --lang en --look auto --save "D:\Lib\tools\_offline_out"
