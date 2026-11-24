# Build Watch monitor

Automatic change detection on free Sentinel-2 satellite imagery (10 m per pixel) for publicly reported Chinese construction sites. Runs every two days on GitHub Actions; big changes open an Issue in this repo.

_Last run: never UTC_

| Site | Latest clear image | Change vs previous | Alerts |
|---|---|---|---|
| **Subi Reef**<br>Spratly Islands | no clear image yet | | |
| **Fiery Cross Reef**<br>Spratly Islands | no clear image yet | | |
| **Mischief Reef**<br>Spratly Islands | no clear image yet | | |
| **Woody Island**<br>Paracel Islands | no clear image yet | | |
| **PLA Support Base Djibouti**<br>Horn of Africa | no clear image yet | | |
| **Ream Naval Base**<br>Cambodia | no clear image yet | | |
| **Jiangnan Shipyard**<br>Shanghai | no clear image yet | | |
| **Hami silo field**<br>Xinjiang | no clear image yet | | |

Imagery: Copernicus Sentinel-2 via Element 84 Earth Search. Change % is the share of the usable area whose colour changed noticeably; clouds, haze and boats can cause false alarms, so check each alert by eye.

**Run it now:** Actions tab → *Monitor sites* → *Run workflow*. **Add a site:** add a line to `sites.json`. **Get alerts by email:** Watch this repo → Custom → Issues.
