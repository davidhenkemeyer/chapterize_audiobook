@echo off
rem ---------------------------------------------------------------------------
rem Chapterize all 10 Malazan Book of the Fallen audiobooks.
rem Run from anywhere; it switches to the folder this .bat lives in, so keep it
rem next to chapterize.py.
rem
rem For each book: a --dry-run first (transcribes + writes plan.csv), then the
rem real run (reuses the cached transcript, then builds one .m4b with a chapter
rem marker per scene, in the output folder).
rem Books whose audio folder or epub can't be found are skipped with a message.
rem Finished books are skipped automatically (an existing .m4b is not rebuilt),
rem but comment a line out with "rem" to skip a book entirely.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "AUDIO_ROOT=c:\AudioBooks"
set "EPUB_ROOT=d:\tmp"

rem call :book <num> <title as in audio folder> <book title> <epub filename>
rem   Audio folder:  <AUDIO_ROOT>\Steven Erikson - Malazan Book of the Fallen <num> - <title as in audio folder>
rem   Output folder: <AUDIO_ROOT>\Malazan <num without leading 0> - <book title, apostrophes removed>
rem                  (transcripts are cached in its _work folder)
rem   Album/title:   Malazan <num without leading 0> - <book title>  (also the .m4b filename)

call :book 01 "Gardens of the Moon"   "Gardens of the Moon"  "Malazan 1 - Gardens of the Moon - Steven Erickson.epub"
call :book 02 "Deadhouse Gates"       "Deadhouse Gates"      "Malazan 2 - DeadHouse Gates - Steven Erickson.epub"
call :book 03 "Memories of Ice"       "Memories of Ice"      "Malazan 3 - Memories of Ice - Steven Erickson.epub"
call :book 04 "House of Chains"       "House of Chains"      "Malazan  4 - House of Chains - Steven Erickson.epub"
call :book 05 "Midnight Tides"        "Midnight Tides"       "Malazan 5 - Midnight Tides - Erikson_ Steven.epub"
call :book 06 "Bonehunters"           "The Bonehunters"      "Malazan 6 - The Bonehunters - Steven Erikson.epub"
call :book 07 "Reaper's Gale"         "Reaper's Gale"        "Malazan 7 - Reaper's Gale - Steven Erickson.epub"
call :book 08 "Toll the Hounds"       "Toll the Hounds"      "Malazan 08 - Toll the Hounds - Steven Erikson.epub"
call :book 09 "Dust of Dreams"        "Dust of Dreams"       "Malazan 9 - Dust of Dreams - Steven Erikson.epub"
call :book 10 "The Crippled God"      "The Crippled God"     "Malazan 10 - The Crippled God - Erikson_ Steven.epub"

echo.
echo All done.
endlocal
exit /b 0

:book
set "NUM=%~1"
rem "1%NUM% - 100" turns 01 into 1 (a plain 08/09 would be read as bad octal).
set /a N=1%NUM% - 100
set "TITLE=%~3"
set "AUDIO=%AUDIO_ROOT%\Steven Erikson - Malazan Book of the Fallen %NUM% - %~2"
set "OUT=%AUDIO_ROOT%\Malazan %N% - %TITLE:'=%"
set "ALBUM=Malazan %N% - %TITLE%"
set "EPUB=%EPUB_ROOT%\%~4"
echo.
echo ===========================================================================
echo Book %NUM%: %TITLE%
echo ===========================================================================
if not exist "%AUDIO%\" (
    echo   SKIPPED: audio folder not found: %AUDIO%
    exit /b 0
)
if not exist "%EPUB%" (
    echo   SKIPPED: epub not found: %EPUB%
    exit /b 0
)
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --album "%ALBUM%" --dry-run --out "%OUT%"
if errorlevel 1 (
    echo   FAILED during dry run for book %NUM%; moving on.
    exit /b 0
)
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --album "%ALBUM%" --out "%OUT%"
if errorlevel 1 echo   FAILED during the real run for book %NUM%.
exit /b 0
