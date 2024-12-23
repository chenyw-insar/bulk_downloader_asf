# Bulk Downloader for ASF Data

This script automates the process of downloading data from the Alaska Satellite Facility (ASF) using Earthdata Login for authentication. 



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

Copyright (c) 2024 Y-W. Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.



# Author's Info

Author: Y-W. Chen
E-mail: mkcchenyaowen@gmail.com
ORCID: https://orcid.org/0000-0002-1290-001X
Date: Dec. 23, 2024
