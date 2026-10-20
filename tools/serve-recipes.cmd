@echo off
rem Serves library\ to the Nook on port 8080. Close this window to stop.
py -3 "%~dp0serve-recipes.py" %*
