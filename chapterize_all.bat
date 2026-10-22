@echo off
rem ---------------------------------------------------------------------------
rem Chapterize all 10 Malazan Book of the Fallen audiobooks.
rem Run from anywhere; it switches to the folder this .bat lives in, so keep it
rem next to chapterize.py.
rem
rem For each book: a --dry-run first (transcribes + writes plan.csv), then the
rem real run (reuses the cached transcript, then cuts the audio).
rem Books whose audio folder or epub can't be found are skipped with a message.
rem Finished books are skipped automatically (existing files are not re-cut),
rem but comment a line out with "rem" to skip a book entirely.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "AUDIO_ROOT=c:\AudioBooks"
set "EPUB_ROOT=d:\tmp"

rem call :book <num> <title as in audio folder> <title for output folder> <epub filename>
rem Epub names below assume the pattern "Malazan N - Title - Steven Erickson.epub".

rem call :book 01 "Gardens of the Moon"   "Gardens of the Moon"  "Malazan 1 - Gardens of the Moon - Steven Erickson.epub"
rem call :book 02 "Deadhouse Gates"       "Deadhouse Gates"      "Malazan 2 - Deadhouse Gates - Steven Erickson.epub"
rem call :book 03 "Memories of Ice"       "Memories of Ice"      "Malazan 3 - Memories of Ice - Steven Erickson.epub"
call :book 04 "House of Chains"       "House of Chains"      "Malazan  4 - House of Chains - Steven Erickson.epub"
rem call :book 05 "Midnight Tides"       "Midnight Tides"       "Malazan 5 - Midnight Tides - Steven Erickson.epub"
rem call :book 06 "The Bonehunters"      "The Bonehunters"      "Malazan 6 - The Bonehunters - Steven Erickson.epub"
rem call :book 07 "Reaper's Gale"        "Reapers Gale"         "Malazan 7 - Reaper's Gale - Steven Erickson.epub"
call :book 08 "Toll the Hounds"       "Toll the Hounds"      "Malazan 08 - Toll the Hounds - Steven Erickson.epub"
call :book 09 "Dust of Dreams"        "Dust of Dreams"       "Malazan 9 - Dust of Dreams - Steven Erikson.epub"
call :book 10 "The Crippled God"      "The Crippled God"     "Malazan 10 - The Crippled God - Erikson_ Steven.epub"

echo.
echo All done.
endlocal
exit /b 0

:book
set "NUM=%~1"
set "AUDIO=%AUDIO_ROOT%\Steven Erikson - Malazan Book of the Fallen %NUM% - %~2"
set "OUT=%AUDIO_ROOT%\Malazan %NUM% - %~3"
set "EPUB=%EPUB_ROOT%\%~4"
echo.
echo ===========================================================================
echo Book %NUM%: %~2
echo ===========================================================================
if not exist "%AUDIO%\" (
    echo   SKIPPED: audio folder not found: %AUDIO%
    exit /b 0
)
if not exist "%EPUB%" (
    echo   SKIPPED: epub not found: %EPUB%
    exit /b 0
)
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --dry-run --out "%OUT%"
if errorlevel 1 (
    echo   FAILED during dry run for book %NUM%; moving on.
    exit /b 0
)
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --out "%OUT%"
if errorlevel 1 echo   FAILED during the real run for book %NUM%.
exit /b 0