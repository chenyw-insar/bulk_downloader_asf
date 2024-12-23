# Bulk Downloader for ASF Data

This script provides a solution to address the "Could not get file HEAD" error (https://forum.earthdata.nasa.gov/viewtopic.php?t=6287).
It automatically downloads data from the Alaska Satellite Facility (ASF) using Earthdata Login for authentication. 



# Features

**Cookie-Based Authentication**: Automatically validates or retrieves Earthdata login cookies.

**Resumable Downloads**: Detects incomplete downloads and resumes from where they left off.

**File Integrity Check**: Ensures the downloaded files match the expected MD5 checksums (if provided).

**Progress Display**: Real-time feedback on download progress.



# Requirements

Python 3.6 or newer

Libraries: requests, hashlib, shutil



# Usage

1. Prepare the Script:

   Clone the repository or download the script,

   and place it in a directory where you want the files to be downloaded.

2. Modify the Script:

   Modify the urls list in the script to include the URLs of the files you want to download. 

   example:
         urls = [
        "https://datapool.asf.alaska.edu/SLC/SA/S1A_IW_SLC__xxxx1.zip",
        "https://datapool.asf.alaska.edu/SLC/SA/S1A_IW_SLC__xxxx2.zip"
        ]
   
3. Execute the Script:

   python ./bulk_downloader.py

4. Enter Credentials:

   When prompted, enter your Earthdata username and password, which are used to obtain an authentication cookie.



# Notes

Please ensure compliance with ASF and Earthdata terms of service.

This script is provided "as is" without warranty of any kind. The user is responsible for adhering to the service terms of ASF and Earthdata.

This script is intended for educational, research and personal use. The author is not liable for any misuse or violations of the terms of service of ASF or Earthdata.

**It is recommended to use the official bulk download script whenever possible.**



# License

MIT License

>*Copyright (c) 2024 Y-W. Chen*



# Author's Info

Author: Y-W. Chen

E-mail: mkcchenyaowen@gmail.com

ORCID: https://orcid.org/0000-0002-1290-001X

Date: Dec. 23, 2024
