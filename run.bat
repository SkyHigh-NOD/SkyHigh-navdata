@echo off
rem Build the SkyRadar navdata release ZIP into dist\.
setlocal
cd /d "%~dp0"
python build_navdata.py %*
