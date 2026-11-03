@echo off
rem ---------------------------------------------------------------------------
rem Chapterize all 10 Malazan Book of the Fallen audiobooks from scratch.
rem Run from anywhere; it switches to the folder this .bat lives in, so keep it
rem next to chapterize.py.
rem
rem First the omnibus epub is split into one epub per book (its scene breaks
rem become the scene markers). Then for each book: the old output (.m4b, plan.csv
rem and any per-scene .m4a files) is deleted, a --dry-run transcribes and writes
rem plan.csv, and the real run builds one .m4b with a chapter marker per scene.
rem Books whose audio folder or epub can't be found are skipped with a message;
rem comment a line out with "rem" to skip a book entirely.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem The original audiobooks (read only).
set "SOURCE_ROOT=c:\AudiobooksBackup"
rem Where the chapterized books go, one folder per book.
set "OUT_ROOT=c:\AudioBooks"
set "EPUB_ROOT=d:\tmp"
set "OMNIBUS=%EPUB_ROOT%\Steven Erikson - The Complete Malazan Book of the Fallen (Omnibus) (v5.0) (epub).epub"
rem 1 = also redo the speech-to-text (hours per book). 0 = reuse the transcripts
rem cached in each book's _work folder; they depend only on the audio.
set "RETRANSCRIBE=0"

rem goto, not an if-block: the ")" in the omnibus name would end the block.
if not exist "%OMNIBUS%" goto :no_omnibus
echo Splitting the omnibus into one epub per book...
python split_omnibus.py "%OMNIBUS%" "%EPUB_ROOT%"
if errorlevel 1 goto :split_failed
goto :books
:no_omnibus
echo Omnibus not found, using the existing "(omnibus)" epubs in %EPUB_ROOT%: "%OMNIBUS%"
goto :books
:split_failed
echo FAILED to split the omnibus.
exit /b 1

:books
rem call :book <num> <title as in audio folder> <book title> <epub filename>
rem   Audio folder:  <SOURCE_ROOT>\Steven Erikson - Malazan Book of the Fallen <num> - <title as in audio folder>
rem   Output folder: <OUT_ROOT>\Malazan <num without leading 0> - <book title, apostrophes removed>
rem                  (transcripts are cached in its _work folder)
rem   Album/title:   Malazan <num without leading 0> - <book title>  (also the .m4b filename)

call :book 01 "Gardens of the Moon"   "Gardens of the Moon"  "Malazan 1 - Gardens of the Moon - Steven Erikson (omnibus).epub"
call :book 02 "Deadhouse Gates"       "Deadhouse Gates"      "Malazan 2 - Deadhouse Gates - Steven Erikson (omnibus).epub"
call :book 03 "Memories of Ice"       "Memories of Ice"      "Malazan 3 - Memories of Ice - Steven Erikson (omnibus).epub"
call :book 04 "House of Chains"       "House of Chains"      "Malazan 4 - House of Chains - Steven Erikson (omnibus).epub"
call :book 05 "Midnight Tides"        "Midnight Tides"       "Malazan 5 - Midnight Tides - Steven Erikson (omnibus).epub"
call :book 06 "Bonehunters"           "The Bonehunters"      "Malazan 6 - The Bonehunters - Steven Erikson (omnibus).epub"
call :book 07 "Reaper's Gale"         "Reaper's Gale"        "Malazan 7 - Reaper's Gale - Steven Erikson (omnibus).epub"
call :book 08 "Toll the Hounds"       "Toll the Hounds"      "Malazan 8 - Toll the Hounds - Steven Erikson (omnibus).epub"
call :book 09 "Dust of Dreams"        "Dust of Dreams"       "Malazan 9 - Dust of Dreams - Steven Erikson (omnibus).epub"
call :book 10 "The Crippled God"      "The Crippled God"     "Malazan 10 - The Crippled God - Steven Erikson (omnibus).epub"

echo.
echo All done.
endlocal
exit /b 0

:book
set "NUM=%~1"
rem "1%NUM% - 100" turns 01 into 1 (a plain 08/09 would be read as bad octal).
set /a N=1%NUM% - 100
set "TITLE=%~3"
set "AUDIO=%SOURCE_ROOT%\Steven Erikson - Malazan Book of the Fallen %NUM% - %~2"
set "OUT=%OUT_ROOT%\Malazan %N% - %TITLE:'=%"
set "ALBUM=Malazan %N% - %TITLE%"
set "EPUB=%EPUB_ROOT%\%~4"
echo.
echo ===========================================================================
echo Book %NUM%: %TITLE%
echo ===========================================================================
if not exist "%AUDIO%\" (
    echo   SKIPPED: audio folder not found: "%AUDIO%"
    exit /b 0
)
if not exist "%EPUB%" (
    rem Quoted: a ")" in the name would end this block.
    echo   SKIPPED: epub not found: "%EPUB%"
    exit /b 0
)
rem Start fresh: an existing .m4b would be kept, and old per-scene files would
rem show up next to the new book in a library.
if exist "%OUT%\*.m4b" del /q "%OUT%\*.m4b"
if exist "%OUT%\*.m4a" del /q "%OUT%\*.m4a"
if exist "%OUT%\plan.csv" del /q "%OUT%\plan.csv"
if "%RETRANSCRIBE%"=="1" if exist "%OUT%\_work\" rd /s /q "%OUT%\_work"
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --album "%ALBUM%" --dry-run --out "%OUT%"
if errorlevel 1 (
    echo   FAILED during dry run for book %NUM%; moving on.
    exit /b 0
)
python chapterize.py --audio-dir "%AUDIO%" --epub "%EPUB%" --album "%ALBUM%" --out "%OUT%"
if errorlevel 1 echo   FAILED during the real run for book %NUM%.
exit /b 0
